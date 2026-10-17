#!/usr/bin/env python3
"""bidviz_layout —— 施工组织设计十维度体系：分类、分组、行映射。

供 text_pages 结构化排版使用。红线（覆盖率不变量）：
- 本模块产出的所有"新增文字"（维度横幅、导览、互指页码）只能由调用方排在
  原文块之前或整块之后的独立行上；
- 块内细节强调只用无文字图形（色轨、底纹、下划线）；
- split_list_items / line_map 均保持原文字符顺序与内容，不做任何改写。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from bidviz_vector import text_width, tokenize

# ---------------------------------------------------------------- 十维度

@dataclass(frozen=True)
class Dim:
    key: str
    name: str       # 维度名（印在横幅上）
    glyph: str      # 单字徽（页眉/徽章）
    color: str      # 主色（色轨/横幅/下划线）
    tint: str       # 浅底（横幅底/句级底纹）
    pattern: str    # 检测信号（正则源码）


_DIM_LIST: list[Dim] = [
    Dim("overview", "概况依据", "概", "#607d8b", "#eceff1",
        r"编制依据|工程概况|编制说明|规范|标准|图集|设计文件|招标文件|地质|气象|建设地点|建设规模"),
    Dim("org", "部署组织", "组", "#5c6bc0", "#e8eaf6",
        r"组织机构|岗位职责|项目经理|项目总工|技术负责人|施工员|质检员|安全员|资料员|材料员|班组长|"
        r"职责分工|管理人员|劳动力组织|部署"),
    Dim("quality", "质量验收", "质", "#43a047", "#e8f5e9",
        r"验收|检验|试验|实测|允许偏差|合格|质量标准|质量控制|旁站|复检|抽样"),
    Dim("process", "方案工艺", "工", "#00897b", "#e0f2f1",
        r"施工工艺|施工方案|工序|工法|技术交底|操作要点|施工方法|开挖|垫层|砌筑|支护|浇筑|敷设|"
        r"安装|回填|养护|封堵|支架|接地|制作|调试|敷设|测量|放线"),
    Dim("time", "进度工期", "进", "#f57c00", "#fff3e0",
        r"工期|进度计划|总工期|日历天|工作日|阶段划分|穿插|关键线路|里程碑|开工|竣工"),
    Dim("resource", "资源配置", "资", "#8d6e63", "#efebe9",
        r"劳动力|人力资源|机械设备|主要机具|周转材料|材料计划|设备投入|资源配置|投入计划|用量"),
    Dim("layout", "平面运输", "平", "#26a69a", "#e0f2f1",
        r"总平面|平面布置|堆场|加工场|临时道路|运输|交通组织|围挡|临建|出入口|场地布置"),
    Dim("safety", "安全风险", "安", "#e53935", "#ffebee",
        r"安全|风险|危险|防护|事故|隐患|坍塌|触电|高空作业|吊装|有限空间|安全帽|安全带"),
    Dim("civil", "文明季节", "文", "#7cb342", "#f1f8e9",
        r"文明施工|环境保护|环保|扬尘|噪声|废水|建筑垃圾|冬季施工|雨季施工|冬雨季|夏季施工"),
    Dim("emergency", "应急保护", "应", "#d81b60", "#fce4ec",
        r"应急预案|应急响应|应急演练|成品保护|抢修|抢险|救援|应急物资"),
]

DIMS: dict[str, Dim] = {d.key: d for d in _DIM_LIST}
DIM_ORDER: list[str] = [d.key for d in _DIM_LIST]
_RES: dict[str, re.Pattern[str]] = {k: re.compile(d.pattern) for k, d in DIMS.items()}

# 图型 → 主维度：图版页徽章用它标注"这张图吸收了什么"
KIND_DIM: dict[str, str] = {
    "hero_scene": "process", "process_flow": "process", "detail_section": "quality",
    "assembly": "resource", "layout_plan": "layout", "site_plan": "layout",
    "org_duty": "org", "mgmt_system": "org", "progress": "time",
    "risk_point": "safety", "qa_flow": "quality",
}


def get_dim(key: str | None) -> Dim | None:
    return DIMS.get(key) if key else None


def classify_para(text: str) -> tuple[str | None, dict[str, int]]:
    """段落维度归类：argmax 且得分达标才归类，否则 None（叙述流，不挂维度）。

    达标规则：最高分 >= 2，或全段只命中 1 次且恰好属于该维度。
    低置信不归类，避免把正文过度切碎。
    """
    scores = {k: len(r.findall(text)) for k, r in _RES.items()}
    total = sum(scores.values())
    if not total:
        return None, scores
    key = max(scores, key=lambda k: (scores[k], -DIM_ORDER.index(k)))
    if scores[key] >= 2 or (scores[key] == 1 and total == 1):
        return key, scores
    return None, scores


# ---------------------------------------------------------------- 块分组

@dataclass
class Unit:
    """一个渲染单元：heading=标题块；group=连续同维度段落；list=列表段。"""
    kind: str                     # heading | group | list
    dim: str | None
    banner: bool                  # group 是否出横幅（>=2 段或 >=240 字）
    blocks: list[Any]


def group_blocks(blocks: Sequence[Any]) -> list[Unit]:
    """把节内块整理成渲染单元：标题独立；连续同维度段落合并为组。"""
    units: list[Unit] = []
    buf: list[Any] = []
    buf_dim: str | None = None

    def flush() -> None:
        nonlocal buf, buf_dim
        if buf:
            chars = sum(len(getattr(b, "text", "")) for b in buf)
            units.append(Unit("group", buf_dim, len(buf) >= 2 or chars >= 240, buf))
            buf, buf_dim = [], None

    for b in blocks:
        text = getattr(b, "text", "").strip()
        if not text:
            continue
        if getattr(b, "kind", "body") == "heading":
            flush()
            units.append(Unit("heading", None, False, [b]))
        elif is_list_para(text):
            flush()
            units.append(Unit("list", classify_para(text)[0], False, [b]))
        else:
            dim = classify_para(text)[0]
            if buf and dim != buf_dim:
                flush()
            buf_dim = dim
            buf.append(b)
    flush()
    return units


# ---------------------------------------------------------------- 列表识别

LIST_MARK_RE = re.compile(r"(\d{1,2}[）)]|[①-⑩]|（\d{1,2}）)")
_LIST_LEAD_RE = re.compile(r"[。；：:；]\s*$")


def is_list_para(text: str) -> bool:
    """段首即为列表编号、且切出 >=2 项的段落才按列表渲染。"""
    t = text.strip()
    if not LIST_MARK_RE.match(t):
        return False
    return len(split_list_items(t)) >= 2


def split_list_items(text: str) -> list[tuple[str, str]]:
    """按 1）/①/（1）切列表项，返回 [(mark, item_text)]。

    mark 可为空（引导句）。拼接还原即原文：mark + item == 原片段，
    字符顺序与内容不变，覆盖率审计安全。
    仅当编号紧跟句读/引导句之后才切分，避免把 "C30)" 这类文本误切。
    """
    parts = LIST_MARK_RE.split(text.strip())
    items: list[tuple[str, str]] = []
    lead = parts[0].strip()
    if lead:
        items.append(("", lead))
    i = 1
    while i < len(parts) - 1:
        mark, seg = parts[i], parts[i + 1]
        prev = items[-1][1] if items else ""
        if prev and not _LIST_LEAD_RE.search(prev) and items[-1][0] == "":
            items[-1] = ("", prev + mark + seg)      # 编号出现在句中：不切
        else:
            items.append((mark, seg.strip()))
        i += 2
    return items


# ---------------------------------------------------------------- 行映射

def line_map(text: str, size: float, max_w: float, bold: bool = False) -> tuple[list[str], list[int]]:
    """折行并给出每行首字符在原文中的偏移。折行逻辑与 bidviz_vector.wrap 逐字一致。"""
    lines: list[str] = []
    starts: list[int] = []
    pos = 0
    for para in text.split("\n"):
        para_start = pos
        stripped = para.strip()
        pos += len(para) + 1
        if not stripped:
            lines.append("")
            starts.append(para_start)
            continue
        base = para_start + (len(para) - len(para.lstrip()))
        line = ""
        off = 0
        for tok in tokenize(stripped):
            candidate = line + tok
            if line and text_width(candidate, size, bold) > max_w:
                lines.append(line.rstrip())
                starts.append(base + off)
                if tok.isspace():
                    off += len(tok) - len(tok.lstrip())
                    line = tok.lstrip()
                else:
                    line = tok
            else:
                line = candidate
                off += len(tok)
        lines.append(line.rstrip())
        starts.append(base + off)
    return (lines or [""]), (starts or [0])


def spans(lines: Sequence[str], starts: Sequence[int], marks: Sequence[tuple[int, int]],
          size: float, bold: bool = False) -> dict[int, list[tuple[float, float]]]:
    """把原文字符区间映射为每行的 (x 偏移, 宽)，供下划线/底纹定位。

    x 偏移按 token 实测宽度累加（与 wrap 折行同一口径），token 内按字符均摊。
    """
    out: dict[int, list[tuple[float, float]]] = {}
    if not marks:
        return out
    for i, ln in enumerate(lines):
        s, e = starts[i], starts[i] + len(ln)
        hits = [(max(a, s), min(b, e)) for a, b in marks if min(b, e) > max(a, s)]
        if not hits:
            continue
        # char_x[k] = 行内第 k 个字符相对行首的 x 偏移（pt）
        char_x = [0.0] * (len(ln) + 1)
        cx, px = 0, 0.0
        for tok in tokenize(ln):
            tw = text_width(tok, size, bold)
            for k in range(len(tok)):
                char_x[cx + k] = px + tw * k / len(tok)
            px += tw
            cx += len(tok)
        char_x[len(ln)] = px
        for a, b in hits:
            xa, xb = char_x[a - s], char_x[b - s]
            if xb > xa:
                out.setdefault(i, []).append((xa, xb - xa))
    return out


# ---------------------------------------------------------------- 句级标记

PARAM_SENT_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|km|%|天|d|h|min|s|级|次|组|座|孔|kV|V|A|MPa|kN|μm|kg|t|人|道|处|倍|度|℃)")
RISK_SENT_RE = re.compile(r"严禁|不得|禁止|防止|避免|注意|危险|事故|消防|隐患|破坏|塌方|坍塌|裂缝|渗漏|空鼓|超挖|触电|坠落")
QA_SENT_RE = re.compile(r"验收|检验|试验|实测|允许偏差|合格|质量标准|旁站|复检")


def sentence_marks(text: str) -> list[tuple[str, int, int]]:
    """按句切分并标注细节类型，返回 [(kind, start, end)]，优先级 risk > qa > param。

    kind ∈ risk（浅红底纹+左tick）/ qa（浅绿底纹）/ param（维度色下划线）。
    """
    marks: list[tuple[str, int, int]] = []
    for m in re.finditer(r"[^。；;！!？?\n]+", text):
        s = m.group().strip()
        if len(s) <= 4:
            continue
        a, b = m.start(), m.end()
        if RISK_SENT_RE.search(s):
            marks.append(("risk", a, b))
        elif QA_SENT_RE.search(s):
            marks.append(("qa", a, b))
        elif PARAM_SENT_RE.search(s):
            marks.append(("param", a, b))
    return marks


# ---------------------------------------------------------------- 结构导览

H3_RE = re.compile(r"^[一二三四五六七八九十]+、")
H4_RE = re.compile(r"^（[一二三四五六七八九十\d]{1,3}）")


def outline_of(item: Any) -> list[tuple[int, str]]:
    """提取本节 一、/（一） 级标题做结构导览，返回 [(level, text)]（level 3/4）。"""
    out: list[tuple[int, str]] = []
    for b in getattr(item, "blocks", []):
        if getattr(b, "kind", "body") != "heading":
            continue
        t = getattr(b, "text", "").strip()
        if H3_RE.match(t):
            out.append((3, t))
        elif H4_RE.match(t):
            out.append((4, t))
    return out


def dim_profile(item: Any, top: int = 5) -> list[tuple[str, int]]:
    """本节维度画像：各维度命中的正文段数，降序取 top。供徽章/图版页/Web。"""
    counts: dict[str, int] = {}
    for b in getattr(item, "blocks", []):
        if getattr(b, "kind", "body") == "heading":
            continue
        t = getattr(b, "text", "").strip()
        if not t:
            continue
        key = classify_para(t)[0]
        if key:
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], DIM_ORDER.index(kv[0])))[:top]
