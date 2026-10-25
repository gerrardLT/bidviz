#!/usr/bin/env python3
"""bidviz_templates —— V1.3 新体系组件库与页装配器。

设计原则（与 V1.3 规划一致）：
- **组件只引用 token 名**：颜色/字号/间距全从 tokens.toml 取，不硬编码。
- **纯代码 + 真实文字**：AI 图外所有元素矢量；页面级文字不进 AI 图。
- **A3 竖版**（297×420mm = 841.89×1190.55pt）：由 `bv.set_page('A3','portrait')` 切换。
- **确定性**：相同 page_plan + tokens → 相同像素输出（视觉回归基线可比对）。

阶段 2 提供 T-B1 版式（S003 样板）所需组件：
    title_block / kpi_strip / figure_with_callouts / prose_column /
    icon_table / footer_skyline / compose_page

阶段 3+ 扩展：process_strip / chevron_cards / timeline_bar / org_chart / hero_banner。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

import bidviz_vector as bv
from bidviz_vector import Sheet, text_width, wrap

# ---------------------------------------------------------------- tokens 加载

_TOKENS_DEFAULT = Path(__file__).resolve().parent.parent / "docs" / "layout-md" / "tokens.toml"


def load_tokens(path: Path | None = None) -> dict[str, Any]:
    """加载 tokens.toml；缺省用 docs/layout-md/tokens.toml。"""
    p = Path(path) if path else _TOKENS_DEFAULT
    with p.open("rb") as fh:
        return tomllib.load(fh)


def _tk(tokens: dict, section: str, key: str, default: Any = None) -> Any:
    """安全读取 tokens[section][key]。"""
    return tokens.get(section, {}).get(key, default)


# ---------------------------------------------------------------- page_plan 数据结构


@dataclass
class KPI:
    label: str          # 指标名（如"总工期"）
    value: str          # 数值（如"60"）
    unit: str = ""      # 单位（如"日历天"）
    note: str = ""      # 附注（如"含型号 ZC-YJV22"）
    icon: str = ""      # 可选图标字符（如"◷"）；空则不画


@dataclass
class Callout:
    no: str             # 编号（如"1"）
    text: str           # 标注文字（如"电缆沟开挖"）
    anchor: tuple[float, float] = (0.5, 0.5)   # 相对锚点 (0~1, 0~1)


@dataclass
class FigureSlot:
    image: Path | None = None       # AI 图路径；None 时画占位框
    callouts: list[Callout] = field(default_factory=list)
    caption: str = ""               # 图注
    text_spec: list[str] = field(default_factory=list)   # D10 图内标注文字（用于隐藏层）
    fit: str = "contain"            # contain（顶对齐留白）| cover（裁切铺满，去旧标注）
    crop_top: float = 0.0           # cover 前裁掉原图顶部比例（去旧素材标题白带）
    cover_anchor: tuple[float, float] = (0.5, 0.5)   # cover 裁切窗口中心比例


@dataclass
class ProseBlock:
    text: str
    kind: str = "body"              # body / heading / quote


@dataclass
class PagePlan:
    """页规划：标题区 → KPI → 内容区（图/矢量图型/正文/表）→ 页脚。

    T-B1（阶段 2）用 icon_table_*；V1.3 阶段 3 模板用 tables + vector。
    """
    chapter: str = ""
    section: str = ""
    page_no: int = 1
    total_pages: int = 1
    template_id: str = "T-B1"
    main_class: str = ""
    kpis: list[KPI] = field(default_factory=list)
    figure: FigureSlot | None = None
    prose: list[ProseBlock] = field(default_factory=list)
    icon_table_headers: list[str] = field(default_factory=list)
    icon_table_rows: list[list[str]] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)     # [{caption,headers,rows,col_ratios}]
    vector: dict | None = None                           # {kind, ...}（timeline/org/chevron）
    footer_note: str = ""


_DISPLAY_TRACE_RE = re.compile(r"[（(]\s*逐字取自原文\s*[）)]|逐字取自原文")
_SEQ_PREFIX_RE = re.compile(r"^\s*(?:\d{1,3}|[①-⑩]|[一二三四五六七八九十]+)\s*[、.．)）]?\s*")


def _clean_display(value: Any) -> str:
    text = str(value or "")
    text = _DISPLAY_TRACE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_table_dict(table: dict) -> dict:
    """成品表格只保留内容：去掉编辑说明，序列列重排 1..N。"""
    out = dict(table)
    out["caption"] = _clean_display(out.get("caption", ""))
    headers = [_clean_display(x) for x in (out.get("headers") or [])]
    out["headers"] = headers
    seq = bool(headers) and any(k in headers[0] for k in
                                ("序号", "阶段", "工序", "步骤", "环节"))
    rows = []
    for i, row in enumerate(out.get("rows") or [], 1):
        cells = [_clean_display(x) for x in row]
        if seq and cells:
            label = _SEQ_PREFIX_RE.sub("", cells[0]).strip()
            cells[0] = f"{i}. {label}" if label else str(i)
        if not seq and cells and len(cells[0]) < 2:
            continue
        rows.append(cells)
    out["rows"] = rows
    return out


def page_plan_from_dict(raw: dict, root: Path, page_no: int = 1,
                        total_pages: int = 1) -> PagePlan:
    """page-plan.json 的页对象 → PagePlan；v13 与 html 引擎共用同一口径。"""
    kpis = [KPI(**k) for k in raw.get("kpis", [])]
    fig_raw = raw.get("figure")
    figure = None
    if fig_raw:
        img = fig_raw.get("image")
        img_path = (Path(root) / img) if img else None
        callouts = [Callout(no=str(c["no"]), text=c.get("text", ""),
                            anchor=tuple(c.get("anchor", (0.5, 0.5))))
                    for c in fig_raw.get("callouts", [])]
        figure = FigureSlot(image=img_path, callouts=callouts,
                            caption=_clean_display(fig_raw.get("caption", "")),
                            text_spec=fig_raw.get("text_spec", []),
                            fit=fig_raw.get("fit", "contain"),
                            crop_top=float(fig_raw.get("crop_top", 0.0)),
                            cover_anchor=tuple(fig_raw.get("cover_anchor", (0.5, 0.5))))
    prose = [ProseBlock(text=_clean_display(p["text"]), kind=p.get("kind", "body"))
             for p in raw.get("prose", [])]
    it = raw.get("icon_table", {})
    return PagePlan(
        chapter=raw.get("title", {}).get("chapter", ""),
        section=raw.get("title", {}).get("section", ""),
        page_no=page_no,
        total_pages=total_pages,
        template_id=raw.get("template_id", "T-B1"),
        kpis=kpis,
        figure=figure,
        prose=prose,
        icon_table_headers=[_clean_display(x) for x in it.get("headers", [])],
        icon_table_rows=[[_clean_display(x) for x in row] for row in it.get("rows", [])],
        tables=[_clean_table_dict(t) for t in raw.get("tables", [])],
        vector=raw.get("vector"),
        footer_note=raw.get("footer_note", ""),
    )


# ---------------------------------------------------------------- 组件实现


def title_block(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                chapter: str, section: str, page_no: int, total_pages: int) -> float:
    """顶部标题区：章/节面包屑 + 页码；返回占用高度。

    视觉：深蓝底（palette.header_bg）通栏 + 白字；右侧页码。
    """
    h = 34.0
    bg = _tk(tk, "palette", "header_bg", "#1f4e79")
    fg = _tk(tk, "palette", "header_text", "#ffffff")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    size_h1 = float(_tk(tk, "typography", "title_h1", 15.0))
    size_h2 = float(_tk(tk, "typography", "title_h2", 13.0))
    size_pn = float(_tk(tk, "typography", "page_no", 7.5))

    sheet.rect(x, y, w, h, fill=bg)
    # 章 · 节 面包屑（左）
    crumb = f"{chapter}  ›  {section}" if chapter and section else (chapter or section)
    sheet.text(x + 14, y + 6, crumb, size_h2, True, fg, max_w=w - 90)
    # 页码（右）
    pn = f"{page_no} / {total_pages}"
    sheet.text(x + w - 14, y + 6, pn, size_pn, False, fg, align="right")
    # 下划线（细）
    sheet.line(x, y + h, x + w, y + h, muted, 0.5)
    return h


def kpi_strip(sheet: Sheet, tk: dict, x: float, y: float, w: float,
              kpis: Sequence[KPI]) -> float:
    """KPI 三区（或 1–2 区，D12 按类浮动）；返回占用高度。

    v1.1 样图样式：圆角白卡 + 细蓝边 + 左圆环图标（CJK 单字）+ 超大数字。
    """
    if not kpis:
        return 0.0
    gap = float(_tk(tk, "spacing", "gap_kpi_card", 16.0))
    kpi_h = float(_tk(tk, "components", "kpi_h", 50.0))
    icon_r = float(_tk(tk, "components", "kpi_icon_r", 14.0))
    val_size = float(_tk(tk, "components", "kpi_value_size", 17.0))
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size_small = float(_tk(tk, "typography", "small", 8.4))

    n = len(kpis)
    card_w = (w - gap * (n - 1)) / n
    for i, k in enumerate(kpis):
        cx = x + i * (card_w + gap)
        # 圆角白卡 + 细蓝边（样图样式）
        sheet.rect(cx, y, card_w, kpi_h, fill="white", stroke=accent, lw=0.9, radius=6.0)
        # 左圆环图标 + CJK 单字（字体不含线条图标，用单字近似——踩坑 #8）
        icon_cx = cx + icon_r + 10
        icon_cy = y + kpi_h / 2
        if k.icon:
            sheet.rect(icon_cx - icon_r, icon_cy - icon_r, icon_r * 2, icon_r * 2,
                       fill=None, stroke=accent, lw=1.3, radius=icon_r)
            sheet.text(icon_cx, icon_cy - 7.5, k.icon, 11.5, True, accent, align="center")
        text_x = icon_cx + icon_r + 10
        inner_w = card_w - (text_x - cx) - 10
        # 大数值 + 单位
        val_str = f"{k.value}{k.unit}" if k.unit else k.value
        sheet.text(text_x, y + 7, val_str, val_size, True, text_main, max_w=inner_w)
        # 标签 / 附注
        sheet.text(text_x, y + 30, k.label, size_small, False, muted, max_w=inner_w)
        if k.note:
            sheet.text(text_x, y + 40, k.note, size_small - 0.7, False, muted, max_w=inner_w)
    return kpi_h


def figure_with_callouts(sheet: Sheet, tk: dict, x: float, y: float, w: float, h: float,
                         slot: FigureSlot) -> None:
    """左 55% 图位：AI 图 + 矢量编号叠标 + 图注 + 隐藏检索层。

    - AI 图：contain 适配，白底占位（无图时画灰框 + "待生成" 字样）。
    - 编号叠标：callout.anchor 是相对坐标 (0~1)，映射到图片实际显示区域。
    - 图注：底部居中，caption 字号。
    - 隐藏层：text_spec 逐条 hidden_text 写入（D10 可搜索可审计）。
    """
    border = _tk(tk, "palette", "border", "#c1d0d9")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    size_cap = float(_tk(tk, "typography", "caption", 9.5))
    callout_w = float(_tk(tk, "components", "callout_w", 24.0))
    callout_h = float(_tk(tk, "components", "callout_h", 14.0))

    # v1.1：图签移入图内左上（白标签），不再预留图区底部图注行
    caption_h = 0.0
    img_h = h - caption_h
    img_w = w

    if slot.image and Path(slot.image).exists():
        sheet.image(Path(slot.image), x, y, img_w, img_h, dpi=200, fit=slot.fit,
                    crop_top=slot.crop_top, anchor=slot.cover_anchor)
        # 计算实际显示区域（锚点基准）：cover=铺满整区；contain=顶对齐按宽适配
        try:
            with Image.open(slot.image) as im:
                iw, ih = im.size
            if slot.crop_top > 0:
                ih = int(ih * (1 - min(0.9, slot.crop_top)))
            if slot.fit == "cover":
                ox, oy, dw, dh = x, y, img_w, img_h
            else:
                scale = min(img_w / iw, img_h / ih)
                dw, dh = iw * scale, ih * scale
                ox = x + (img_w - dw) / 2
                oy = y   # 顶对齐（不垂直居中，避免图悬浮页中）
        except Exception:
            ox, oy, dw, dh = x, y, img_w, img_h
        # 图签（v1.1 样图样式）：图内左上白色圆角小标签
        if slot.caption:
            cap_size = 8.2
            cap_w = text_width(slot.caption, cap_size) + 16
            sheet.rect(ox + 8, oy + 8, min(cap_w, dw - 16), cap_size * 1.9,
                       fill="white", stroke=accent, lw=0.6, radius=2.5)
            sheet.text(ox + 16, oy + 8 + 3, slot.caption, cap_size, True, accent,
                       max_w=min(cap_w, dw - 16) - 12)
    else:
        # 占位框
        sheet.rect(x, y, img_w, img_h, fill="#f4f7f9", stroke=border, lw=0.8)
        sheet.text(x + img_w / 2, y + img_h / 2 - 6, "AI 图待生成", 12, True, muted,
                   align="center")
        sheet.text(x + img_w / 2, y + img_h / 2 + 10,
                   f"（{img_w:.0f}×{img_h:.0f}pt 预留）", 8, False, muted, align="center")
        ox, oy, dw, dh = x, y, img_w, img_h

    # 编号叠标（v1.1 样图样式）：深蓝圆片白字编号 + 深蓝圆角标签白字 + 锚点圆点引线
    for c in slot.callouts:
        ax = ox + dw * c.anchor[0]
        ay = oy + dh * c.anchor[1]
        r = callout_w / 2
        # 锚点小圆点 + 短引线（圆片在锚点右上方，标签随其右）
        sheet.rect(ax - 1.3, ay - 1.3, 2.6, 2.6, fill=accent)
        sheet.line(ax, ay, ax + 6, ay - 6, accent, 0.7)
        # 深蓝圆片（白字编号）
        ccx, ccy = ax + 6 + r, ay - 6 - r
        sheet.rect(ccx - r, ccy - r, callout_w, callout_w,
                   fill=accent, stroke=None, radius=r)
        sheet.text(ccx, ccy - 4, c.no, 8.5, True, "white", align="center")
        # 深蓝圆角标签（白字）
        if c.text:
            label_size = 7.8
            tw = text_width(c.text, label_size) + 12
            sheet.rect(ccx + r + 3, ccy - callout_h / 2, tw, callout_h,
                       fill=accent, stroke=None, radius=2.5)
            sheet.text(ccx + r + 9, ccy - callout_h / 2 + 3.5, c.text,
                       label_size, False, "white", max_w=tw - 6)

    # 隐藏检索层（D10）：text_spec 逐条写入，使纯视觉页可 Ctrl+F
    # （v1.1：排在图区顶部图签下方，远离表区；render mode 3 不可见）
    if slot.text_spec:
        hy = y + 34
        for ts in slot.text_spec:
            sheet.hidden_text(x + 4, hy, ts, size_cap)
            hy += size_cap * 1.4


def prose_column(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                 blocks: Sequence[ProseBlock], max_h: float,
                 para_gap: float | None = None) -> float:
    """右 45% 正文流：按 D3 保留 1–2 段关键原文；返回占用高度。

    - heading：深蓝加粗，段前留白。
    - body：正文色，行高 leading_body，段间距 para_gap（v1.1 样图段距）。
    - quote：左竖线 + 灰底（预留，阶段 3 用）。
    """
    if not blocks:
        return 0.0
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size_normal = float(_tk(tk, "typography", "normal", 9.5))
    size_h2 = float(_tk(tk, "typography", "title_h2", 13.0))
    leading_body = float(_tk(tk, "line_spacing", "leading_body", 13.8))
    gap = para_gap if para_gap is not None else 4.0

    cy = y
    used = 0.0
    for b in blocks:
        if used >= max_h:
            break
        if b.kind == "heading":
            # 段前留白
            cy += 6
            used += 6
            sheet.text(x, cy, b.text, size_h2, True, accent, max_w=w)
            cy += size_h2 * 1.4
            used += size_h2 * 1.4
        elif b.kind == "quote":
            # 左竖线 + 灰底
            qh = sheet.para_height(b.text, w - 16, size_normal, leading_body) + 8
            if used + qh > max_h:
                break
            sheet.rect(x, cy, w, qh, fill="#f4f7f9", stroke=None)
            sheet.rect(x, cy, 2.5, qh, fill=accent)
            sheet.para(x + 10, cy + 4, b.text, w - 16, size_normal, leading_body,
                       text_main)
            cy += qh
            used += qh
        else:  # body
            ph = sheet.para_height(b.text, w, size_normal, leading_body)
            if used + ph > max_h:
                # 截断到剩余空间
                remain_lines = int((max_h - used) / leading_body)
                if remain_lines <= 0:
                    break
                sheet.para(x, cy, b.text, w, size_normal, leading_body, text_main,
                           max_lines=remain_lines)
                used = max_h
                cy += remain_lines * leading_body
                break
            # 首行缩进 2 字符（对齐样图 1 正文观感）
            indent = size_normal * 2
            lines = wrap(b.text, size_normal, w)   # wrap(text, size, max_w)
            ty = cy
            for li, ln in enumerate(lines):
                if li == 0:
                    sheet.text(x + indent, ty, ln, size_normal, False, text_main)
                else:
                    sheet.text(x, ty, ln, size_normal, False, text_main)
                ty += leading_body
            cy += ph + gap
            used += ph + gap
    return used


def icon_table(sheet: Sheet, tk: dict, x: float, y: float, w: float,
               headers: Sequence[str], rows: Sequence[Sequence[str]],
               max_h: float | None = None) -> float:
    """底部通栏图标表：图标列（固定宽）+ 数据列（等分）；返回占用高度。

    列结构（对齐样图 1）：[图标列 icon_cell_w] + headers 对应的数据列。
    rows 每行 = [icon_char, *data_cells]；表头图标列留空。
    视觉：深蓝表头 + 白/浅灰交替行 + 浅灰分隔线。
    """
    if not headers or not rows:
        return 0.0
    header_bg = _tk(tk, "palette", "header_bg", "#1f4e79")
    header_fg = _tk(tk, "palette", "header_text", "#ffffff")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    row_alt = _tk(tk, "palette", "row_alt_light", "#f8fbfd")
    size_th = float(_tk(tk, "typography", "table_header", 9.5))
    size_small = float(_tk(tk, "typography", "small", 8.4))
    leading_table = float(_tk(tk, "line_spacing", "leading_table", 13.2))
    icon_cell_w = float(_tk(tk, "components", "icon_cell_w", 30.0))
    pad = 5.0

    # 列宽：图标列固定 + 第一数据列（类别）窄列 + 其余数据列分剩余（对齐样图 1）
    n_data = len(headers)
    data_w = w - icon_cell_w
    if n_data == 1:
        col_w = [icon_cell_w, data_w]
    else:
        first_data_w = min(150.0, max(90.0, data_w * 0.25))
        rest = (data_w - first_data_w) / (n_data - 1)
        col_w = [icon_cell_w, first_data_w] + [rest] * (n_data - 1)
    n_cols = len(col_w)

    # 表头（图标列留空；v1.1 行高加大对齐样图）
    hh = size_th * 1.9 + pad * 2
    sheet.rect(x, y, w, hh, fill=header_bg)
    cx = x + icon_cell_w
    for i, hd in enumerate(headers):
        sheet.text(cx + pad, y + pad, str(hd), size_th, True, header_fg,
                   max_w=col_w[i + 1] - pad * 2)
        cx += col_w[i + 1]
        if i < n_data - 1:
            sheet.line(cx, y, cx, y + hh, "#2e4d65", 0.5)

    cy = y + hh
    used = hh
    for r_idx, row in enumerate(rows):
        # 行高：按最长数据单元格折行数
        lines = 1
        for i in range(n_data):
            cell = row[i + 1] if i + 1 < len(row) else ""
            cw = col_w[i + 1] - pad * 2
            lines = max(lines, len(wrap(str(cell), size_small, cw)))
        rh = max(leading_table * lines + pad * 2.8, size_small * 1.45 + pad * 2.8)
        if max_h is not None and used + rh > max_h:
            break
        bg = "white" if r_idx % 2 == 0 else row_alt
        sheet.rect(x, cy, w, rh, fill=bg, stroke=border, lw=0.4)
        # 图标列（居中）
        icon_txt = str(row[0]) if row else ""
        if icon_txt:
            sheet.text(x + icon_cell_w / 2, cy + rh / 2 - size_small,
                       icon_txt, size_small + 3, True, text_main, align="center",
                       max_w=icon_cell_w - 4)
        # 数据列
        cx = x + icon_cell_w
        for i in range(n_data):
            cw = col_w[i + 1]
            txt = str(row[i + 1]) if i + 1 < len(row) else ""
            if i == 0:
                # 第一数据列（类别）：加粗居中
                sheet.text(cx + cw / 2, cy + rh / 2 - size_small / 2, txt,
                           size_small, True, text_main, align="center",
                           max_w=cw - pad * 2)
            else:
                lines_txt = wrap(txt, size_small, cw - pad * 2)
                ty = cy + pad
                for ln in lines_txt:
                    sheet.text(cx + pad, ty, ln, size_small, False, text_main)
                    ty += leading_table
            cx += cw
            if i < n_data - 1:
                sheet.line(cx, cy, cx, cy + rh, border, 0.4)
        # 图标列右分隔线
        sheet.line(x + icon_cell_w, cy, x + icon_cell_w, cy + rh, border, 0.4)
        cy += rh
        used += rh

    # 外框
    sheet.rect(x, y, w, used, stroke=header_bg, lw=0.8)
    return used


def icon_table_height(tk: dict, w: float, headers: Sequence[str],
                      rows: Sequence[Sequence[str]]) -> float:
    """纯计算图标表占用高度（与 :func:`icon_table` 同逻辑，供布局预留）。"""
    if not headers or not rows:
        return 0.0
    size_th = float(_tk(tk, "typography", "table_header", 9.5))
    size_small = float(_tk(tk, "typography", "small", 8.4))
    leading_table = float(_tk(tk, "line_spacing", "leading_table", 13.2))
    icon_cell_w = float(_tk(tk, "components", "icon_cell_w", 30.0))
    pad = 5.0
    n_data = len(headers)
    data_w = w - icon_cell_w
    if n_data == 1:
        col_w = [icon_cell_w, data_w]
    else:
        first_data_w = min(150.0, max(90.0, data_w * 0.25))
        rest = (data_w - first_data_w) / (n_data - 1)
        col_w = [icon_cell_w, first_data_w] + [rest] * (n_data - 1)
    total = size_th * 1.6 + pad * 2
    for row in rows:
        lines = 1
        for i in range(n_data):
            cell = row[i + 1] if i + 1 < len(row) else ""
            cw = col_w[i + 1] - pad * 2
            lines = max(lines, len(wrap(str(cell), size_small, cw)))
        total += max(leading_table * lines + pad * 2, size_small * 1.45 + pad * 2)
    return total


def footer_skyline(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                   note: str = "") -> float:
    """页脚：细线 + 项目名 + 页码占位（由 title_block 已画页码，此处只画装饰）。

    视觉：浅灰细线 + 居中/右对齐小字（footer 字号）。
    """
    h = 18.0
    border = _tk(tk, "palette", "border", "#c1d0d9")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    size_ft = float(_tk(tk, "typography", "footer", 7.5))
    sheet.line(x, y, x + w, y, border, 0.5)
    if note:
        sheet.text(x + w / 2, y + 5, note, size_ft, False, muted, align="center",
                   max_w=w - 20)
    return h


# ---------------------------------------------------------------- 矢量图型库（阶段 3.5）
#
# 四个图型全部「纯代码 + 真实文字」：数据逐字取自原文（三级标题/岗位名/风险句），
# 不含日期推算、不含人名、不含任何模型生成内容；颜色只引用 tokens。


def _arrow(sheet: Sheet, x: float, y: float, size: float, color: str) -> None:
    """右向箭头（线 + 两条斜线箭头，避免依赖字形符号）。y 为箭头纵中心。"""
    sheet.line(x, y, x + size, y, color, 1.0)
    sheet.line(x + size, y, x + size - 3.6, y - 2.8, color, 1.0)
    sheet.line(x + size, y, x + size - 3.6, y + 2.8, color, 1.0)


def process_strip(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                  steps: Sequence[str]) -> float:
    """横向工序条：n 个步骤框 + 箭头连接；步骤名逐字原文（≤8 字截断）。"""
    steps = [s for s in steps if str(s).strip()]
    if not steps:
        return 0.0
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    box_h = 30.0
    gap = 14.0
    n = len(steps)
    box_w = (w - gap * (n - 1)) / n
    for i, s in enumerate(steps):
        bx = x + i * (box_w + gap)
        sheet.rect(bx, y, box_w, box_h, fill="white", stroke=border, lw=0.7)
        sheet.text(bx + box_w / 2, y + 9, str(s), 8.6, True, text_main,
                   align="center", max_w=box_w - 6)
        if i < n - 1:
            _arrow(sheet, bx + box_w + 2.5, y + box_h / 2, gap - 5, accent)
    return box_h


def chevron_cards(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                  cards: Sequence[dict], max_h: float | None = None) -> float:
    """风险对策卡列：每卡 = 左侧警示条 + 标题 + 原文句；文字逐字原文。"""
    if not cards:
        return 0.0
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    warn = _tk(tk, "palette", "warn_red", "#c0392b")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size_small = float(_tk(tk, "typography", "small", 8.4))
    gap = 8.0
    cy = y
    used = 0.0
    for c in cards:
        body = str(c.get("body", ""))
        lines = wrap(body, size_small, w - 22)[:4]
        card_h = 20.0 + len(lines) * (size_small * 1.45) + 8.0
        if max_h is not None and used + card_h + gap > max_h:
            break
        sheet.rect(x, cy, w, card_h, fill="white", stroke=border, lw=0.7)
        sheet.rect(x, cy, 3.0, card_h, fill=warn)
        sheet.text(x + 12, cy + 6, str(c.get("title", ""))[:12], 9.2, True, accent,
                   max_w=w - 20)
        ty = cy + 20.0
        for ln in lines:
            sheet.text(x + 12, ty, ln, size_small, False, text_main)
            ty += size_small * 1.45
        cy += card_h + gap
        used += card_h + gap
    return used - gap if used > gap else used


def timeline_bar(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                 phases: Sequence[dict]) -> float:
    """阶段时序条（无日期）：n 段分段条 + 段名；**不推算任何起止日期**（保真铁律）。"""
    phases = [p for p in phases if str(p.get("label", "")).strip()]
    if not phases:
        return 0.0
    header_bg = _tk(tk, "palette", "header_bg", "#1f4e79")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    row_alt = _tk(tk, "palette", "row_alt_light", "#f8fbfd")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    bar_h = 30.0
    seg_w = w / len(phases)
    for i, p in enumerate(phases):
        sx = x + i * seg_w
        fill = row_alt if i % 2 else "white"
        sheet.rect(sx, y, seg_w, bar_h, fill=fill, stroke=header_bg, lw=0.9)
        sheet.text(sx + seg_w / 2, y + 5, str(p["label"]), 8.6, True, text_main,
                   align="center", max_w=seg_w - 8)
        sheet.text(sx + seg_w / 2, y + bar_h + 4, f"阶段 {i + 1}", 7.4, False, muted,
                   align="center")
        if i < len(phases) - 1:
            _arrow(sheet, sx + seg_w - 5.5, y + bar_h / 2, 11.0, header_bg)
    return bar_h + 14.0


def org_chart(sheet: Sheet, tk: dict, x: float, y: float, w: float,
              nodes: Sequence[str]) -> float:
    """组织关系图：首岗位居中为顶，其余横排一层；只画原文出现的岗位名。"""
    nodes = [str(n).strip() for n in nodes if str(n).strip()]
    if not nodes:
        return 0.0
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    box_w, box_h = 150.0, 26.0
    top_x = x + (w - box_w) / 2
    sheet.rect(top_x, y, box_w, box_h, fill="white", stroke=accent, lw=1.2)
    sheet.text(x + w / 2, y + 7, nodes[0], 9.4, True, accent, align="center",
               max_w=box_w - 8)
    rest = nodes[1:8]
    if not rest:
        return box_h
    row_y = y + box_h + 34.0
    stem_x = x + w / 2
    sheet.line(stem_x, y + box_h, stem_x, row_y - box_h / 2 - 8, border, 0.8)
    n = len(rest)
    rw = min(120.0, (w - 16 * (n - 1)) / n)
    total_w = n * rw + 16 * (n - 1)
    rx0 = x + (w - total_w) / 2
    bus_y = row_y - box_h / 2 - 8
    sheet.line(rx0 + rw / 2, bus_y, rx0 + total_w - rw / 2, bus_y, border, 0.8)
    for i, name in enumerate(rest):
        bx = rx0 + i * (rw + 16)
        sheet.line(bx + rw / 2, bus_y, bx + rw / 2, row_y - box_h / 2, border, 0.8)
        sheet.rect(bx, row_y - box_h / 2, rw, box_h, fill="white", stroke=border, lw=0.8)
        sheet.text(bx + rw / 2, row_y - box_h / 2 + 7, name, 8.4, False, text_main,
                   align="center", max_w=rw - 6)
    return (row_y + box_h / 2) - y


# ---------------------------------------------------------------- 通用数据表（阶段 3.4）

from bidviz_vector import Cell, TableSpec   # noqa: E402


def data_table_height(tk: dict, w: float, headers: Sequence[str],
                      rows: Sequence[Sequence[str]],
                      col_ratios: Sequence[float] | None = None) -> float:
    """数据表高度估算（caption + 表头 + 行；与 Sheet.table 同口径）。"""
    cap_h = 16.0
    size = 9.0
    pad = 5.0
    ratios = list(col_ratios) if col_ratios else [1.0 / max(1, len(headers))] * len(headers)
    col_w = [w * r for r in ratios]
    h = size * 1.6 + pad * 2
    for row in rows:
        lines = 1
        for i, cell in enumerate(row):
            cw = (col_w[i] if i < len(col_w) else w) - pad * 2
            lines = max(lines, len(wrap(str(cell), size, cw)))
        h += max(13.2 * lines + pad * 2, size * 1.45 + pad * 2)
    return h + cap_h


def data_table(sheet: Sheet, tk: dict, x: float, y: float, w: float,
               caption: str, headers: Sequence[str], rows: Sequence[Sequence[str]],
               col_ratios: Sequence[float] | None = None,
               max_h: float | None = None) -> tuple[float, int]:
    """通栏数据表：题注（强调蓝）+ Sheet.table；返回 (占用高度, 已画行数)。

    max_h 不足时截断到能画的行，剩余行由调用方续排（续页重复表头）。
    """
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    header_bg = _tk(tk, "palette", "header_bg", "#1f4e79")
    ratios = list(col_ratios) if col_ratios else [1.0 / max(1, len(headers))] * len(headers)
    spec = TableSpec(headers=[str(h) for h in headers],
                     rows=[[str(c) for c in r] for r in rows],
                     col_w=[w * r for r in ratios], header_bg=header_bg,
                     font_size=9.0, pad=5.0, header_size=9.5)
    sheet.text(x, y, caption, 9.5, True, accent, max_w=w)
    used, drawn = sheet.table(spec, x, y + 16.0, max_h=(max_h - 16.0) if max_h else None)
    return used + 16.0, drawn


# ---------------------------------------------------------------- 页骨架与 8 类模板（阶段 3.4）


def _page_frame(sheet: Sheet, tk: dict, plan: PagePlan) -> tuple[float, float, float]:
    """公共骨架：标题区 + 可选 KPI；返回 (content_x, content_w, cy)。"""
    margin_x = float(_tk(tk, "spacing", "margin_x", 72.0))
    margin_top = float(_tk(tk, "spacing", "margin_top", 82.0))
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))
    content_x = margin_x
    content_w = sheet.w - margin_x * 2
    cy = margin_top - 40
    title_h = title_block(sheet, tk, content_x, cy, content_w,
                          plan.chapter, plan.section, plan.page_no, plan.total_pages)
    cy += title_h + gap_row
    kpi_h = kpi_strip(sheet, tk, content_x, cy, content_w, plan.kpis)
    cy += kpi_h + (gap_row if kpi_h else 0)
    return content_x, content_w, cy


def _footer_anchor(sheet: Sheet, tk: dict) -> float:
    margin_bottom = float(_tk(tk, "spacing", "margin_bottom", 62.0))
    return sheet.h - margin_bottom + 20


def _render_figure_prose_table(sheet: Sheet, tk: dict, plan: PagePlan,
                               right_kind: str = "prose") -> None:
    """A/C/H 共用：中部 [图 | 右栏(prose/chevron)] + 底部表（装得下的第一张）。"""
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))
    gap_col = float(_tk(tk, "spacing", "gap_col", 22.6))
    x, w, cy = _page_frame(sheet, tk, plan)
    y_footer = _footer_anchor(sheet, tk)

    tables = plan.tables or []
    table_h = 0.0
    first_table = None
    if tables:
        t0 = tables[0]
        first_table = t0
        table_h = data_table_height(tk, w, t0.get("headers", []), t0.get("rows", []),
                                    t0.get("col_ratios"))
    icon_h = icon_table_height(tk, w, plan.icon_table_headers, plan.icon_table_rows) \
        if plan.icon_table_headers else 0.0
    bottom_h = max(table_h, icon_h)

    vec_h = 0.0
    if plan.vector and plan.vector.get("kind") == "chevron_cards":
        cards = plan.vector.get("cards", [])
        vec_h = sum(20.0 + min(4, len(wrap(str(c.get("body", "")), 8.4, w * 0.45 - 22)))
                    * 12.2 + 8.0 + 8.0 for c in cards) if cards else 0.0

    mid_y = cy
    mid_h = max(120.0, (y_footer - gap_row - bottom_h) - mid_y) if (bottom_h or 1) else \
        (y_footer - mid_y)
    if plan.vector and plan.vector.get("kind") == "chevron_cards":
        mid_h = max(120.0, (y_footer - gap_row - bottom_h - vec_h * 0.5) - mid_y)

    left_ratio = 0.55
    left_w = (w - gap_col) * left_ratio
    right_w = (w - gap_col) * (1 - left_ratio)

    if plan.figure:
        figure_with_callouts(sheet, tk, x, mid_y, left_w, mid_h, plan.figure)
    rx = x + left_w + gap_col
    if right_kind == "chevron" and plan.vector:
        chevron_cards(sheet, tk, rx, mid_y, right_w,
                      plan.vector.get("cards", []), max_h=mid_h)
    else:
        prose_column(sheet, tk, rx, mid_y, right_w, plan.prose, mid_h)
        if plan.vector and plan.vector.get("kind") == "chevron_cards":
            chevron_cards(sheet, tk, rx, mid_y + 90.0, right_w,
                          plan.vector.get("cards", []), max_h=mid_h - 90.0)

    ty = y_footer - gap_row - bottom_h
    if icon_h and plan.icon_table_headers:
        icon_table(sheet, tk, x, ty, w, plan.icon_table_headers, plan.icon_table_rows)
    elif first_table:
        data_table(sheet, tk, x, ty, w, first_table.get("caption", ""),
                   first_table.get("headers", []), first_table.get("rows", []),
                   first_table.get("col_ratios"))
    footer_skyline(sheet, tk, x, y_footer, w, plan.footer_note)


def _render_vector_then_tables(sheet: Sheet, tk: dict, plan: PagePlan,
                               vector_first: bool = True) -> None:
    """E/F/G 共用：标题 + [KPI] + 矢量图型（时序/组织/工序）+ 通栏表序列。"""
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))
    x, w, cy = _page_frame(sheet, tk, plan)
    y_footer = _footer_anchor(sheet, tk)

    if vector_first and plan.vector:
        kind = plan.vector.get("kind")
        if kind == "timeline_bar":
            vh = timeline_bar(sheet, tk, x, cy, w, plan.vector.get("phases", []))
        elif kind == "org_chart":
            vh = org_chart(sheet, tk, x, cy, w, plan.vector.get("nodes", []))
        elif kind == "process_strip":
            vh = process_strip(sheet, tk, x, cy, w, plan.vector.get("steps", []))
        else:
            vh = 0.0
        cy += vh + gap_row
    elif plan.figure:
        fh = min(300.0, y_footer - cy - 120.0)
        if fh > 80:
            figure_with_callouts(sheet, tk, x, cy, w, fh, plan.figure)
            cy += fh + gap_row

    max_h = y_footer - gap_row - cy
    for t in plan.tables:
        th = data_table_height(tk, w, t.get("headers", []), t.get("rows", []),
                               t.get("col_ratios"))
        if max_h <= 40:
            break
        used, drawn = data_table(sheet, tk, x, cy, w, t.get("caption", ""),
                                 t.get("headers", []), t.get("rows", []),
                                 t.get("col_ratios"), max_h=max_h)
        cy += used + 6.0
        max_h -= used + 6.0
    footer_skyline(sheet, tk, x, y_footer, w, plan.footer_note)


def compose_page_ta1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-A1 总览目标型：KPI≤3 + 总览图 + 范围边界正文 + 任务对照表。"""
    _render_figure_prose_table(sheet, tk, plan)


# ---------------------------------------------------------------- T-B1 v2（样图同构版式，v1.1）


def page_header_lines(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                      left_text: str, right_text: str) -> float:
    """页眉（样图样式）：白底细线夹层，左章名深蓝、右标语灰 + 三斜线装饰。

    返回占用高度（夹层含下底线，约 26pt）。
    """
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    size_h2 = float(_tk(tk, "typography", "title_h2", 13.0))
    size_ft = float(_tk(tk, "typography", "footer", 7.5))
    h = 26.0
    sheet.line(x, y, x + w, y, border, 0.5)                       # 顶细线
    sheet.text(x, y + 5, left_text, size_h2, True, accent, max_w=w * 0.6)
    # 右侧标语（页眉右，先给三斜线留位）
    slash_x = x + w - 34
    sheet.text(slash_x - 8, y + 6, right_text, size_ft, False, muted, align="right",
               max_w=w * 0.32)
    for i, lw in enumerate((1.3, 0.9, 0.6)):
        sx = slash_x + i * 7
        sheet.line(sx, y + 16, sx + 7, y + 7, accent, lw)
    sheet.line(x, y + h, x + w, y + h, border, 1.0)               # 底稍粗线
    return h


def chapter_title_block(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                        chapter: str) -> float:
    """章大标题（样图层级一）：左斜切块 + 大字 + 左粗右细渐变线。"""
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    size = float(_tk(tk, "typography", "chapter_title", 16.0))
    skew_w = float(_tk(tk, "components", "header_skew_w", 5.0))
    sheet.polygon([(x, y), (x + skew_w, y), (x + skew_w - 3, y + 17), (x - 3, y + 17)],
                  fill=accent)
    sheet.text(x + skew_w + 8, y + 1, chapter, size, True, accent, max_w=w - 30)
    # 渐变粗线：左 3pt 渐弱到右 0.5pt（四段）
    cy = y + 24
    segs = ((0.00, 3.0), (0.22, 2.2), (0.45, 1.4), (0.68, 0.8), (0.86, 0.5))
    line_len = min(150.0, w * 0.35)
    ends = [r for r, _ in segs[1:]] + [1.0]
    for (r0, lw), r1 in zip(segs, ends):
        sheet.line(x + r0 * line_len, cy, x + max(r0 * line_len + 2, r1 * line_len), cy,
                   accent, lw)
    return 30.0


def section_title_line(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                       section: str) -> float:
    """节标题条（样图层级二）：小斜切块 + 节名 + 右延细线。"""
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size = float(_tk(tk, "typography", "section_title", 13.5))
    skew_w = float(_tk(tk, "components", "header_skew_w", 5.0)) * 0.7
    sheet.polygon([(x, y), (x + skew_w, y), (x + skew_w - 2.2, y + 13), (x - 2.2, y + 13)],
                  fill=accent)
    sheet.text(x + skew_w + 7, y + 0.5, section, size, True, accent, max_w=w * 0.7)
    tw = text_width(section, size, True) + skew_w + 16
    sheet.line(x + tw, y + 7.5, x + w, y + 7.5, border, 0.6)
    return 18.0


def sub_section_title(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                      text: str, glyph: str = "要") -> float:
    """小节标题（样图表题行）：圆环图标（CJK 单字）+ 标题 + 右延细线。"""
    accent = _tk(tk, "palette", "accent_blue", "#004890")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size = float(_tk(tk, "typography", "sub_section", 11.0))
    r = 8.0
    sheet.rect(x, y, r * 2, r * 2, fill=None, stroke=accent, lw=1.2, radius=r)
    sheet.text(x + r, y + 3.2, glyph, 9.5, True, accent, align="center")
    tx = x + r * 2 + 8
    sheet.text(tx, y + 3.5, text, size, True, accent, max_w=w - 40)
    tw = text_width(text, size, True) + r * 2 + 18
    sheet.line(x + tw, y + r, x + w, y + r, border, 0.6)
    return r * 2 + 6


def footer_v2(sheet: Sheet, tk: dict, x: float, y: float, w: float,
              page_no: int, total_pages: int, note: str = "") -> float:
    """页脚（样图样式）：细灰线 + 居中页码「— N —」+ 右下天际线剪影。"""
    border = _tk(tk, "palette", "border", "#c1d0d9")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    skyline = _tk(tk, "palette", "skyline_blue", "#dbe7f1")
    size_pn = float(_tk(tk, "typography", "page_no", 9.0))
    h = 20.0
    sheet.line(x, y, x + w, y, border, 0.5)
    # 居中页码
    pn = f"— {page_no} —" if total_pages <= 1 else f"— {page_no}/{total_pages} —"
    sheet.text(x + w / 2, y + 5, pn, size_pn, False, muted, align="center")
    # 右下天际线剪影（固定高度序列，确定性渲染）
    heights = (0.35, 0.62, 0.92, 0.45, 1.0, 0.7, 0.4, 0.8, 0.55)
    bw, gapx = 11.0, 2.5
    total = len(heights) * bw + (len(heights) - 1) * gapx
    sx = x + w - total
    for i, hf in enumerate(heights):
        bh = 16.0 * hf
        sheet.rect(sx + i * (bw + gapx), y + h - bh, bw, bh, fill=skyline)
    if note:
        sheet.text(x, y + 5, note[:28], size_pn - 1.2, False, muted, max_w=w * 0.35)
    return h


def compose_page_tb1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-B1 v2（样图同构，v1.1 待重签认）：页眉线夹层 → 章大标题(渐变线) → 节标题条
    → KPI 圆图标卡 → 左 55% 图(深蓝标注片+图签) + 右 45% 正文 → 表题行 → 图标表
    → 天际线页脚。所有尺寸/颜色取 tokens；页码在页脚居中（样图样式）。
    """
    w = sheet.w
    h = sheet.h
    margin_x = float(_tk(tk, "spacing", "margin_x", 40.0))
    margin_top = float(_tk(tk, "spacing", "margin_top", 56.0))
    margin_bottom = float(_tk(tk, "spacing", "margin_bottom", 56.0))
    gap_col = float(_tk(tk, "spacing", "gap_col", 22.6))
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))

    x = margin_x
    cw = w - margin_x * 2

    # 1) 页眉线夹层
    cy = margin_top - 26
    cy += page_header_lines(sheet, tk, x, cy, cw, plan.chapter, plan.footer_note[:20])

    # 2) 章大标题 + 渐变线
    cy += 10
    cy += chapter_title_block(sheet, tk, x, cy, cw, plan.chapter)

    # 3) 节标题条
    cy += 6
    cy += section_title_line(sheet, tk, x, cy, cw, plan.section)

    # 4) KPI 圆图标卡
    cy += 8
    kpi_h = kpi_strip(sheet, tk, x, cy, cw, plan.kpis)
    cy += kpi_h + (gap_row if kpi_h else 4)

    # 5) 底部布局：页脚锚定页底；表区（表题行 + 表）按实际内容回推表顶
    y_footer = h - margin_bottom
    icon_h = icon_table_height(tk, cw, plan.icon_table_headers, plan.icon_table_rows)
    sub_h = sub_section_title_height(plan)
    first_tbl = plan.tables[0] if plan.tables else None
    if icon_h:
        block_h = sub_h + icon_h
    elif first_tbl:
        # 首页通用表最多占 380pt；更高则截断，剩余行经 _tb1_remainder 转续页
        t_h = data_table_height(tk, cw, first_tbl.get("headers", []),
                                first_tbl.get("rows", []), first_tbl.get("col_ratios"))
        block_h = sub_h + min(t_h, 380.0)
    else:
        block_h = 0.0
    table_top = y_footer - 10 - block_h

    # 6) 主区：左 55% 图 + 右 45% 正文（填满 KPI 到表区）
    mid_y = cy
    mid_h = max(120.0, table_top - 12 - mid_y)
    left_w = (cw - gap_col) * 0.55
    right_w = (cw - gap_col) * 0.45
    if plan.figure:
        figure_with_callouts(sheet, tk, x, mid_y, left_w, mid_h, plan.figure)
    gap_para = float(_tk(tk, "spacing", "gap_para", 9.0))
    prose_column(sheet, tk, x + left_w + gap_col, mid_y, right_w, plan.prose, mid_h,
                 para_gap=gap_para)

    # 7) 表题行 + 表（图标表 or 通用表；通用表 max_h 截断防页外——踩坑 #15）
    plan._tb1_remainder = None
    if icon_h:
        ty = table_top
        ty += sub_section_title(sheet, tk, x, ty, cw,
                                first_table_caption(plan) or "重点工序实施要点")
        icon_table(sheet, tk, x, ty, cw, plan.icon_table_headers, plan.icon_table_rows)
    elif first_tbl:
        ty = table_top
        ty += sub_section_title(sheet, tk, x, ty, cw,
                                first_table_caption(plan) or "重点工序实施要点")
        max_h = y_footer - 4 - ty
        used, drawn = data_table(sheet, tk, x, ty, cw, first_tbl.get("caption", ""),
                                 first_tbl.get("headers", []),
                                 first_tbl.get("rows", []),
                                 first_tbl.get("col_ratios"), max_h=max_h)
        rows = first_tbl.get("rows", [])
        if drawn < len(rows):
            rest = dict(first_tbl)
            rest["rows"] = rows[drawn:]
            rest["caption"] = str(first_tbl.get("caption", "")) + "（续）"
            plan._tb1_remainder = rest

    # 8) 天际线页脚（页码居中）
    footer_v2(sheet, tk, x, y_footer + 10, cw, plan.page_no, plan.total_pages)


def sub_section_title_height(plan: PagePlan) -> float:
    return 22.0


def first_table_caption(plan: PagePlan) -> str:
    if plan.tables:
        return str(plan.tables[0].get("caption") or "")
    return ""


def compose_page_tc1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-C1 构造空间型：构造主图 + 矢量编号叠标 + 原文对应 + 参数表。"""
    _render_figure_prose_table(sheet, tk, plan)


def compose_page_td1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-D1 参数资源型：通栏表为主，无 KPI 无图（小幅识别图不占版面）。"""
    _render_vector_then_tables(sheet, tk, plan, vector_first=False)


def compose_page_te1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-E1 计划时序型：时序条（无日期）+ 前置条件/资源表。"""
    _render_vector_then_tables(sheet, tk, plan, vector_first=True)


def compose_page_tf1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-F1 组织协同型：组织图 + 职责接口表。"""
    _render_vector_then_tables(sheet, tk, plan, vector_first=True)


def compose_page_tg1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-G1 检验清单型：检验表主体（通栏）+ 完整判定条件。"""
    _render_vector_then_tables(sheet, tk, plan, vector_first=True)


def compose_page_th1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-H1 风险对策型：风险示意 + 对策卡（chevron）+ 措施原文表。"""
    _render_figure_prose_table(sheet, tk, plan, right_kind="chevron")


def compose_content_page(sheet: Sheet, tk: dict, plan: PagePlan,
                         tables: Sequence[dict]) -> list[dict]:
    """续页（内容页）：标题区 + 通栏表序列（表头重复）。

    返回未容纳的表（含部分画出后的剩余行，caption 已加「（续）」），
    由调用方决定继续续页或按 ≤3 页预算记溢出。
    """
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))
    margin_x = float(_tk(tk, "spacing", "margin_x", 72.0))
    content_w = sheet.w - margin_x * 2
    y_footer = _footer_anchor(sheet, tk)
    x, _w, cy = _page_frame(sheet, tk, plan)
    leftover: list[dict] = []
    for t in tables:
        max_h = y_footer - gap_row - cy
        if max_h <= 60:
            leftover.append(dict(t))
            continue
        used, drawn = data_table(sheet, tk, x, cy, content_w, t.get("caption", ""),
                                 t.get("headers", []), t.get("rows", []),
                                 t.get("col_ratios"), max_h=max_h)
        rows = t.get("rows", [])
        if drawn < len(rows):
            rest = dict(t)
            rest["rows"] = rows[drawn:]
            rest["caption"] = str(t.get("caption", "")) + "（续）"
            leftover.append(rest)
        cy += used + 6.0
    footer_skyline(sheet, tk, x, y_footer, content_w, plan.footer_note)
    return leftover


# ---------------------------------------------------------------- 页装配器分发

_TEMPLATE_FUNCS = {
    "T-A1": compose_page_ta1,
    "T-B1": compose_page_tb1,
    "T-C1": compose_page_tc1,
    "T-D1": compose_page_td1,
    "T-E1": compose_page_te1,
    "T-F1": compose_page_tf1,
    "T-G1": compose_page_tg1,
    "T-H1": compose_page_th1,
}


def compose_page(sheet: Sheet, tk: dict, plan: PagePlan, template: str = "T-B1") -> None:
    """页装配器分发（V1.3 阶段 3：8 类模板齐备）。"""
    tid = template or plan.template_id or "T-B1"
    fn = _TEMPLATE_FUNCS.get(tid)
    if fn is None:
        raise NotImplementedError(f"template {tid} 尚未实现")
    fn(sheet, tk, plan)
