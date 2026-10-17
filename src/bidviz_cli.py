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
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

os.environ.setdefault("PYTHONUTF8", "1")   # 子进程中文不乱码

from bidviz_config import load as load_cfg  # noqa: E402

PROJECTS = BASE / "projects"


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
    load_cfg(root)
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
    out = Path(args.out) if args.out else cfg.output / "施工组织设计-图册-A3.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.engine == "v13" or (args.engine is None and cfg.layout_engine == "v13"):
        # 新体系（V1.3）：独立子进程跑 build_v13——import 即切 A3 竖版画布，
        # 不得与旧体系同进程混跑（踩坑 #11）
        print("运行 V1.3 新体系编译（page-plan → A3 竖版）…")
        r = subprocess.run([sys.executable, str(BASE / "src" / "build_v13.py"),
                            "--root", str(root), "--out", str(out)], cwd=BASE)
        raise SystemExit(r.returncode)
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


def cmd_status(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    print(f"项目根    : {root}")
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
    out = cfg.output
    if out.exists():
        for f in sorted(out.glob("*.pdf")):
            print(f"  产物           {f.name}  {round(f.stat().st_size/1024/1024,1)} MB")


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
    p.add_argument("--engine", choices=["legacy", "v13"], default=None,
                   help="排版引擎：legacy=旧两页体系（默认）；v13=V1.3 新体系（A3 竖版 8 类模板）"
                        "；缺省读 bidviz.toml [layout].engine")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("audit", help="覆盖率审计")
    p.add_argument("project")
    p.add_argument("--pdf")
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

    p = sub.add_parser("status", help="项目状态")
    p.add_argument("project")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
