#!/usr/bin/env python3
"""bidviz_plan —— 每节图位规划：先分析内容，再定画几张、每张画什么。

四条设计决策（与项目事实边界一致）：
  1. 表格级内容（参数表/材料表/检验计划）不进图，由矢量正文承载 —— 零错字、可反抽。
  2. 每节 1~2 张主图，都是主图级，没有主/辅之分；张数由内容体量与维度决定。
  3. 编造类图表一律不出图：RACI 职责矩阵（0% 原文依据）、程序兜底风险条目、
     推算出来的工期天数。
  4. 规划先落盘 work/figure-plan.json，人工可审可改，确认后才花 API 钱生图。

本模块不做渲染、不发网络请求：只分析内容、产出规划、构建提示词、定位图像。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from bidviz_config import get as _cfg
import bidviz_layout as layout

PLAN_VERSION = 2
PLAN_NAME = "figure-plan.json"


# ---------------------------------------------------------------- 图型库

@dataclass(frozen=True)
class Kind:
    key: str
    name: str          # 图题，印在题注条上
    scene: str         # 画面语汇（提示词主体）
    focus: str         # 这张图要吸收/表现什么
    style: str = ""    # 复用 e1.STYLES 的风格键；留空则按内容自动路由
    ban: str = ""      # 该图型特有的禁止项
    ratio: float = 1.414


KINDS: dict[str, Kind] = {k.key: k for k in [
    Kind("hero_scene", "主视觉 · 施工工况全景",
         "写实三维施工场景轴测图，机械与人员处于作业状态，工序按先后关系布置在同一画面",
         "把本节的工序顺序、关键部位、材料设备与风险点位全部以引线标注吸收进同一画面，"
         "一张图讲清这一节怎么干"),
    Kind("process_flow", "工序分解与工艺要点图",
         "横向工艺流程画面，各工序按施工先后排列并用箭头连接，每道工序画出真实作业面",
         "表现工序的先后穿插关系与每道工序的作业要点"),
    Kind("detail_section", "关键部位断面大样与参数标注图",
         "工程制图风格断面大样图，白底深蓝细线，分层构造、标高线、尺寸引线与公差标注",
         "把原文给出的构造层次、厚度、标高、间距、允许偏差标注到断面上"),
    Kind("assembly", "构造分层与材料装配分解图",
         "轴测爆炸分解图，构件按施工层级拆开悬浮，表现分层工艺与装配关系",
         "表现本节主要材料、构件的分层位置与装配顺序"),
    Kind("layout_plan", "总平面布置与运输组织图",
         "航拍场地俯视图，叠加工程红线、作业区分隔、围挡、材料堆场与运输路线箭头",
         "表现作业面分区、临时设施位置、材料堆场与运输路线的组织关系"),
    Kind("site_plan", "场地平面与线路走向图",
         "航拍场地俯视图，叠加线路走向、工井与排管位置、周边既有设施",
         "表现线路走向、构筑物位置与周边环境关系"),
    Kind("org_duty", "组织职责与管控流程图",
         "现代扁平矢量信息图，岗位框与管控流程箭头，层级清晰",
         "只表现原文提到的岗位名称与其管控动作的先后关系"),
    Kind("mgmt_system", "管理体系与制度流程图",
         "现代扁平矢量信息图，体系分层框图与制度执行流程箭头",
         "表现本节管理制度、交底、检查、闭环处置的流程关系"),
    Kind("progress", "阶段工序穿插与进度示意",
         "横向阶段条带示意图，各阶段工序按先后穿插排列，箭头表示推进方向",
         "表现阶段划分与工序穿插关系"),
    Kind("risk_point", "安全风险点位与防护布置图",
         "写实施工现场画面，风险点位用红橙警示圈注与箭头标出，防护设施画到位",
         "只表现原文提到的风险点位与对应防护措施"),
    Kind("qa_flow", "质量控制点与验收流程图",
         "写实施工完成面画面，质量控制点用绿色标记框与引线标出，验收顺序用箭头表示",
         "表现原文提到的质量控制点、实测项目与验收顺序"),
]}

DEFAULT_KIND = "hero_scene"


# ---------------------------------------------------------------- 内容特征

@dataclass
class Features:
    chars: int = 0
    items: int = 0          # 三级分项（一、二、三…）数量
    heads: int = 0
    proc: int = 0           # 原文落地的工序句数
    param: int = 0          # 含实测数值的参数句数
    risk: int = 0           # 原文落地的风险句数
    mat: int = 0            # 材料设备条数
    time: int = 0
    org: int = 0
    safe: int = 0
    qa: int = 0
    layout: int = 0
    style: str = "bim_realistic"
    steps: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    param_sents: list[str] = field(default_factory=list)
    risk_sents: list[str] = field(default_factory=list)
    mat_names: list[str] = field(default_factory=list)


TIME_RE = re.compile(r"工期|天数|进度|计划|阶段|工序穿插|\d+\s*(?:天|日历天|工作日)")
ORG_RE = re.compile(r"项目经理|项目总工|技术负责人|安全员|质检员|施工员|资料员|材料员|班组长|"
                    r"职责|组织机构|岗位|责任人")
SAFE_RE = re.compile(r"安全|风险|危险|事故|隐患|防护|应急|坍塌|触电|高空|吊装|有限空间")
QA_RE = re.compile(r"质量|验收|检验|试验|偏差|允许|合格|控制指标|实测")
LAYOUT_RE = re.compile(r"总平面|布置|场地|临建|围挡|堆场|道路|出入口|运输|交通组织")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _sig(item: Any) -> str:
    """节内容指纹：正文变了就要重算规划。"""
    text = "".join(getattr(b, "text", "") for b in item.blocks)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def analyze(item: Any) -> Features:
    """从一个节（WorkItem）里量出内容特征，作为「画几张、画什么」的依据。

    所有计数只认原文里真出现的句子；程序兜底模板不计入，避免把编造内容画进图。
    """
    import bidviz_e1 as e1
    import bidviz_charts as bc

    f = Features()
    body = [b.text for b in item.blocks if getattr(b, "kind", "body") != "heading"]
    heads = [b.text for b in item.blocks if getattr(b, "kind", "body") == "heading"]
    text = "".join(body)
    f.chars = len(text)
    f.heads = len(heads)
    f.items = sum(1 for h in heads if re.match(r"^[一二三四五六七八九十]+、", h))
    f.style = e1.pick_style(item)

    # 工序：原文里含工艺动词的句子（flow_steps 已过滤掉兜底句）
    f.steps = flow_steps(item, limit=8)
    f.proc = len(f.steps)

    # 参数：只取原文中含实测数值的句子，不做「名称/值」切分（切分不可靠）
    seen = set()
    for b in item.blocks:
        if getattr(b, "kind", "body") == "heading":
            continue
        for s in e1.split_sentences(b.text):
            if e1.PARAM_RE.search(s) and s[:16] not in seen:
                seen.add(s[:16])
                f.param_sents.append(s[:64])
    f.param = len(f.param_sents)

    # 风险：只保留原文里真出现的风险句
    rt = _norm(text)
    for s in e1.extract_risks(item):
        if _norm(s)[:10] in rt:
            f.risk_sents.append(s[:60])
    f.risk = len(f.risk_sents)

    # 材料设备
    try:
        mats = bc.extract_materials(item.blocks)
    except Exception:
        mats = []
    f.mat_names = [m[0] for m in mats if _norm(m[0])[:6] in rt]
    f.mat = len(f.mat_names)

    f.time = len(TIME_RE.findall(text))
    f.org = len(ORG_RE.findall(text))
    f.safe = len(SAFE_RE.findall(text))
    f.qa = len(QA_RE.findall(text))
    f.layout = len(LAYOUT_RE.findall(text))
    f.objects = e1.key_objects(item, limit=14)
    return f


def flow_steps(item: Any, limit: int = 8) -> list[str]:
    """工序序列：只保留原文里真出现过的句子，剔除通用兜底句。"""
    import bidviz_e1 as e1
    text = _norm("".join(getattr(b, "text", "") for b in item.blocks))
    out: list[str] = []
    for _title, desc in e1.extract_steps(item):
        d = desc.strip()
        if not d or _norm(d)[:10] not in text or d in out:
            continue
        out.append(d[:48])
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 内容报表策展

_ORD_RE = re.compile(r"^(\d{1,2}[、.)）]|[①-⑩]|（\d{1,2}）)\s*")
_TRIG_RE = re.compile(r"严禁|不得|禁止|防止|避免|必须|应当|确保|严控|应")
_SEP = "，,、；;：: 　"
_CELL_MAX = 70      # 报表单元格上限（取原文前缀，仍逐字可审计）


def _cut_two(s: str, idx: int, min_left: int = 2, max_left: int = 22) -> tuple[str, str]:
    """只在 idx 之前的干净分隔符处切两列；切不出（或左段过长）则不拆，返回 ("", 全句)。"""
    cut = -1
    for i in range(min(idx, len(s)) - 1, min_left - 1, -1):
        if s[i] in _SEP:
            cut = i
            break
    if cut < min_left:
        return "", s.strip(_SEP)
    left = s[:cut].strip(_SEP)
    right = s[cut:].strip(_SEP)
    if not left or not right or len(left) > max_left:
        return "", s.strip(_SEP)
    return left, right


def _row_risk(s: str) -> list[str]:
    m = _TRIG_RE.search(s)
    if m and m.start() >= 4:
        left, right = s[:m.start()].strip(_SEP), s[m.start():].strip(_SEP)
        if left and right:
            return [left, right[:_CELL_MAX]]
    left, right = _cut_two(s, min(18, len(s)))
    return [left, right[:_CELL_MAX]]


def _row_param(s: str) -> list[str]:
    """参数行：左列=数值前的主语短语，右列=原文全句（不截断、不砍词）。"""
    import bidviz_e1 as e1
    m = e1.PARAM_RE.search(s)
    if not m:
        return ["", s]
    clause_start = max([s.rfind(c, 0, m.start()) for c in _SEP] + [-1]) + 1
    left = s[clause_start:m.start()].strip(_SEP)
    if len(left) > 18:
        left = left[-18:]
    if not left:
        left = s[max(0, m.start() - 10):m.start()].strip(_SEP)
    return [left, s[:_CELL_MAX]]


def _row_proc(s: str, n: int = 0) -> list[str]:
    m = _ORD_RE.match(s)
    head = m.group(0).strip() if m else ""
    rest = s[m.end():] if m else s
    left, right = _cut_two(rest, min(16, len(rest)))
    stage = ((head + " " + left) if left else (head or (str(n) if n else ""))).strip(_SEP)
    return [stage, right[:_CELL_MAX]]


def curate_tables(item: Any) -> list[tuple[str, list[str], list[list[str]], list[float]]]:
    """把本节原文策展为报表：一句只归一类（风险>参数>工序），材料只留真名词。

    返回 (题注, 表头, 行, 列宽比例)；所有单元格文字逐字取自原文。
    """
    import bidviz_e1 as e1
    import bidviz_charts as bc
    risk: list[list[str]] = []
    param: list[list[str]] = []
    proc: list[list[str]] = []
    seen: set[str] = set()
    for b in getattr(item, "blocks", []):
        if getattr(b, "kind", "body") == "heading":
            continue
        for s in e1.split_sentences(getattr(b, "text", "")):
            k = s[:24]
            if k in seen:
                continue
            if e1.RISK_RE.search(s):
                seen.add(k)
                risk.append(_row_risk(s))
            elif e1.PARAM_RE.search(s):
                seen.add(k)
                param.append(_row_param(s))
            elif any(w in s for w in e1.STEP_WORDS):
                seen.add(k)
                proc.append(_row_proc(s, len(proc) + 1))
    out: list[tuple[str, list[str], list[list[str]], list[float]]] = []
    # 要点式摘要：每表限行数，保证一节内容尽量一页装下
    if proc:
        out.append(("施工工序", ["阶段 / 工序", "作业要点（逐字取自原文）"], proc[:6], [0.30, 0.70]))
    if param:
        out.append(("参数与指标", ["名称 / 部位", "指标 · 要求（逐字取自原文）"], param[:6], [0.30, 0.70]))
    if risk:
        out.append(("风险与控制要求", ["风险情形", "控制要求（逐字取自原文）"], risk[:6], [0.34, 0.66]))
    try:
        mats = bc.extract_materials(getattr(item, "blocks", []))
    except Exception:
        mats = []
    mat = [[m[0], m[1]] for m in mats if m and m[0]][:8]
    if mat:
        out.append(("材料与设备", ["名称", "规格 / 型号"], mat, [0.40, 0.60]))
    if not out:
        body = [[b.text.strip()] for b in getattr(item, "blocks", [])
                if getattr(b, "kind", "body") != "heading" and b.text.strip()]
        out.append(("内容摘录", ["原文（逐字）"], body, [1.0]))
    return out


# ---------------------------------------------------------------- 规划规则

def thresholds() -> dict[str, int]:
    t = {"chars": 2600, "items": 4, "param": 8, "mat": 10, "layout": 6, "time": 6, "org": 4}
    t.update(_cfg().plan.get("thresholds", {}) or {})
    return t


def second_reasons(f: Features) -> list[str]:
    """本节为什么需要第二张图；返回空表示一张图就够。"""
    t = thresholds()
    why = []
    if f.chars >= t["chars"]:
        why.append(f"正文 {f.chars} 字")
    if f.items >= t["items"]:
        why.append(f"{f.items} 个分项")
    if f.param >= t["param"]:
        why.append(f"{f.param} 条参数")
    if f.mat >= t["mat"]:
        why.append(f"{f.mat} 项材料设备")
    if f.layout >= t["layout"]:
        why.append("含平面布置与运输组织")
    if f.time >= t["time"]:
        why.append("含阶段工期穿插")
    if f.org >= t["org"]:
        why.append("含岗位职责分工")
    return why


def second_kind(f: Features) -> str:
    """第二张图的图型：由内容特征路由，不是固定表格。"""
    if f.layout >= thresholds()["layout"]:
        return "site_plan" if f.style == "gis_satellite" else "layout_plan"
    if f.style == "gis_satellite":
        return "site_plan"
    if f.style == "exploded_assembly" or f.mat >= thresholds()["mat"]:
        return "assembly"
    if f.style == "cad_wireframe" or f.param >= 10:
        return "detail_section"
    if f.org >= thresholds()["org"]:
        return "org_duty"
    if f.time >= thresholds()["time"]:
        return "progress"
    if f.style == "vector_infographic":
        return "mgmt_system"
    if f.safe >= 20:
        return "risk_point"
    if f.qa >= 20:
        return "qa_flow"
    return "process_flow"


def plan_section(item: Any) -> list[dict]:
    """规划一个节要画哪几张图。返回 figure dict 列表（含可直接生图的提示词）。"""
    f = analyze(item)
    max_fig = _cfg().max_figures
    allow_second = _cfg().second_figure

    figs = [make_figure(item, f, 1, DEFAULT_KIND,
                        f"本节主视觉：{f.items} 个分项 / {f.proc} 道工序 / {f.param} 条参数 / "
                        f"{f.risk} 条风险点位，全部以引线标注吸收进同一画面")]
    if allow_second and max_fig >= 2:
        why = second_reasons(f)
        if why:
            kind = second_kind(f)
            figs.append(make_figure(item, f, 2, kind,
                                    f"一张图装不下（{'、'.join(why)}），补一张{KINDS[kind].name}"))
    return figs[:max(1, max_fig)]


def make_figure(item: Any, f: Features, no: int, kind_key: str, why: str) -> dict:
    kind = KINDS.get(kind_key) or KINDS[DEFAULT_KIND]
    g = gen_config(no, kind_key)
    style = kind.style or f.style
    return {
        "no": no,
        "kind": kind_key,
        "name": kind.name,
        "style": style,
        "why": why,
        "ratio": ratio_of_size(g["size"], kind.ratio),
        "gen": g,
        "prompt": figure_prompt(item, f, kind_key, style),
        "custom": False,
    }


def ratio_of_size(size: str, default: float = 1.414) -> float:
    m = re.match(r"\s*(\d+)\s*[x×]\s*(\d+)", str(size or ""))
    if not m:
        return default
    w, h = int(m.group(1)), int(m.group(2))
    return round(w / h, 3) if h else default


# ---------------------------------------------------------------- 提示词

GLOBAL_BAN = ("【禁止绘制】不要标题栏、不要页眉页脚、不要侧边栏、不要页码、不要水印、不要 Logo、"
              "不要图框、不要二维码、不要英文句子、不要长段正文；"
              "不要画职责分工矩阵（RACI）或任何原文没有给出的表格。")

NO_FAKE_TIME = "不得标注具体天数、日期或工期数字，原文没有明确给出的一律不写。"
NO_FAKE_NAME = "不得出现具体人名；岗位名称只能用原文给出的称呼。"


def _absorb_lines(f: Features, kind_key: str) -> list[str]:
    """按图型挑选要吸收进画面的原文事实（逐字，不改写）。"""
    lines: list[str] = []
    if f.objects:
        lines.append("【画面主体（构件/部位/工序名，逐字取自原文）】" + "、".join(f.objects[:14]))
    if kind_key in ("hero_scene", "process_flow", "progress", "assembly"):
        if f.steps:
            lines.append("【按施工先后表现的工序】")
            lines += [f"  {i}. {s}" for i, s in enumerate(f.steps[:8], 1)]
    if kind_key in ("hero_scene", "detail_section", "qa_flow", "assembly"):
        if f.param_sents:
            lines.append("【可标注的参数（只能照抄其中出现的数字与单位，不得改写或换算）】")
            lines += [f"  · {s}" for s in f.param_sents[:6]]
    if kind_key in ("hero_scene", "risk_point"):
        if f.risk_sents:
            lines.append("【需用红橙警示标记的风险点位（只画原文提到的，不得增补）】")
            lines += [f"  · {s}" for s in f.risk_sents[:5]]
    if kind_key in ("assembly", "layout_plan", "hero_scene"):
        if f.mat_names:
            lines.append("【涉及的主要材料设备】" + "、".join(f.mat_names[:10]))
    return lines


def figure_prompt(item: Any, f: Features, kind_key: str, style: str | None = None) -> str:
    """构建一张图的提示词：图型画面 + 原文事实 + 标注规则 + 禁止清单。"""
    import bidviz_e1 as e1
    kind = KINDS.get(kind_key) or KINDS[DEFAULT_KIND]
    style = style or kind.style or f.style
    meta = e1.STYLES.get(style) or e1.STYLES["bim_realistic"]
    body = "；".join(b.text for b in item.blocks if getattr(b, "kind", "body") != "heading")[:2600]
    absorb = _absorb_lines(f, kind_key)

    lines = [
        f"为{_cfg().prompt_subject}生成：{kind.scene}。",
        f"视觉语汇：{meta['scene']}。",
        f"本图主题：{item.title}（{item.section_path[:40]}）。",
        f"这张图要表现：{kind.focus}。",
        "",
    ]
    lines += absorb
    lines += [
        "",
        "【图中所有中文标注必须逐字取自以下原文，不得使用原文之外的任何词汇】",
        body,
        "",
        "【标注要求】",
        "不少于 14 个、最多 20 个标注，每个 2~8 个字，用引线指向对应构件或作业面。",
        "优先选取原文中的构件名、工序名、材料名、设备名、部位名与参数值。",
        "标注字号清晰可读、分布均匀不重叠，全部为规范简体中文，不得出现乱码或错别字。",
    ]
    if kind.ban:
        lines.append(kind.ban)
    if kind_key in ("progress", "layout_plan", "site_plan", "org_duty", "mgmt_system"):
        lines.append(NO_FAKE_TIME)
    if kind_key in ("org_duty", "mgmt_system"):
        lines.append(NO_FAKE_NAME)
    if kind_key == "risk_point" and not f.risk_sents:
        lines.append("原文未给出具体风险点，只表现通用安全防护设施，不得编造事故类型。")
    lines += [
        "无 CAD、BIM 模型或现场照片，本图为文字约束的通用工程示意，不得暗示真实坐标或现场实景。",
        GLOBAL_BAN,
        f"横向构图，宽高比约 {kind.ratio}:1，主体居中，四周自然留白。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 每图生图配置

def gen_config(no: int, kind_key: str) -> dict[str, str]:
    """每张图可各自指定供应商/模型/尺寸：第一张与第二张不必用同一个生图 API。

    优先级：[image.figure_overrides]."<图序>" > ."<图型>" > [image] 全局 > 代码默认。
    """
    cfg = _cfg()
    base = {"provider": cfg.provider, "model": cfg.model, "size": cfg.image_size}
    over = cfg.figure_overrides
    for key in (kind_key, str(no)):      # 后写的覆盖先写的，所以图序比图型更具体
        hit = over.get(key) or {}
        for k in ("provider", "model", "size"):
            v = hit.get(k)
            if isinstance(v, str) and v.strip():
                base[k] = v.strip()
    return base


def make_api(fig: dict) -> Any:
    """按该图的生图配置构造 ImageAPI；显式 provider 优先于全局环境变量。"""
    import bidviz
    from bidviz_config import provider_credentials
    g = fig.get("gen") or gen_config(fig.get("no", 1), fig.get("kind", DEFAULT_KIND))
    try:
        cred = provider_credentials(g["provider"], env_first=False)
        return bidviz.ImageAPI(base_url=cred["baseUrl"], api_key=cred["apiKey"], model=g["model"])
    except RuntimeError:
        return bidviz.ImageAPI(provider_name=g["provider"], model=g["model"])


# ---------------------------------------------------------------- 规划落盘

def plan_path(root: Path) -> Path:
    return Path(root) / "work" / PLAN_NAME


def load_plan(root: Path) -> dict | None:
    p = plan_path(root)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("sections") else None


def save_plan(root: Path, plan: dict) -> Path:
    p = plan_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def _load_sections(root: Path) -> list[Any]:
    """取（已投影编辑的）节列表。"""
    import bidviz_e1 as e1
    import build_e1 as be
    from bidviz_config import load as _load
    cfg = _cfg()
    if cfg.root != Path(root).resolve():
        _load(root)
    blocks, _pm = be.load_project(Path(root))
    sections = e1.group_sections(blocks)
    be.apply_edits(Path(root), sections)
    return sections


def sync_gen(plan: dict) -> int:
    """按当前配置刷新每张图的生图参数与宽高比（配置改了不必重算提示词）。"""
    changed = 0
    for entry in (plan.get("sections") or {}).values():
        for f in entry.get("figures", []):
            kind = f.get("kind") or DEFAULT_KIND
            g = gen_config(int(f.get("no", 1)), kind)
            ratio = ratio_of_size(g["size"], (KINDS.get(kind) or KINDS[DEFAULT_KIND]).ratio)
            if f.get("gen") != g or f.get("ratio") != ratio:
                f["gen"], f["ratio"] = g, ratio
                changed += 1
    return changed


def ensure_plan(root: Path, force: bool = False, only: Sequence[str] | None = None) -> dict:
    """读规划；缺节、内容变更或 force 时重算。人工改过的提示词（custom）不覆盖。"""
    root = Path(root)
    plan = None if force else load_plan(root)
    sections = _load_sections(root)
    wanted = {s.strip().upper() for s in only} if only else None
    if wanted:
        sections = [s for s in sections if s.section_id.upper() in wanted]
        if not sections:
            raise SystemExit(f"未找到指定节：{only}")

    old = (plan or {}).get("sections", {})
    out: dict[str, Any] = {}
    changed = 0
    for sec in sections:
        sid = sec.section_id
        item = sec.as_visual_item()
        sig = _sig(item)
        dims = layout.dim_profile(item)      # 十维计数 top，供图版页徽章与 Web
        prev = old.get(sid)
        if prev and prev.get("sig") == sig and prev.get("figures"):
            prev["dims"] = dims              # 旧规划 backfill/刷新 dims
            out[sid] = prev
            continue
        figs = plan_section(item)
        if prev:  # 保留人工改过的提示词
            prev_prompts = {f.get("no"): f for f in prev.get("figures", [])}
            for f in figs:
                pf = prev_prompts.get(f["no"])
                if pf and pf.get("custom") and pf.get("prompt"):
                    f["prompt"], f["custom"] = pf["prompt"], True
        out[sid] = {"title": sec.title, "chars": item.chars, "sig": sig, "figures": figs,
                    "dims": dims}
        changed += 1
    # 未在 only 范围内的节保持原样
    for sid, val in old.items():
        out.setdefault(sid, val)

    plan = {"version": PLAN_VERSION, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sections": out}
    changed += sync_gen(plan)
    plan["changed"] = changed
    save_plan(root, plan)
    return plan


def section_figures(root: Path, sid: str, item: Any | None = None) -> list[dict]:
    """取某节的图位规划；规划缺失或内容已变时即时重算（不落盘）。"""
    plan = load_plan(root)
    entry = (plan or {}).get("sections", {}).get(sid)
    if entry and entry.get("figures") and (item is None or entry.get("sig") == _sig(item)):
        return entry["figures"]
    if item is None:
        return []
    return plan_section(item)


def plan_summary(plan: dict) -> dict:
    """规划汇总：总图数、每节图数分布、图型分布。"""
    from collections import Counter
    secs = plan.get("sections", {})
    counts = Counter(len(v.get("figures", [])) for v in secs.values())
    kinds = Counter(f.get("kind") for v in secs.values() for f in v.get("figures", []))
    return {"sections": len(secs), "figures": sum(k * v for k, v in counts.items()),
            "per_section": {str(k): counts[k] for k in sorted(counts)},
            "kinds": dict(kinds.most_common())}


# ---------------------------------------------------------------- 图像定位

def figure_filename(sid: str, no: int) -> str:
    """第 1 张沿用既有 {sid}.png（已生成的主视觉零迁移），第 N 张为 {sid}__N.png。"""
    return f"{sid}.png" if no <= 1 else f"{sid}__{no}.png"


def resolve_images(root: Path, sid: str, figs: Sequence[dict],
                   dirs: Sequence[Path] | None = None) -> dict[int, Path | None]:
    """按图片目录优先级为每张规划图找文件；找不到为 None（编译端画占位框）。"""
    from bidviz_config import load as _load
    cfg = _cfg()
    if cfg.root != Path(root).resolve():
        cfg = _load(root)
    search = [Path(d) for d in dirs] if dirs else list(cfg.image_dirs)
    out: dict[int, Path | None] = {}
    for f in figs:
        no = int(f.get("no", 1))
        hit = None
        for d in search:
            p = d / figure_filename(sid, no)
            if p.exists() and p.stat().st_size > 2000:
                hit = p
                break
        out[no] = hit
    return out


def image_ratio(path: Path | None, default: float = 1.414) -> float:
    """读图片真实宽高比，排版时按 contain 精确定高，避免大片留白。"""
    if not path or not Path(path).exists():
        return default
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return w / h if h else default
    except Exception:
        return default
