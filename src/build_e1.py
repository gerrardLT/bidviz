#!/usr/bin/env python3
"""E1 文档编译器：分项 → 视觉页 + 数据页；并支持从输出 PDF 反抽文字校验覆盖率。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import fitz

import bidviz_vector as bv
import bidviz_e1 as e1

MM = bv.MM
PAGE_W, PAGE_H = bv.PAGE_W, bv.PAGE_H


def load_project(root: Path) -> tuple[list[Any], dict[int, list[str]]]:
    import bidviz_v2 as v2
    blocks = [v2.SourceBlock(**x) for x in json.loads((root / "work/blocks.json").read_text(encoding="utf-8"))]
    pm = json.loads((root / "work/page-media-map.json").read_text(encoding="utf-8"))
    page_media = {int(k): v for k, v in pm.items()}
    return blocks, page_media


def apply_edits(root: Path, sections: list[e1.Section]) -> list[Any]:
    """把非破坏式 edits.json 投影到内存；原 blocks.json 不改。"""
    path = root / "work" / "edits.json"
    if not path.exists():
        return [b for sec in sections for b in sec.blocks]
    try:
        edits = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [b for sec in sections for b in sec.blocks]

    from bidviz_v2 import SourceBlock
    for sec in sections:
        edit = edits.get(sec.section_id)
        if not isinstance(edit, dict):
            continue
        if isinstance(edit.get("title"), str) and edit["title"].strip():
            sec.title = edit["title"].strip()
        raw_blocks = edit.get("blocks")
        if not isinstance(raw_blocks, list) and isinstance(edit.get("body"), str):
            paragraphs = [x.strip() for x in re.split(r"\n{2,}", edit["body"]) if x.strip()]
            raw_blocks, pos = [], 0
            for block in sec.blocks:
                if block.kind == "heading":
                    raw_blocks.append({"kind": "heading", "text": block.text})
                elif pos < len(paragraphs):
                    raw_blocks.append({"kind": "body", "text": paragraphs[pos]})
                    pos += 1
            raw_blocks.extend({"kind": "body", "text": text} for text in paragraphs[pos:])
        if not isinstance(raw_blocks, list):
            continue
        template = sec.blocks[0] if sec.blocks else None
        if template is None:
            continue
        blocks = []
        for n, raw in enumerate(raw_blocks):
            if not isinstance(raw, dict) or not str(raw.get("text", "")).strip():
                continue
            text = str(raw["text"]).strip()
            blocks.append(SourceBlock(
                block_id=f"{sec.section_id}-E{n + 1:04d}",
                source_page=template.source_page,
                order=template.order + n,
                kind="heading" if raw.get("kind") == "heading" else "body",
                section_path=sec.chapter,
                text=text,
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            ))
        if blocks:
            sec.items = [e1.WorkItem(sec.section_id, sec.title, sec.chapter, blocks,
                                     sorted({b.source_page for b in blocks}))]
    return [b for sec in sections for b in sec.blocks]


def pick_details(item: e1.WorkItem, page_media: dict[int, list[str]],
                 global_media: list[str], n: int = 3) -> list[str]:
    out: list[str] = []
    for sp in item.source_pages:
        for f in page_media.get(sp, []):
            if f not in out:
                out.append(f)
            if len(out) >= n:
                return out
    for f in global_media:
        if f not in out:
            out.append(f)
        if len(out) >= n:
            break
    return out


def build(root: Path, out_pdf: Path, total_pages: int | None = None,
          limit: int | None = None, hero_dir: Path | None = None) -> tuple[int, list[list[tuple[float, float, float, float]]]]:
    from bidviz_config import load as _load_cfg, get as _cfg
    _load_cfg(root)
    e1.apply_style()
    blocks, page_media = load_project(root)
    global_media = sorted(str(p) for p in (root / "work/page-media").glob("*.*"))
    sections = e1.group_sections(blocks)
    if limit:
        sections = sections[:limit]
    apply_edits(root, sections)

    hero_dir = hero_dir or _cfg().image_dir
    sheet = bv.Sheet(out_pdf)
    page_regions: list[list[tuple[float, float, float, float]]] = []
    page_no = 0
    est_total = total_pages or 0

    for sec in sections:
        vis = sec.as_visual_item()
        # ---------- 视觉页（按节） ----------
        page_no += 1
        candidates = [hero_dir] if hero_dir != _cfg().image_dir else _cfg().image_dirs
        hero = next((d / f"{sec.section_id}.png" for d in candidates
                     if (d / f"{sec.section_id}.png").exists()), None)
        e1.visual_page(
            sheet, vis,
            hero,
            e1.extract_params(vis),
            e1.extract_risks(vis),
            e1.extract_steps(vis),
            page_no, est_total, root,
        )
        page_regions.append([(0, 0, PAGE_W, PAGE_H)])
        sheet.c.showPage()

        # ---------- 图表页（摘要，不替代完整正文） ----------
        page_no += 1
        e1.chart_page(sheet, vis, page_no, est_total)
        page_regions.append([(0, 0, PAGE_W, PAGE_H)])
        sheet.c.showPage()

        # ---------- 正文页：矢量文字，承载完整原文/编辑后正文 ----------
        page_no += 1
        used = e1.text_pages(sheet, vis, page_no, est_total)
        page_regions.extend([e1.data_regions() for _ in range(used)])
        page_no += used - 1
        sheet.c.showPage()

    sheet.c.save()
    return page_no, page_regions


def build_final(root: Path, out_pdf: Path, limit: int | None = None,
                hero_dir: Path | None = None) -> tuple[int, list[list[tuple[float, float, float, float]]]]:
    """双遍编译：首遍测页数，次遍写准确总页码。"""
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    probe = out_pdf.with_name(out_pdf.stem + ".pass1.pdf")
    try:
        total, _ = build(root, probe, limit=limit, hero_dir=hero_dir)
        return build(root, out_pdf, total_pages=total, limit=limit, hero_dir=hero_dir)
    finally:
        probe.unlink(missing_ok=True)


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def audit_coverage(pdf: Path, root: Path, page_regions: list[list[tuple[float, float, float, float]]] | None = None,
                   limit: int | None = None) -> dict[str, Any]:
    """从输出 PDF 反抽全部文字，验证原文是否逐字排进页面。

    只用整页反抽：包含关系是保守判定（抽得到就一定在 PDF 里），不会误报达标。
    """
    blocks_all, _ = load_project(root)
    sections = e1.group_sections(blocks_all)
    if limit:
        sections = sections[:limit]
    blocks = apply_edits(root, sections)

    doc = fitz.open(pdf)
    # 两个视角合并判定：
    # 1) 整页全文：覆盖视觉页（标题在页眉带内）
    # 2) 去页眉页脚后的正文流：段落跨页时，页眉页脚文字会插进两半中间，
    #    导致整页拼接不再连续，必须用不带页眉页脚的版本才能匹配。
    whole_full = norm("".join(page.get_text() for page in doc))
    body_band = fitz.Rect(0, e1.HEAD_H, PAGE_W, PAGE_H - e1.FOOT_H)
    whole_body = norm("".join(page.get_text(clip=body_band) for page in doc))

    missing = []
    checked = 0
    for b in blocks:
        t = norm(b.text)
        if len(t) < 4:
            continue
        checked += 1
        if t not in whole_full and t not in whole_body:
            missing.append({"block_id": b.block_id, "chars": len(t), "text": b.text[:90]})
    return {
        "pdf_pages": len(doc),
        "blocks_checked": checked,
        "blocks_missing": len(missing),
        "coverage_pct": round(100 * (checked - len(missing)) / checked, 2) if checked else 100.0,
        "source_chars": sum(len(b.text) for b in blocks),
        "pdf_bytes": pdf.stat().st_size,
        "missing_sample": missing[:12],
    }


def generate_heroes(root: Path, provider: str = "gptimage2", model: str = "gpt-image-2.5",
                    only_missing: bool = True, limit: int | None = None, only: str | None = None,
                    attempts: int = 4, pause: float = 3.0, size: str = "1536x1024") -> dict[str, Any]:
    """为每个「节」生成一张视觉页主图，落盘为 work/visuals/{section_id}.png。

    - only: 只重生指定节（如 S006），支持单节局部修改
    - 默认用 gpt-image-2.5（中文标注为真字），无汉字需求的节可改 provider
    - 生图接口会间歇性断连，每张图带指数退避重试
    """
    import bidviz
    blocks_all, _ = load_project(root)
    sections = e1.group_sections(blocks_all)
    if only:
        wanted = {x.strip().upper() for x in only.split(",")}
        sections = [s for s in sections if s.section_id.upper() in wanted]
        if not sections:
            raise SystemExit(f"未找到指定节：{only}")
    if limit:
        sections = sections[:limit]

    api = bidviz.ImageAPI(provider_name=provider, model=model)
    out_dir = root / "work/visuals"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = root / "work/generation-log-e1.json"
    log: list[dict[str, Any]] = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []

    done = skipped = failed = 0
    for i, sec in enumerate(sections, 1):
        target = out_dir / f"{sec.section_id}.png"
        if only_missing and target.exists() and target.stat().st_size > 2000:
            skipped += 1
            continue
        vis = sec.as_visual_item()
        style = e1.pick_style(vis)
        rec: dict[str, Any] = {"section_id": sec.section_id, "title": sec.title,
                              "style": style, "provider": provider, "model": model}
        prompt = e1.visual_prompt(vis, style)
        ok = False
        last = ""
        for attempt in range(1, attempts + 1):
            try:
                api.generate(prompt, target, recorder=rec, size=size)
                ok = True
                break
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"[:200]
                if attempt < attempts:
                    time.sleep(pause * (2 ** (attempt - 1)))
        if ok:
            done += 1
            rec["ok"] = True
            rec["attempts"] = attempt
        else:
            failed += 1
            rec["ok"] = False
            rec["error"] = last
        log.append(rec)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{i}/{len(sections)}] {sec.section_id} style={style} "
              f"{'OK' if ok else 'FAIL'} attempts={attempt} {last}", flush=True)
        time.sleep(pause)

    return {"total": len(sections), "generated": done, "skipped": skipped, "failed": failed}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="build", choices=["build", "generate", "all"])
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--provider", default="gptimage2")
    ap.add_argument("--model", default="gpt-image-2.5")
    ap.add_argument("--size", default="1536x1024")
    ap.add_argument("--only", default=None, help="只重生指定节，如 S006 或 S006,S007")
    ap.add_argument("--force", action="store_true", help="重新生成已有主图")
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "output/施工组织设计-矢量图册-A3.pdf"

    if args.cmd in ("generate", "all"):
        t0 = time.time()
        stat = generate_heroes(root, args.provider, args.model,
                               only_missing=not args.force, limit=args.limit,
                               only=args.only, size=args.size)
        print(f"生图完成: {stat}  耗时 {round(time.time()-t0,1)}s", flush=True)

    if args.cmd in ("build", "all"):
        t0 = time.time()
        pages1, _ = build(root, out.with_suffix(".pass1.pdf"), total_pages=None, limit=args.limit)
        pages2, regions = build(root, out, total_pages=pages1, limit=args.limit)
        out.with_suffix(".pass1.pdf").unlink(missing_ok=True)
        print(f"页数: {pages2}  渲染耗时: {round(time.time()-t0, 2)}s", flush=True)
        print(f"输出: {out}  {round(out.stat().st_size/1024, 1)} KB", flush=True)
        rep = audit_coverage(out, root, limit=args.limit)
        print(json.dumps(rep, ensure_ascii=False, indent=2)[:2500], flush=True)
        (out.parent / "audit-vector.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
