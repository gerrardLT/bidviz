#!/usr/bin/env python3
"""E1 文档编译器：分项 → 图版页（本节规划的 1~N 张 AI 主图）+ 正文页；并支持从输出 PDF 反抽文字校验覆盖率。"""

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
import bidviz_plan as plan

MM = bv.MM
PAGE_W, PAGE_H = bv.PAGE_W, bv.PAGE_H


def load_project(root: Path) -> tuple[list[Any], dict[int, list[str]]]:
    """加载项目 blocks 与 page-media-map；保持 2-tuple 返回以兼容既有调用方。

    issues.json 走 :func:`load_issues`，避免污染 load_project 的签名。
    """
    import bidviz_v2 as v2
    blocks = [v2.SourceBlock(**x) for x in json.loads((root / "work/blocks.json").read_text(encoding="utf-8"))]
    pm = json.loads((root / "work/page-media-map.json").read_text(encoding="utf-8"))
    page_media = {int(k): v for k, v in pm.items()}
    return blocks, page_media


def load_issues(root: Path) -> dict[str, Any]:
    """加载 work/issues.json；文件缺失或损坏时返回空 dict（不阻断 build）。"""
    path = root / "work" / "issues.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


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
          limit: int | None = None, hero_dir: Path | None = None,
          ensure: bool = True) -> tuple[int, list[list[tuple[float, float, float, float]]]]:
    """编译图册：每节 = 一页 AI 图 + 一页内容报表（工程报表式，无多余装饰）。"""
    from bidviz_config import load as _load_cfg, get as _cfg
    _load_cfg(root)
    e1.apply_style()
    blocks, page_media = load_project(root)
    global_media = sorted(str(p) for p in (root / "work/page-media").glob("*.*"))
    sections = e1.group_sections(blocks)
    if limit:
        sections = sections[:limit]
    apply_edits(root, sections)

    # 图位规划先落盘；两遍编译时由 build_final 统一 ensure，这里只兜底规划缺失的情况
    if ensure and plan.load_plan(root) is None:
        plan.ensure_plan(root)

    hero_dir = hero_dir or _cfg().image_dir
    sheet = bv.Sheet(out_pdf)
    page_regions: list[list[tuple[float, float, float, float]]] = []
    page_no = 0
    est_total = total_pages or 0

    for sec in sections:
        vis = sec.as_visual_item()
        sid = sec.section_id
        # ---------- 图位规划与图像定位 ----------
        figs = plan.section_figures(root, sid, vis)
        dirs = [hero_dir] if hero_dir and hero_dir != _cfg().image_dir else None
        imgs = plan.resolve_images(root, sid, figs, dirs=dirs)

        # ---------- 图页（按节）：本节规划的全部主图排在一页 ----------
        page_no += 1
        e1.gallery_page(sheet, vis, imgs, figs, page_no, est_total, root)
        page_regions.append([(0, 0, PAGE_W, PAGE_H)])
        sheet.c.showPage()

        # ---------- 内容报表页：工序/参数/风险/资源表格 ----------
        page_no += 1
        used = e1.content_tables_page(sheet, vis, page_no, est_total)
        page_regions.extend([e1.data_regions() for _ in range(used)])
        page_no += used - 1
        sheet.c.showPage()

    sheet.c.save()
    return page_no, page_regions


def build_final(root: Path, out_pdf: Path, limit: int | None = None,
                hero_dir: Path | None = None) -> tuple[int, list[list[tuple[float, float, float, float]]]]:
    """先落盘图位规划，再双遍编译：首遍测页数，次遍写准确总页码。

    V1.3 阶段 1：附加跑 audit_layout 与 issues 门禁，报告写入 output/。
    乱度门禁与 issues 门禁当前仅告警（不阻断），阻断策略在阶段 2/3 引入。
    """
    from bidviz_config import load as _load_cfg
    _load_cfg(root)
    plan.ensure_plan(root)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    probe = out_pdf.with_name(out_pdf.stem + ".pass1.pdf")
    final_tmp = out_pdf.with_name(out_pdf.stem + ".final-tmp.pdf")
    try:
        total, _ = build(root, probe, limit=limit, hero_dir=hero_dir, ensure=False)
        pages, regions = build(root, final_tmp, total_pages=total, limit=limit,
                               hero_dir=hero_dir, ensure=False)
        import bidviz_ioutil
        bidviz_ioutil.atomic_replace_from(final_tmp, out_pdf)
    finally:
        probe.unlink(missing_ok=True)
        final_tmp.unlink(missing_ok=True)

    # ---- 附加门禁：audit_layout + issues（仅告警，不阻断） ----
    _run_post_gates(root, out_pdf)
    return pages, regions


def _run_post_gates(root: Path, out_pdf: Path) -> None:
    """跑乱度门禁与 issues 门禁，报告写入 output/，stdout 告警。"""
    # 1) audit_layout
    try:
        import audit_layout
        base = Path(__file__).resolve().parent.parent
        tokens = base / "docs" / "layout-md" / "tokens.toml"
        if tokens.exists():
            rep_path = out_pdf.parent / "audit-layout.json"
            rep = audit_layout.audit_pdf(out_pdf, tokens, rep_path)
            total_pages = len(rep["pages"])
            failed = rep["failed_pages"]
            print(f"[gate] audit_layout：{total_pages} 页，超标 {len(failed)} 页"
                  + (f" → {failed[:8]}{'…' if len(failed) > 8 else ''}" if failed else ""))
    except Exception as exc:   # pragma: no cover
        print(f"[gate] audit_layout 跳过：{exc}")

    # 2) issues 门禁
    try:
        import bidviz_issues
        doc = bidviz_issues.load(root)
        passed, reason = bidviz_issues.gate_check(doc)
        if not passed:
            print(f"[gate] issues BLOCK：{reason}（阶段 1 仅告警，阶段 2/3 将阻断 final）")
        else:
            print(f"[gate] issues PASS（{reason}）")
    except Exception as exc:   # pragma: no cover
        print(f"[gate] issues 跳过：{exc}")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def audit_coverage(pdf: Path, root: Path, page_regions: list[list[tuple[float, float, float, float]]] | None = None,
                   limit: int | None = None) -> dict[str, Any]:
    """从输出 PDF 反抽全部文字，校验内容报表里每一行（逐字取自原文的提炼内容）确实排进页面。

    不再要求原文全文 100% 出现：成品以图为主、报表为辅，只审计实际展示的提炼内容。
    """
    blocks_all, _ = load_project(root)
    sections = e1.group_sections(blocks_all)
    if limit:
        sections = sections[:limit]
    apply_edits(root, sections)

    doc = fitz.open(pdf)
    whole = norm("".join(page.get_text() for page in doc))
    n_pages = doc.page_count

    missing = []
    checked = 0
    for sec in sections:
        for _cap, _heads, rows, _ratios in e1.content_tables(sec.as_visual_item()):
            for row in rows:
                for cell in row:
                    t = norm(str(cell))
                    if len(t) < 4:
                        continue
                    checked += 1
                    if t not in whole:
                        missing.append({"section": sec.section_id, "chars": len(t),
                                        "text": str(cell)[:90]})
    doc.close()
    return {
        "pdf_pages": n_pages,
        "rows_checked": checked,
        "rows_missing": len(missing),
        "coverage_pct": round(100 * (checked - len(missing)) / checked, 2) if checked else 100.0,
        "pdf_bytes": pdf.stat().st_size,
        "missing_sample": missing[:12],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="build", choices=["build"])
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    root = Path(args.root)
    out = Path(args.out) if args.out else root / "output/施工组织设计-矢量图册-A3.pdf"

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
