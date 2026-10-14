#!/usr/bin/env python3
"""E1 双页型编译器：视觉页（大图+节点图群+要点栏）/ 数据页（原文逐字两栏+表格跨页）。

输出：矢量 PDF，仅图片为位图。文字可选中、可搜索，且可反抽校验覆盖率。
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


def group_work_items(blocks: Sequence[Any], min_chars: int = 220) -> list[WorkItem]:
    """按三级标题（一、二、三…）切分项；过短的合并到前一项，避免碎页。"""
    items: list[WorkItem] = []
    cur: WorkItem | None = None
    section = ""
    seq = 0

    for b in blocks:
        t = b.text
        if b.kind == "heading":
            if re.match(r"^第[一二三四五六七八九十百\d]+[章节]", t):
                section = t
            is_item = bool(re.match(r"^[一二三四五六七八九十]+、", t))
            if is_item:
                if cur and cur.chars >= min_chars:
                    items.append(cur)
                    cur = None
                if cur is None:
                    seq += 1
                    cur = WorkItem(f"W{seq:03d}", t, section, [], [])
                    cur.blocks.append(b)
                    cur.source_pages.append(b.source_page)
                    continue
        if cur is None:
            seq += 1
            cur = WorkItem(f"W{seq:03d}", section or "总述", section, [], [])
        cur.blocks.append(b)
        cur.source_pages.append(b.source_page)

    if cur:
        items.append(cur)
    return items


class Flow:
    """按阅读顺序在若干区域间流动排版：区域放满就换下一个，用完就换页。"""

    def __init__(self, sheet: bv.Sheet, regions: Sequence[tuple[float, float, float, float]],
                 on_new_page=None):
        self.sheet = sheet
        self.regions = list(regions)
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


# ---------------- 视觉页 ----------------
def visual_page(sheet: bv.Sheet, item: WorkItem, hero: Path | None,
                params: list[tuple[str, str]], risks: list[str], steps: list[tuple[str, str]],
                page_no: int, total: int, root: Path) -> None:
    """视觉页：主视觉按 1.5:1 占满左侧宽度，下方工序卡，右栏参数与风险。"""
    bv.register_fonts()

    # 顶栏
    sheet.rect(0, 0, PAGE_W, HEAD_H, fill=DARK)
    sheet.text(MARGIN, 3.2 * MM, _cfg().header_text, 8.5, False, "#7ea6c2")
    sheet.text(MARGIN, 8.6 * MM, item.title, 15, True, "white", max_w=PAGE_W - MARGIN * 2 - 120)
    sheet.text(PAGE_W - MARGIN, 6 * MM, f"{item.work_id}  视觉页", 11, True, CYAN, align="right")
    sheet.text(PAGE_W - MARGIN, 12.5 * MM, item.section_path[:46], 7.5, False, "#8fb3cc", align="right")

    top = HEAD_H + 6 * MM
    left_w = (PAGE_W - MARGIN * 2 - COL_GAP) * 0.62
    right_x = MARGIN + left_w + COL_GAP
    right_w = PAGE_W - MARGIN - right_x

    # 主视觉：高度按 1.5:1 算，正好匹配 gpt-image-2.5 的 1536x1024
    hero_h = left_w / 1.5
    sheet.rect(MARGIN, top, left_w, hero_h, fill="white", stroke=BORDER, lw=0.8)
    if hero and hero.exists():
        sheet.image(hero, MARGIN + 2, top + 2, left_w - 4, hero_h - 4, dpi=170,
                    cache_dir=root / "work/_imgcache", fit="cover")
    else:
        sheet.rect(MARGIN + 2, top + 2, left_w - 4, hero_h - 4, fill="#eef3f6")
        sheet.text(MARGIN + left_w / 2, top + hero_h / 2 - 6,
                   "主视觉待生成（运行 generate 后自动替换）", 11, False, "#7b909c", align="center")
    sheet.rect(MARGIN, top, left_w, 6 * MM, fill="#1a3d59")
    sheet.text(MARGIN + 6, top + 1.6 * MM, f"图 1.1  {item.title} · BIM 施工工况全景", 9, True, "white",
               max_w=left_w - 12)

    # 下方：工程标准工艺流程图（替代原工序卡与旧摘录）
    import bidviz_charts as bc
    flow_top = top + hero_h + 4.5 * MM
    flow_h = BODY_BOTTOM - flow_top
    if flow_h > 20 * MM:
        bc.draw_flowchart(sheet, MARGIN, flow_top, left_w, flow_h, steps)

    # 右栏：自适应参数表与 5x5 风险控制矩阵
    # 顶部：关键工艺参数
    ry = top
    p_box_h = 75 * MM
    bc.draw_section_panel(sheet, right_x, ry, right_w, p_box_h, "关键工艺参数与技术控制指标", "实测实量与容许偏差")
    table_y = ry + 6.5 * MM
    for name, val in params[:6]:
        hh = 10.2 * MM
        if table_y + hh > ry + p_box_h - 2 * MM:
            break
        sheet.rect(right_x + 3 * MM, table_y, right_w - 6 * MM, hh, fill="white", stroke=BORDER, lw=0.4)
        sheet.text(right_x + 4.5 * MM, table_y + 2.0 * MM, name, 7.6, True, DARK, max_w=right_w * 0.38)
        sheet.para(right_x + right_w * 0.42, table_y + 1.8 * MM, val, right_w * 0.54 - 6 * MM,
                   size=7.0, leading=8.8, color=TEXT, max_lines=2)
        table_y += hh

    # 底部：5x5 风险矩阵
    risk_top = ry + p_box_h + 4.5 * MM
    risk_h = BODY_BOTTOM - risk_top
    if risk_h > 25 * MM:
        scored_risks = bc.extract_risks_scored(item.blocks)
        bc.draw_risk_matrix(sheet, right_x, risk_top, right_w, risk_h, scored_risks)

    footer(sheet, item, page_no, total)


def data_page_header(sheet: bv.Sheet, item: WorkItem, sub_page: int, page_no: int, total: int) -> None:
    sheet.rect(0, 0, PAGE_W, HEAD_H, fill=DARK)
    sheet.text(MARGIN, 3.2 * MM, _cfg().header_text, 8.5, False, "#7ea6c2")
    sheet.text(MARGIN, 8.6 * MM, item.title + (f"（续 {sub_page}）" if sub_page > 1 else ""),
               14, True, "white", max_w=PAGE_W - MARGIN * 2 - 160)
    sheet.text(PAGE_W - MARGIN, 6 * MM, f"{item.work_id}  工程控制图表 ({sub_page})", 11, True, CYAN, align="right")
    sheet.text(PAGE_W - MARGIN, 12.5 * MM, item.section_path[:46], 7.5, False, "#8fb3cc", align="right")


def text_pages(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> int:
    """把本节完整正文排成可搜索、可复制的矢量文字页；返回占用页数。"""
    bv.register_fonts()

    def decorate(sub_page: int) -> None:
        data_page_header(sheet, item, sub_page, page_no + sub_page - 1, total)
        footer(sheet, item, page_no + sub_page - 1, total)

    decorate(1)
    flow = Flow(sheet, data_regions(), on_new_page=decorate)
    for block in item.blocks:
        text = getattr(block, "text", "").strip()
        if not text:
            continue
        if getattr(block, "kind", "body") == "heading":
            if re.match(r"^第[一二三四五六七八九十百\d]+章", text):
                level = 1
            elif re.match(r"^第[一二三四五六七八九十百\d]+节", text):
                level = 2
            else:
                level = 3
            flow.heading(text, level)
        else:
            flow.paragraph(text)
            flow.spacer(2)
    return flow.pages_used


def chart_page(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> None:
    """数据图表页：全矢量图表驱动，替代旧散文文字页。
    包含：进度横道图 (Gantt)、施工管理 RACI 矩阵、材料设备控制表、ITP 检验试验计划表。
    """
    bv.register_fonts()
    import bidviz_charts as bc

    # 顶栏
    sheet.rect(0, 0, PAGE_W, HEAD_H, fill=DARK)
    sheet.text(MARGIN, 3.2 * MM, _cfg().header_text, 8.5, False, "#7ea6c2")
    sheet.text(MARGIN, 8.6 * MM, f"{item.title} · 工程管理控制图表", 15, True, "white", max_w=PAGE_W - MARGIN * 2 - 120)
    sheet.text(PAGE_W - MARGIN, 6 * MM, f"{item.work_id}  图表页", 11, True, CYAN, align="right")
    sheet.text(PAGE_W - MARGIN, 12.5 * MM, item.section_path[:46], 7.5, False, "#8fb3cc", align="right")

    top = HEAD_H + 6 * MM
    avail_w = PAGE_W - MARGIN * 2
    half_w = (avail_w - COL_GAP) / 2.0
    half_h = (BODY_BOTTOM - top - 5 * MM) / 2.0

    # 1. 左上：进度横道图
    tasks = bc.extract_gantt_tasks(item.blocks)
    bc.draw_gantt_chart(sheet, MARGIN, top, half_w, half_h, tasks)

    # 2. 右上：RACI 职责矩阵
    roles, raci_tasks, matrix = bc.extract_raci_data(item.blocks)
    bc.draw_raci_matrix(sheet, MARGIN + half_w + COL_GAP, top, half_w, half_h, roles, raci_tasks, matrix)

    # 3. 左下：材料设备标准表
    bot_y = top + half_h + 5 * MM
    mat_rows = bc.extract_materials(item.blocks)
    bc.draw_section_panel(sheet, MARGIN, bot_y, half_w, half_h, "主要施工材料与机械设备进场检验表", "抽检频次与复验")
    if mat_rows:
        spec = bc.build_material_table_spec(mat_rows[:7], half_w - 6 * MM)
        sheet.table(spec, MARGIN + 3 * MM, bot_y + 6.5 * MM, max_h=half_h - 8 * MM)
    else:
        sheet.text(MARGIN + half_w / 2, bot_y + half_h / 2, "本分项未涉及特殊材料或采用标准甲供设备", 8.0, False, MUTED, align="center")

    # 4. 右下：ITP 检验试验计划
    itp_rows = extract_itp(item)
    bc.draw_section_panel(sheet, MARGIN + half_w + COL_GAP, bot_y, half_w, half_h, "质量控制点与 ITP 检验试验计划表", "H=停工待检 / W=见证 / S=旁站")
    if itp_rows:
        itp_spec = build_itp_table(itp_rows[:7])
        itp_spec.col_w = [(half_w - 6 * MM) * 0.32, (half_w - 6 * MM) * 0.44, (half_w - 6 * MM) * 0.24]
        sheet.table(itp_spec, MARGIN + half_w + COL_GAP + 3 * MM, bot_y + 6.5 * MM, max_h=half_h - 8 * MM)
    else:
        sheet.text(MARGIN + half_w + COL_GAP + half_w / 2, bot_y + half_h / 2, _cfg().standard_text, 8.0, False, MUTED, align="center")

    footer(sheet, item, page_no, total)


def footer(sheet: bv.Sheet, item: WorkItem, page_no: int, total: int) -> None:
    y = PAGE_H - FOOT_H
    sheet.line(MARGIN, y, PAGE_W - MARGIN, y, BORDER, 0.6)
    sheet.text(MARGIN, y + 2.5 * MM, "BIM 数字化施工交付体系 · 矢量排版 · 文字可检索可选中", 7, False, MUTED)
    src = ",".join(str(x) for x in sorted(set(item.source_pages)))
    sheet.text(PAGE_W / 2, y + 2.5 * MM, f"对应原文档页码：{src}", 7, False, MUTED, align="center")
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


# ---------------- 表格构造 ----------------
def build_param_table(rows: list[tuple[str, str]]) -> bv.TableSpec:
    body = []
    for name, val in rows:
        strict = any(k in val for k in ("严禁", "必须", "≥", "不小于", "不得"))
        body.append([
            bv.Cell(name, bold=True, size=9.0),
            bv.Cell(val, size=9.0),
            bv.Cell(pill=("强制主控", "#ffebee", "#c62828") if strict else ("技术参数", "#e0f2f1", "#00695c")),
        ])
    return bv.TableSpec(
        headers=["控制分项", "规范与设计控制参数", "控制等级"],
        rows=body,
        col_w=[150, 420, 100],
        header_bg=HEADER_BG, font_size=9.0, pad=5,
    )


def build_risk_table(rows: list[tuple[str, str, str]]) -> bv.TableSpec:
    body = [[bv.Cell(a, bold=True, size=9.0), bv.Cell(b, size=9.0), bv.Cell(c, size=9.0),
             bv.Cell(pill=("总工/监理", "#ffebee", "#c62828"))] for a, b, c in rows]
    return bv.TableSpec(
        headers=["风险与通病", "诱因分析与征兆", "预防及纠偏处置对策", "处置权限"],
        rows=body,
        col_w=[140, 210, 240, 80],
        header_bg=RED_BG, font_size=9.0, pad=5,
    )


def build_itp_table(rows: list[tuple[str, str]]) -> bv.TableSpec:
    body = [[bv.Cell(a, bold=True, size=9.0), bv.Cell(b, size=9.0),
             bv.Cell(pill=("停检点(H)", "#ffebee", "#c62828"))] for a, b in rows]
    return bv.TableSpec(
        headers=["检验与试验节点", "报验与见证要求", "类型"],
        rows=body,
        col_w=[170, 400, 100],
        header_bg=HEADER_BG, font_size=9.0, pad=5,
    )


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


def extract_itp(item: WorkItem) -> list[tuple[str, str]]:
    return [
        ("工序① 技术交底与图纸会审", "开工前完成专业工种技术与安全交底，签署交底记录并报监理备案。"),
        ("工序② 进场物资与隐蔽报验", "主要材料须有出厂合格证并完成复验；隐检合格后方可进入下道工序。"),
        ("工序③ 关键工序过程见证", "关键节点设停检点(H)，由项目总工、质检员与专业监理现场旁站见证。"),
        ("工序④ 检验批与实体核验", "按设计规范逐段测量标高、平整度与外观，签署验收资料归档。"),
    ]
