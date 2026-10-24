#!/usr/bin/env python3
"""bidviz_html —— AI 局部插图 + HTML/CSS 排版的备选引擎（engine=html）。

和 fullimage 的区别：
- AI 只画局部插图（figure-plan 每节 1–2 张），图内不出现任何文字；
- 正文、表格、KPI、页眉页脚、页码全部由 HTML/CSS 矢量排版；
- Chromium/Edge headless 打印成矢量 PDF，文字可搜索、可审计；
- 需要页图时再按 dpi 渲染 PNG（真页码命名）。

产物：
  work/illustrations/{Sxxx}.png / {Sxxx}__2.png   局部插图
  work/illustrations-ledger.json                 插图台账
  work/html/album.html                           汇编 HTML
  work/html-pages.json                           页号 → 节/块/表行 清单
  output/施工组织设计-图册-A3-html.pdf            矢量 PDF（正式）
  output/施工组织设计-图册-A3-html-DRAFT.pdf      DRAFT
  output/page-images/<PDF名>/001_Sxxx_pN.png      可选页图
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_mod
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_plan as plan_mod  # noqa: E402
import bidviz_templates as tpl  # noqa: E402

try:  # PyMuPDF 新版本推荐 pymupdf，旧版本仍是 fitz
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

TOKENS_PATH = BASE / "docs" / "layout-md" / "tokens-html.toml"
CSS_PATH = BASE / "templates" / "html" / "album.css"
ILLUSTRATION_PROMPT = BASE / "prompts" / "illustration.md"
OUT_SUFFIX = "施工组织设计-图册-A3-html"
STORE_DIR = "work/html"
ILLUSTRATION_DIR = "work/illustrations"
ILLUSTRATION_LEDGER = "illustrations-ledger.json"
HTML_PAGES = "work/html-pages.json"


# ---------------------------------------------------------------- token / 配置


def load_tokens(path: Path | None = None) -> dict[str, Any]:
    with (Path(path) if path else TOKENS_PATH).open("rb") as fh:
        return tomllib.load(fh)


def _px(value: float) -> float:
    """CSS 测量值（96dpi 像素）→ PDF pt。"""
    return float(value) * 72.0 / 96.0


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _ratio_value(ratio: str) -> float:
    """'16:9' / '4:3' / '1:1' → 宽高比；未知返回 4/3。"""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)\s*", str(ratio or ""))
    if not m:
        return 4.0 / 3.0
    w, h = float(m.group(1)), float(m.group(2))
    return w / h if w and h else 4.0 / 3.0


def _content_width_pt(tokens: dict[str, Any]) -> float:
    page = tokens["page"]
    return page["width_mm"] * 72.0 / 25.4 - 2 * page["margin_x"]


# ---------------------------------------------------------------- 数据模型


@dataclass
class Block:
    block_id: str
    kind: str                       # kpi / figure / vector / prose / table
    data: dict[str, Any] = field(default_factory=dict)
    height: float = 0.0
    header_height: float = 0.0
    row_heights: list[float] = field(default_factory=list)


@dataclass
class TableChunk:
    table_id: str
    caption: str
    headers: list[str]
    rows: list[list[str]]
    col_ratios: list[float]
    first: bool = True


@dataclass
class HtmlPage:
    sid: str
    section_page: int
    chapter: str
    section: str
    footer_note: str
    blocks: list[Any] = field(default_factory=list)
    overflow: list[str] = field(default_factory=list)
    global_no: int = 0


# ---------------------------------------------------------------- page-plan


def load_page_plan(root: Path) -> dict[str, Any]:
    pp = plan_mod.load_page_plan(root)
    if not pp:
        pp = plan_mod.build_page_plan(root)
    return pp or {"sections": {}}


def load_figure_plan(root: Path) -> dict[str, Any]:
    fp = plan_mod.load_plan(root)
    if not fp:
        fp = plan_mod.ensure_plan(root)
    return fp or {"sections": {}}


def section_title(entry: dict[str, Any]) -> tuple[str, str]:
    pages = entry.get("pages") or []
    if pages:
        title = pages[0].get("title") or {}
        return title.get("chapter", ""), title.get("section", entry.get("title", ""))
    return entry.get("title", ""), entry.get("title", "")


def section_plans(root: Path, entry: dict[str, Any]) -> list[tpl.PagePlan]:
    raw_pages = entry.get("pages") or []
    out = []
    for i, raw in enumerate(raw_pages, 1):
        out.append(tpl.page_plan_from_dict(
            raw, root, int(raw.get("page_no", i)), int(raw.get("total_pages", len(raw_pages)))))
    return out


def load_section_items(root: Path) -> dict[str, Any]:
    """blocks.json → Section 对象，供 HTML 重新策展完整表格（不吃 page-plan 的 70 字截断）。"""
    try:
        import bidviz_e1 as e1
        import build_e1 as be
        blocks, _ = be.load_project(Path(root))
        return {s.section_id: s for s in e1.group_sections(blocks)}
    except Exception:
        return {}


def html_tables(root: Path, sid: str, item: Any, cell_max: int) -> list[dict[str, Any]] | None:
    """HTML 专用表格：同一套策展规则，但单元格保留更长的原文。"""
    if item is None:
        return None
    try:
        raw = plan_mod.curate_tables(item, cell_max=cell_max)
    except Exception:
        return None
    out = []
    for caption, headers, rows, ratios in raw:
        clean = []
        for row in rows:
            cells = list(row)
            if len(cells) >= 2 and len(str(cells[0]).strip()) <= 1 and str(cells[1]).strip():
                head = str(cells[1]).strip()
                cells[0] = head[:12] + ("…" if len(head) > 12 else "")
            clean.append(cells)
        out.append({"caption": caption, "headers": list(headers),
                    "rows": clean, "col_ratios": list(ratios)})
    return out


def section_figures(root: Path, sid: str, fp: dict[str, Any]) -> list[dict[str, Any]]:
    entry = (fp.get("sections") or {}).get(sid, {}) or {}
    out = []
    for fig in entry.get("figures", []):
        no = int(fig.get("no", 1))
        path = Path(root) / ILLUSTRATION_DIR / plan_mod.figure_filename(sid, no)
        out.append({
            "no": no,
            "path": path if path.is_file() and path.stat().st_size > 2000 else None,
            "caption": str(fig.get("name", "") or ""),
            "kind": str(fig.get("kind", "") or ""),
            "ratio": str(fig.get("ratio", "") or ""),
            "why": str(fig.get("why", "") or ""),
        })
    return out


# ---------------------------------------------------------------- 内容流


def _table_block(sid: str, table: dict[str, Any], index: int) -> Block:
    headers = [str(x) for x in (table.get("headers") or [])]
    rows = []
    for row in table.get("rows") or []:
        cells = [str(x) for x in row]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        rows.append(cells[:len(headers)] if headers else cells)
    ratios = [float(x) for x in (table.get("col_ratios") or [])]
    return Block(
        block_id=f"{sid}:t{index}",
        kind="table",
        data={"caption": str(table.get("caption", "") or ""), "headers": headers,
              "rows": rows, "col_ratios": ratios, "table_id": f"{sid}:t{index}"},
    )


def build_section_blocks(root: Path, sid: str, entry: dict[str, Any],
                         figures: list[dict[str, Any]],
                         tables: list[dict[str, Any]] | None = None,
                         cover_meta: dict[str, Any] | None = None
                         ) -> tuple[list[Block], list[tpl.PagePlan]]:
    plans = section_plans(root, entry)
    chapter, sec_title = section_title(entry)
    blocks: list[Block] = []
    counter = 0

    def add(kind: str, data: dict[str, Any]) -> None:
        nonlocal counter
        counter += 1
        blocks.append(Block(block_id=f"{sid}:b{counter}", kind=kind, data=data))

    # 封面：T-A1 专用版式，不用 page-plan 的表格
    is_cover = (sid == "S001" and "封面" in str(entry.get("title", "")))
    if is_cover and cover_meta:
        add("cover", dict(cover_meta))
        return blocks, plans

    # KPI：只取第一个页对象
    if plans and plans[0].kpis:
        add("kpi", {"kpis": [
            {"label": k.label, "value": k.value, "unit": k.unit, "note": k.note, "icon": k.icon}
            for k in plans[0].kpis]})

    # 局部插图：只放已生成的；缺图不占版面（Web/构建报告会列 missing）
    present = [f for f in figures if f.get("path")]
    if present:
        add("figure", {"figure": present[0]})

    if plans and plans[0].vector:
        add("vector", {"vector": plans[0].vector})

    table_index = 0
    use_tables = tables if tables is not None else [
        t for plan in plans for t in plan.tables]
    for plan in plans:
        for prose in plan.prose:
            text = prose.text.strip()
            if not text:
                continue
            add("prose", {"text": text, "kind": prose.kind,
                          "level": _prose_level(text, prose.kind)})
    for table in use_tables:
        table_index += 1
        blocks.append(_table_block(sid, table, table_index))

    if len(present) > 1:
        for fig in present[1:]:
            add("figure", {"figure": fig})

    if not blocks:
        add("prose", {"text": "", "kind": "body", "level": 1})
    return blocks, plans


def _prose_level(text: str, kind: str) -> int:
    if kind == "heading":
        if re.match(r"^第[一二三四五六七八九十百\d]+[章节]", text):
            return 2
        if re.match(r"^[一二三四五六七八九十]+、", text):
            return 3
        if re.match(r"^（[一二三四五六七八九十]+）|^\([一二三四五六七八九十]+\)", text):
            return 4
        return 3
    return 1


# ---------------------------------------------------------------- HTML 片段


def _colgroup(headers: Sequence[str], ratios: Sequence[float]) -> str:
    n = len(headers)
    if not n:
        return ""
    vals = list(ratios[:n]) if ratios else [1.0] * n
    if len(vals) < n:
        vals += [1.0] * (n - len(vals))
    total = sum(v for v in vals if v > 0) or float(n)
    cols = "".join(f'<col style="width:{max(2.0, v / total * 100):.3f}%">' for v in vals)
    return f"<colgroup>{cols}</colgroup>"


def _table_html(headers: Sequence[str], rows: Sequence[Sequence[str]],
                ratios: Sequence[float], caption: str = "") -> str:
    cap = f'<div class="table-caption">{html_mod.escape(caption)}</div>' if caption else ""
    head = ""
    if headers:
        head = "<thead><tr>" + "".join(
            f"<th>{html_mod.escape(str(h))}</th>" for h in headers) + "</tr></thead>"
    body = "".join(
        "<tr>" + "".join(f"<td>{html_mod.escape(str(c))}</td>" for c in row) + "</tr>"
        for row in rows)
    return (f'<div class="table-wrap">{cap}<table class="report">'
            f'{_colgroup(headers, ratios)}{head}<tbody>{body}</tbody></table></div>')


def _kpi_html(kpis: Sequence[dict[str, Any]]) -> str:
    cards = []
    for k in kpis:
        icon = f'<span class="kpi-icon">{html_mod.escape(str(k.get("icon", "")))}</span>' if k.get("icon") else ""
        unit = f'<span class="kpi-unit">{html_mod.escape(str(k.get("unit", "")))}</span>' if k.get("unit") else ""
        note = f'<div class="kpi-note">{html_mod.escape(str(k.get("note", "")))}</div>' if k.get("note") else ""
        cards.append(
            f'<div class="kpi-card"><div class="kpi-label">{html_mod.escape(str(k.get("label", "")))}</div>'
            f'<div class="kpi-value">{icon}{html_mod.escape(str(k.get("value", "")))}{unit}</div>{note}</div>')
    return '<div class="kpi-grid">' + "".join(cards) + "</div>"


def _figure_html(fig: dict[str, Any]) -> str:
    caption = html_mod.escape(str(fig.get("caption", "") or "局部插图"))
    path = fig.get("path")
    if path:
        uri = Path(path).resolve().as_uri()
        return (f'<figure class="figure"><img src="{html_mod.escape(uri, quote=True)}" alt="">'
                f'<figcaption>{caption}</figcaption></figure>')
    return (f'<figure class="figure"><div class="figure-placeholder">'
            f'插图待生成 · {caption}</div><figcaption>{caption}</figcaption></figure>')


def _vector_html(vector: dict[str, Any]) -> str:
    kind = str(vector.get("kind", "") or "")
    if kind == "timeline_bar":
        phases = vector.get("phases") or []
        if phases:
            items = []
            for i, p in enumerate(phases, 1):
                if not isinstance(p, dict):
                    p = {"label": str(p)}
                no = html_mod.escape(str(p.get("no", i)))
                name = html_mod.escape(str(p.get("name") or p.get("label") or ""))
                note = html_mod.escape(str(p.get("note", "")))
                items.append(f'<div class="phase"><div class="phase-no">{no}</div>'
                             f'<div class="phase-name">{name}</div>'
                             f'<div class="phase-note">{note}</div></div>')
            return '<div class="timeline">' + "".join(items) + "</div>"
    if kind == "org_chart":
        nodes = vector.get("nodes") or []
        if nodes:
            rows: dict[int, list[Any]] = {}
            for n in nodes:
                if isinstance(n, dict):
                    rows.setdefault(int(n.get("level", 0)), []).append(n)
                else:
                    rows.setdefault(0, []).append(str(n))
            html_rows = []
            for level in sorted(rows):
                cells = []
                for n in rows[level]:
                    if isinstance(n, dict):
                        title = str(n.get("name") or n.get("title") or "")
                        note = str(n.get("note") or "")
                    else:
                        title, note = str(n), ""
                    cells.append(f'<div class="org-node"><div class="title">{html_mod.escape(title)}</div>'
                                 f'<div class="note">{html_mod.escape(note)}</div></div>')
                html_rows.append(f'<div class="org-row">{"".join(cells)}</div>')
            return '<div class="org-chart">' + "".join(html_rows) + "</div>"
    if kind == "chevron_cards":
        cards = [c for c in (vector.get("cards") or []) if isinstance(c, dict)]
        if cards:
            cls = "chevron-cards many" if len(cards) > 3 else "chevron-cards"
            return f'<div class="{cls}">' + "".join(
                f'<div class="chevron"><div class="title">{html_mod.escape(str(c.get("title") or c.get("name") or ""))}</div>'
                f'<div class="note">{html_mod.escape(str(c.get("body") or c.get("note") or ""))}</div></div>'
                for c in cards if isinstance(c, dict)) + "</div>"
    # 未知矢量类型 → 退化成表
    headers = ["项目", "内容"]
    rows = [[str(vector.get("kind", "")), json.dumps(vector, ensure_ascii=False)[:300]]]
    return '<div class="vector-fallback">' + _table_html(headers, rows, [0.25, 0.75]) + "</div>"


def _prose_html(block: Block) -> str:
    text = str(block.data.get("text", ""))
    level = int(block.data.get("level", 1))
    if not text:
        return ""
    if level >= 3:
        tag = "h3" if level == 3 else "h4"
        return f'<div class="prose"><{tag}>{html_mod.escape(text)}</{tag}></div>'
    if level == 2:
        return f'<div class="prose"><h3>{html_mod.escape(text)}</h3></div>'
    return f'<div class="prose"><p>{html_mod.escape(text)}</p></div>'


def _cover_html(data: dict[str, Any]) -> str:
    project = html_mod.escape(str(data.get("project", "")))
    voltage = html_mod.escape(str(data.get("voltage", "")))
    doc_type = html_mod.escape(str(data.get("doc_type", "施工组织设计")))
    fields = ["投标编号", "投标单位", "法人代表", "投标日期"]
    field_html = "".join(
        f'<div class="cover-field"><span>{html_mod.escape(f)}：</span><i></i></div>'
        for f in fields)
    return f"""<div class="cover-inner">
  <div class="cover-kicker">投标文件</div>
  <div class="cover-project">{project}</div>
  <div class="cover-voltage">{voltage} 电力接入工程</div>
  <div class="cover-doc">{doc_type}</div>
  <div class="cover-rule"></div>
  <div class="cover-fields">{field_html}</div>
  <div class="cover-note">安科生物项目 · 施工组织设计</div>
</div>"""


def block_inner_html(block: Block) -> str:
    if block.kind == "cover":
        return _cover_html(block.data)
    if block.kind == "kpi":
        return _kpi_html(block.data.get("kpis") or [])
    if block.kind == "figure":
        return _figure_html(block.data.get("figure") or {})
    if block.kind == "vector":
        return _vector_html(block.data.get("vector") or {})
    if block.kind == "prose":
        return _prose_html(block)
    if block.kind == "table":
        return _table_html(block.data.get("headers") or [], block.data.get("rows") or [],
                           block.data.get("col_ratios") or [], block.data.get("caption", ""))
    return ""


def _measure_block_html(block: Block) -> str:
    """测量页：块外层带 data-measure-id；表格按行单独测量。"""
    if block.kind == "table":
        d = block.data
        headers = d.get("headers") or []
        rows = d.get("rows") or []
        tid = d["table_id"]
        cap = (f'<div class="table-caption">{html_mod.escape(d.get("caption", ""))}</div>'
               if d.get("caption") else "")
        head = ("<thead><tr data-measure-id=\"" + tid + ":h\">" +
                "".join(f"<th>{html_mod.escape(str(h))}</th>" for h in headers) + "</tr></thead>")
        body = "".join(
            f'<tr data-measure-id="{tid}:r{i}">' +
            "".join(f"<td>{html_mod.escape(str(c))}</td>" for c in row) + "</tr>"
            for i, row in enumerate(rows))
        return (f'<div class="table-wrap" data-measure-id="{tid}">{cap}'
                f'<table class="report">{_colgroup(headers, d.get("col_ratios") or [])}'
                f'{head}<tbody>{body}</tbody></table></div>')
    return f'<div data-measure-id="{block.block_id}">{block_inner_html(block)}</div>'


# ---------------------------------------------------------------- 测量


def _css_vars(tokens: dict[str, Any]) -> str:
    p = tokens["page"]
    pal = tokens["palette"]
    typ = tokens["typography"]
    sp = tokens["spacing"]
    comp = tokens["components"]
    fonts = tokens["fonts"]
    cw = _content_width_pt(tokens)
    vars_ = {
        "--margin-x": f"{p['margin_x']}pt",
        "--header-top": f"{p['header_top']}pt",
        "--header-h": f"{p['header_h']}pt",
        "--chapter-top": f"{p['chapter_top']}pt",
        "--chapter-h": f"{p['chapter_h']}pt",
        "--section-top": f"{p['section_top']}pt",
        "--section-h": f"{p['section_h']}pt",
        "--content-top-first": f"{p['content_top_first']}pt",
        "--content-top-cont": f"{p['content_top_cont']}pt",
        "--footer-top": f"{p['footer_top']}pt",
        "--footer-h": f"{p['footer_h']}pt",
        "--content-width": f"{cw:.3f}pt",
        "--text-main": pal["text_main"],
        "--text-sub": pal["text_sub"],
        "--muted": pal["muted"],
        "--border": pal["border"],
        "--accent-blue": pal["accent_blue"],
        "--chapter-title-color": pal["chapter_title"],
        "--section-title-color": pal["section_title"],
        "--sub-title": pal["sub_title"],
        "--header-chapter-color": pal["header_chapter"],
        "--header-project-color": pal["header_project"],
        "--footer-text": pal["footer_text"],
        "--row-alt": pal["row_alt"],
        "--section-band": pal["section_band"],
        "--header-bg": pal["header_bg"],
        "--header-text": pal["header_text"],
        "--header-chapter-size": f"{typ['header_chapter']}pt",
        "--header-project-size": f"{typ['header_project']}pt",
        "--chapter-title-size": f"{typ['chapter_title']}pt",
        "--section-title-size": f"{typ['section_title']}pt",
        "--sub-section-size": f"{typ['sub_section']}pt",
        "--level-three-size": f"{typ['level_three']}pt",
        "--table-header-size": f"{typ['table_header']}pt",
        "--table-body-size": f"{typ['table_body']}pt",
        "--body-size": f"{typ['body']}pt",
        "--caption-size": f"{typ['caption']}pt",
        "--page-no-size": f"{typ['page_no']}pt",
        "--paragraph-gap": f"{sp['paragraph_gap']}pt",
        "--block-gap": f"{sp['table_gap']}pt",
        "--cell-pad-x": f"{sp['cell_pad_x']}pt",
        "--cell-pad-y": f"{sp['cell_pad_y']}pt",
        "--line-height-body": str(sp["line_height_body"]),
        "--line-height-table": str(sp["line_height_table"]),
        "--kpi-gap": f"{comp['kpi_gap']}pt",
        "--kpi-pad": f"{comp['kpi_pad']}pt",
        "--kpi-value-size": f"{comp['kpi_value_size']}pt",
        "--figure-max-h": f"{comp['figure_max_h']}pt",
        "--placeholder-h": f"{comp['placeholder_h']}pt",
        "--font-body": fonts["body"],
        "--font-heading": fonts["heading"],
    }
    body = ";".join(f"{k}:{v}" for k, v in vars_.items())
    return f":root{{{body}}}"


def _html_document(title: str, tokens: dict[str, Any], inner: str, script: str = "") -> str:
    css = CSS_PATH.read_text(encoding="utf-8")
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html_mod.escape(title)}</title>
<style>{_css_vars(tokens)}</style>
<style>{css}</style>
</head><body>
{inner}
{script}
</body></html>"""


def build_measure_html(sections: Sequence[tuple[str, list[Block]]], tokens: dict[str, Any]) -> str:
    body = []
    for sid, blocks in sections:
        for block in blocks:
            body.append(_measure_block_html(block))
    script = """
<script>
(async function () {
  try {
    await document.fonts.ready;
    await Promise.all(Array.from(document.images).map(function (img) {
      return img.complete ? Promise.resolve() : new Promise(function (r) { img.onload = r; img.onerror = r; });
    }));
    var out = {};
    document.querySelectorAll('[data-measure-id]').forEach(function (el) {
      out[el.getAttribute('data-measure-id')] = el.getBoundingClientRect().height;
    });
    document.getElementById('__metrics').textContent = JSON.stringify(out);
  } catch (e) {
    document.getElementById('__metrics').textContent = JSON.stringify({__error: String(e)});
  }
})();
</script>
<pre id="__metrics" style="display:none"></pre>"""
    inner = f'<div class="measure-content">{"".join(body)}</div>'
    return _html_document("bidviz-html-measure", tokens, inner, script)


_METRICS_RE = re.compile(r'<pre id="__metrics"[^>]*>(.*?)</pre>', re.S)


def parse_metrics_text(raw: str) -> dict[str, float]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out = {}
    for key, value in data.items():
        try:
            out[str(key)] = _px(float(value))
        except (TypeError, ValueError):
            continue
    return out


def parse_metrics(dom: str) -> dict[str, float]:
    m = _METRICS_RE.search(dom or "")
    if not m:
        return {}
    return parse_metrics_text(html_mod.unescape(m.group(1)))


# ---------------------------------------------------------------- 浏览器


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def find_browser(preferred: str = "auto") -> str:
    candidates: list[str] = []
    if preferred and preferred.lower() != "auto":
        candidates.append(preferred)
    env = os.getenv("BIDVIZ_BROWSER", "").strip()
    if env:
        candidates.append(env)
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.getenv(env_name)
        if not root:
            continue
        candidates += [
            str(Path(root) / "Google/Chrome/Application/chrome.exe"),
            str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
        ]
    seen = set()
    for c in candidates:
        p = Path(c).expanduser()
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return str(p)
    raise RuntimeError(
        "未找到 Chrome/Edge。请在 bidviz.toml [html].browser 指定可执行文件路径，"
        "或设置 BIDVIZ_BROWSER 环境变量。")


def _chrome_flags(browser: str) -> list[str]:
    edge = "msedge" in Path(browser).name.lower()
    flags = ["--headless=new", "--disable-gpu", "--no-sandbox",
             "--allow-file-access-from-files", "--virtual-time-budget=15000",
             "--window-size=1600,6000"]
    if not edge:
        # Chrome 新旧版本的页眉页脚开关不同，两个都传；未知参数会被忽略。
        flags += ["--no-pdf-header-footer", "--print-to-pdf-no-header"]
    else:
        flags += ["--no-pdf-header-footer"]
    return flags


def _run_browser(browser: str, args: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    last: Exception | None = None
    for headless in ("--headless=new", "--headless"):
        cmd = [browser] + [headless if a == "--headless=new" else a for a in args]
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last = exc
    raise RuntimeError(f"浏览器启动失败：{last}")


def _playwright_measure(uri: str) -> dict[str, float]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        try:
            page.goto(uri, wait_until="load", timeout=60000)
            page.wait_for_function(
                "document.getElementById('__metrics') && "
                "document.getElementById('__metrics').textContent.length > 2",
                timeout=30000)
            raw = page.eval_on_selector("#__metrics", "el => el.textContent")
        finally:
            browser.close()
    return parse_metrics_text(raw or "")


def measure_blocks(sections: Sequence[tuple[str, list[Block]]], tokens: dict[str, Any],
                   work_dir: Path, browser: str) -> dict[str, float]:
    work_dir.mkdir(parents=True, exist_ok=True)
    html_path = work_dir / "measure.html"
    html_path.write_text(build_measure_html(sections, tokens), encoding="utf-8")
    if browser == "playwright" or playwright_available():
        try:
            return _playwright_measure(html_path.resolve().as_uri())
        except Exception as exc:
            print(f"[WARN] Playwright 测量失败：{exc}", flush=True)
            if browser == "playwright":
                return {}
    args = _chrome_flags(browser) + ["--dump-dom", html_path.resolve().as_uri()]
    try:
        proc = _run_browser(browser, args, timeout=180)
    except RuntimeError:
        return {}
    return parse_metrics(proc.stdout or "")


# ---------------------------------------------------------------- 估算与分页


def _estimate_block(block: Block, tokens: dict[str, Any]) -> None:
    typ, sp, comp = tokens["typography"], tokens["spacing"], tokens["components"]
    cw = _content_width_pt(tokens)
    if block.kind == "prose":
        text = str(block.data.get("text", ""))
        level = int(block.data.get("level", 1))
        size = {1: typ["body"], 3: typ["level_three"], 4: typ["sub_section"]}.get(level, typ["body"])
        per_line = max(8, int(cw / max(8.0, size)))
        lines = max(1, math.ceil(len(text) / per_line))
        line_h = size * (sp["line_height_body"] if level == 1 else 1.3)
        block.height = lines * line_h + (sp["paragraph_gap"] if level == 1 else 4)
    elif block.kind == "figure":
        fig = block.data.get("figure") or {}
        ratio = _ratio_value(fig.get("ratio", ""))
        block.height = min(comp["figure_max_h"], cw / max(0.5, ratio)) + typ["caption"] + 6
        if not fig.get("path"):
            block.height = comp["placeholder_h"] + typ["caption"] + 6
    elif block.kind == "kpi":
        block.height = comp["kpi_pad"] * 2 + 28 + 22 + 14
    elif block.kind == "cover":
        block.height = 860.0
    elif block.kind == "vector":
        kind = str((block.data.get("vector") or {}).get("kind", ""))
        block.height = 150 if kind == "timeline_bar" else 180
    elif block.kind == "table":
        d = block.data
        headers = d.get("headers") or []
        rows = d.get("rows") or []
        ratios = d.get("col_ratios") or [1.0] * max(1, len(headers))
        widths = [cw * (r / sum(ratios or [1.0])) for r in ratios] if headers else [cw]
        def row_h(cells: Sequence[str], size: float) -> float:
            h = 0.0
            for i, cell in enumerate(cells):
                w = widths[i] if i < len(widths) else cw / max(1, len(cells))
                per = max(3, int(w / max(6.0, size)))
                lines = max(1, math.ceil(len(str(cell)) / per))
                h = max(h, lines * size * sp["line_height_table"])
            return h + 2 * sp["cell_pad_y"] + 2
        block.header_height = row_h(headers, typ["table_header"]) if headers else 0.0
        block.row_heights = [row_h(r, typ["table_body"]) for r in rows]
        block.height = block.header_height + sum(block.row_heights)


def fill_heights(blocks: Sequence[Block], metrics: dict[str, float],
                 tokens: dict[str, Any]) -> None:
    for block in blocks:
        if block.kind == "table":
            tid = block.data["table_id"]
            block.header_height = metrics.get(f"{tid}:h", 0.0)
            block.row_heights = [
                metrics.get(f"{tid}:r{i}", 0.0) for i in range(len(block.data.get("rows") or []))]
            if (not block.header_height and block.data.get("headers")) or any(h <= 0 for h in block.row_heights):
                _estimate_block(block, tokens)
        else:
            block.height = metrics.get(block.block_id, 0.0)
            if block.height <= 0:
                _estimate_block(block, tokens)


def _capacities(tokens: dict[str, Any], safety: float = 0.0) -> tuple[float, float]:
    p = tokens["page"]
    bottom = p["content_bottom"] - max(0.0, safety)
    return bottom - p["content_top_first"], bottom - p["content_top_cont"]


def paginate_section(sid: str, blocks: list[Block], plans: list[tpl.PagePlan],
                     chapter: str, sec_title: str, footer_note: str,
                     tokens: dict[str, Any], safety: float = 0.0) -> list[HtmlPage]:
    cap_first, cap_cont = _capacities(tokens, safety)
    gap = float(tokens["spacing"]["table_gap"])
    pages: list[HtmlPage] = []
    current: list[Any] = []
    used = 0.0
    cap = cap_first
    overflow: list[str] = []

    def new_page() -> None:
        nonlocal current, used, cap
        page = HtmlPage(sid=sid, section_page=len(pages) + 1, chapter=chapter,
                        section=sec_title, footer_note=footer_note, blocks=current)
        pages.append(page)
        current, used, cap = [], 0.0, cap_cont

    # 标题与后段绑定：heading 尽量不和后续内容分页
    for idx, block in enumerate(blocks):
        if block.kind == "table":
            d = block.data
            rows = list(d.get("rows") or [])
            header_h = block.header_height
            row_heights = list(block.row_heights)
            first_chunk = True
            while rows:
                first_rh = row_heights[0] if row_heights else 0.0
                gap_before = gap if current else 0.0
                if current and used + gap_before + header_h + first_rh > cap:
                    new_page()
                    gap_before = 0.0
                chunk_rows: list[list[str]] = []
                chunk_first = first_chunk
                chunk_used = gap_before + header_h
                while rows:
                    rh = row_heights[0] if row_heights else 0.0
                    over = used + chunk_used + rh > cap
                    if over and chunk_rows:
                        break
                    if over and not chunk_rows and current:
                        new_page()
                        chunk_used = header_h
                        continue
                    if over and not chunk_rows and not current:
                        overflow.append(f"{d['table_id']}:row")
                    chunk_rows.append(rows.pop(0))
                    if row_heights:
                        row_heights.pop(0)
                    chunk_used += rh
                current.append(TableChunk(
                    table_id=d["table_id"], caption=d.get("caption", ""),
                    headers=list(d.get("headers") or []), rows=chunk_rows,
                    col_ratios=list(d.get("col_ratios") or []), first=chunk_first))
                used += chunk_used
                first_chunk = False
                if rows:
                    new_page()
            continue

        need = block.height + (gap if current else 0.0)
        if block.kind == "prose" and int(block.data.get("level", 1)) >= 3:
            nxt = blocks[idx + 1].height if idx + 1 < len(blocks) else 0.0
            if current and used + need + nxt + gap > cap:
                new_page()
                need = block.height
        if current and used + need > cap:
            new_page()
            need = block.height
        if not current and block.height > cap:
            overflow.append(block.block_id)
        current.append(block)
        used += need

    if current or not pages:
        page = HtmlPage(sid=sid, section_page=len(pages) + 1, chapter=chapter,
                        section=sec_title, footer_note=footer_note, blocks=current)
        pages.append(page)
    if len(pages) > 3:
        overflow.append(f"{sid}:pages={len(pages)}")
    for p in pages:
        p.overflow = overflow
    return pages


# ---------------------------------------------------------------- 最终 HTML


def _skyline_svg() -> str:
    return ('<svg class="skyline" viewBox="0 0 180 18" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M0 16 L10 16 L10 10 L18 10 L18 16 L26 16 L26 6 L34 6 L34 16 '
            'L44 16 L44 12 L52 12 L52 16 L62 16 L62 3 L70 3 L70 16 L80 16 L80 9 '
            'L88 9 L88 16 L100 16 L100 13 L108 13 L108 16 L118 16 L118 5 L126 5 '
            'L126 16 L138 16 L138 11 L146 11 L146 16 L158 16 L158 8 L166 8 L166 16 '
            'L180 16 L180 18 L0 18 Z" fill="#dbe7f1"/></svg>')


def render_block_final(item: Any) -> str:
    if isinstance(item, TableChunk):
        return _table_html(item.headers, item.rows, item.col_ratios,
                           item.caption if item.first else "")
    return block_inner_html(item)


def page_html(page: HtmlPage, global_no: int, tokens: dict[str, Any],
              draft: bool = False, project_name: str = "") -> str:
    cont = page.section_page > 1
    is_cover = any(isinstance(b, Block) and b.kind == "cover" for b in page.blocks)
    cls = "page cover" if is_cover else ("page continuation" if cont else "page")
    if draft:
        cls += " draft"
    chapter = html_mod.escape("投标文件" if is_cover else (page.chapter or page.section))
    project = html_mod.escape(project_name)
    chapter_block = "" if (cont or is_cover) else (
        f'<div class="chapter-title"><h1>{html_mod.escape(page.chapter or page.section)}</h1></div>')
    section_block = "" if is_cover else (
        f'<div class="section-title"><h2>{html_mod.escape(page.section)}</h2><span class="section-line"></span></div>')
    content = "".join(render_block_final(b) for b in page.blocks)
    return f"""<section class="{cls}" data-sid="{html_mod.escape(page.sid)}" data-section-page="{page.section_page}">
  <header class="page-header">
    <span class="header-chapter">{chapter}</span>
    <span class="header-project">{project}</span>
    <span class="header-marks"><i></i><i></i><i></i></span>
  </header>
  {chapter_block}
  {section_block}
  <main class="content">{content}</main>
  <footer class="page-footer"><div class="page-no">— {global_no} —</div>{_skyline_svg()}</footer>
</section>"""


def render_album_html(pages: Sequence[HtmlPage], tokens: dict[str, Any],
                      draft: bool = False, project_name: str = "") -> str:
    inner = "\n".join(page_html(p, i, tokens, draft=draft, project_name=project_name)
                      for i, p in enumerate(pages, 1))
    return _html_document("施工组织设计-图册-A3-html", tokens, inner)


# ---------------------------------------------------------------- PDF / PNG


def _playwright_pdf(html_path: Path, out_pdf: Path) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1600, "height": 1200})
        try:
            page.goto(Path(html_path).resolve().as_uri(), wait_until="load", timeout=60000)
            page.emulate_media(media="print")
            page.wait_for_function("document.fonts.status === 'loaded'", timeout=30000)
            page.wait_for_function(
                "[...document.images].every(function (i) { return i.complete; })",
                timeout=30000)
            page.wait_for_timeout(300)
            page.pdf(path=str(out_pdf.resolve()), width="297mm", height="420mm",
                     print_background=True,
                     margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                     prefer_css_page_size=True)
        finally:
            browser.close()


def render_pdf(html_path: Path, out_pdf: Path, browser: str) -> Path:
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out_pdf.unlink(missing_ok=True)
    if browser == "playwright" or playwright_available():
        try:
            _playwright_pdf(html_path, out_pdf)
        except Exception as exc:
            print(f"[WARN] Playwright 打印失败：{exc}", flush=True)
            if browser == "playwright":
                raise
    if not out_pdf.is_file() or out_pdf.stat().st_size < 1000:
        args = _chrome_flags(browser) + [
            f"--print-to-pdf={out_pdf.resolve()}",
            Path(html_path).resolve().as_uri(),
        ]
        proc = _run_browser(browser, args, timeout=300)
        if not out_pdf.is_file() or out_pdf.stat().st_size < 1000:
            raise RuntimeError(
                "HTML→PDF 失败：\n"
                f"exit={proc.returncode}\nstdout={proc.stdout[-1000:]}\nstderr={proc.stderr[-2000:]}")
    return out_pdf


def export_pages(pdf_path: Path, pages: Sequence[HtmlPage], out_dir: Path,
                 dpi: int = 200) -> dict[str, Any]:
    import csv as _csv
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf_path)
    entries = []
    try:
        if doc.page_count != len(pages):
            raise RuntimeError(f"PDF 页数 {doc.page_count} 与清单 {len(pages)} 不一致")
        for i, page in enumerate(pages, 1):
            name = f"{i:03d}_{page.sid}_p{page.section_page}.png"
            pix = doc[i - 1].get_pixmap(dpi=dpi, alpha=False)
            dst = out_dir / name
            dst.write_bytes(pix.tobytes("png"))
            entries.append({"pdf_page": i, "sid": page.sid,
                            "section_page": page.section_page, "file": name,
                            "bytes": dst.stat().st_size})
    finally:
        doc.close()

    keep = {e["file"] for e in entries}
    for f in out_dir.iterdir():
        if f.is_file() and re.fullmatch(r"\d{3}_.*\.png", f.name) and f.name not in keep:
            f.unlink()
    (out_dir / "index.json").write_text(json.dumps(
        {"schema": "bidviz-html-page-images/1", "pdf": str(pdf_path), "pages": entries},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (out_dir / "index.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.writer(fh)
        w.writerow(["pdf_page", "sid", "section_page", "file", "bytes"])
        for e in entries:
            w.writerow([e["pdf_page"], e["sid"], e["section_page"], e["file"], e["bytes"]])
    return {"dir": out_dir, "pages": len(entries), "manifest": out_dir / "index.json"}


# ---------------------------------------------------------------- 审计


def export_pdf_pages(root: Path, pdf_path: Path, out_dir: Path | None = None,
                     dpi: int = 200) -> dict[str, Any]:
    """按 work/html-pages.json 给已生成的 HTML PDF 导出真页码 PNG。"""
    root = Path(root)
    mp = root / HTML_PAGES
    if not mp.exists():
        raise FileNotFoundError(mp)
    data = json.loads(mp.read_text(encoding="utf-8"))
    pages = [HtmlPage(sid=str(p.get("sid", "")),
                      section_page=int(p.get("section_page", 1)),
                      chapter=str(p.get("chapter", "")),
                      section=str(p.get("section", "")),
                      footer_note="", blocks=[])
             for p in data.get("pages", [])]
    out = Path(out_dir) if out_dir else Path(pdf_path).parent / "page-images" / Path(pdf_path).stem
    return export_pages(Path(pdf_path), pages, out, dpi=dpi)


def audit_html_pdf(pdf_path: Path, pages: Sequence[HtmlPage],
                   expected: Sequence[str]) -> dict[str, Any]:
    doc = fitz.open(pdf_path)
    result: dict[str, Any] = {"pdf": str(pdf_path), "ok": True, "issues": []}
    try:
        result["pages"] = doc.page_count
        if doc.page_count != len(pages):
            result["ok"] = False
            result["issues"].append(f"页数 {doc.page_count} != 清单 {len(pages)}")
        text_parts = []
        for i in range(doc.page_count):
            page = doc[i]
            rect = page.rect
            if abs(rect.width - 841.89) > 2 or abs(rect.height - 1190.55) > 2:
                result["ok"] = False
                result["issues"].append(f"第 {i + 1} 页尺寸 {rect.width:.1f}x{rect.height:.1f} 非 A3")
            text_parts.append(page.get_text())
            if not page.get_text().strip() and i < len(pages) and pages[i].blocks:
                result["issues"].append(f"第 {i + 1} 页无可提取文字")
        flat = _normalize("".join(text_parts))
        missing = []
        checked = 0
        for snippet in expected:
            needle = _normalize(snippet)
            if len(needle) < 2:
                continue
            checked += 1
            if needle not in flat:
                missing.append(snippet[:60])
        result["coverage_checked"] = checked
        result["missing"] = missing[:20]
        result["coverage"] = round((checked - len(missing)) / checked, 4) if checked else 1.0
        if missing:
            result["ok"] = False
            result["issues"].append(f"文本缺失 {len(missing)} 处")
        result["overflow"] = [b for p in pages for b in p.overflow]
        if result["overflow"]:
            result["ok"] = False
            result["issues"].append(f"溢出块 {len(result['overflow'])} 个")
    finally:
        doc.close()
    return result


# ---------------------------------------------------------------- 局部插图


_CALLOUT_RE = re.compile(r"标注|引线|编号|文字|说明|图签|图例|箭头|图注")
NO_TEXT_ADDENDUM = (
    "\n\n【复核约束】上一次生成出现了引线/文字标注。本次绝对不要任何引线、"
    "编号圆点、标注圈、文字标签、图例、箭头文字；只画无标注的纯工程场景。")


def _clean_illustration_text(text: Any) -> str:
    """去掉 figure-plan 里“引线标注/编号/文字说明”等会诱导模型画标签的句子。"""
    parts = [p.strip() for p in re.split(r"[。；;\n]", str(text or "")) if p.strip()]
    kept = [p for p in parts if not _CALLOUT_RE.search(p)]
    return "；".join(kept)


def _illustration_context(root: Path, sid: str, fig: dict[str, Any], pp: dict[str, Any]) -> str:
    entry = (pp.get("sections") or {}).get(sid, {}) or {}
    chunks = []
    for page in entry.get("pages", [])[:1]:
        for prose in page.get("prose", [])[:2]:
            chunks.append(str(prose.get("text", "")))
        for table in page.get("tables", [])[:1]:
            chunks.append(str(table.get("caption", "")))
            chunks.append("；".join(str(c) for c in (table.get("headers") or [])))
    text = "\n".join(x for x in chunks if x)
    return text[:900] or str(fig.get("why", ""))


def illustration_prompt(root: Path, sid: str, fig: dict[str, Any],
                        pp: dict[str, Any]) -> str:
    template = ILLUSTRATION_PROMPT.read_text(encoding="utf-8")
    repl = {
        "{{name}}": str(fig.get("name", "")),
        "{{kind}}": str(fig.get("kind", "")),
        "{{style}}": _clean_illustration_text(fig.get("style", "")),
        "{{why}}": _clean_illustration_text(fig.get("why", "")),
        "{{ratio}}": str(fig.get("ratio", "")),
        "{{context}}": _illustration_context(root, sid, fig, pp),
    }
    for key, value in repl.items():
        template = template.replace(key, value)
    return template


def _illustration_size(fig: dict[str, Any]) -> str:
    size = str((fig.get("gen") or {}).get("size", "") or "").strip()
    if size:
        return size
    ratio = _ratio_value(fig.get("ratio", ""))
    if ratio >= 1.2:
        return "1536x1024"
    if ratio <= 0.83:
        return "1024x1536"
    return "1024x1024"


_OCR_ENGINE = None
_OCR_LOCK = __import__("threading").Lock()


def _rapidocr_boxes(path: Path) -> list[Any]:
    """RapidOCR 只用来统计图内文字/标注框；置信度不可靠，按框数告警。"""
    global _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
        with _OCR_LOCK:
            if _OCR_ENGINE is None:
                _OCR_ENGINE = RapidOCR()
            result, _ = _OCR_ENGINE(str(path))
        return list(result or [])
    except Exception:
        return []


def _ledger_path(root: Path) -> Path:
    return Path(root) / "work" / ILLUSTRATION_LEDGER


def _load_ledger(root: Path) -> dict[str, Any]:
    p = _ledger_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(root: Path, led: dict[str, Any]) -> None:
    p = _ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_illustrations(root: Path, ids: Sequence[str], force: bool = False,
                           limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    root = Path(root)
    pp = load_page_plan(root)
    fp = load_figure_plan(root)
    out_dir = root / ILLUSTRATION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    led = _load_ledger(root)
    todo = []
    for sid in ids:
        for fig in (fp.get("sections") or {}).get(sid, {}).get("figures", []):
            todo.append((sid, fig))
    if limit:
        todo = todo[:limit]

    ok = fail = skip = 0
    for n, (sid, fig) in enumerate(todo, 1):
        no = int(fig.get("no", 1))
        stem = Path(plan_mod.figure_filename(sid, no)).stem
        out = out_dir / f"{stem}.png"
        tag = f"[{n}/{len(todo)}] {sid} 图{no} {fig.get('kind', '')}"
        if out.exists() and out.stat().st_size > 2000 and not force:
            skip += 1
            print(f"{tag} 已存在，跳过", flush=True)
            continue
        prompt = illustration_prompt(root, sid, fig, pp)
        g = plan_mod.gen_config(no, str(fig.get("kind", "")))
        size = _illustration_size(fig)
        rec = {"sid": sid, "no": no, "kind": fig.get("kind", ""),
               "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
               "size": size, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        if dry_run:
            print(f"{tag} dry-run provider={g['provider']} model={g['model']} size={size} "
                  f"prompt={len(prompt)}字", flush=True)
            continue
        api = plan_mod.make_api({**fig, "gen": {**g, "size": size}})
        attempts = 3
        sizes = [size] + [s for s in ("1536x1024", "1024x1536", "1024x1024") if s != size]
        last = ""
        done = False
        t0 = time.time()
        for attempt in range(1, attempts + 1):
            current_size = sizes[min(attempt - 1, len(sizes) - 1)]
            try:
                rec["size"] = current_size
                attempt_rec: dict[str, Any] = {}
                api.generate(prompt, out, size=current_size, recorder=attempt_rec)
                rec.update(attempt_rec)
                done = True
                break
            except Exception as exc:  # 生图接口会间歇断连/尺寸不支持，指数退避
                last = f"{type(exc).__name__}: {exc}"[:200]
                if attempt < attempts:
                    time.sleep(2 ** (attempt - 1))
        rec.update({"ok": done, "attempts": attempt, "sec": round(time.time() - t0, 1),
                    "kb": round(out.stat().st_size / 1024) if done and out.exists() else 0})
        if done:
            boxes = _rapidocr_boxes(out)
            rec["text_boxes"] = len(boxes)
            if boxes:
                print(f"{tag} 检测到 {len(boxes)} 处图内文字/标注，按无文字约束重试一次",
                      flush=True)
                retry_prompt = prompt + NO_TEXT_ADDENDUM
                try:
                    retry_rec: dict[str, Any] = {}
                    api.generate(retry_prompt, out, size=rec.get("size", size), recorder=retry_rec)
                    rec.update(retry_rec)
                    boxes2 = _rapidocr_boxes(out)
                    rec["text_boxes"] = len(boxes2)
                    rec["text_retry"] = True
                    if not boxes2:
                        prompt = retry_prompt
                except Exception as exc:
                    rec["text_retry_error"] = f"{type(exc).__name__}: {exc}"[:160]
            (out_dir / f"_prompt_{stem}.txt").write_text(prompt, encoding="utf-8")
            ok += 1
            rec.pop("error", None)
        else:
            rec["error"] = last
            fail += 1
        led[f"{sid}#{no}"] = rec
        _save_ledger(root, led)
        print(f"{tag} {'OK' if done else 'FAIL'} {rec.get('kb', '')}KB "
              f"{rec['sec']}s {last}", flush=True)
    text_warn = sum(1 for rec in led.values() if rec.get("text_boxes"))
    return {"planned": len(todo), "generated": ok, "skipped": skip, "failed": fail,
            "text_warn": text_warn, "out_dir": str(out_dir), "dry_run": dry_run}


# ---------------------------------------------------------------- build


def _expected_from_blocks(meta: dict[str, dict[str, Any]]) -> list[str]:
    """审计口径 = 实际进入 HTML 的块（而不是 page-plan 的截断副本）。"""
    out: list[str] = []
    for m in meta.values():
        for block in m["blocks"]:
            if block.kind == "cover":
                for key in ("project", "voltage", "doc_type"):
                    if block.data.get(key):
                        out.append(str(block.data[key]))
            elif block.kind == "prose":
                text = str(block.data.get("text", ""))
                if text:
                    out.append(text)
            elif block.kind == "kpi":
                for k in block.data.get("kpis") or []:
                    out.append(str(k.get("label", "")))
                    out.append(str(k.get("value", "")))
            elif block.kind == "table":
                d = block.data
                out.extend(str(x) for x in (d.get("headers") or []))
                for row in d.get("rows") or []:
                    out.extend(str(x) for x in row)
    return out


def _expected_texts(root: Path, sids: Sequence[str], pp: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for sid in sids:
        entry = (pp.get("sections") or {}).get(sid, {}) or {}
        for page in entry.get("pages", []):
            for k in page.get("kpis", []):
                out.append(str(k.get("label", "")))
                out.append(str(k.get("value", "")))
            for p in page.get("prose", []):
                if p.get("text"):
                    out.append(str(p["text"]))
            for table in page.get("tables", []):
                out.extend(str(x) for x in (table.get("headers") or []))
                for row in table.get("rows") or []:
                    out.extend(str(x) for x in row)
    return out


def build(root: Path, sids: Sequence[str] | None = None, out_pdf: Path | None = None,
          draft: bool = False, emit_png: bool | None = None,
          browser: str | None = None) -> dict[str, Any]:
    from bidviz_config import load as load_cfg
    root = Path(root)
    cfg = load_cfg(root)
    tokens = load_tokens()
    pp = load_page_plan(root)
    fp = load_figure_plan(root)
    all_sids = [sid for sid in sorted((pp.get("sections") or {}).keys())]
    chosen = list(sids) if sids else all_sids
    chosen = [sid for sid in chosen if sid in (pp.get("sections") or {})]
    if not chosen:
        raise RuntimeError("没有可编译的节")

    sections: list[tuple[str, list[Block]]] = []
    meta: dict[str, dict[str, Any]] = {}
    items = load_section_items(root)
    for sid in chosen:
        entry = pp["sections"][sid]
        figs = section_figures(root, sid, fp)
        tables = html_tables(root, sid, items.get(sid), cfg.html_table_cell_max)
        blocks, plans = build_section_blocks(
            root, sid, entry, figs, tables=tables,
            cover_meta={"project": cfg.name, "voltage": cfg.voltage,
                        "doc_type": cfg.doc_type})
        chapter, sec_title = section_title(entry)
        footer = plans[0].footer_note if plans else ""
        meta[sid] = {"chapter": chapter, "section": sec_title, "footer": footer,
                     "blocks": blocks, "plans": plans,
                     "missing_figures": [f.get("caption", "") for f in figs if not f.get("path")]}
        sections.append((sid, blocks))

    if (browser or "").lower() == "playwright" or playwright_available():
        browser_path = "playwright"
    else:
        browser_path = find_browser(browser or cfg.html_browser)
    work_dir = root / STORE_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    metrics = measure_blocks(sections, tokens, work_dir, browser_path)
    if not metrics:
        print("[WARN] 浏览器测量失败，改用 Python 估算分页", flush=True)
    for _sid, blocks in sections:
        fill_heights(blocks, metrics, tokens)

    out_pdf = Path(out_pdf) if out_pdf else cfg.output / (
        f"{OUT_SUFFIX}-DRAFT.pdf" if draft else f"{OUT_SUFFIX}.pdf")
    html_path = work_dir / ("album-DRAFT.html" if draft else "album.html")
    expected = _expected_from_blocks(meta)

    pages: list[HtmlPage] = []
    audit: dict[str, Any] = {"ok": False, "issues": ["未渲染"], "missing": []}
    safety = 24.0
    for attempt in range(3):
        pages = []
        for sid, _blocks in sections:
            m = meta[sid]
            pages.extend(paginate_section(
                sid, m["blocks"], m["plans"], m["chapter"], m["section"],
                m["footer"], tokens, safety=safety))
        html_path.write_text(render_album_html(pages, tokens, draft=draft,
                                               project_name=cfg.name), encoding="utf-8")
        render_pdf(html_path, out_pdf, browser_path)
        audit = audit_html_pdf(out_pdf, pages, expected)
        if audit["ok"] or attempt == 2:
            break
        safety += 24.0
        print(f"[WARN] 检测到裁切/缺失，安全边距 +24pt 重排（第 {attempt + 2} 次）",
              flush=True)
    if not audit["ok"]:
        print(f"[WARN] HTML 审计：{audit['issues']}；缺失示例 {audit.get('missing', [])[:8]}",
              flush=True)

    manifest_path = root / HTML_PAGES
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": "bidviz-html-pages/1",
        "pdf": str(out_pdf),
        "html": str(html_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pages": [
            {"pdf_page": i, "sid": p.sid, "section_page": p.section_page,
             "chapter": p.chapter, "section": p.section,
             "blocks": [b.block_id if isinstance(b, Block) else
                        {"table": b.table_id, "rows": len(b.rows), "first": b.first}
                        for b in p.blocks]}
            for i, p in enumerate(pages, 1)
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    png_info = None
    do_png = cfg.html_emit_png if emit_png is None else emit_png
    if do_png:
        png_dir = cfg.output / "page-images" / Path(out_pdf).stem
        png_info = export_pages(out_pdf, pages, png_dir, dpi=cfg.html_dpi)

    missing = [name for m in meta.values() for name in m.get("missing_figures", [])]
    return {"pdf": out_pdf, "html": html_path, "pages": len(pages),
            "manifest": manifest_path, "audit": audit,
            "png": png_info, "browser": browser_path,
            "missing_figures": missing,
            "overflow": [x for p in pages for x in p.overflow]}


# ---------------------------------------------------------------- CLI


def _chapter_sids(root: Path, keyword: str) -> list[str]:
    pp = load_page_plan(root)
    out = []
    for sid in sorted((pp.get("sections") or {}).keys()):
        chapter, _section = section_title(pp["sections"][sid])
        if keyword in chapter:
            out.append(sid)
    return out


def main(argv: list[str] | None = None) -> int:
    from bidviz_config import load as load_cfg
    ap = argparse.ArgumentParser(description="HTML 混合引擎（局部 AI 图 + HTML/CSS 排版）")
    ap.add_argument("root", nargs="?", default="projects/anke-35kv")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--section", default=None, help="如 S002 或 S002,S004")
    ap.add_argument("--chapter", default=None)
    ap.add_argument("--generate-illustrations", action="store_true")
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--png", action="store_true")
    ap.add_argument("--draft", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--browser", default=None)
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_absolute():
        root = (BASE / root).resolve()
    load_cfg(root)
    if not args.generate_illustrations and not args.pdf:
        ap.error("需要 --generate-illustrations 和/或 --pdf")
    if args.section:
        ids = [s.strip().upper() for s in args.section.split(",") if s.strip()]
    elif args.chapter:
        ids = _chapter_sids(root, args.chapter)
    else:
        pp = load_page_plan(root)
        ids = sorted((pp.get("sections") or {}).keys())

    if args.generate_illustrations or not args.pdf:
        summary = generate_illustrations(root, ids, force=args.force,
                                         limit=args.limit, dry_run=args.dry_run)
        print(f"[OK] 局部插图：{summary}", flush=True)
        if not args.pdf:
            return 0
    if args.pdf:
        result = build(root, ids, Path(args.out) if args.out else None,
                       draft=args.draft, emit_png=args.png, browser=args.browser)
        print(f"[OK] HTML 图册：{result['pdf']}（{result['pages']} 页）", flush=True)
        print(f"     HTML: {result['html']}", flush=True)
        print(f"     清单: {result['manifest']}", flush=True)
        audit = result["audit"]
        print(f"     审计: {'PASS' if audit['ok'] else 'WARN'} "
              f"文本覆盖 {audit.get('coverage', 0):.2%} 溢出 {len(result['overflow'])}", flush=True)
        if result["png"]:
            print(f"     页图: {result['png']['dir']}", flush=True)
        if result.get("missing_figures"):
            print(f"     缺失插图: {len(result['missing_figures'])} 张", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
