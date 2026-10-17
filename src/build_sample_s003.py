#!/usr/bin/env python3
"""build_sample_s003 —— 渲染 S003 样板页（T-B1 版式）到 output/_样板-S003.pdf。

用法：
    python src/build_sample_s003.py [--root projects/anke-35kv] [--out output/_样板-S003.pdf]
    python src/build_sample_s003.py --contact-sheet   # 额外生成与样图 1 的并排对比 PNG

流程：
1. 读 work/page-plan.json（仅 S003 一节一页）。
2. bv.set_page('A3','portrait') 切换竖版画布。
3. templates.compose_page(sheet, tokens, plan, 'T-B1') 装配。
4. 落盘 PDF；可选生成 contact sheet（左=样图1，右=渲染页 PNG）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_templates as tpl
import bidviz_vector as bv
from bidviz_templates import (
    Callout, FigureSlot, KPI, PagePlan, ProseBlock,
)
from bidviz_vector import Sheet

MM = bv.MM


def _page_plan_from_dict(raw: dict, root: Path) -> PagePlan:
    """把 page-plan.json 的单页 dict 转成 PagePlan dataclass。"""
    kpis = [KPI(**k) for k in raw.get("kpis", [])]
    fig_raw = raw.get("figure")
    figure = None
    if fig_raw:
        img = fig_raw.get("image")
        img_path = (root / img) if img else None
        callouts = [Callout(no=c["no"], text=c.get("text", ""),
                            anchor=tuple(c.get("anchor", (0.5, 0.5))))
                    for c in fig_raw.get("callouts", [])]
        figure = FigureSlot(image=img_path, callouts=callouts,
                            caption=fig_raw.get("caption", ""),
                            text_spec=fig_raw.get("text_spec", []),
                            fit=fig_raw.get("fit", "contain"),
                            crop_top=float(fig_raw.get("crop_top", 0.0)),
                            cover_anchor=tuple(fig_raw.get("cover_anchor", (0.5, 0.5))))
    prose = [ProseBlock(text=p["text"], kind=p.get("kind", "body"))
             for p in raw.get("prose", [])]
    it = raw.get("icon_table", {})
    return PagePlan(
        chapter=raw.get("title", {}).get("chapter", ""),
        section=raw.get("title", {}).get("section", ""),
        page_no=raw.get("page_no", 1),
        total_pages=raw.get("total_pages", 1),
        kpis=kpis,
        figure=figure,
        prose=prose,
        icon_table_headers=it.get("headers", []),
        icon_table_rows=it.get("rows", []),
        footer_note=raw.get("footer_note", ""),
    )


def render_s003(root: Path, out_pdf: Path) -> Path:
    """渲染 S003 样板页到 out_pdf；返回 out_pdf。"""
    pp_path = root / "work" / "page-plan.json"
    if not pp_path.exists():
        raise SystemExit(f"缺少 page-plan.json：{pp_path}")
    pp = json.loads(pp_path.read_text(encoding="utf-8"))
    sec = pp.get("sections", {}).get("S003")
    if not sec or not sec.get("pages"):
        raise SystemExit("page-plan.json 中没有 S003 页")
    page_raw = sec["pages"][0]
    plan = _page_plan_from_dict(page_raw, root)

    # 切竖版画布（新体系）
    bv.set_page("A3", "portrait")
    tokens = tpl.load_tokens()

    sheet = Sheet(out_pdf)
    tpl.compose_page(sheet, tokens, plan, template="T-B1")
    sheet.save()
    return out_pdf


def make_contact_sheet(root: Path, pdf_path: Path, out_png: Path,
                       sample: Path | None = None) -> Path:
    """生成并排对比 PNG：左=样图 1，右=渲染页（300DPI 缩到同高）。"""
    import fitz
    from PIL import Image

    sample = sample or (BASE / "docs" / "example-image" /
                        "ChatGPT_图像_2026年10月16日_10_21_30-1.png")
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=150)
    rendered = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()

    ref = Image.open(sample).convert("RGB")
    # 同高并排
    target_h = max(rendered.height, ref.height)
    def _scale(im: Image.Image) -> Image.Image:
        r = target_h / im.height
        return im.resize((int(im.width * r), target_h), Image.Resampling.LANCZOS)
    ref_s = _scale(ref)
    ren_s = _scale(rendered)
    gap = 24
    canvas = Image.new("RGB", (ref_s.width + ren_s.width + gap * 3, target_h + gap * 2),
                       "white")
    canvas.paste(ref_s, (gap, gap))
    canvas.paste(ren_s, (gap * 2 + ref_s.width, gap))
    canvas.save(out_png, "PNG")
    return out_png


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--contact-sheet", action="store_true",
                    help="额外生成与样图 1 的并排对比 PNG")
    args = ap.parse_args()

    root = (BASE / args.root).resolve()
    out = Path(args.out) if args.out else root / "output" / "_样板-S003.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)

    render_s003(root, out)
    print(f"[OK] S003 样板页已渲染：{out}（{out.stat().st_size/1024:.0f} KB）")

    if args.contact_sheet:
        cs = out.with_name("_样板-S003-contact-sheet.png")
        make_contact_sheet(root, out, cs)
        print(f"[OK] contact sheet 已生成：{cs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
