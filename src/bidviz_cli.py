#!/usr/bin/env python3
"""bidviz —— 施工组织设计图册生成器 CLI。

用法：
    bidviz init     <项目名>             新建项目
    bidviz ingest   <项目>               解析输入文档 → blocks.json
    bidviz plan     <项目>               分析章节内容 → 每节画几张图（落盘可审改）
    bidviz generate <项目> [--all|--chapter 第一章|--section S006] [--fig 2] [--force]
    bidviz build    <项目> [--out xxx.pdf]
    bidviz audit    <项目>               覆盖率审计
    bidviz serve    <项目> [--port 8770] 启动网页编辑器
    bidviz pages    <项目> [--pdf xxx]   PDF 按真实页码导出页图（001_…png）
    bidviz status   <项目>               项目状态
    bidviz list                          列出所有项目

项目路径可以是名字（在 projects/ 下）或绝对路径。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

os.environ.setdefault("PYTHONUTF8", "1")   # 子进程中文不乱码

from bidviz_config import load as load_cfg  # noqa: E402

PROJECTS = BASE / "projects"
GLOBAL_MEMORY = BASE / "memory"


def _memory_roots(root: Path) -> list[Path]:
    return [GLOBAL_MEMORY, root / "memory"]


def _scope_from_args(args, root: Path, *, operation: str = "") -> dict:
    engine = getattr(args, "engine", None) or ""
    if not engine:
        try:
            engine = load_cfg(root).layout_engine
        except Exception:
            engine = ""
    return {
        "project": root.name,
        "engine": engine,
        "operation": getattr(args, "operation", None) or operation,
        "section": getattr(args, "section", None) or "",
        "page": getattr(args, "page", None) or "",
        "provider": getattr(args, "provider", None) or "",
        "model": getattr(args, "model", None) or "",
    }


def _combined_memory(root: Path, query: str, scope: dict, *, include_inactive: bool = False,
                     statuses: set[str] | None = None, limit: int = 50) -> list[dict]:
    import bidviz_memory as memory
    records = []
    for mem_root in _memory_roots(root):
        if not mem_root.exists():
            continue
        suffix = "global" if mem_root == GLOBAL_MEMORY else "project"
        index = root / "work" / f"memory-index-{suffix}.sqlite3"
        records.extend(memory.search_records(
            mem_root, query, scope=scope, include_inactive=include_inactive,
            statuses=statuses, limit=limit, refresh_index=True, index_path=index))
    records.sort(key=lambda item: (-float(item.get("score", 0)), item["id"]))
    return records[:limit]


def _run_evidence(root: Path, command: str, args) -> dict:
    """Collect best-effort telemetry evidence without affecting command success."""
    import bidviz_memory as memory
    try:
        cfg = load_cfg(root)
        paths: list[Path] = []
        if command == "ingest":
            paths += [root / "work" / "blocks.json", root / "work" / "pages.json"]
        elif command == "plan":
            paths += [root / "work" / "figure-plan.json", root / "work" / "page-plan.json"]
        elif command == "generate":
            engine = getattr(args, "engine", None) or cfg.layout_engine
            if engine == "fullimage":
                paths += [root / "work" / "fullimage" / "ledger.json"]
            elif engine == "html":
                paths += [root / "work" / "illustrations-ledger.json"]
            else:
                paths += [root / "work" / "generation-chapter.json"]
        elif command == "build":
            engine = getattr(args, "engine", None) or cfg.layout_engine
            names = {"legacy": "施工组织设计-图册-A3.pdf",
                     "v13": "施工组织设计-图册-A3-v13.pdf",
                     "fullimage": "施工组织设计-图册-A3-fullimage.pdf",
                     "html": "施工组织设计-图册-A3-html.pdf"}
            paths += [Path(args.out) if getattr(args, "out", None) else cfg.output / names[engine]]
        elif command in {"audit", "issues"}:
            paths += list(cfg.output.glob("audit-*.json")) + [root / "work" / "issues.json"]
        artifacts = []
        for path in paths:
            if path.is_file():
                try:
                    rel = path.resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    rel = str(path.resolve())
                artifacts.append({"path": rel, "bytes": path.stat().st_size,
                                  "sha256": memory.sha256_file(path)})
        return {"artifacts": sorted(artifacts, key=lambda x: x["path"])}
    except Exception as exc:
        print(f"[WARN] 运行证据收集失败：{exc}", file=sys.stderr)
        return {"artifacts": []}


class _NullRun:
    run_id = ""
    def record(self, *_args, **_kwargs):
        return {}
    def close(self, *_args, **_kwargs):
        return {}


def _project_run(root: Path, command: str, args) -> object:
    """Best-effort telemetry; logging failure must not block the requested command."""
    import bidviz_memory as memory
    existing = os.getenv("BIDVIZ_RUN_ID", "").strip()
    run_id = existing or time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    run_dir = root / "work" / "runs" / run_id
    try:
        recorder = memory.RunRecorder(
            run_dir, run_id=run_id,
            metadata={"project": root.name, "command": command,
                      "scope": _scope_from_args(args, root, operation=command)},
        )
    except Exception as exc:
        print(f"[WARN] 运行事件记录不可用：{exc}", file=sys.stderr)
        return _NullRun()
    os.environ["BIDVIZ_RUN_ID"] = run_id
    os.environ["BIDVIZ_RUN_DIR"] = str(run_dir)
    return recorder


def _record_run(recorder, event: str, **fields) -> None:
    try:
        recorder.record(event, **fields)
    except Exception as exc:
        print(f"[WARN] 运行事件记录失败：{exc}", file=sys.stderr)


def _close_run(recorder, status: str, **fields) -> None:
    try:
        recorder.close(status, **fields)
    except Exception as exc:
        print(f"[WARN] 运行结果记录失败：{exc}", file=sys.stderr)


# ---------------------------------------------------------------- helpers

def resolve_project(name: str) -> Path:
    p = Path(name)
    if p.is_absolute() and p.exists():
        return p
    if (PROJECTS / name).exists():
        return PROJECTS / name
    # 允许模糊匹配
    hits = [d for d in PROJECTS.iterdir() if d.is_dir() and name in d.name] if PROJECTS.exists() else []
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit(f"匹配到多个项目：{[h.name for h in hits]}")
    raise SystemExit(f"项目不存在：{name}（可运行 bidviz list 查看）")


def need(cfg, key: str, hint: str) -> None:
    if not getattr(cfg, key, None):
        raise SystemExit(hint)


# ---------------------------------------------------------------- commands

def cmd_list(_args) -> None:
    if not PROJECTS.exists():
        print("还没有任何项目")
        return
    for d in sorted(PROJECTS.iterdir()):
        if not d.is_dir():
            continue
        toml = d / "bidviz.toml"
        name = ""
        if toml.exists():
            try:
                import tomllib
                with toml.open("rb") as f:
                    name = tomllib.load(f).get("project", {}).get("name", "")
            except Exception:
                pass
        imgs = sum(len(list((d / "work" / sub).glob("*.png")))
                   for sub in ("visuals", "visuals-2k", "visuals-2k-yoshub")
                   if (d / "work" / sub).exists())
        print(f"  {d.name:<22} {name:<22} 图片 {imgs:>3} 张")


def cmd_init(args) -> None:
    d = PROJECTS / args.name
    if d.exists():
        raise SystemExit(f"已存在：{d}")
    (d / "inputs").mkdir(parents=True)
    (d / "work").mkdir(parents=True)
    (d / "output").mkdir(parents=True)
    (d / "bidviz.toml").write_text(
        f'''# {args.name}

[project]
name = "{args.name}"
voltage = "{args.voltage}"
doc_type = "施工组织设计"
header = ""
standard = ""

[image]
provider = "grokimg"
model = "gpt-image-2-2K/4K"
size = "2048x1448"
dir = "work/visuals"

[paths]
inputs = "inputs"
work = "work"
output = "output"
''', encoding="utf-8")
    print(f"[OK] 已创建 {d}")
    print(f"  1) 把 Word/PDF 放进 {d / 'inputs'}")
    print(f"  2) 运行 bidviz ingest {args.name}")
    print(f"  3) 运行 bidviz plan {args.name} -v   确认每节画几张图")
    print(f"  4) 运行 bidviz generate {args.name} --all")


def cmd_ingest(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    source = Path(args.source).resolve() if args.source else next(
        (p for ext in ("*.docx", "*.doc", "*.pdf") for p in sorted(cfg.inputs.glob(ext))
         if not p.stem.endswith("-source")), None)
    if source is None or not source.exists():
        raise SystemExit(f"未找到输入 Word/PDF：{cfg.inputs}")
    print("运行输入解析流水线（Word/PDF → blocks.json/pages.json）…")
    r = subprocess.run([sys.executable, str(BASE / "src" / "run_pipeline.py"), str(source),
                        "--project-dir", str(root), "--ingest-only"], cwd=BASE)
    raise SystemExit(r.returncode)


def cmd_plan(args) -> None:
    """先分析章节内容，再定每节画几张、每张画什么；落盘供人工审改。"""
    root = resolve_project(args.project)
    load_cfg(root)
    import bidviz_plan as plan
    only = [x.strip() for x in args.section.split(",") if x.strip()] if args.section else None
    p = plan.ensure_plan(root, force=args.force, only=only)
    s = plan.plan_summary(p)
    print(f"[OK] 图位规划已写入 {plan.plan_path(root)}")
    print(f"  节 {s['sections']} 个，共规划 {s['figures']} 张图，本次重算 {p.get('changed', 0)} 节")
    print(f"  每节图数分布：{s['per_section']}")
    print(f"  图型分布　　：{s['kinds']}")
    if args.verbose:
        for sid, entry in sorted(p["sections"].items()):
            print(f"  {sid} {entry.get('title', '')}")
            for f in entry.get("figures", []):
                mark = "*" if f.get("custom") else " "
                print(f"    图{f['no']}{mark}[{f['kind']}] {f['name']} — {f['why']}")
    print("  确认无误后运行 bidviz generate 生图（提示词可直接改 figure-plan.json，"
          "改过的不会被覆盖）")


def cmd_generate(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    engine = args.engine or cfg.layout_engine

    if engine == "html":
        import bidviz_html as bh
        if args.section:
            ids = [s.strip().upper() for s in args.section.split(",") if s.strip()]
        elif args.chapter:
            ids = bh._chapter_sids(Path(root), args.chapter)
        else:
            pp = bh.load_page_plan(Path(root))
            ids = sorted((pp.get("sections") or {}).keys())
        summary = bh.generate_illustrations(
            Path(root), ids, force=args.force, limit=args.limit, dry_run=args.dry_run)
        print(f"[OK] HTML 局部插图：规划 {summary['planned']}，生成 {summary['generated']}，"
              f"跳过 {summary['skipped']}，失败 {summary['failed']}")
        print(f"     目录：{summary['out_dir']}")
        return

    if engine == "fullimage":
        cmd = [sys.executable, "-m", "bidviz_fullimage", str(root)]
        if args.section:
            cmd += ["--section", args.section]
        elif args.chapter:
            cmd += ["--chapter", args.chapter]
        else:
            cmd += ["--all"]
        if args.force:
            cmd.append("--force")
        if args.dry_run:
            cmd.append("--dry-run")
        raise SystemExit(subprocess.run(cmd, cwd=BASE).returncode)

    cmd = [sys.executable, str(BASE / "src" / "generate_chapter.py"), "--root", str(root)]
    if args.section:
        cmd += ["--section", args.section]
    elif args.chapter:
        cmd += ["--chapter", args.chapter]
    else:
        cmd.append("--all")
    if args.fig:
        cmd += ["--fig", str(args.fig)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.force:
        cmd.append("--force")
    if args.dry_run:
        cmd.append("--dry-run")
    if args.outdir:
        cmd += ["--outdir", args.outdir]
    raise SystemExit(subprocess.run(cmd, cwd=BASE).returncode)


def cmd_build(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    engine = args.engine or cfg.layout_engine
    default_names = {
        "legacy": "施工组织设计-图册-A3.pdf",
        "v13": "施工组织设计-图册-A3-v13.pdf",
        "fullimage": "施工组织设计-图册-A3-fullimage.pdf",
        "html": "施工组织设计-图册-A3-html.pdf",
    }
    if args.out:
        out = Path(args.out)
    else:
        name = default_names.get(engine, default_names["legacy"])
        if engine == "fullimage" and getattr(args, "draft", False):
            name = Path(name).stem + "-DRAFT.pdf"
        out = cfg.output / name
    out.parent.mkdir(parents=True, exist_ok=True)

    if engine == "v13":
        # 新体系（V1.3）：独立子进程跑 build_v13——import 即切 A3 竖版画布，
        # 不得与旧体系同进程混跑（踩坑 #11）
        print("运行 V1.3 新体系编译（page-plan → A3 竖版）…")
        r = subprocess.run([sys.executable, str(BASE / "src" / "build_v13.py"),
                            "--root", str(root), "--out", str(out)], cwd=BASE)
        raise SystemExit(r.returncode)

    if engine == "html":
        import bidviz_html as bh
        print("运行 HTML 混合引擎（局部 AI 图 + HTML/CSS 排版）…")
        result = bh.build(Path(root), None, out, draft=False,
                          emit_png=True if getattr(args, "png", False) else None)
        print(f"[OK] HTML 图册：{result['pdf']}（{result['pages']} 页）")
        print(f"     审计：{'PASS' if result['audit']['ok'] else 'WARN'} "
              f"文本覆盖 {result['audit'].get('coverage', 0):.2%}")
        if result.get("png"):
            print(f"     页图：{result['png']['dir']}")
        missing = result.get("missing_figures") or []
        if missing:
            print(f"     缺失插图 {len(missing)} 张（可运行 bidviz generate <项目> --engine html --all）")
        return

    if engine == "fullimage":
        import bidviz_plan as plan_mod
        import bidviz_fullimage as fi
        import bidviz_fullimage_store as store
        pp = plan_mod.load_page_plan(root) or plan_mod.build_page_plan(root)
        sections = pp.get("sections") or {}
        sids = sorted(sections.keys())
        import bidviz_e1 as e1
        import build_e1 as be
        blocks, _page_media = be.load_project(root)
        actual_sections = e1.group_sections(blocks)
        be.apply_edits(root, actual_sections)
        by_sid = {section.section_id: section for section in actual_sections}
        expected = []
        for sid in sids:
            section = by_sid.get(sid)
            if section is None:
                raise SystemExit(f"blocks 中无节 {sid}")
            pages = fi.split_pages(fi._section_blocks(root, sid))
            for page_no in range(1, len(pages) + 1):
                ctx = fi.page_context(root, sid, page_no, section.title)
                prompt = store.get_prompt_override(
                    root, sid, page_no, ctx["auto_prompt"], ctx["source_hash"])["resolved_prompt"]
                store.ensure_page_hashes(root, sid, page_no, {
                    **ctx, "prompt_hash": store.sha256_text(prompt),
                })
                expected.append((sid, page_no))
        summary = store.gate_summary(root, expected, verify_artifacts=True)
        draft = bool(getattr(args, "draft", False))
        if not draft and not summary.get("can_build_final"):
            raise SystemExit("正式汇编被门禁阻断：仍有 stale/missing/blocked 页面")

        def resolver(sid, page):
            if draft:
                candidates = store.list_candidates(root, sid, page)
                if candidates:
                    return root / candidates[-1]["path"]
            published = store.published_path(root, sid, page)
            return published if published.exists() else None

        fi.build_fullimage_pdf(root, sids, out, image_resolver=resolver, draft=draft)
        print(f"[OK] 全图图册：{out}")
        return

    import build_e1 as be
    pages, regions = be.build_final(root, out)
    print(f"[OK] 已生成 {out}（{pages} 页，{round(out.stat().st_size/1024/1024, 1)} MB）")
    audit = be.audit_coverage(out, root, regions)
    (cfg.output / "audit-vector.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 内容覆盖率 {audit['coverage_pct']}%（缺失 {audit['rows_missing']} 行提炼内容）")


def cmd_issues(args) -> None:
    """扫描 blocks.json 生成 work/issues.json（与 python -m bidviz_issues 等价）。"""
    root = resolve_project(args.project)
    load_cfg(root)
    import bidviz_issues
    argv = [str(root)]
    if args.report:
        argv.append("--report")
    if args.json:
        argv.append("--json")
    raise SystemExit(bidviz_issues.main(argv))


def cmd_audit(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    engine = getattr(args, "engine", None) or cfg.layout_engine
    if engine == "html" and not args.layout and not args.issues:
        import bidviz_html as bh
        pdf = Path(args.pdf) if args.pdf else cfg.output / (bh.OUT_SUFFIX + ".pdf")
        manifest = json.loads((root / bh.HTML_PAGES).read_text(encoding="utf-8"))
        pages = [bh.HtmlPage(sid=str(p.get("sid", "")),
                             section_page=int(p.get("section_page", 1)),
                             chapter=str(p.get("chapter", "")),
                             section=str(p.get("section", "")),
                             footer_note="", blocks=[])
                 for p in manifest.get("pages", [])]
        rep = bh.audit_html_pdf(pdf, pages, [])
        (cfg.output / "audit-html.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] HTML 审计：{rep['pages']} 页；"
              f"{'PASS' if rep['ok'] else 'WARN'}；问题 {rep.get('issues', [])[:3]}")
        return
    pdf = Path(args.pdf) if args.pdf else cfg.output / "施工组织设计-图册-A3.pdf"

    ran_any = False

    if args.layout:
        ran_any = True
        if not pdf.exists():
            raise SystemExit(f"找不到 PDF（--layout）：{pdf}")
        import audit_layout
        tokens = BASE / "docs" / "layout-md" / "tokens.toml"
        if not tokens.exists():
            raise SystemExit(f"找不到 tokens.toml：{tokens}")
        out_json = cfg.output / "audit-layout.json"
        rep = audit_layout.audit_pdf(pdf, tokens, out_json)
        total = len(rep["pages"])
        failed = rep["failed_pages"]
        print(f"[OK] 乱度审计 {pdf.name}：共 {total} 页，超标 {len(failed)} 页"
              f"（填充色块≤{rep.get('threshold_max_fill', '?')}、色种≤{rep.get('threshold_max_colors', '?')}、左边界≤{rep.get('threshold_max_x0', '?')}）")
        if failed:
            preview = failed[:12]
            more = "…" if len(failed) > 12 else ""
            print(f"     超标页：{preview}{more}")
        print(f"     报告已写入 {out_json}")

    if args.issues:
        ran_any = True
        import bidviz_issues
        doc = bidviz_issues.load(root)
        summary = doc.get("summary") or bidviz_issues.summarize(
            [bidviz_issues.Issue(**x) for x in doc.get("issues", []) if isinstance(x, dict)]
        )
        print(f"[OK] issues：共 {summary.get('total', 0)} 条，按类型 {summary.get('by_type', {})}，按状态 {summary.get('by_status', {})}")
        passed, reason = bidviz_issues.gate_check(doc)
        print(f"     门禁：{'PASS' if passed else 'BLOCK'}（{reason}）")
        if args.rescan:
            fresh = bidviz_issues.scan(root)
            fresh = bidviz_issues.merge_status(doc, fresh)
            path = bidviz_issues.save(root, fresh)
            print(f"     已重新扫描并写入 {path}")

    if not ran_any:
        # 默认行为：覆盖率审计（向后兼容）
        if not pdf.exists():
            raise SystemExit(f"找不到 PDF：{pdf}")
        import build_e1 as be
        rep = be.audit_coverage(pdf, root)
        (cfg.output / "audit-vector.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OK] 内容覆盖率 {rep['coverage_pct']}%（缺失 {rep['rows_missing']} 行提炼内容）")


def cmd_serve(args) -> None:
    root = resolve_project(args.project)
    load_cfg(root)
    print(f"编辑器： http://localhost:{args.port}/   项目：{root}")
    raise SystemExit(subprocess.run(
        [sys.executable, str(BASE / "web" / "server.py"), str(args.port), str(root)],
        cwd=BASE).returncode)


def cmd_pages(args) -> None:
    """把 PDF 的每页图按真实页码导出：001_S001_p1.png …（默认硬链接，不占额外磁盘）。"""
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    engine = args.engine or cfg.layout_engine
    names = {
        "fullimage": "施工组织设计-图册-A3-fullimage.pdf",
        "html": "施工组织设计-图册-A3-html.pdf",
        "v13": "施工组织设计-图册-A3-v13.pdf",
        "legacy": "施工组织设计-图册-A3.pdf",
    }
    pdf = Path(args.pdf) if args.pdf else cfg.output / names.get(engine, names["fullimage"])
    if engine == "html":
        import bidviz_html as bh
        summary = bh.export_pdf_pages(root, pdf, dpi=cfg.html_dpi)
    else:
        import bidviz_fullimage as fi
        summary = fi.export_page_images(root, pdf, force_copy=args.copy)
    print(f"[OK] 页码图：{summary['dir']}")
    if "generated" in summary:
        print(f"     共 {summary['pages']} 张（源图 {summary['generated']}，"
              f"PDF 渲染 {summary['rendered']}）；清单 {summary['manifest']}")
    else:
        print(f"     共 {summary['pages']} 张；清单 {summary['manifest']}")


def cmd_status(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    print(f"项目根    : {root}")
    print(f"引擎      : {cfg.layout_engine}")
    print(f"名称      : {cfg.name}  ({cfg.voltage})")
    print(f"页眉      : {cfg.header_text}")
    print(f"生图      : {cfg.provider} / {cfg.model} / {cfg.image_size}")
    print(f"图片目录  : {cfg.image_dir}")
    print(f"图位规划  : 每节最多 {cfg.max_figures} 张"
          f"{'' if cfg.second_figure else '（已关闭第二张）'}")
    print()
    import bidviz_plan as plan
    if plan.load_plan(root) is None:
        print("  figure-plan    [-] 未规划，先运行 bidviz plan")
    else:
        from generate_chapter import figure_progress
        g = figure_progress(root)
        print(f"  figure-plan    [OK] {g['sections']} 节 / 规划 {g['planned']} 张 / "
              f"已生成 {g['generated']} 张" + (f" / 失败 {g['failed']}" if g["failed"] else ""))
        print(f"  图型分布     {g['kinds']}")
    for label, p in (("blocks.json", root / "work" / "blocks.json"),
                     ("pages.json", root / "work" / "pages.json")):
        print(f"  {label:<14} {'[OK] ' + str(round(p.stat().st_size/1024)) + ' KB' if p.exists() else '[-] 缺失'}")
    for d in reversed(cfg.image_dirs):
        if d.exists():
            print(f"  {d.name:<14} {len(list(d.glob('*.png')))} 张")
    if cfg.layout_engine == "html":
        import bidviz_html as bh
        fp = plan.load_plan(root) or {}
        planned = sum(len(e.get("figures", [])) for e in (fp.get("sections") or {}).values())
        generated = 0
        for sid, e in (fp.get("sections") or {}).items():
            for f in e.get("figures", []):
                path = root / "work" / "illustrations" / plan.figure_filename(sid, int(f.get("no", 1)))
                if path.is_file():
                    generated += 1
        print(f"  局部插图       {generated}/{planned} 张")
    out = cfg.output
    if out.exists():
        for f in sorted(out.glob("*.pdf")):
            print(f"  产物           {f.name}  {round(f.stat().st_size/1024/1024,1)} MB")
    try:
        import bidviz_memory as memory
        health = [memory.memory_health(p) for p in _memory_roots(root) if p.exists()]
        counts: dict[str, int] = {}
        conflicts = errors = records = 0
        for item in health:
            records += int(item.get("records", 0))
            conflicts += len(item.get("conflicts", []))
            errors += len(item.get("errors", {}))
            for key, value in item.get("status_counts", {}).items():
                counts[key] = counts.get(key, 0) + int(value)
        print(f"  记忆控制面     {records} 条 / {counts} / conflicts {conflicts} / errors {errors}")
    except Exception as exc:
        print(f"  记忆控制面     [WARN] {exc}")


def _memory_record(root: Path, record_id: str) -> tuple[Path, Path]:
    import bidviz_memory as memory
    for memory_root in _memory_roots(root):
        path = memory.find_record_path(memory_root, record_id)
        if path is not None:
            return memory_root, path
    raise SystemExit(f"记忆不存在：{record_id}")


def cmd_memory(args) -> None:
    import bidviz_memory as memory
    root = resolve_project(args.project)
    roots = [p for p in _memory_roots(root) if p.exists()]
    scope = _scope_from_args(args, root, operation=getattr(args, "operation", ""))
    action = args.memory_cmd

    if action == "validate":
        reports = []
        for memory_root in roots:
            report = memory.validate_memory(memory_root, fix=args.fix)
            reports.append({"root": str(memory_root), **report})
        ok = all(item["ok"] for item in reports)
        print(json.dumps({"ok": ok, "reports": reports}, ensure_ascii=False, indent=2))
        if not ok:
            raise SystemExit(1)
        return
    if action == "reindex":
        reports = []
        for memory_root in roots:
            suffix = "global" if memory_root == GLOBAL_MEMORY else "project"
            index = root / "work" / f"memory-index-{suffix}.sqlite3"
            reports.append(memory.rebuild_index(memory_root, index))
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
        return
    if action in {"search", "context", "list"}:
        statuses = {args.status} if getattr(args, "status", None) else None
        query = getattr(args, "query", "") or ""
        records = _combined_memory(
            root, query, scope, include_inactive=args.include_inactive,
            statuses=statuses, limit=args.limit)
        if action == "context":
            chunks, used = [], 0
            for record in records:
                chunk = f"[{record['id']} | {record['effective_status']} | {record['kind']}]\n{record['title']}\n{record['claim']}"
                sep = "\n\n" if chunks else ""
                if used + len(sep) + len(chunk) > args.char_budget:
                    break
                chunks.append(chunk)
                used += len(sep) + len(chunk)
            text = "\n\n".join(chunks)
            payload = {"scope": scope, "text": text,
                       "memory_ids": [r["id"] for r in records[:len(chunks)]],
                       "characters": len(text), "char_budget": args.char_budget}
            payload["context_hash"] = memory.sha256_text(memory.canonical_json(payload))
        else:
            payload = records
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if action == "doctor":
        reports = [{"root": str(p), **memory.memory_health(p)} for p in roots]
        index_reports = []
        for memory_root in roots:
            suffix = "global" if memory_root == GLOBAL_MEMORY else "project"
            index_reports.append(memory.rebuild_index(
                memory_root, root / "work" / f"memory-index-{suffix}.sqlite3"))
        payload = {"ok": all(r["ok"] for r in reports), "reports": reports,
                   "indexes": index_reports}
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        if not payload["ok"]:
            raise SystemExit(1)
        return
    if action == "propose":
        proposals = memory.propose_from_run_events(
            root / "memory", root / "work" / "runs", project=root.name,
            min_occurrences=args.min_occurrences, save=args.save)
        print(json.dumps(proposals, ensure_ascii=False, indent=2))
        return
    memory_root, _path = _memory_record(root, args.id)
    if action == "promote":
        kwargs = {"actor": args.actor}
        if args.to == "human_signed":
            kwargs.update({"explicit_authority": True, "signed_by": args.signed_by})
        result = memory.promote_record(memory_root, args.id, args.to, **kwargs)
    elif action == "invalidate":
        result = memory.invalidate_record(
            memory_root, args.id, args.reason, actor=args.actor, status=args.status)
    else:
        raise SystemExit(f"未知 memory 操作：{action}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(prog="bidviz", description="施工组织设计图册生成器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出所有项目")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("init", help="新建项目")
    p.add_argument("name")
    p.add_argument("--voltage", default="35kV")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("ingest", help="解析输入文档")
    p.add_argument("project")
    p.add_argument("--source", help="Word/PDF 路径；省略则取项目 inputs/ 中第一份文档")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("plan", help="分析内容并规划每节图位")
    p.add_argument("project")
    p.add_argument("--section", help="只重算指定节，如 S006 或 S006,S012")
    p.add_argument("--force", action="store_true", help="忽略已有规划全部重算")
    p.add_argument("-v", "--verbose", action="store_true", help="逐节列出图号、图型与理由")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("generate", help="按规划生图")
    p.add_argument("project")
    p.add_argument("--engine", choices=["legacy", "v13", "fullimage", "html"], default=None)
    p.add_argument("--all", action="store_true")
    p.add_argument("--chapter")
    p.add_argument("--section", help="只跑指定节，如 S006 或 S006,S012")
    p.add_argument("--fig", type=int, help="只跑每节的第几张图")
    p.add_argument("--limit", type=int, help="最多生几张（试跑用）")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只打印提示词与生图参数")
    p.add_argument("--outdir")
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("build", help="汇编 PDF + 审计")
    p.add_argument("project")
    p.add_argument("--out")
    p.add_argument("--engine", choices=["legacy", "v13", "fullimage", "html"], default=None,
                   help="排版引擎：legacy=旧两页体系；v13=V1.3 新体系；"
                        "fullimage=整页 AI 图；html=局部 AI 图 + HTML/CSS；缺省读 bidviz.toml")
    p.add_argument("--png", action="store_true", help="html 引擎同时导出真页码 PNG")
    p.add_argument("--draft", action="store_true", help="fullimage 允许候选页并添加 DRAFT 水印")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("audit", help="覆盖率审计")
    p.add_argument("project")
    p.add_argument("--pdf")
    p.add_argument("--engine", choices=["legacy", "v13", "fullimage", "html"], default=None)
    p.add_argument("--layout", action="store_true", help="跑乱度门禁（audit_layout）")
    p.add_argument("--issues", action="store_true", help="查看 issues.json 门禁状态")
    p.add_argument("--rescan", action="store_true", help="与 --issues 合用：重新扫描并回写")
    p.set_defaults(fn=cmd_audit)

    p = sub.add_parser("issues", help="扫描并生成 work/issues.json（初筛规则）")
    p.add_argument("project")
    p.add_argument("--report", action="store_true", help="只打印摘要，不写盘")
    p.add_argument("--json", action="store_true", help="以 JSON 打印完整结果")
    p.set_defaults(fn=cmd_issues)

    p = sub.add_parser("serve", help="启动网页编辑器")
    p.add_argument("project")
    p.add_argument("--port", type=int, default=8770)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("pages", help="PDF 按真实页码导出页图")
    p.add_argument("project")
    p.add_argument("--pdf", help="PDF 路径；缺省按当前引擎选择")
    p.add_argument("--engine", choices=["legacy", "v13", "fullimage", "html"], default=None)
    p.add_argument("--copy", action="store_true", help="复制而非硬链接（fullimage 用）")
    p.set_defaults(fn=cmd_pages)

    p = sub.add_parser("status", help="项目状态")
    p.add_argument("project")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("memory", help="多层记忆控制面")
    mem = p.add_subparsers(dest="memory_cmd", required=True)

    def memory_common(parser, *, query: bool = False) -> None:
        parser.add_argument("project")
        if query:
            parser.add_argument("query", nargs="?", default="")
        parser.add_argument("--engine", choices=["legacy", "v13", "fullimage", "html"], default=None)
        parser.add_argument("--operation", default="")
        parser.add_argument("--section", default="")
        parser.add_argument("--page", default="")
        parser.add_argument("--provider", default="")
        parser.add_argument("--model", default="")

    q = mem.add_parser("validate", help="校验 durable memory 与依赖")
    q.add_argument("project")
    q.add_argument("--fix", action="store_true")
    q.set_defaults(fn=cmd_memory)
    q = mem.add_parser("reindex", help="重建可删除的 SQLite 索引")
    q.add_argument("project")
    q.set_defaults(fn=cmd_memory)
    for name, help_text in (("search", "检索记忆"), ("context", "生成最小上下文"),
                            ("list", "列出记忆")):
        q = mem.add_parser(name, help=help_text)
        memory_common(q, query=name in {"search", "context"})
        q.add_argument("--status", choices=sorted(__import__("bidviz_memory").STATUSES))
        q.add_argument("--include-inactive", action="store_true")
        q.add_argument("--limit", type=int, default=50)
        if name == "context":
            q.add_argument("--char-budget", type=int, default=4000)
        q.set_defaults(fn=cmd_memory)
    q = mem.add_parser("doctor", help="校验并重建索引")
    q.add_argument("project")
    q.set_defaults(fn=cmd_memory)
    q = mem.add_parser("propose", help="从 run events 提炼候选记忆")
    q.add_argument("project")
    q.add_argument("--min-occurrences", type=int, default=2)
    q.add_argument("--save", action="store_true")
    q.set_defaults(fn=cmd_memory)
    q = mem.add_parser("promote", help="按证据门禁晋升记忆")
    q.add_argument("project")
    q.add_argument("id")
    q.add_argument("--to", required=True,
                   choices=["corroborated", "machine_verified", "human_signed"])
    q.add_argument("--actor", default="cli-user")
    q.add_argument("--signed-by", default="")
    q.set_defaults(fn=cmd_memory)
    q = mem.add_parser("invalidate", help="使记忆失效或驳回")
    q.add_argument("project")
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q.add_argument("--actor", default="cli-user")
    q.add_argument("--status", choices=["stale", "rejected"], default="stale")
    q.set_defaults(fn=cmd_memory)

    args = ap.parse_args()
    if args.cmd == "memory":
        args.fn(args)
        return
    project = getattr(args, "project", None)
    if not project:
        args.fn(args)
        return
    root = resolve_project(project)
    recorder = _project_run(root, args.cmd, args)
    _record_run(recorder, "command.start", command=args.cmd,
                scope=_scope_from_args(args, root, operation=args.cmd))
    try:
        args.fn(args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code in (None, "") else 1)
        evidence = _run_evidence(root, args.cmd, args) if code == 0 else {}
        _record_run(recorder, "command.exit", command=args.cmd, exit_code=code,
                    outcome="success" if code == 0 else "failed", **evidence)
        _close_run(recorder, "success" if code == 0 else "failed", exit_code=code, **evidence)
        raise
    except BaseException as exc:
        _record_run(recorder, "command.error", command=args.cmd,
                    error_type=type(exc).__name__, error=str(exc))
        _close_run(recorder, "error", error_type=type(exc).__name__, error=str(exc))
        raise
    else:
        evidence = _run_evidence(root, args.cmd, args)
        _record_run(recorder, "command.result", command=args.cmd, outcome="success", **evidence)
        _close_run(recorder, "success", exit_code=0, **evidence)


if __name__ == "__main__":
    main()
