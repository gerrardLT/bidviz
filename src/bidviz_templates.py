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
    """T-B1 页规划：标题区 → KPI 三区 → 左图右文 → 底部图标表。"""
    chapter: str = ""
    section: str = ""
    page_no: int = 1
    total_pages: int = 1
    kpis: list[KPI] = field(default_factory=list)
    figure: FigureSlot | None = None
    prose: list[ProseBlock] = field(default_factory=list)
    icon_table_headers: list[str] = field(default_factory=list)
    icon_table_rows: list[list[str]] = field(default_factory=list)
    footer_note: str = ""


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

    视觉：每卡白底 + 深蓝左边条 + 大数字 + 小标签；卡间 gap_kpi_card。
    """
    if not kpis:
        return 0.0
    gap = float(_tk(tk, "spacing", "gap_kpi_card", 16.0))
    kpi_h = float(_tk(tk, "components", "kpi_h", 50.0))
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size_h1 = float(_tk(tk, "typography", "title_h1", 15.0))
    size_small = float(_tk(tk, "typography", "small", 8.4))

    n = len(kpis)
    card_w = (w - gap * (n - 1)) / n
    for i, k in enumerate(kpis):
        cx = x + i * (card_w + gap)
        # 卡底
        sheet.rect(cx, y, card_w, kpi_h, fill="white", stroke=border, lw=0.6)
        # 左边条（强调色）
        sheet.rect(cx, y, 4.0, kpi_h, fill=accent)
        # 可选图标（左侧圆形区）
        text_x = cx + 14
        if k.icon:
            icon_cx = cx + 24
            icon_cy = y + kpi_h / 2
            sheet.rect(icon_cx - 11, icon_cy - 11, 22, 22, fill=None,
                       stroke=accent, lw=1.0, radius=11)
            sheet.text(icon_cx, icon_cy - 7, k.icon, 11, True, accent, align="center")
            text_x = cx + 42
        # 大数值 + 单位
        val_str = f"{k.value}{k.unit}" if k.unit else k.value
        sheet.text(text_x, y + 8, val_str, size_h1 + 4, True, text_main,
                   max_w=card_w - (text_x - cx) - 10)
        # 标签
        sheet.text(text_x, y + 28, k.label, size_small, False, muted,
                   max_w=card_w - (text_x - cx) - 10)
        # 附注（可选）
        if k.note:
            sheet.text(text_x, y + 40, k.note, size_small - 0.5, False, muted,
                       max_w=card_w - (text_x - cx) - 10)
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
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    size_cap = float(_tk(tk, "typography", "caption", 9.5))
    callout_w = float(_tk(tk, "components", "callout_w", 24.0))
    callout_h = float(_tk(tk, "components", "callout_h", 14.0))

    # 图注占底部一行
    caption_h = size_cap * 1.6 if slot.caption else 0.0
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
    else:
        # 占位框
        sheet.rect(x, y, img_w, img_h, fill="#f4f7f9", stroke=border, lw=0.8)
        sheet.text(x + img_w / 2, y + img_h / 2 - 6, "AI 图待生成", 12, True, muted,
                   align="center")
        sheet.text(x + img_w / 2, y + img_h / 2 + 10,
                   f"（{img_w:.0f}×{img_h:.0f}pt 预留）", 8, False, muted, align="center")
        ox, oy, dw, dh = x, y, img_w, img_h

    # 编号叠标（矢量白片：编号 + 文字同片，保证图上可读）
    for c in slot.callouts:
        ax = ox + dw * c.anchor[0]
        ay = oy + dh * c.anchor[1]
        label_size = 8.0
        text_w = text_width(c.text, label_size) if c.text else 0.0
        plate_w = callout_w + (text_w + 8 if c.text else 0)
        # 白片（编号区 + 文字区）
        sheet.rect(ax - callout_w / 2, ay - callout_h / 2, plate_w, callout_h,
                   fill="white", stroke=accent, lw=0.8, radius=2.0)
        # 编号（居中于编号区）
        sheet.text(ax, ay - callout_h / 2 + 2, c.no, 8.5, True, accent,
                   align="center", max_w=callout_w - 2)
        # 文字（片内右侧）
        if c.text:
            sheet.text(ax + callout_w / 2 + 4, ay - callout_h / 2 + 2.5, c.text,
                       label_size, False, text_main, max_w=text_w + 4)
        # 锚点引线（白片中心到锚点的小圆点）
        sheet.rect(ax - 1.2, ay - 1.2, 2.4, 2.4, fill=accent)

    # 图注
    if slot.caption:
        sheet.text(x + img_w / 2, y + img_h + 4, slot.caption, size_cap, False,
                   text_main, align="center", max_w=img_w)

    # 隐藏检索层（D10）：text_spec 逐条写入，使纯视觉页可 Ctrl+F
    if slot.text_spec:
        hy = y + img_h + caption_h + 4
        for ts in slot.text_spec:
            sheet.hidden_text(x + 4, hy, ts, size_cap)
            hy += size_cap * 1.4


def prose_column(sheet: Sheet, tk: dict, x: float, y: float, w: float,
                 blocks: Sequence[ProseBlock], max_h: float) -> float:
    """右 45% 正文流：按 D3 保留 1–2 段关键原文；返回占用高度。

    - heading：深蓝加粗，段前留白。
    - body：正文色，行高 leading_body。
    - quote：左竖线 + 灰底（预留，阶段 3 用）。
    """
    if not blocks:
        return 0.0
    text_main = _tk(tk, "palette", "text_main", "#1a2c38")
    accent = _tk(tk, "palette", "accent_blue", "#1f4e79")
    muted = _tk(tk, "palette", "muted", "#5a6e7c")
    border = _tk(tk, "palette", "border", "#c1d0d9")
    size_normal = float(_tk(tk, "typography", "normal", 9.5))
    size_h2 = float(_tk(tk, "typography", "title_h2", 13.0))
    leading_body = float(_tk(tk, "line_spacing", "leading_body", 13.8))

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
            cy += ph + 4
            used += ph + 4
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

    # 表头（图标列留空）
    hh = size_th * 1.6 + pad * 2
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
        rh = max(leading_table * lines + pad * 2, size_small * 1.45 + pad * 2)
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


# ---------------------------------------------------------------- T-B1 页装配器


def compose_page_tb1(sheet: Sheet, tk: dict, plan: PagePlan) -> None:
    """T-B1 版式装配（S003 样板）：标题区 → KPI 三区 → 左图右文 → 底部图标表 → 页脚。

    所有尺寸从 tokens 取；布局比例固定（左 55% / 右 45%）。
    """
    w = sheet.w
    h = sheet.h
    margin_x = float(_tk(tk, "spacing", "margin_x", 72.0))
    margin_top = float(_tk(tk, "spacing", "margin_top", 82.0))
    margin_bottom = float(_tk(tk, "spacing", "margin_bottom", 62.0))
    gap_col = float(_tk(tk, "spacing", "gap_col", 22.6))
    gap_row = float(_tk(tk, "spacing", "gap_row", 14.2))

    content_x = margin_x
    content_w = w - margin_x * 2

    # 1) 标题区
    cy = margin_top - 40   # title_block 占 margin_top 内
    title_h = title_block(sheet, tk, content_x, cy, content_w,
                          plan.chapter, plan.section, plan.page_no, plan.total_pages)
    cy += title_h + gap_row

    # 2) KPI 三区
    kpi_h = kpi_strip(sheet, tk, content_x, cy, content_w, plan.kpis)
    cy += kpi_h + gap_row

    # 3) 底部布局：页脚线锚定页底，图标表钉在页脚线上方，中部填满剩余
    y_footer = h - margin_bottom + 20
    icon_h = icon_table_height(tk, content_w, plan.icon_table_headers,
                               plan.icon_table_rows)
    icon_y = (y_footer - gap_row - icon_h) if icon_h else y_footer

    # 4) 中部：左图右文（填满 mid_y 到图标表之间）
    mid_y = cy
    mid_h = max(100.0, icon_y - gap_row - mid_y)

    left_ratio = 0.55
    left_w = (content_w - gap_col) * left_ratio
    right_w = (content_w - gap_col) * (1 - left_ratio)

    # 左：图位
    if plan.figure:
        figure_with_callouts(sheet, tk, content_x, mid_y, left_w, mid_h, plan.figure)

    # 右：正文
    prose_column(sheet, tk, content_x + left_w + gap_col, mid_y, right_w,
                 plan.prose, mid_h)

    # 5) 底部图标表（钉在页脚线上方）
    if icon_h:
        icon_table(sheet, tk, content_x, icon_y, content_w,
                   plan.icon_table_headers, plan.icon_table_rows)

    # 6) 页脚
    footer_skyline(sheet, tk, content_x, y_footer, content_w, plan.footer_note)


# ---------------------------------------------------------------- 入口


def compose_page(sheet: Sheet, tk: dict, plan: PagePlan, template: str = "T-B1") -> None:
    """页装配器分发；阶段 2 只实现 T-B1，阶段 3 扩展其余 7 类。"""
    if template == "T-B1":
        compose_page_tb1(sheet, tk, plan)
    else:
        raise NotImplementedError(f"template {template} 尚未实现（阶段 3+）")
