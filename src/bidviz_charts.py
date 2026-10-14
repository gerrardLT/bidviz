"""bidviz_charts.py - 工程图表渲染器与内容提取器

将散文体施工组织设计转换为图表混合的高级工程图册。
包含 7 种核心图型渲染器与对应的内容提取器：
1. 工艺流程图 (Flowchart)
2. 工艺参数表 (Spec Table) - 基于 TableSpec
3. ITP 检验试验计划表 (ITP Table) - 基于 TableSpec
4. 风险矩阵热力图 (Risk Matrix 5x5)
5. 组织架构图 (Org Chart)
6. RACI 职责矩阵 (RACI Matrix) - 基于 TableSpec
7. 进度横道图 (Gantt Chart)
8. 材料设备表 (Material Table) - 基于 TableSpec
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence, Any
import bidviz_vector as bv

# 调色盘（国网电力蓝青工程风）
DARK = "#0c2333"
HEADER_BG = "#13334c"
CYAN = "#26a69a"
ACCENT = "#00897b"
BORDER = "#c1d0d9"
TEXT = "#1a2c38"
MUTED = "#5a6e7c"
RED_BG = "#8a2a2a"
WARN = "#e65100"
BG_LIGHT = "#f4f7f9"
MM = 72.0 / 25.4


# ==========================================
# 1. 内容提取器 (Extractors)
# ==========================================

def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"[。；;\n]+", text)
    return [c.strip() for c in chunks if len(c.strip()) >= 4]


def extract_materials(blocks: Sequence[Any]) -> list[tuple[str, str, str]]:
    """提取材料与设备：名称、规格/型号、用途/控制要求"""
    res = []
    seen = set()
    pat = re.compile(r"([A-Za-z0-9\u4e00-\u9fa5]{2,12}(?:钢筋|混凝土|角钢|电缆|排管|套管|支架|变压器|开关柜|挖掘机|吊车|自卸车|仪器|试验仪))")
    for b in blocks:
        if getattr(b, "kind", "") == "heading":
            continue
        for s in split_sentences(getattr(b, "text", "")):
            m = pat.search(s)
            if m:
                name = m.group(1)
                if name in seen or len(name) < 2:
                    continue
                seen.add(name)
                # 寻找规格
                spec_m = re.search(r"(\d+(?:\.\d+)?(?:mm|cm|m|kV|V|t|kg|级|厚|高|宽|\*|×|C\d+|HRB\d+)[A-Za-z0-9*×]*)", s)
                spec = spec_m.group(1) if spec_m else "符合设计规范"
                req = s[:36]
                res.append((name, spec, req))
                if len(res) >= 12:
                    return res
    return res


def extract_org_roles(blocks: Sequence[Any]) -> list[tuple[str, str]]:
    """提取组织架构人员与职责：岗位名称、核心职责"""
    roles = []
    seen = set()
    pat = re.compile(r"(项目经理|项目总工|技术负责人|安全员|质检员|施工员|资料员|材料员|班组长|专职安全员)")
    for b in blocks:
        if getattr(b, "kind", "") == "heading":
            continue
        for s in split_sentences(getattr(b, "text", "")):
            m = pat.search(s)
            if m:
                role = m.group(1)
                if role in seen:
                    continue
                seen.add(role)
                desc = s[:40]
                roles.append((role, desc))
    # 兜底通用配置
    if len(roles) < 3:
        defaults = [
            ("项目经理", "全面主持现场施工组织、协调资源与安全生产第一责任人"),
            ("项目技术负责人", "负责工程技术方案审核、工艺交底与质量重大问题处置"),
            ("专职安全员", "负责作业面日常安全监督巡视、旁站监理协调与隐患闭环排查"),
            ("质检工程师", "负责工序报验、实测实量、隐蔽工程验收与试验台账管理"),
            ("电气专业工长", "负责班组具体施工任务下达、工序交接检验与进度日清控制"),
        ]
        for r, d in defaults:
            if r not in seen:
                roles.append((r, d))
    return roles[:6]


def extract_gantt_tasks(blocks: Sequence[Any], default_days: int = 60) -> list[tuple[str, int, int]]:
    """提取工期节点与横道图任务：任务名称、起始天、结束天"""
    tasks = []
    # 优先从文本识别工期或工序
    sents = [s for b in blocks if getattr(b, "kind", "") != "heading" for s in split_sentences(getattr(b, "text", ""))]
    step_words = ["准备", "开挖", "垫层", "浇筑", "安装", "敷设", "试验", "验收", "恢复"]
    step_sents = [s for s in sents if any(w in s for w in step_words)]
    
    if len(step_sents) >= 4:
        span = max(1, default_days // len(step_sents[:6]))
        curr = 1
        for i, s in enumerate(step_sents[:6]):
            t_name = s[:12]
            tasks.append((t_name, curr, min(default_days, curr + span + 2)))
            curr += span
    else:
        # 标准阶段划分
        stages = [
            ("施工准备与测量放线", 1, 8),
            ("主体基槽开挖与支护", 6, 20),
            ("构筑物主体浇筑及养护", 18, 38),
            ("电缆管线敷设与头制作", 32, 50),
            ("电气交接试验与耐压", 46, 55),
            ("联调验收送电与场地恢复", 52, default_days),
        ]
        tasks = stages
    return tasks


def extract_raci_data(blocks: Sequence[Any]) -> tuple[list[str], list[str], list[list[str]]]:
    """提取 RACI 矩阵：角色列表、任务列表、关系矩阵(R/A/C/I)"""
    roles = ["项目经理", "技术负责", "专业工长", "专职安全员", "质检员"]
    tasks = [
        "施工方案编审与技术交底",
        "特种设备与施工机具进场报验",
        "隐蔽工程及基槽开挖联合验收",
        "危险性较大分部分项旁站监督",
        "原材料出厂合格证与进场复验",
        "电气高压耐压试验与调试记录",
        "竣工预验收与送电手续办理",
    ]
    # 标准合理授权配置
    matrix = [
        ["A", "R", "C", "C", "I"],
        ["C", "C", "R", "A", "C"],
        ["I", "A", "R", "C", "R"],
        ["I", "C", "C", "R", "I"],
        ["I", "C", "R", "I", "A"],
        ["I", "A", "R", "C", "R"],
        ["A", "R", "C", "I", "I"],
    ]
    return roles, tasks, matrix


def extract_risks_scored(blocks: Sequence[Any]) -> list[tuple[str, int, int, str]]:
    """提取带 L(可能性 1-5)、S(后果 1-5) 评分的风险项: (名称, L, S, 防范措施)"""
    res = []
    seen = set()
    high_words = ["高空", "吊装", "坍塌", "触电", "带电", "淹溺", "有限空间", "爆管", "火灾"]
    for b in blocks:
        if getattr(b, "kind", "") == "heading":
            continue
        for s in split_sentences(getattr(b, "text", "")):
            if any(k in s for k in ("风险", "危险", "事故", "隐患", "坍塌", "触电", "违章", "保护")):
                name = s[:14]
                if name in seen:
                    continue
                seen.add(name)
                # 规则评分
                is_high = any(w in s for w in high_words)
                s_score = 4 if is_high else 3
                l_score = 3 if "易" in s or "频繁" in s else 2
                measure = s[14:48] if len(s) > 14 else "严格执行标准化防护并设警戒隔离"
                res.append((name, l_score, s_score, measure))
                if len(res) >= 6:
                    break
    if len(res) < 3:
        defaults = [
            ("有限空间作业有害气体中毒窒息", 2, 5, "先通风、再检测、后作业，强制佩戴便携式四合一气体报警仪"),
            ("大型起重机吊装臂架触碰带电体", 2, 4, "设专职信号工指挥，作业半径外侧搭设绝缘硬质安全隔离防护栏"),
            ("深基坑边坡雨水冲刷失稳坍塌", 3, 4, "放坡坡面喷射C20混凝土封闭，坡顶设截水沟并严禁堆积重载"),
            ("临时用电电缆机械损伤破皮漏电", 3, 3, "采用TN-S系统并配备二级漏电保护，过路段加设钢管刚性套管"),
        ]
        for d in defaults:
            if d[0] not in seen:
                res.append(d)
    return res[:5]


# ==========================================
# 2. 图表渲染器 (Renderers)
# ==========================================

def draw_section_panel(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                       title: str, subtitle: str = "") -> None:
    """绘制统一图表容器边框与标题顶条"""
    sheet.rect(x, y, w, h, fill="white", stroke=BORDER, lw=0.6)
    sheet.rect(x, y, w, 5.5 * MM, fill=DARK)
    sheet.text(x + 5, y + 1.6 * MM, title, size=8.5, bold=True, color="white")
    if subtitle:
        sheet.text(x + w - 5, y + 1.8 * MM, subtitle, size=7.0, bold=False, color="#8fb3cc", align="right")


def draw_flowchart(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                   steps: list[tuple[str, str]], title: str = "工艺流程与质量检验控制图") -> float:
    """绘制现代化工艺流程图 (带状态、流向箭头、门禁质量检查点)"""
    draw_section_panel(sheet, x, y, w, h, title, "标准工艺卡控流程")
    
    body_y = y + 7.5 * MM
    body_h = h - 9.0 * MM
    n = min(6, max(3, len(steps)))
    steps = steps[:n]
    
    box_w = (w - (n - 1) * 7 * MM - 8 * MM) / n
    box_h = body_h - 2 * MM
    
    for i, (name, desc) in enumerate(steps):
        bx = x + 4 * MM + i * (box_w + 7 * MM)
        by = body_y
        
        # 步骤卡片
        sheet.rect(bx, by, box_w, box_h, fill="#f8fafc", stroke=BORDER, lw=0.6, radius=1.5)
        # 顶部步骤序号标
        sheet.rect(bx, by, box_w, 4.2 * MM, fill="#e2e8f0")
        sheet.text(bx + 4, by + 1.2 * MM, f"STAGE {i+1:02d}", size=6.5, bold=True, color="#475569")
        
        # 步骤名称
        sheet.text(bx + box_w / 2, by + 6.2 * MM, name, size=8.0, bold=True, color=DARK, align="center", max_w=box_w - 4)
        sheet.line(bx + 4, by + 10.5 * MM, bx + box_w - 4, by + 10.5 * MM, color=CYAN, lw=0.6)
        
        # 核心内容/规范
        sheet.para(bx + 3, by + 12.0 * MM, desc, box_w - 6, size=6.8, leading=8.6, color=MUTED, max_lines=4)
        
        # 底部门禁标签
        gate_txt = "主控隐蔽" if i in (1, 3) else ("停工待检" if i == n - 1 else "合格放行")
        gate_bg = "#ffebee" if "停" in gate_txt else ("#e0f2f1" if "合格" in gate_txt else "#fff3e0")
        gate_fg = "#c62828" if "停" in gate_txt else ("#00695c" if "合格" in gate_txt else "#e65100")
        sheet.pill(bx + box_w / 2 - 14, by + box_h - 4.5 * MM, gate_txt, bg=gate_bg, fg=gate_fg, size=6.5, pad_x=3.0, pad_y=1.5)
        
        # 连接箭头
        if i < n - 1:
            ax = bx + box_w
            ay = by + box_h / 2
            sheet.line(ax + 1 * MM, ay, ax + 5.5 * MM, ay, color=CYAN, lw=1.0)
            sheet.line(ax + 4.2 * MM, ay - 1.2 * MM, ax + 5.5 * MM, ay, color=CYAN, lw=1.0)
            sheet.line(ax + 4.2 * MM, ay + 1.2 * MM, ax + 5.5 * MM, ay, color=CYAN, lw=1.0)
            
    return h


def draw_risk_matrix(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                     risks: list[tuple[str, int, int, str]], title: str = "重大施工安全风险评价与管控矩阵 (L×S)") -> float:
    """绘制 5x5 L×S 风险热力矩阵与右侧对应管控措施表"""
    draw_section_panel(sheet, x, y, w, h, title, "半定量风险管控")
    
    # 左侧矩阵，右侧措施明细
    grid_size = min(h - 12 * MM, w * 0.38)
    gx = x + 10 * MM
    gy = y + 8 * MM
    cell_s = grid_size / 5.0
    
    # 5x5 单元格颜色渐变 (L: 1..5 横向, S: 1..5 纵向自下而上)
    # L=列(1..5), S=行(1..5自下而上)
    def cell_color(l: int, s: int) -> str:
        score = l * s
        if score >= 15:
            return "#ffcdd2"  # 红色重大
        if score >= 9:
            return "#ffe0b2"   # 橙色较大
        if score >= 5:
            return "#fff9c4"   # 黄色一般
        return "#e8f5e9"       # 绿色低风险
    
    for row in range(5):  # row 0 对应 S=5(顶), row 4 对应 S=1(底)
        s_val = 5 - row
        for col in range(5):
            l_val = col + 1
            cx = gx + col * cell_s
            cy = gy + row * cell_s
            color = cell_color(l_val, s_val)
            sheet.rect(cx, cy, cell_s, cell_s, fill=color, stroke="#cfd8dc", lw=0.4)
            sheet.text(cx + cell_s / 2, cy + cell_s / 2 - 2.5, f"{l_val*s_val}", size=6.0, color="#78909c", align="center")
    
    # 轴标签
    sheet.text(gx + grid_size / 2, gy + grid_size + 3.0 * MM, "可能性 L (1→5 极小至极高)", size=6.8, bold=True, color=DARK, align="center")
    sheet.text(gx - 3.5 * MM, gy + grid_size / 2 + 10, "严重度 S (1→5 轻微至灾难)", size=6.8, bold=True, color=DARK, align="center")
    
    # 投点绘制
    for name, l, s, _ in risks:
        col = max(0, min(4, l - 1))
        row = max(0, min(4, 5 - s))
        px = gx + col * cell_s + cell_s / 2
        py = gy + row * cell_s + cell_s / 2
        # 画红圆点与标号
        sheet.rect(px - 3, py - 3, 6, 6, fill="#d32f2f", radius=3)
    
    # 右侧措施表格列表
    rx = gx + grid_size + 10 * MM
    rw = x + w - rx - 4 * MM
    ry = gy
    
    sheet.rect(rx, ry, rw, 4.5 * MM, fill="#37474f")
    sheet.text(rx + 4, ry + 1.2 * MM, "重点风险源", size=7.2, bold=True, color="white")
    sheet.text(rx + 55 * MM, ry + 1.2 * MM, "等级", size=7.2, bold=True, color="white")
    sheet.text(rx + 70 * MM, ry + 1.2 * MM, "技术预防与应急管控措施", size=7.2, bold=True, color="white")
    ry += 4.5 * MM
    
    for name, l, s, measure in risks[:5]:
        score = l * s
        lvl_str = "重大" if score >= 15 else ("较大" if score >= 9 else ("一般" if score >= 5 else "低"))
        lvl_bg = "#ffebee" if "重大" in lvl_str else ("#fff3e0" if "较大" in lvl_str else "#f1f8e9")
        lvl_fg = "#c62828" if "重大" in lvl_str else ("#e65100" if "较大" in lvl_str else "#2e7d32")
        
        row_h = 7.0 * MM
        sheet.rect(rx, ry, rw, row_h, fill="white", stroke=BORDER, lw=0.4)
        sheet.text(rx + 3, ry + 2.0 * MM, name, size=7.0, bold=True, color=TEXT, max_w=50 * MM)
        sheet.pill(rx + 55 * MM, ry + 1.5 * MM, f"{lvl_str} ({score})", bg=lvl_bg, fg=lvl_fg, size=6.0, pad_x=2.5, pad_y=1.0)
        sheet.para(rx + 70 * MM, ry + 1.8 * MM, measure, rw - 72 * MM, size=6.5, leading=8.5, color=MUTED, max_lines=2)
        ry += row_h

    return h


def draw_gantt_chart(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                     tasks: list[tuple[str, int, int]], title: str = "施工总进度控制横道计划图 (Gantt)") -> float:
    """绘制带时间轴刻度的规范施工进度甘特图"""
    draw_section_panel(sheet, x, y, w, h, title, "总工期 60 日历天")
    
    header_h = 6.0 * MM
    gy = y + 7.5 * MM
    label_w = 55 * MM
    chart_w = w - label_w - 8 * MM
    chart_x = x + 4 * MM + label_w
    total_days = 60
    
    # 时间轴顶栏
    sheet.rect(chart_x, gy, chart_w, header_h, fill="#eceff1")
    # 划分 6 个旬/10天标尺
    for i in range(7):
        day = i * 10
        dx = chart_x + (day / total_days) * chart_w
        sheet.line(dx, gy, dx, y + h - 3 * MM, color="#cfd8dc", lw=0.4)
        if i < 6:
            sheet.text(dx + 2, gy + 1.6 * MM, f"第{day+1}-{day+10}天", size=6.5, bold=True, color="#455a64")
    
    row_y = gy + header_h
    row_h = (h - 16 * MM) / max(1, len(tasks))
    
    for i, (name, s_day, e_day) in enumerate(tasks):
        sheet.rect(x + 4 * MM, row_y, label_w, row_h, fill="#f8fafc" if i % 2 == 0 else "white", stroke=BORDER, lw=0.4)
        sheet.text(x + 6 * MM, row_y + row_h / 2 - 3, f"{i+1}. {name}", size=7.2, bold=True, color=DARK, max_w=label_w - 4 * MM)
        
        # 任务条
        bx = chart_x + (s_day / total_days) * chart_w
        bw = max(6.0, ((e_day - s_day) / total_days) * chart_w)
        bar_h = row_h * 0.55
        by = row_y + (row_h - bar_h) / 2
        
        color = "#00897b" if i % 2 == 0 else "#0288d1"
        sheet.rect(bx, by, bw, bar_h, fill=color, radius=1.2)
        sheet.text(bx + bw / 2, by + bar_h / 2 - 2.5, f"{s_day}d~{e_day}d", size=6.0, color="white", align="center")
        
        row_y += row_h
        
    return h


def draw_raci_matrix(sheet: bv.Sheet, x: float, y: float, w: float, h: float,
                     roles: list[str], tasks: list[str], matrix: list[list[str]],
                     title: str = "施工管理组织与工序责任矩阵 (RACI)") -> float:
    """绘制 RACI 责任矩阵表"""
    draw_section_panel(sheet, x, y, w, h, title, "R=执行 / A=负责 / C=咨询 / I=知会")
    
    th_y = y + 7.5 * MM
    task_w = 65 * MM
    col_w = (w - 8 * MM - task_w) / len(roles)
    
    # 表头
    sheet.rect(x + 4 * MM, th_y, task_w, 5.5 * MM, fill="#263238")
    sheet.text(x + 7 * MM, th_y + 1.5 * MM, "工序与管理职责项", size=7.2, bold=True, color="white")
    for ci, r in enumerate(roles):
        cx = x + 4 * MM + task_w + ci * col_w
        sheet.rect(cx, th_y, col_w, 5.5 * MM, fill="#37474f")
        sheet.text(cx + col_w / 2, th_y + 1.5 * MM, r, size=7.2, bold=True, color="white", align="center")
        
    row_y = th_y + 5.5 * MM
    row_h = (h - 15 * MM) / max(1, len(tasks))
    
    raci_colors = {
        "R": ("#e8f5e9", "#2e7d32"),
        "A": ("#ffebee", "#c62828"),
        "C": ("#fff8e1", "#f57f17"),
        "I": ("#e1f5fe", "#0288d1"),
    }
    
    for ri, t in enumerate(tasks):
        bg = "#fafafa" if ri % 2 == 0 else "white"
        sheet.rect(x + 4 * MM, row_y, task_w, row_h, fill=bg, stroke=BORDER, lw=0.4)
        sheet.text(x + 7 * MM, row_y + row_h / 2 - 3, t, size=7.0, bold=True, color=TEXT, max_w=task_w - 5 * MM)
        
        for ci in range(len(roles)):
            cx = x + 4 * MM + task_w + ci * col_w
            sheet.rect(cx, row_y, col_w, row_h, fill=bg, stroke=BORDER, lw=0.4)
            code = matrix[ri][ci] if ri < len(matrix) and ci < len(matrix[ri]) else "-"
            c_bg, c_fg = raci_colors.get(code, ("#f5f5f5", "#757575"))
            sheet.pill(cx + col_w / 2 - 6, row_y + row_h / 2 - 3.5, code, bg=c_bg, fg=c_fg, size=7.0, pad_x=3.0, pad_y=1.0)
            
        row_y += row_h
        
    return h


def build_material_table_spec(rows: list[tuple[str, str, str]], max_w: float) -> bv.TableSpec:
    """构建材料设备明细 TableSpec"""
    body = []
    for name, spec, req in rows:
        body.append([
            bv.Cell(name, bold=True, size=8.0),
            bv.Cell(spec, size=7.5),
            bv.Cell(req, size=7.2),
        ])
    col_w = [max_w * 0.28, max_w * 0.28, max_w * 0.44]
    return bv.TableSpec(
        headers=["材料/设备名称", "主要规格型号/等级", "进场复检与质量控制要求"],
        rows=body,
        col_w=col_w,
        header_bg=HEADER_BG, font_size=8.0, pad=4,
    )
