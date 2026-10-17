#!/usr/bin/env python3
"""E1 双页型编译器：图版页（本节规划的 1~N 张 AI 主图）/ 正文页（原文逐字两栏）。

输出：矢量 PDF，仅图片为位图。文字可选中、可搜索，且可反抽校验覆盖率。
画几张、每张画什么，由 bidviz_plan 先分析内容再规划，落盘 work/figure-plan.json。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import fitz

import bidviz_vector as bv
from bidviz_config import get as _cfg

MM = bv.MM
PAGE_W, PAGE_H = bv.PAGE_W, bv.PAGE_H

MARGIN = 12 * MM
HEAD_H = 22 * MM
FOOT_H = 11 * MM
COL_GAP = 8 * MM
BODY_TOP = HEAD_H + 6 * MM
BODY_BOTTOM = PAGE_H - FOOT_H - 4 * MM

DARK = "#0c2333"
HEADER_BG = "#13334c"
ACCENT = "#00897b"
CYAN = "#26a69a"
BORDER = "#c1d0d9"
TEXT = "#1a2c38"
MUTED = "#5a6e7c"
WARN = "#e65100"
RED_BG = "#8a2a2a"


def apply_style() -> None:
    """把当前项目的色板投影到既有排版常量。"""
    global DARK, HEADER_BG, ACCENT, CYAN, BORDER, TEXT, MUTED, WARN, RED_BG
    style = _cfg().style
    DARK = style["dark"]
    HEADER_BG = style["header_bg"]
    ACCENT = style["accent"]
    CYAN = style["cyan"]
    BORDER = style["border"]
    TEXT = style["text"]
    MUTED = style["muted"]
    WARN = style["warn"]
    RED_BG = style["danger_bg"]


# 中文字号：A3 打印舒适区
BODY_SIZE = 9.5
BODY_LEAD = 13.8

# 工程报表式配色：单一结构蓝 + 一个警示红，其余黑白灰
TABLE_ACCENT = "#1f4e79"
WARN_RED = "#c0392b"


@dataclass
class WorkItem:
    """一个分项：1 页视觉页 + N 页数据页。"""
    work_id: str
    title: str
    section_path: str
    blocks: list[Any] = field(default_factory=list)
    source_pages: list[int] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(len(b.text) for b in self.blocks)


def section_key(block: Any) -> str:
    """块所属「节」的稳定键：取 section_path 的章/节两级，忽略三级及以下。

    源块 section_path 由 bidviz_v2.build_blocks 写成「章 / 节 / 一、 / （一）」面包屑，
    因此前两级对同一节内所有块（含三级/四级标题与正文）恒定不变。
    """
    sp = getattr(block, "section_path", "") or ""
    parts = [p for p in sp.split(" / ") if p.strip()]
    return " / ".join(parts[:2]) if parts else ""


_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百\d]+章")


def effective_keys(blocks: Sequence[Any]) -> list[str]:
    """逐块的有效节键；章级标题（只有 1 级 path）归入其后一个节，避免自成空节。

    只有 8 个章标题、且每章首节紧跟在章标题之后，若不归并会凭空多出 8 个
    “章标题节”，既破坏节数不变量又浪费整对页面。
    """
    keys = [section_key(b) for b in blocks]
    for i, b in enumerate(blocks):
        if b.kind == "heading" and _CHAPTER_RE.match(b.text) and i + 1 < len(blocks):
            keys[i] = keys[i + 1]
    return keys


def group_work_items(blocks: Sequence[Any], min_chars: int = 220) -> list[WorkItem]:
    """按「节」边界 + 三级标题（一、二、…）切分项。

    - 节边界（有效节键变化）必切断，保证每节以自己的章/节标题开头，
      不再把下一节标题排进上一节末页；
    - 同一节内章标题紧跟节标题时，两者作为本节的前导块合并（章在前）；
    - 节内再按三级标题细分，过短的三级项并入前一项，避免碎页。
    """
    blocks = list(blocks)
    keys = effective_keys(blocks)
    items: list[WorkItem] = []
    cur: WorkItem | None = None
    cur_key: str | None = None
    seq = 0

    def start(key: str, title: str, first: Any) -> None:
        nonlocal cur, seq
        seq += 1
        cur = WorkItem(f"W{seq:03d}", title, key, [], [])
        cur.blocks.append(first)
        cur.source_pages.append(first.source_page)

    for b, key in zip(blocks, keys):
        t = b.text
        # 节边界：有效键变化 → 先切断上一节
        if cur is not None and key != cur_key:
            items.append(cur)
            cur = None
        cur_key = key
        if b.kind == "heading":
            is_boundary = bool(re.match(r"^第[一二三四五六七八九十百\d]+[章节]", t))
            is_item = bool(re.match(r"^[一二三四五六七八九十]+、", t))
            if is_boundary:
                # 章/节标题：开启新节首块；若本节已开（章紧跟节）则并入作为前导块
                if cur is None:
                    start(key, t, b)
                else:
                    cur.blocks.append(b)
                    cur.source_pages.append(b.source_page)
                continue
            if is_item:
                if cur and cur.chars >= min_chars:
                    items.append(cur)
                    cur = None
                if cur is None:
                    start(key, t, b)
                    continue
        if cur is None:
            start(key, key.split(" / ")[-1] if key else "总述", b)
        else:
            cur.blocks.append(b)
            cur.source_pages.append(b.source_page)

    if cur:
        items.append(cur)
    return items


class Flow:
    """按阅读顺序在若干区域间流动排版：区域放满就换下一个，用完就换页。"""

    def __init__(self, sheet: bv.Sheet, regions: Sequence[tuple[float, float, float, float]],
                 on_new_page=None, rest_regions=None):
        self.sheet = sheet
        self.regions = list(regions)
        self.rest_regions = list(rest_regions) if rest_regions else list(regions)
        self.idx = 0
        self.y = self.regions[0][1] if self.regions else 0
        self.on_new_page = on_new_page
        self.pages_used = 1

    @property
    def region(self) -> tuple[float, float, float, float]:
        return self.regions[self.idx]

    @property
    def remaining(self) -> float:
        x, y0, w, h = self.region
        return (y0 + h) - self.y

    def _advance_region(self) -> bool:
        if self.idx + 1 < len(self.regions):
            self.idx += 1
            self.y = self.region[1]
            return True
        return False

    def _new_page(self) -> None:
        self.sheet.c.showPage()
        self.pages_used += 1
        self.regions = self.rest_regions      # 首页可能让位导览框，次页起用整栏
        self.idx = 0
        self.y = self.region[1]
        if self.on_new_page:
            self.on_new_page(self.pages_used)

    def _ensure(self, need: float) -> bool:
        """确保当前区域有 need 高度；不足则换区域/换页。返回 False 表示本项内容结束。"""
        guard = 0
        while self.remaining < need:
            guard += 1
            if guard > 64:
                return False
            if not self._advance_region():
                self._new_page()
        return True

    def heading(self, text: str, level: int = 1) -> None:
        size = {1: 12.5, 2: 11.0, 3: 10.0}.get(level, 10.0)
        need = size * 1.9
        self._ensure(need)
        x, _, w, _ = self.region
        if level <= 2:
            self.sheet.rect(x, self.y, w, size * 1.7, fill="#eaf1f5")
            self.sheet.rect(x, self.y, 2.5, size * 1.7, fill=ACCENT)
            self.sheet.text(x + 8, self.y + size * 0.42, text, size, True, DARK, max_w=w - 16)
            self.y += size * 1.9
        else:
            self.sheet.text(x, self.y, text, size, True, HEADER_BG, max_w=w)
            self.y += need

    def paragraph(self, text: str, size: float = BODY_SIZE, lead: float = BODY_LEAD,
                  color: str = TEXT, indent: bool = True) -> None:
        x, _, w, _ = self.region
        lines = bv.wrap(text, size, w)
        first = True
        for ln in lines:
            if not self._ensure(lead):
                return
            cx, _, cw, _ = self.region
            pad = size * 2 if (indent and first) else 0
            self.sheet.text(cx + pad, self.y, ln, size, False, color)
            self.y += lead
            first = False

    def caption(self, text: str) -> None:
        """报表题注：蓝色加粗小标题 + 同宽细下划线（工程报表式，无底色）。"""
        need = 17
        if not self._ensure(need):
            return
        x, _, w, _ = self.region
        self.sheet.text(x, self.y, text, 9.5, True, TABLE_ACCENT)
        self.sheet.line(x, self.y + 12.5, x + bv.text_width(text, 9.5, True), self.y + 12.5,
                        TABLE_ACCENT, 1.0)
        self.y += need

    def spacer(self, h: float) -> None:
        if self._ensure(h):
            self.y += h

    def table(self, spec: bv.TableSpec) -> None:
        """表格跨页续排：表头每页重画。列宽超栏宽时等比缩放。"""
        x, _, w, _ = self.region
        total = sum(spec.col_w)
        if total > w:
            k = w / total
            spec = bv.TableSpec(spec.headers, spec.rows, [c * k for c in spec.col_w], spec.header_bg,
                                spec.font_size, spec.pad, spec.header_size)
        remaining = list(spec.rows)
        while remaining:
            if self.remaining < spec.font_size * 3:
                if not self._advance_region():
                    self._new_page()
            x, _, w, _ = self.region
            sub = bv.TableSpec(spec.headers, remaining, spec.col_w, spec.header_bg,
                               spec.font_size, spec.pad, spec.header_size)
            used, drawn = self.sheet.table(sub, x, self.y, max_h=self.remaining)
            if drawn == 0:
                if not self._advance_region():
                    self._new_page()
                continue
            self.y += used
            remaining = remaining[drawn:]
            if remaining:
                if not self._advance_region():
                    self._new_page()
        self.spacer(4)


# ---------------- 数据页区域 ----------------
def data_regions() -> list[tuple[float, float, float, float]]:
    col_w = (PAGE_W - MARGIN * 2 - COL_GAP) / 2
    h = BODY_BOTTOM - BODY_TOP
    return [
        (MARGIN, BODY_TOP, col_w, h),
        (MARGIN + col_w + COL_GAP, BODY_TOP, col_w, h),
    ]


# ---------------- 图版页：本节规划的 1~N 张主图，全部由 AI 生成 ----------------
FIG_BAR_H = 6.5 * MM     # 题注条高度
FIG_GAP = 4.5 * MM       # 图位间距
IDX_HEAD_H = 7 * MM      # 图目录表头高
IDX_ROW_H = 8.2 * MM     # 图目录行高


def _src_label(item: WorkItem) -> str:
    src = ",".join(str(x) for x in sorted(set(item.source_pages)))
    return f"原文 P{src}" if src else ""


def index_height(n: int) -> float:
    """底部图目录卡高度：随图数增减，剩下的全给图。"""
    return IDX_HEAD_H + 2.2 * MM + IDX_ROW_H * max(1, n) + 2 * MM


def figure_cell(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                img: Path | None, label: str, src: str, root: Path,
                ph: str = "待生成") -> None:
    """一个图位：外框 + contain 图片 + 顶部题注条。

    缺图时只画占位框，不回退到程序矢量绘图（能 AI 生成的一律 AI 生成）。
    """
    sheet.rect(x, y, w, h, fill="white", stroke=BORDER, lw=0.8)
    ix, iy, iw, ih = x + 2, y + FIG_BAR_H, w - 4, h - FIG_BAR_H - 4
    if img and img.exists():
        sheet.image(img, ix, iy, iw, ih, dpi=170, cache_dir=root / "work/_imgcache", fit="contain")
    else:
        sheet.rect(ix, iy, iw, ih, fill="#eef3f6")
        sheet.text(x + w / 2, iy + ih / 2 - 9, ph, 12, True, "#7b909c", align="center")
        sheet.text(x + w / 2, iy + ih / 2 + 7, "运行 bidviz generate 后自动替换", 8, False,
                   "#94a9b6", align="center")
    sheet.rect(x, y, w, FIG_BAR_H, fill="#1a3d59")
    sheet.text(x + 5, y + 1.7 * MM, label, 8.6, True, "white", max_w=w - 58 * MM)
    if src:
        sheet.text(x + w - 5, y + 1.9 * MM, src, 7, False, "#8fb3cc", align="right")


def draw_figure_index(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                      item: WorkItem, figs: list[dict], imgs: dict) -> None:
    """本节图目录：图号、图题、为何画这张、生成状态与原文页码。"""
    sheet.rect(x, y, w, h, fill="#f6f8fa", stroke=BORDER, lw=0.8)
    sheet.rect(x, y, w, IDX_HEAD_H, fill=HEADER_BG)
    sheet.text(x + 5, y + 1.9 * MM, "本节图目录与原文溯源", 9.5, True, "white")
    sheet.text(x + w - 5, y + 2.2 * MM, _src_label(item), 7.5, False, "#9dc0d6", align="right")
    cy = y + IDX_HEAD_H + 2.2 * MM
    for f in figs:
        if cy + IDX_ROW_H > y + h - 2 * MM:
            break
        no = int(f.get("no", 1))
        ok = bool(imgs.get(no))
        sheet.rect(x + 3, cy - 1.6, w - 6, IDX_ROW_H - 2.4, fill="white", stroke=BORDER, lw=0.3)
        sheet.text(x + 5, cy, f"图 {no}", 8.2, True, ACCENT)
        sheet.text(x + 16 * MM, cy, f.get("name", ""), 8.2, False, TEXT, max_w=64 * MM)
        sheet.text(x + 82 * MM, cy, str(f.get("why", ""))[:64], 7.4, False, MUTED,
                   max_w=w - 128 * MM)
        sheet.text(x + w - 5, cy, "已生成" if ok else "待生成", 7.8, True,
                   "#2e7d32" if ok else WARN, align="right")
        cy += IDX_ROW_H
    if cy <= y + IDX_HEAD_H + 2.2 * MM:
        sheet.text(x + w / 2, y + h / 2, "本节未规划图位", 8.5, False, MUTED, align="center")


def gallery_page(sheet: bv.Sheet, item: WorkItem, imgs: dict, figs: list[dict],
                 page_no: int, total: int, root: Path) -> None:
    """图版页：把本节规划的 1~N 张主图排满一页，底部附矢量图目录。

    页上每一张图都是 AI 生成的位图；只有题注、图目录与页眉页脚是矢量文字。
    """
    bv.register_fonts()
    import bidviz_plan as plan

    sheet.rect(0, 0, PAGE_W, HEAD_H, fill=DARK)
    sheet.text(MARGIN, 3.2 * MM, _cfg().header_text, 8.5, False, "#7ea6c2")
    sheet.text(MARGIN, 8.6 * MM, item.title, 15, True, "white", max_w=PAGE_W - MARGIN * 2 - 120)
    sheet.text(PAGE_W - MARGIN, 6 * MM, f"{item.work_id}  图版页", 11, True, CYAN, align="right")
    sheet.text(PAGE_W - MARGIN, 12.5 * MM, item.section_path[:46], 7.5, False, "#8fb3cc", align="right")

    top = HEAD_H + 6 * MM
    avail_w = PAGE_W - MARGIN * 2
    body_h = BODY_BOTTOM - top
    n = len(figs)

    if n == 0:      # 未规划图位：只画占位说明，不回退程序绘图
        sheet.rect(MARGIN, top, avail_w, body_h, fill="#eef3f6", stroke=BORDER, lw=0.8)
        sheet.text(PAGE_W / 2, top + body_h / 2 - 8, "本节未规划图位", 13, True, "#7b909c",
                   align="center")
        sheet.text(PAGE_W / 2, top + body_h / 2 + 10, "先运行 bidviz plan 生成图位规划", 9, False,
                   "#94a9b6", align="center")
        footer(sheet, item, page_no, total)
        return

    idx_h = min(index_height(n), body_h * 0.4)
    stack_h = body_h - idx_h - FIG_GAP            # 留给图区的总高
    cols = 1 if n == 1 else 2
    rows = -(-n // cols)
    cell_w = (avail_w - COL_GAP * (cols - 1)) / cols
    budget = (stack_h - FIG_GAP * (rows - 1)) / rows   # 每行高度上限
    src = _src_label(item)

    # 先按每张图的真实宽高比算出画出来的尺寸，再定每行实际高度，最后整组垂直居中，
    # 避免图与图目录之间出现大片空洞。
    placed: list[tuple[int, int, bool, float, float, float, int, Any, dict]] = []
    row_h = [0.0] * rows
    for i, f in enumerate(figs):
        no = int(f.get("no", i + 1))
        img = imgs.get(no)
        ratio = max(plan.image_ratio(img, float(f.get("ratio") or 1.414)), 0.3)
        span = rows > 1 and n % cols == 1 and i == n - 1     # 奇数张时末格跳列
        w = avail_w if span else cell_w
        dw = min(w, budget * ratio)
        dh = dw / ratio
        r, c = divmod(i, cols)
        placed.append((r, c, span, w, dw, dh, no, img, f))
        row_h[r] = max(row_h[r], dh)

    used = sum(row_h) + FIG_GAP * (rows - 1) + FIG_GAP + idx_h
    y0 = top + max(0.0, (body_h - used) / 2)
    row_y = [y0]
    for r in range(1, rows):
        row_y.append(row_y[-1] + row_h[r - 1] + FIG_GAP)

    for r, c, span, w, dw, dh, no, img, f in placed:
        x = MARGIN + (0 if span else c * (cell_w + COL_GAP))
        label = (f"图 {no}  {item.title} · {f.get('name', '')}" if n == 1
                 else f"图 {no}  {f.get('name', '')}")
        figure_cell(sheet, x + (w - dw) / 2, row_y[r] + (row_h[r] - dh) / 2, dw, dh, img,
                    label, src if no == 1 else "", root, ph=f"图 {no} 待生成")

    draw_figure_index(sheet, MARGIN, row_y[-1] + row_h[-1] + FIG_GAP, avail_w, idx_h,
                      item, figs, imgs)
    footer(sheet, item, page_no, total)


def _crumb(item: WorkItem) -> str:
    parts = [p for p in (item.section_path or "").split(" / ") if p.strip()]
    return "  ›  ".join(parts) if parts else (item.title or "总述")


def content_tables(item: WorkItem) -> list:
    """本节内容报表：由 bidviz_plan.curate_tables 策展（一句只归一类、拆字段、材料只留真名词）。"""
    import bidviz_plan as plan
    return plan.curate_tables(item)


def content_tables_page(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> int:
    """内容要点页：工程报表式。把本节提炼为工序/参数/风险/资源表格填入两栏；返回页数。"""
    bv.register_fonts()
    col_w = (PAGE_W - MARGIN * 2 - COL_GAP) / 2

    def decorate(sub_page: int) -> None:
        data_page_header(sheet, item, page_no + sub_page - 1, total)
        footer(sheet, item, page_no + sub_page - 1, total)

    decorate(1)
    flow = Flow(sheet, data_regions(), on_new_page=decorate)
    for caption, headers, rows, ratios in content_tables(item):
        flow.caption(caption)
        widths = [max(24.0, col_w * r) for r in ratios]
        spec = bv.TableSpec(headers, rows, widths, header_bg=TABLE_ACCENT, font_size=8.4)
        flow.table(spec)
        flow.spacer(5)
    return flow.pages_used


def data_page_header(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> None:
    sheet.rect(0, 0, PAGE_W, HEAD_H, fill=DARK)
    sheet.text(MARGIN, 3.2 * MM, _cfg().header_text, 8.5, False, "#7ea6c2")
    sheet.text(MARGIN, 8.8 * MM, _crumb(item), 13, True, "white", max_w=PAGE_W - MARGIN * 2 - 160)
    sheet.text(PAGE_W - MARGIN, 6 * MM, item.work_id, 11, True, CYAN, align="right")
    sheet.text(PAGE_W - MARGIN, 12.5 * MM, "内容要点", 7.5, False, "#8fb3cc", align="right")


def footer(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> None:
    y = PAGE_H - FOOT_H
    sheet.line(MARGIN, y, PAGE_W - MARGIN, y, BORDER, 0.6)
    src = ",".join(str(x) for x in sorted(set(item.source_pages)))
    sheet.text(MARGIN, y + 2.5 * MM, f"对应原文档页码：{src}", 7, False, MUTED)
    page_label = f"第 {page_no} / {total} 页" if total else f"第 {page_no} 页"
    sheet.text(PAGE_W - MARGIN, y + 2.5 * MM, page_label, 7.5, True, ACCENT, align="right")


@dataclass
class Section:
    """一个节：1 页视觉页 + 该节下各分项的数据页。"""
    section_id: str
    title: str
    chapter: str
    items: list[WorkItem] = field(default_factory=list)

    @property
    def blocks(self) -> list[Any]:
        return [b for it in self.items for b in it.blocks]

    @property
    def chars(self) -> int:
        return sum(it.chars for it in self.items)

    @property
    def source_pages(self) -> list[int]:
        return sorted({b.source_page for b in self.blocks})

    def as_visual_item(self) -> WorkItem:
        """把整节合成一个伪分项，用于渲染视觉页。"""
        return WorkItem(self.section_id, self.title, self.chapter, self.blocks, self.source_pages)


def group_sections(blocks: Sequence[Any], min_chars: int = 220) -> list[Section]:
    """按「节」分组；每节下挂各分项。"""
    items = group_work_items(blocks, min_chars)
    sections: list[Section] = []
    cur: Section | None = None
    seq = 0
    for it in items:
        title = it.section_path.split(" / ")[-1] if it.section_path else "总述"
        if cur is None or cur.title != title:
            seq += 1
            cur = Section(f"S{seq:03d}", title, it.section_path, [])
            sections.append(cur)
        cur.items.append(it)
    return sections


# ---------------- 7 种工程图像风格与自动路由 ----------------
STYLES: dict[str, dict[str, str]] = {
    "bim_realistic": {
        "name": "三维写实 BIM / 工况渲染",
        "scene": "写实三维 BIM 施工场景轴测图，PBR 材质，真实光影，机械与人员处于作业状态",
        "keywords": "开挖 浇筑 砌筑 安装 敷设 回填 吊装 工井 排管 沟槽 电缆沟 支架 现场",
    },
    "cad_wireframe": {
        "name": "CAD 工程线框 / 断面制图",
        "scene": "工程制图风格断面图，白底深蓝细线，分层构造、标高线、尺寸引线与公差标注",
        "keywords": "断面 剖面 标高 尺寸 厚度 垫层 基础 构造 坡度 间距 大样",
    },
    "exploded_assembly": {
        "name": "轴测爆炸分解 / 装配结构",
        "scene": "轴测爆炸分解图，构件按施工层级拆开悬浮，表现分层工艺与装配关系",
        "keywords": "分层 构造 装配 拼装 层 多层 套 剥切 接头 附件 盖板 压顶",
    },
    "gis_satellite": {
        "name": "GIS 实景 / 总平面叠加",
        "scene": "航拍场地俯视图，叠加工程红线、线路走向、围挡与运输路线箭头",
        "keywords": "总平面 平面布置 位置 走向 路线 道路 周边 临建 营地 交通 出入口",
    },
    "photo_realistic": {
        "name": "现场摄影级写实",
        "scene": "真实施工现场摄影质感，强调构件表面、材质纹理与完成观感",
        "keywords": "成品 观感 外观 质量 验收 实景 效果 平整度 色泽 标识",
    },
    "vector_infographic": {
        "name": "现代矢量图解",
        "scene": "现代扁平矢量信息图，清晰流程方框与层级图标，用于体系与制度表达",
        "keywords": "体系 制度 组织 架构 流程 管理 职责 培训 交底 应急 预案",
    },
    "sketch_technical": {
        "name": "手绘工法交底",
        "scene": "工程网格纸手绘技术草图，重点部位红色圈注与箭头批注",
        "keywords": "交底 要点 难点 重点 措施 控制 注意 特殊 临时",
    },
}


def pick_style(item: WorkItem) -> str:
    text = item.title + " " + "".join(b.text for b in item.blocks[:40] if b.kind != "heading")
    best, best_score = "bim_realistic", 0
    for key, meta in STYLES.items():
        score = sum(text.count(w) for w in meta["keywords"].split())
        if score > best_score:
            best, best_score = key, score
    return best


_NUM_PREFIX = re.compile(
    r"^\s*(?:[\u4e00-\u9fa5]{1,3}[、.．]\s*|（[\u4e00-\u9fa5]{1,3}）\s*|\([\u4e00-\u9fa5]{1,3}\)\s*|\d+[、.．)]\s*|\d+\s+)"
)


def strip_numbering(s: str) -> str:
    """去掉「一、」「（二）」「3.」这类编号前缀，避免模型把它们当标题画进图里。"""
    prev = None
    while prev != s:
        prev = s
        s = _NUM_PREFIX.sub("", s).strip()
    return s


def key_objects(item: WorkItem, limit: int = 16) -> list[str]:
    """从该节原文里抽出「画面主体」：优先三级标题，它们是原文自己的工序/构件名。"""
    heads = [strip_numbering(b.text) for b in item.blocks if b.kind == "heading"]
    heads = [h for h in heads if 2 <= len(h) <= 20 and h != item.title]
    if len(heads) >= 4:
        return heads[:limit]
    # 退化：从正文抽高频技术名词
    text = "".join(b.text for b in item.blocks if b.kind != "heading")
    cand: dict[str, int] = {}
    for m in re.finditer(r"[\u4e00-\u9fa5]{2,6}", text):
        w = m.group()
        if any(k in w for k in ("施工", "验收", "质量", "安全", "控制", "作业", "检查")):
            cand[w] = cand.get(w, 0) + 1
    top = sorted(cand.items(), key=lambda kv: -kv[1])[:limit]
    return [w for w, _ in top] or heads[:limit]


def visual_prompt(item: WorkItem, style: str | None = None, max_source_chars: int = 2600) -> str:
    """生成提示词。图中中文标注必须逐字取自原文，不允许模型自创词汇。"""
    style = style or pick_style(item)
    meta = STYLES[style]
    body = "；".join(b.text for b in item.blocks if b.kind != "heading")[:max_source_chars]
    objs = key_objects(item)
    obj_line = "、".join(objs) if objs else "（见下方原文）"
    return "\n".join([
        f"为{_cfg().prompt_subject}生成：{meta['scene']}。",
        f"本页主题：{item.title}。",
        f"【画面应表现的工程场景】{obj_line}",
        "只画这些场景本身，绝对不要把这些短语写成图中的标题、栏目名、分区标签或说明文字。",
        "画面主体必须与上述主题直接相关，不要画无关的通用工地全景。",
        "",
        "【图中所有中文标注必须逐字取自以下原文，不得使用原文之外的任何词汇】",
        body,
        "",
        "【标注要求】",
        "尽量多标：不少于 14 个、最多 20 个标注，每个 2~8 个字。",
        "优先选取原文中的构件名、工序名、材料名、设备名、部位名。",
        "标注用引线指向对应构件，字号清晰可读，分布均匀，不要挤在一起。",
        "不要写长句、不要写规范编号、不要编造原文里没有的内容。",
        "",
        "【禁止绘制】不要标题栏、不要页眉页脚、不要侧边栏、不要页码、不要水印、不要 Logo、不要图框。",
        "画面只有工程主体与引线标注，四周自然留白。横向构图，宽高比约 3:2。",
    ])


# ---------------- 结构化抽取 ----------------
PARAM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|km|%|天|d|h|min|s|级|次|组|座|孔|kV|V|A|MPa|kN|μm|kg|t|人|道|处|倍|度|℃)")
RISK_RE = re.compile(r"严禁|不得|禁止|防止|避免|注意|危险|事故|消防|隐患|破坏|塌方|裂缝|渗漏|空鼓|超挖|触电|坠落")
STEP_WORDS = ("准备", "勘察", "测量", "放线", "开挖", "垫层", "砌筑", "支架", "接地", "敷设", "试验",
              "安装", "浇筑", "回填", "养护", "验收", "封堵", "制作", "调试", "检查")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[。；;！!\n]", text) if len(s.strip()) > 4]


def extract_params(item: WorkItem) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen = set()
    for b in item.blocks:
        if b.kind == "heading":
            continue
        for s in split_sentences(b.text):
            if PARAM_RE.search(s):
                parts = re.split(r"[，,：:]", s)
                name = parts[0][:16] if len(parts) >= 2 else s[:14]
                val = "，".join(parts[1:])[:60] if len(parts) >= 2 else s[14:74]
                if name in seen:
                    continue
                seen.add(name)
                out.append((name, val))
    return out


def extract_risks(item: WorkItem) -> list[str]:
    out, seen = [], set()
    for b in item.blocks:
        if b.kind == "heading":
            continue
        for s in split_sentences(b.text):
            if RISK_RE.search(s) and s[:14] not in seen:
                seen.add(s[:14])
                out.append(s[:80])
    return out


def extract_steps(item: WorkItem) -> list[tuple[str, str]]:
    sents = [s for b in item.blocks if b.kind != "heading" for s in split_sentences(b.text)]
    picked = [s for s in sents if any(w in s for w in STEP_WORDS)]
    if len(picked) < 5:
        picked = sents
    steps = []
    for i in range(5):
        title = f"{i+1:02d} {['准备与勘察','基础与开挖','主体施工','配套与防护','验收与养护'][i]}"
        desc = picked[i][:70] if i < len(picked) else "严格执行施工组织设计与国家现行规范标准，强化工序协同管理。"
        steps.append((title, desc))
    return steps
