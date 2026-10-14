#!/usr/bin/env python3
"""bidviz —— 施工组织设计图册生成器 CLI。

用法：
    bidviz init     <项目名>             新建项目
    bidviz ingest   <项目>               解析输入文档 → blocks.json
    bidviz generate <项目> [--all|--chapter 第一章] [--force]
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
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

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
    print(f"  3) 运行 bidviz generate {args.name} --all")


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


def cmd_generate(args) -> None:
    root = resolve_project(args.project)
    load_cfg(root)
    cmd = [sys.executable, str(BASE / "src" / "generate_chapter.py"), "--root", str(root)]
    cmd += ["--all"] if args.all else (["--chapter", args.chapter] if args.chapter else ["--all"])
    if args.force:
        cmd.append("--force")
    if args.outdir:
        cmd += ["--outdir", args.outdir]
    raise SystemExit(subprocess.run(cmd, cwd=BASE).returncode)


def cmd_build(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    out = Path(args.out) if args.out else cfg.output / "施工组织设计-图册-A3.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    import build_e1 as be
    pages, regions = be.build_final(root, out)
    print(f"[OK] 已生成 {out}（{pages} 页，{round(out.stat().st_size/1024/1024, 1)} MB）")
    audit = be.audit_coverage(out, root, regions)
    (cfg.output / "audit-vector.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 正文覆盖率 {audit['coverage_pct']}%（缺失 {audit['blocks_missing']} 块）")


def cmd_audit(args) -> None:
    root = resolve_project(args.project)
    cfg = load_cfg(root)
    pdf = Path(args.pdf) if args.pdf else cfg.output / "施工组织设计-图册-A3.pdf"
    if not pdf.exists():
        raise SystemExit(f"找不到 PDF：{pdf}")
    import build_e1 as be
    be.audit_coverage(pdf, root)


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
    print()
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

    p = sub.add_parser("generate", help="生成主视觉")
    p.add_argument("project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--chapter")
    p.add_argument("--force", action="store_true")
    p.add_argument("--outdir")
    p.set_defaults(fn=cmd_generate)

    p = sub.add_parser("build", help="汇编 PDF + 审计")
    p.add_argument("project")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("audit", help="覆盖率审计")
    p.add_argument("project")
    p.add_argument("--pdf")
    p.set_defaults(fn=cmd_audit)

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
