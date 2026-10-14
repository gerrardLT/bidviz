#!/usr/bin/env python3
"""施工组织设计可视化 · 方案 A (高端工程画册级) 全量 50 页编译渲染引擎。

架构规范：
- 顶栏：国网电力科技蓝青系 Banner (项目名、主标题、章节路径、A3编号块)
- 左侧 (60% 宽)：
  1. 主图容器：大尺度 BIM 工况/三维全景图 (fit_cover 铺满无灰边)
  2. 3 槽位挂接节点特写图群 (优先关联原文档本页提取出的真实技术插图，不足时轮转)
  3. 底部横向 5 施工工序/管控流水卡片
- 右侧 (40% 宽)：
  1. 核心工艺参数与技术控制指标表 (标准工程框线表 + 彩色胶囊等级)
  2. 质量通病防护与应急处置矩阵 (红头框线矩阵表 + 处置权限胶囊)
  3. 检验与试验计划 (ITP) 联合报验管理卡 (停检点 H / 见证点 W 报验流)
- 底栏：BIM 数字化施工交付体系防伪标识与技术说明
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

import bidviz

W, H = 4961, 3508

DARK_TEAL = "#0c2333"      # 顶栏及主标深墨蓝
HEADER_BG = "#13334c"      # 表头深蓝
TEAL_ACCENT = "#00897b"    # 国网绿青强调色
CYAN_LINE = "#26a69a"      # 青色描边/重点引线
BG_LIGHT = "#f4f7f9"       # 浅冷灰底色
PANEL_BG = "#ffffff"       # 白色卡片
BORDER_COLOR = "#c1d0d9"   # 灰色工程框线
TEXT_MAIN = "#1a2c38"      # 主文字
TEXT_MUTED = "#5a6e7c"     # 辅助文字
ORANGE_WARN = "#e65100"    # 重点警告红橙

def get_font(size: int, bold: bool = False):
    return bidviz.pil_font(size, bold)

def fit_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    src = bidviz._flatten(image)
    ratio = max(size[0] / src.width, size[1] / src.height)
    resized = src.resize((round(src.width * ratio), round(src.height * ratio)), Image.Resampling.LANCZOS)
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))

def draw_pill(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, font: Any, bg_color: str = "#e0f2f1", text_color: str = "#00695c") -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 16, 7
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + th + pad_y * 2), radius=8, fill=bg_color)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=text_color)
    return tw + pad_x * 2

def draw_table_bordered(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, headers: list[str], rows: list[list[Any]], col_widths: list[int], header_bg: str = DARK_TEAL) -> int:
    font_th = get_font(26, True)
    font_td = get_font(24, False)
    font_pill = get_font(20, True)
    
    th_h = 70
    draw.rectangle((x, y, x + w, y + th_h), fill=header_bg)
    cur_x = x
    for i, h in enumerate(headers):
        draw.text((cur_x + 16, y + 20), h, font=font_th, fill="white")
        cur_x += col_widths[i]
        if i < len(headers) - 1:
            draw.line((cur_x, y, cur_x, y + th_h), fill="#2e4d65", width=2)
            
    cur_y = y + th_h
    for r_idx, row in enumerate(rows):
        row_h = 64
        bg = "#ffffff" if r_idx % 2 == 0 else "#f8fbfd"
        draw.rectangle((x, cur_y, x + w, cur_y + row_h), fill=bg, outline=BORDER_COLOR, width=1)
        cur_x = x
        for c_idx, val in enumerate(row):
            cx = cur_x + 16
            cy = cur_y + 17
            if isinstance(val, tuple) and val[0] == "pill":
                draw_pill(draw, cx, cy - 5, val[1], font_pill, val[2], val[3])
            else:
                s_val = str(val)
                if len(s_val) > 28: s_val = s_val[:27] + "…"
                draw.text((cx, cy), s_val, font=font_td, fill=TEXT_MAIN)
            cur_x += col_widths[c_idx]
            if c_idx < len(row) - 1:
                draw.line((cur_x, cur_y, cur_x, cur_y + row_h), fill=BORDER_COLOR, width=1)
        cur_y += row_h
        
    draw.rectangle((x, y, x + w, cur_y), outline=header_bg, width=2)
    return cur_y - y

def draw_header_bar(draw: ImageDraw.ImageDraw, title: str, section_path: str, page_num: str):
    from bidviz_config import get as _cfg
    draw.rectangle((0, 0, W, 220), fill=DARK_TEAL)
    header_text = _cfg().header_text or "施工组织设计图册"
    draw.text((120, 45), header_text, font=get_font(30), fill="#7ea6c2")
    
    font_title = get_font(60, True)
    draw.text((120, 98), title[:36], font=font_title, fill="white")
    
    t_bbox = draw.textbbox((0, 0), title[:36], font=font_title)
    sub_x = 120 + (t_bbox[2] - t_bbox[0]) + 40
    
    # 截取章节路径
    path_clean = " / ".join(section_path.split(" / ")[-2:]) if section_path else "主要施工方案与技术措施"
    draw.text((sub_x, 118), f"|  {path_clean[:38]}", font=get_font(34), fill="#4db6ac")
    
    draw.rectangle((W - 860, 40, W - 120, 180), fill="#102d42", outline="#244c6b", width=2)
    draw.text((W - 830, 60), "STAGE / SECTION", font=get_font(22), fill="#6d92ab")
    sec_short = section_path.split(" / ")[0] if section_path else "第一章"
    draw.text((W - 830, 95), sec_short[:16], font=get_font(28, True), fill="white")
    draw.text((W - 400, 68), page_num, font=get_font(56, True), fill=CYAN_LINE)

def draw_footer_bar(draw: ImageDraw.ImageDraw, page_id: str, src_pages: list[int]):
    y = H - 170
    draw.rectangle((0, y, W, H), fill="#e9eff2")
    draw.line((0, y, W, y), fill=BORDER_COLOR, width=2)
    
    draw.rectangle((120, y + 35, 480, y + 125), fill=DARK_TEAL)
    draw.text((140, y + 50), "BIM 数字化施工交付体系", font=get_font(24, True), fill="white")
    draw.text((140, y + 85), "HIGH-DENSITY ATLAS SPEC", font=get_font(18), fill="#7ea6c2")
    
    p_str = ",".join(map(str, src_pages)) if src_pages else "全案"
    draw.text((520, y + 50), f"技术声明：本页包含三维BIM工况图、节点详图、装配示意与现场实测数据，对应原文档第 [{p_str}] 页内容。", font=get_font(24), fill=TEXT_MUTED)
    draw.text((520, y + 88), "审查状态：★ 方案通过 (总工签发)    审核责任人：项目总工 / 质检工程师    版本编号：VER-2025-A3", font=get_font(22), fill=DARK_TEAL)
    
    draw.text((W - 700, y + 65), f"图册页码：{page_id}", font=get_font(30, True), fill=TEAL_ACCENT)


# =========================================================================
# 结构化抽取引擎：将一个页面所含的文本块智能提取成表格行、风险项和工序步骤
# =========================================================================
def parse_page_structure(page_blocks_text: str, page_title: str) -> dict[str, Any]:
    sentences = [s.strip() for s in re.split(r'[。；;!\n]', page_blocks_text) if len(s.strip()) > 3]
    
    # 1. 抽取核心参数表 (Table 1: 8~10 行)
    t1_rows = []
    for s in sentences:
        if re.search(r'\d+(?:\.\d+)?(?:mm|cm|m|%|天|级|次|组|座|孔|kV|MPa|μm|人|h|度|道|kg|处)', s):
            parts = re.split(r'[,，:：]', s)
            if len(parts) >= 2:
                item = parts[0][:14]
                spec = "，".join(parts[1:])[:30]
            else:
                item = s[:12]
                spec = s[12:38]
            is_strict = any(k in s for k in ["必须", "严禁", "≥", "不小于", "符合要求"])
            pill_tag = "强制主控" if is_strict else "技术参数"
            pill_color = ("#ffebee", "#c62828") if is_strict else ("#e0f2f1", "#00695c")
            t1_rows.append([item, spec, "尺量/巡检/台账", ("pill", pill_tag, pill_color[0], pill_color[1])])
            if len(t1_rows) >= 9:
                break
                
    # 备底通用规范数据，保证参数表始终丰满 (至少 8 行)
    defaults_t1 = [
        ["施工测量放线", "经纬仪/水准仪闭合复测", "每施工段全数复核", ("pill", "主控项目", "#e8f5e9", "#2e7d32")],
        ["材料进场验收", "出厂合格证及第三方检测报告", "批批报验进场", ("pill", "质量保证", "#fff3e0", "#ef6c00")],
        ["工序交接检验", "三检制(自检/互检/专检)", "上道不合格下道不准做", ("pill", "强制门禁", "#ffebee", "#c62828")],
        ["隐蔽工程验收", "四方联合隐蔽验收签证", "隐蔽前全数检查", ("pill", "主控项目", "#e8f5e9", "#2e7d32")],
        ["现场安全监护", "专职安全员旁站监督管理", "全天候作业巡视", ("pill", "安全受力", "#f3e5f5", "#7b1fa2")],
        ["成品保护措施", "硬质围挡与防雨防潮覆盖", "交工前专人看护", ("pill", "使用功能", "#e8f5e9", "#2e7d32")],
        ["技术交底制度", "三级交底(图纸/规范/安全)", "作业前签字归档", ("pill", "管理规范", "#e1f5fe", "#0277bd")],
        ["质量通病防治", "消除裂缝、空鼓、渗漏缺陷", "定期质量抽查", ("pill", "质量目标", "#e8f5e9", "#2e7d32")]
    ]
    for d in defaults_t1:
        if len(t1_rows) >= 9: break
        t1_rows.append(d)
        
    # 2. 抽取风险应急与通病矩阵表 (Table 2: 4~5 行)
    t2_rows = []
    risk_keywords = ["严禁", "不得", "防止", "注意", "危险", "事故", "消防", "隐患", "破坏", "避免", "偏差", "保护"]
    for s in sentences:
        if any(k in s for k in risk_keywords):
            parts = re.split(r'[,，:：]', s)
            if len(parts) >= 2:
                r_item = parts[0][:12]
                r_cause = "现场操作或外部干扰因素"
                r_action = "，".join(parts[1:])[:28]
            else:
                r_item = s[:10]
                r_cause = "不当施工或监测缺失"
                r_action = s[10:35]
            t2_rows.append([r_item, r_cause, r_action, ("pill", "总工/监理", "#ffebee", "#c62828")])
            if len(t2_rows) >= 5:
                break
                
    defaults_t2 = [
        ["地下管线外力损坏", "探测不明或机械违规盲目开挖", "人工探槽，未标明严禁动土", ("pill", "总工决策", "#ffebee", "#c62828")],
        ["深基坑/沟槽开裂塌方", "降雨浸泡、周边荷载超限", "放坡打桩，严禁基坑顶部堆载", ("pill", "施工队长", "#fff3e0", "#ef6c00")],
        ["违章作业与安全事故", "未持证上岗或未系安全带防护", "三宝四口五临边全覆盖检查", ("pill", "安全总监", "#ffebee", "#c62828")],
        ["实体质量缺陷与返工", "配合比失准、振捣不实或早拆模", "严控试验配合比，加强原浆压光", ("pill", "质检工程师", "#e8f5e9", "#2e7d32")]
    ]
    for d in defaults_t2:
        if len(t2_rows) >= 5: break
        t2_rows.append(d)

    # 3. 抽取底部 5 个流水工序卡 (Steps: 5 项)
    steps = []
    # 启发式寻找动作短句
    action_sents = [s for s in sentences if any(k in s for k in ["施工", "安装", "开挖", "浇筑", "准备", "验收", "砌筑", "检验", "敷设", "制作", "试验", "管理"])]
    step_titles = ["01 准备与勘察", "02 基础与开挖", "03 主体施工", "04 配套与防护", "05 验收与养护"]
    for i in range(5):
        desc = action_sents[i][:55] if i < len(action_sents) else f"严格执行《施工组织设计》及国家相关规范标准，强化现场工序协同管理。"
        steps.append((step_titles[i], desc))

    # 4. 抽取 ITP 联合报验管理卡片 (4 项)
    itp_items = [
        (f"工序① {page_title[:10]}现场技术交底", "开工前完成图纸会审与专业工种技术、安全交底，签署交底记录并报监理备案。"),
        (f"工序② 进场物资与隐蔽工程报验", "主要构件、材料必须具备出厂合格证并完成复验，隐检合格后方准进入下道工序。"),
        (f"工序③ 关键工序过程联合监督", "关键施工节点设置停检点(H)，由项目总工、质检员与专业监理工程师现场旁站见证。"),
        (f"工序④ 检验批实体观感与交工核验", "按照设计规范和评定标准，逐段进行标高、平整度、外观实体测量并签署验收资料。")
    ]
    
    return {
        "t1_rows": t1_rows,
        "t2_rows": t2_rows,
        "steps": steps,
        "itp_items": itp_items
    }


# =========================================================================
# 单页渲染主函数：方案 A 完整组装
# =========================================================================
def _block_field(block: Any, name: str) -> Any:
    """block 可能是 SourceBlock 数据类或已序列化 dict，两种都支持。"""
    return block[name] if isinstance(block, dict) else getattr(block, name)


def render_album_page(page: dict[str, Any], block_map: dict[str, Any], page_media_map: dict[int, list[str]], global_media: list[str], output_path: Path, project_root: Path | None = None):
    img = Image.new("RGB", (W, H), BG_LIGHT)
    draw = ImageDraw.Draw(img)
    
    page_id = page["page_id"]
    title = page["title"]
    section_path = page["section_path"]
    src_pages = page.get("source_pages", [])
    
    draw_header_bar(draw, title, section_path, page_id)
    
    # 汇总本页所有原文段落
    all_text = " ".join(_block_field(block_map[bid], "text") for bid in page["block_ids"] if bid in block_map and _block_field(block_map[bid], "kind") != "heading")
    struct_data = parse_page_structure(all_text, title)
    
    # ----------------- SECTION 1: 左侧 60% 宽度复合视觉区 -----------------
    left_x = 120
    left_w = 2850
    
    # 1.1 主图容器（大尺度 BIM 施工场景图）
    main_img_y = 260
    main_img_h = 1680
    draw.rectangle((left_x, main_img_y, left_x + left_w, main_img_y + main_img_h), fill=PANEL_BG, outline=BORDER_COLOR, width=2)
    
    visual_root = (project_root or Path("projects/default")) / "work/visuals"
    bim_path = visual_root / f"{page_id}.png"
    if bim_path.exists() and bim_path.stat().st_size > 1000:
        hero_im = Image.open(bim_path).convert("RGB")
    else:
        # 回退通用占位
        hero_im = Image.new("RGB", (1800, 1200), "#dce5eb")
        d = ImageDraw.Draw(hero_im)
        d.text((100, 500), "BIM CONSTRUCTION VISUALIZATION", font=ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf", 64), fill="#546e7a")
        
    hero_fit = fit_cover(hero_im, (left_w - 20, main_img_h - 80))
    img.paste(hero_fit, (left_x + 10, main_img_y + 70))
    
    # 主图上方标题条
    draw.rectangle((left_x, main_img_y, left_x + left_w, main_img_y + 70), fill="#1a3d59")
    draw.text((left_x + 30, main_img_y + 18), f"图 1.1  {title} · 数字化BIM施工工况全景与关键作业工序解析", font=get_font(28, True), fill="white")
    draw.text((left_x + left_w - 360, main_img_y + 20), "比例：三维轴测透视", font=get_font(22), fill="#a5c4db")
    
    # 1.2 挂接 3 张联动节点特写图群 (3 Detail Callout Slots)
    detail_y = main_img_y + main_img_h + 40
    detail_h = 760
    detail_w = (left_w - 60) // 3
    
    # 寻找配图候选：优先取本源页面提取出的真实原图，不足时用全局图库
    candidates = []
    for sp in src_pages:
        for f in page_media_map.get(sp, []):
            if f not in candidates: candidates.append(f)
    for gm in global_media:
        if len(candidates) >= 3: break
        if gm not in candidates: candidates.append(gm)
        
    detail_titles = [
        "图 1.2-A 核心结构节点详图与实物状态",
        "图 1.2-B 施工装备布置与现场作业防护",
        "图 1.2-C 原材料进场检验与文明施工标准"
    ]
    
    for i in range(3):
        dx = left_x + i * (detail_w + 30)
        draw.rectangle((dx, detail_y, dx + detail_w, detail_y + detail_h), fill=PANEL_BG, outline=BORDER_COLOR, width=2)
        draw.rectangle((dx, detail_y, dx + detail_w, detail_y + 55), fill="#254b6b")
        draw.text((dx + 18, detail_y + 14), detail_titles[i], font=get_font(23, True), fill="white")
        
        # 贴图
        if i < len(candidates) and Path(candidates[i]).exists():
            try:
                sub_im = Image.open(candidates[i])
                sub_fit = bidviz.fit_contain(sub_im, (detail_w - 14, detail_h - 65))
                img.paste(sub_fit, (dx + 7, detail_y + 60))
            except Exception:
                pass
                
    # 1.3 底部施工步骤流图卡 (Step Banner)
    step_y = detail_y + detail_h + 40
    step_h = 440
    draw.rounded_rectangle((left_x, step_y, left_x + left_w, step_y + step_h), radius=16, fill="#eaf1f5", outline=BORDER_COLOR, width=2)
    draw.rectangle((left_x, step_y, left_x + left_w, step_y + 60), fill=DARK_TEAL)
    draw.text((left_x + 30, step_y + 15), f"{title} · 分部分项标准化施工作业顺序与门禁控制要求", font=get_font(26, True), fill="white")
    
    step_item_w = (left_w - 80) // 5
    for i, (st, desc) in enumerate(struct_data["steps"]):
        sx = left_x + 20 + i * (step_item_w + 10)
        sy = step_y + 80
        draw.rounded_rectangle((sx, sy, sx + step_item_w, sy + step_h - 100), radius=10, fill="white", outline="#cbd7de", width=1)
        draw.text((sx + 16, sy + 18), st, font=get_font(23, True), fill=DARK_TEAL)
        draw.line((sx + 16, sy + 52, sx + step_item_w - 16, sy + 52), fill=CYAN_LINE, width=3)
        
        lines = bidviz.wrap_text(desc, 14)
        ty = sy + 65
        for l in lines:
            draw.text((sx + 16, ty), l, font=get_font(20), fill=TEXT_MAIN)
            ty += 30

    # ----------------- SECTION 2: 右侧 40% 宽度精细数据与表格区 -----------------
    right_x = left_x + left_w + 50
    right_w = W - right_x - 120
    
    # 2.1 关键质量控制参数表 (Bordered Engineering Table)
    table1_y = 260
    draw.rectangle((right_x, table1_y, right_x + right_w, table1_y + 60), fill=DARK_TEAL)
    draw.text((right_x + 25, table1_y + 15), f"表 1.1 {title[:16]}核心工艺参数与技术控制指标", font=get_font(26, True), fill="white")
    
    headers1 = ["控制分项", "规范与设计控制参数", "检测方法与频率", "控制等级"]
    col_w1 = [360, 680, 480, 240]
    t1_h = draw_table_bordered(draw, right_x, table1_y + 60, right_w, headers1, struct_data["t1_rows"], col_w1, header_bg=DARK_TEAL)
    
    # 2.2 质量通病防护与应急风险矩阵表 (Risk & Control Table)
    table2_y = table1_y + 60 + t1_h + 40
    draw.rectangle((right_x, table2_y, right_x + right_w, table2_y + 60), fill="#8a2a2a")
    draw.text((right_x + 25, table2_y + 15), "表 1.2 质量通病防护、环境风险与应急处置矩阵", font=get_font(26, True), fill="white")
    
    headers2 = ["风险与通病", "诱因分析与征兆", "预防及纠偏处置对策", "处置权限"]
    col_w2 = [340, 560, 620, 240]
    t2_h = draw_table_bordered(draw, right_x, table2_y + 60, right_w, headers2, struct_data["t2_rows"], col_w2, header_bg="#8a2a2a")
    
    # 2.3 责任矩阵与检验批报验要求 (Responsibility Box)
    resp_y = table2_y + 60 + t2_h + 40
    resp_h = H - resp_y - 200
    draw.rounded_rectangle((right_x, resp_y, right_x + right_w, resp_y + resp_h), radius=16, fill=PANEL_BG, outline=BORDER_COLOR, width=2)
    draw.rectangle((right_x, resp_y, right_x + right_w, resp_y + 60), fill=DARK_TEAL)
    draw.text((right_x + 25, resp_y + 15), "检验与试验计划 (ITP) 联合报验管理矩阵", font=get_font(26, True), fill="white")
    
    for idx, (itp_title, itp_content) in enumerate(struct_data["itp_items"]):
        iy = resp_y + 80 + idx * 135
        if iy + 110 > resp_y + resp_h: break
        draw.text((right_x + 30, iy), itp_title[:24], font=get_font(25, True), fill=DARK_TEAL)
        draw_pill(draw, right_x + 440, iy - 4, "停检点(H)", font=get_font(19, True), bg_color="#ffebee", text_color="#c62828")
        
        c_lines = bidviz.wrap_text(itp_content, 38)
        draw.text((right_x + 30, iy + 45), c_lines[0] if c_lines else "", font=get_font(22), fill=TEXT_MUTED)
        if len(c_lines) > 1:
            draw.text((right_x + 30, iy + 75), c_lines[1], font=get_font(22), fill=TEXT_MUTED)
        if idx < len(struct_data["itp_items"]) - 1:
            draw.line((right_x + 30, iy + 115, right_x + right_w - 30, iy + 115), fill="#e4ebef", width=1)
            
    draw_footer_bar(draw, page_id, src_pages)
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, dpi=(300, 300))
    return output_path


def compile_pdf(images: list[Path], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output), pagesize=landscape(A3), pageCompression=1)
    pw, ph = landscape(A3)
    for p in images:
        c.drawImage(str(p), 0, 0, width=pw, height=ph)
        c.showPage()
    c.save()


def main():
    import argparse
    ap = argparse.ArgumentParser(description="方案 A 高端工程画册级 A3 图册编译器")
    ap.add_argument("--root", default="projects/default", help="项目目录（含 work/ 与 output/）")
    args = ap.parse_args()
    root = Path(args.root)
    pages = json.loads((root / "work/pages.json").read_text(encoding="utf-8"))
    blocks = json.loads((root / "work/blocks.json").read_text(encoding="utf-8"))
    b_map = {b["block_id"]: b for b in blocks}
    
    pm_path = root / "work/page-media-map.json"
    page_media_map = {}
    if pm_path.exists():
        page_media_map = {int(k): v for k, v in json.loads(pm_path.read_text(encoding="utf-8")).items()}
        
    global_media = sorted([str(p) for p in (root / "work/page-media").glob("*.*")])
    
    out_dir = root / "output/album-pages"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    rendered_pages = []
    print(f"Start compiling Style A album for {len(pages)} pages...", flush=True)
    start_time = time.time()
    
    for i, p in enumerate(pages, 1):
        target = out_dir / f"{p['page_id']}.png"
        render_album_page(p, b_map, page_media_map, global_media, target, project_root=root)
        rendered_pages.append(target)
        if i % 25 == 0 or i == len(pages):
            print(f"[{i}/{len(pages)}] Compiled {p['page_id']} in {round(time.time() - start_time, 1)}s", flush=True)
            
    pdf_out = root / "output/施工组织设计-高端工程画册图册-A3-DRAFT.pdf"
    print("Compiling 300 DPI A3 PDF...", flush=True)
    compile_pdf(rendered_pages, pdf_out)
    print(f"Done! PDF exported: {pdf_out} ({round(pdf_out.stat().st_size / (1024*1024), 2)} MB)", flush=True)


if __name__ == "__main__":
    main()
