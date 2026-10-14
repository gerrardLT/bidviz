#!/usr/bin/env python3
"""生成 Style A（高端工程画册级）和 Style B（现代模块化卡片群）两张对照样张。"""

import os
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import bidviz

W, H = 4961, 3508

# 颜色系统：国网电力科技蓝青系 (Power Grid Cyan Blue)
DARK_TEAL = "#0c2333"      # 顶栏及主标深墨蓝
HEADER_BG = "#13334c"      # 表头/大区深蓝
TEAL_ACCENT = "#00897b"    # 国网绿青强调色
CYAN_LINE = "#26a69a"      # 青色描边/重点引线
BG_LIGHT = "#f4f7f9"       # 浅冷灰底色
PANEL_BG = "#ffffff"       # 白色卡片
BORDER_COLOR = "#c1d0d9"   # 灰色工程框线
TEXT_MAIN = "#1a2c38"      # 主文字
TEXT_MUTED = "#5a6e7c"     # 辅助文字
ORANGE_WARN = "#e65100"    # 重点警告红橙
BLUE_PILL = "#e0f2f1"      # 胶囊底色

def get_font(size: int, bold: bool = False):
    return bidviz.pil_font(size, bold)

def draw_pill(draw, x, y, text, font, bg_color="#e0f2f1", text_color="#00695c"):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 16, 8
    draw.rounded_rectangle((x, y, x + tw + pad_x * 2, y + th + pad_y * 2), radius=10, fill=bg_color)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=text_color)
    return tw + pad_x * 2

def draw_table_bordered(draw, x, y, w, headers, rows, col_widths):
    font_th = get_font(28, True)
    font_td = get_font(26, False)
    font_pill = get_font(22, True)
    
    # Header
    th_h = 75
    draw.rectangle((x, y, x + w, y + th_h), fill=DARK_TEAL)
    cur_x = x
    for i, h in enumerate(headers):
        draw.text((cur_x + 18, y + 22), h, font=font_th, fill="white")
        cur_x += col_widths[i]
        if i < len(headers) - 1:
            draw.line((cur_x, y, cur_x, y + th_h), fill="#2e4d65", width=2)
            
    cur_y = y + th_h
    for r_idx, row in enumerate(rows):
        row_h = 68
        bg = "#ffffff" if r_idx % 2 == 0 else "#f8fbfd"
        draw.rectangle((x, cur_y, x + w, cur_y + row_h), fill=bg, outline=BORDER_COLOR, width=1)
        cur_x = x
        for c_idx, val in enumerate(row):
            cx = cur_x + 18
            cy = cur_y + 18
            if isinstance(val, tuple) and val[0] == "pill":
                draw_pill(draw, cx, cy - 6, val[1], font_pill, val[2], val[3])
            else:
                draw.text((cx, cy), str(val), font=font_td, fill=TEXT_MAIN)
            cur_x += col_widths[c_idx]
            if c_idx < len(row) - 1:
                draw.line((cur_x, cur_y, cur_x, cur_y + row_h), fill=BORDER_COLOR, width=1)
        cur_y += row_h
        
    draw.rectangle((x, y, x + w, cur_y), outline=DARK_TEAL, width=2)
    return cur_y - y

def fit_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    src = image.convert("RGB")
    ratio = max(size[0] / src.width, size[1] / src.height)
    resized = src.resize((round(src.width * ratio), round(src.height * ratio)), Image.Resampling.LANCZOS)
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))

def draw_header_bar(draw, title, subtitle, page_num):
    # Top blueprint banner
    draw.rectangle((0, 0, W, 220), fill=DARK_TEAL)
    # Project title
    draw.text((120, 45), "中国科学技术大学东区新建 10kV 专线项目 · 施工组织设计", font=get_font(32), fill="#7ea6c2")
    font_title = get_font(64, True)
    draw.text((120, 95), title, font=font_title, fill="white")
    if subtitle:
        t_bbox = draw.textbbox((0, 0), title, font=font_title)
        sub_x = 120 + (t_bbox[2] - t_bbox[0]) + 45
        draw.text((sub_x, 115), f"|  {subtitle}", font=get_font(36), fill="#4db6ac")
    
    # Top right index block
    draw.rectangle((W - 860, 40, W - 120, 180), fill="#102d42", outline="#244c6b", width=2)
    draw.text((W - 830, 60), "STAGE / SECTION", font=get_font(24), fill="#6d92ab")
    draw.text((W - 830, 95), "第一章 第四节 主要工序", font=get_font(30, True), fill="white")
    draw.text((W - 410, 68), page_num, font=get_font(56, True), fill=CYAN_LINE)

def draw_footer_bar(draw, style_name):
    y = H - 180
    draw.rectangle((0, y, W, H), fill="#e9eff2")
    draw.line((0, y, W, y), fill=BORDER_COLOR, width=2)
    
    # Left legal & BIM tag
    draw.rectangle((120, y + 40, 480, y + 130), fill=DARK_TEAL)
    draw.text((140, y + 55), "BIM 数字化施工交付体系", font=get_font(26, True), fill="white")
    draw.text((140, y + 90), "HIGH-DENSITY ATLAS SPEC", font=get_font(20), fill="#7ea6c2")
    
    draw.text((520, y + 55), "技术声明：本页包含三维BIM工况图、节点详图、装配示意与现场实测数据，严格遵循国家标准规范与设计要求。", font=get_font(26), fill=TEXT_MUTED)
    draw.text((520, y + 95), "审查状态：★ 方案通过 (总工签发)    审核责任人：项目总工 / 质检工程师    版本编号：VER-2025-A3", font=get_font(24), fill=DARK_TEAL)
    
    draw.text((W - 700, y + 70), f"版面形态：{style_name}", font=get_font(32, True), fill=TEAL_ACCENT)

# =========================================================================
# STYLE A: 高端工程画册级 (One Hero + Master-Detail Linked Callouts + Tables)
# =========================================================================
def render_style_a():
    img = Image.new("RGB", (W, H), BG_LIGHT)
    draw = ImageDraw.Draw(img)
    draw_header_bar(draw, "电缆沟与工井标准施工工法", "分部工程工序穿插与节点构造联动", "A3-PROT-A")
    
    # ----------------- SECTION 1: 左侧 60% 宽度复合图文区 -----------------
    left_x = 120
    left_w = 2850
    
    # 1.1 主图容器（BIM 施工场景主图）
    main_img_y = 260
    main_img_h = 1680
    draw.rectangle((left_x, main_img_y, left_x + left_w, main_img_y + main_img_h), fill=PANEL_BG, outline=BORDER_COLOR, width=2)
    
    # 载入 A3-005.png 作为工况全景主图 (fit_cover 铺满无灰边)
    src_hero = Image.open("full-project/work/visuals/A3-005.png").convert("RGB")
    hero_fit = fit_cover(src_hero, (left_w - 20, main_img_h - 80))
    img.paste(hero_fit, (left_x + 10, main_img_y + 70))
    
    # 主图上标题区
    draw.rectangle((left_x, main_img_y, left_x + left_w, main_img_y + 70), fill="#1a3d59")
    draw.text((left_x + 30, main_img_y + 18), "图 1.1  电缆沟开挖、垫层施工、砌筑与接地分层施工工况全景 (三维BIM工法图解)", font=get_font(30, True), fill="white")
    draw.text((left_x + left_w - 380, main_img_y + 20), "比例：空间轴测无畸变", font=get_font(24), fill="#a5c4db")
    
    # 1.2 主图下方挂接 3 张联动节点特写图 (Detail Callouts)
    detail_y = main_img_y + main_img_h + 40
    detail_h = 760
    detail_w = (left_w - 60) // 3
    
    detail_files = ["A3-006.png", "A3-004.png", "A3-002.png"]
    detail_titles = [
        "图 1.2-A 支架膨胀螺栓与防腐节点",
        "图 1.2-B 沟槽开挖边坡与围护详图",
        "图 1.2-C 电力营地与加工区布置"
    ]
    
    for i in range(3):
        dx = left_x + i * (detail_w + 30)
        draw.rectangle((dx, detail_y, dx + detail_w, detail_y + detail_h), fill=PANEL_BG, outline=BORDER_COLOR, width=2)
        draw.rectangle((dx, detail_y, dx + detail_w, detail_y + 55), fill="#254b6b")
        draw.text((dx + 20, detail_y + 14), detail_titles[i], font=get_font(24, True), fill="white")
        
        # 贴图 (使用 fit_contain 防止图片内部引线文字被裁断)
        if Path(f"full-project/work/visuals/{detail_files[i]}").exists():
            d_img = Image.open(f"full-project/work/visuals/{detail_files[i]}").convert("RGB")
            d_fit = bidviz.fit_contain(d_img, (detail_w - 12, detail_h - 62))
            img.paste(d_fit, (dx + 6, detail_y + 56))
            
    # 1.3 底部施工步骤流图卡 (Step Banner)
    step_y = detail_y + detail_h + 40
    step_h = 440
    draw.rounded_rectangle((left_x, step_y, left_x + left_w, step_y + step_h), radius=16, fill="#eaf1f5", outline=BORDER_COLOR, width=2)
    draw.rectangle((left_x, step_y, left_x + left_w, step_y + 60), fill=DARK_TEAL)
    draw.text((left_x + 30, step_y + 15), "电缆沟分部分项标准化施工作业顺序与门禁控制要求", font=get_font(28, True), fill="white")
    
    steps = [
        ("01 测量放线与勘察", "现场管线勘察，明确地下管线分布；用经纬仪、水准仪测放轴线与标高控制桩。"),
        ("02 机械开挖配合清底", "放坡系数1:1，挖至槽底上20cm停机改人工清底；两侧做明排水沟与集水井。"),
        ("03 垫层夯实与砌筑", "基底原土夯实，浇筑C15砼垫层；采用Mu10砖、M5砂浆'三一'砌筑，水平缝8~12mm。"),
        ("04 支架与接地安装", "支架焊接≥50mm，膨胀螺栓深入墙体≥80mm；扁铁搭接≥2倍宽度，三面满焊。"),
        ("05 盖板拼装与养护", "角铁边框45°焊接热镀锌；垫3mm橡胶皮；覆盖浇水养护≥7天；沟内严禁积水。")
    ]
    step_item_w = (left_w - 80) // 5
    for i, (st, desc) in enumerate(steps):
        sx = left_x + 20 + i * (step_item_w + 10)
        sy = step_y + 80
        draw.rounded_rectangle((sx, sy, sx + step_item_w, sy + step_h - 100), radius=10, fill="white", outline="#cbd7de", width=1)
        draw.text((sx + 16, sy + 18), st, font=get_font(24, True), fill=DARK_TEAL)
        draw.line((sx + 16, sy + 52, sx + step_item_w - 16, sy + 52), fill=CYAN_LINE, width=3)
        
        # 描述换行
        lines = bidviz.wrap_text(desc, 14)
        ty = sy + 65
        for l in lines:
            draw.text((sx + 16, ty), l, font=get_font(21), fill=TEXT_MAIN)
            ty += 32

    # ----------------- SECTION 2: 右侧 40% 宽度精细数据与表格区 -----------------
    right_x = left_x + left_w + 50
    right_w = W - right_x - 120
    
    # 2.1 关键质量控制参数表 (Bordered Engineering Table)
    table1_y = 260
    draw.rectangle((right_x, table1_y, right_x + right_w, table1_y + 60), fill=DARK_TEAL)
    draw.text((right_x + 25, table1_y + 15), "表 1.1 电缆沟及工井核心工艺参数与技术控制指标", font=get_font(28, True), fill="white")
    
    headers1 = ["控制分项", "规范与设计控制参数", "检测方法与频率", "控制等级"]
    col_w1 = [360, 680, 480, 240]
    rows1 = [
        ["沟槽开挖放坡", "自然放坡 1:1，严禁超挖", "坡度尺每20m测1处", ("pill", "强制标准", "#ffebee", "#c62828")],
        ["机械开挖预留", "人工清底预留厚度 200mm", "水准仪全程跟测标高", ("pill", "主控项目", "#e8f5e9", "#2e7d32")],
        ["砖砌体砂浆", "Mu10烧结砖，M5.0水泥砂浆", "按批次做抗压试块", ("pill", "合格检验", "#e1f5fe", "#0277bd")],
        ["砌体水平灰缝", "灰缝 8～12mm，饱满度 ≥80%", "百格网与百分表抽检", ("pill", "主控项目", "#e8f5e9", "#2e7d32")],
        ["支架焊接长度", "双面焊接或搭接焊缝 ≥50mm", "焊缝卡尺全数检查", ("pill", "质量保证", "#fff3e0", "#ef6c00")],
        ["支架膨胀螺栓", "深入墙体基体深度 ≥80mm", "钻孔深度尺与拉拔抽测", ("pill", "安全受力", "#f3e5f5", "#7b1fa2")],
        ["钢材除锈防腐", "达到 St2 级，面漆厚度 ≥30μm", "漆膜测厚仪每构件测3点", ("pill", "防腐耐久", "#e0f2f1", "#00695c")],
        ["接地扁铁搭接", "搭接长度 ≥ 2 倍扁铁宽度，三边焊", "游标卡尺全数检测", ("pill", "防雷电气", "#ffebee", "#c62828")],
        ["沟底排水坡度", "纵向坡度 1~2%，横向坡度 2%", "水准仪沿水线逐段测量", ("pill", "使用功能", "#e8f5e9", "#2e7d32")],
        ["预制盖板养护", "常温覆盖洒水养护时间 ≥7天", "养护记录台账核查", ("pill", "强度保障", "#e1f5fe", "#0277bd")]
    ]
    t1_h = draw_table_bordered(draw, right_x, table1_y + 60, right_w, headers1, rows1, col_w1)
    
    # 2.2 质量通病防治与应急风险矩阵表 (Risk & Control Table)
    table2_y = table1_y + 60 + t1_h + 40
    draw.rectangle((right_x, table2_y, right_x + right_w, table2_y + 60), fill="#8a2a2a")
    draw.text((right_x + 25, table2_y + 15), "表 1.2 质量通病防护、环境风险与应急处置矩阵", font=get_font(28, True), fill="white")
    
    headers2 = ["风险与通病", "诱因分析与征兆", "预防及纠偏处置对策", "处置权限"]
    col_w2 = [340, 560, 620, 240]
    rows2 = [
        ["边坡开裂塌方", "降雨入渗、坑顶行车荷载过重", "坑顶10m严禁堆载，加打无间隔槽钢桩", ("pill", "总工决策", "#ffebee", "#c62828")],
        ["沟槽积水浸泡", "地表水汇入或地下潜水溢出", "沿槽设200×300排水沟，20m设集水井强排", ("pill", "施工队长", "#fff3e0", "#ef6c00")],
        ["既有管线破坏", "地下电缆/煤气管线探测不准", "人工轻度探槽，未标明管线严禁机械触碰", ("pill", "安全监察", "#ffebee", "#c62828")],
        ["粉刷空鼓开裂", "基层未充分浇水湿润、素浆风干", "108胶素浆拉毛，严格按配比两遍抹压", ("pill", "质检员", "#e8f5e9", "#2e7d32")],
        ["盖板断裂翘动", "反向受力运输、支承面未垫平", "反向受力报废；支撑点垫3mm耐老化橡胶垫", ("pill", "技术负责人", "#e1f5fe", "#0277bd")]
    ]
    t2_h = draw_table_bordered(draw, right_x, table2_y + 60, right_w, headers2, rows2, col_w2)
    
    # 2.3 责任矩阵与检验批报验要求 (Responsibility Box)
    resp_y = table2_y + 60 + t2_h + 40
    resp_h = H - resp_y - 220
    draw.rounded_rectangle((right_x, resp_y, right_x + right_w, resp_y + resp_h), radius=16, fill=PANEL_BG, outline=BORDER_COLOR, width=2)
    draw.rectangle((right_x, resp_y, right_x + right_w, resp_y + 60), fill=DARK_TEAL)
    draw.text((right_x + 25, resp_y + 15), "检验与试验计划 (ITP) 联合报验管理矩阵", font=get_font(28, True), fill="white")
    
    itp_items = [
        ("工序① 沟槽开挖验收", "施工单位初验合格 → 报验监理、勘察、业主单位进行四方联合验槽 → 签署验槽记录表。"),
        ("工序② 隐蔽工程报验", "垫层浇筑前、接地扁铁焊接完成后，必须进行隐检并拍照存档，未经监理签字严禁隐蔽。"),
        ("工序③ 砂浆与试块送检", "按每台班/每层抽样制作砂浆与混凝土试块，委托具备资质的第三方实验室标养试压。"),
        ("工序④ 实体观感与交工", "棱角顺直、沟壁平整无砂眼龟裂、盖板无翘动，水准仪测定纵横找坡顺畅无倒坡积水。")
    ]
    for idx, (itp_title, itp_content) in enumerate(itp_items):
        iy = resp_y + 80 + idx * 140
        draw.text((right_x + 30, iy), itp_title, font=get_font(26, True), fill=DARK_TEAL)
        draw_pill(draw, right_x + 400, iy - 4, "停检点(H)", font=get_font(20, True), bg_color="#ffebee", text_color="#c62828")
        draw.text((right_x + 30, iy + 45), itp_content, font=get_font(23), fill=TEXT_MUTED)
        if idx < len(itp_items) - 1:
            draw.line((right_x + 30, iy + 115, right_x + right_w - 30, iy + 115), fill="#e4ebef", width=1)
            
    draw_footer_bar(draw, "方案 A：高端工程画册级 (One Hero + Master-Detail Linked Callouts + Tables)")
    
    out_path = Path("full-project/output/A3-PROTOTYPE-STYLE-A.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, dpi=(300, 300))
    print("Rendered:", out_path)
    return out_path

# =========================================================================
# STYLE B: 现代模块化卡片群 (Bento Box: 6 块独立功能卡片矩阵编排)
# =========================================================================
def render_style_b():
    img = Image.new("RGB", (W, H), BG_LIGHT)
    draw = ImageDraw.Draw(img)
    draw_header_bar(draw, "电缆沟与工井综合施工工法 (便当盒模块化看板)", "Bento Box 6 大功能单元并行编排", "A3-PROT-B")
    
    # 3 列 x 2 行 网格参数
    margin_x = 120
    margin_y = 260
    grid_w = W - margin_x * 2
    grid_h = H - margin_y - 220
    gap = 40
    
    col_w = (grid_w - gap * 2) // 3
    row_h = (grid_h - gap) // 2
    
    cards = [
        # (col, row, title, img_file, subtitle)
        (0, 0, "模块一：开挖与基础准备", "A3-004.png", "放坡开挖 / 基槽支护 / 地下管线"),
        (1, 0, "模块二：垫层浇筑与底板夯实", "A3-005.png", "C15垫层 / 坡度线测定 / 水平桩"),
        (2, 0, "模块三：砖砌体沟壁与灰缝控制", "A3-006.png", "Mu10砖 / M5砂浆 / 8~12mm灰缝"),
        (0, 1, "模块四：预埋构件与扁铁接地", "A3-002.png", "焊接≥50mm / 防腐St2级 / 膨胀螺栓"),
        (1, 1, "模块五：抹灰粉刷与找坡压光", "A3-003.png", "两遍压光 / 纵坡1~2% / 严禁积水"),
        (2, 1, "模块六：预制盖板制作与质量交验", "A3-001.png", "角铁45°焊接 / 养护≥7天 / 橡胶垫")
    ]
    
    # 每个模块包含的真实数据表行
    card_tables = [
        [("开挖放坡系数", "自然放坡 1:1", "严禁超挖"), ("清底预留土层", "200mm厚", "改人工清底"), ("护栏警示", "四周封闭红灯", "专职人员观察")],
        [("基底原土处理", "机械+人工打夯", "承载力达标"), ("垫层标高测定", "水准仪全程跟测", "误差≤±5mm"), ("垫层厚度控制", "设计标高C15砼", "无蜂窝杂物")],
        [("砖块强度等级", "Mu10烧结砖", "提前浇水湿润"), ("水平灰缝厚度", "8～12mm", "皮数杆双面控线"), ("砂浆饱满度", "≥80%", "严禁出现通缝")],
        [("支架水平间距", "≤ 400mm", "轴线弹线找平"), ("焊接搭接长度", "≥ 2倍扁铁宽度", "三边满焊牢固"), ("防腐漆膜厚度", "≥ 30μm (St2级)", "涂刷银粉色调和漆")],
        [("打糙配合比", "1:3 水泥砂浆", "提前充分湿润"), ("面层压光遍数", "不少于 3 遍", "终凝前消除抹纹"), ("沟底找坡比例", "纵向1~2% 横向2%", "拉线压出走水槽")],
        [("角铁边框加工", "45°拼角对称焊", "热镀锌后矫正"), ("盖板洒水养护", "常温覆膜 ≥7天", "达12MPa方准上人"), ("搁置减震调平", "3mm橡胶皮垫", "拉线顺直无翘动")]
    ]
    
    for idx, (c, r, title, img_file, subtitle) in enumerate(cards):
        cx = margin_x + c * (col_w + gap)
        cy = margin_y + r * (row_h + gap)
        
        # 卡片底框
        draw.rounded_rectangle((cx, cy, cx + col_w, cy + row_h), radius=20, fill=PANEL_BG, outline=BORDER_COLOR, width=2)
        
        # 卡片 Header
        draw.rectangle((cx, cy, cx + col_w, cy + 85), fill=DARK_TEAL)
        draw.text((cx + 25, cy + 18), title, font=get_font(32, True), fill="white")
        draw.text((cx + col_w - 380, cy + 28), subtitle, font=get_font(22), fill="#8cb5d4")
        
        # 卡片内部：改为【上图下表】结构，保持横向比例不裁剪任何标签
        inner_img_w = col_w - 30
        inner_img_h = int((row_h - 105) * 0.56)
        img_x = cx + 15
        img_y = cy + 95
        
        draw.rectangle((img_x, img_y, img_x + inner_img_w, img_y + inner_img_h), fill="#eef3f6", outline="#cbd7de", width=1)
        if Path(f"full-project/work/visuals/{img_file}").exists():
            c_img = Image.open(f"full-project/work/visuals/{img_file}").convert("RGB")
            c_fit = bidviz.fit_contain(c_img, (inner_img_w - 8, inner_img_h - 8))
            img.paste(c_fit, (img_x + 4, img_y + 4))
            
        # 卡片内部下方：紧凑横排表格 + 交底要点
        tbl_x = img_x
        tbl_w = inner_img_w
        tbl_y = img_y + inner_img_h + 12
        
        # 小表头
        draw.rectangle((tbl_x, tbl_y, tbl_x + tbl_w, tbl_y + 44), fill="#1f425e")
        draw.text((tbl_x + 16, tbl_y + 10), "工艺控制项", font=get_font(20, True), fill="white")
        draw.text((tbl_x + int(tbl_w * 0.35), tbl_y + 10), "核心技术与规范标准", font=get_font(20, True), fill="white")
        draw.text((tbl_x + int(tbl_w * 0.76), tbl_y + 10), "门禁判定", font=get_font(20, True), fill="white")
        
        cur_ty = tbl_y + 44
        for item1, item2, item3 in card_tables[idx]:
            r_h = 52
            draw.rectangle((tbl_x, cur_ty, tbl_x + tbl_w, cur_ty + r_h), fill="#ffffff", outline="#dae3e8", width=1)
            draw.text((tbl_x + 16, cur_ty + 14), item1, font=get_font(20, True), fill=DARK_TEAL)
            draw.text((tbl_x + int(tbl_w * 0.35), cur_ty + 14), item2, font=get_font(20), fill=TEXT_MAIN)
            draw_pill(draw, tbl_x + int(tbl_w * 0.76), cur_ty + 10, item3, get_font(17, True), "#e0f2f1", "#00695c")
            cur_ty += r_h
            
        # 小表下方技术操作核心交底框
        memo_y = cur_ty + 10
        memo_h = cy + row_h - 14 - memo_y
        draw.rounded_rectangle((tbl_x, memo_y, tbl_x + tbl_w, memo_y + memo_h), radius=8, fill="#f2f7fa", outline="#c9d9e2", width=1)
        
        memos = [
            "★ 交底要点：必须与所有管线单位现场标识复核，无间隔打入槽钢桩防护，严禁机械碰撞管线。",
            "★ 交底要点：水准仪全程跟测，严禁超挖倒坡；基础较宽时在基槽中间加设水平控制桩。",
            "★ 交底要点：采用耐火砖与M5砂浆砌筑，转角同时砌筑；砌到标高及时埋设铁件并做试块。",
            "★ 交底要点：铁件预制块上拉通线平整焊接；螺栓孔径精准匹配，钻孔后清孔彻底植入胶栓。",
            "★ 交底要点：塌饼冲筋找平；先做好护角，再粉刷上部面层与侧壁；加盖滤水盖防止淤塞。",
            "★ 交底要点：考虑受力方向严禁反向受力运输；两头经纬仪定点拉线，接缝双面勾缝平平整整。"
        ]
        draw.text((tbl_x + 14, memo_y + 18), memos[idx], font=get_font(19, True), fill=ORANGE_WARN)


    draw_footer_bar(draw, "方案 B：现代模块化卡片群 (Bento Box: 6 大功能单元并行编排)")
    
    out_path = Path("full-project/output/A3-PROTOTYPE-STYLE-B.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, dpi=(300, 300))
    print("Rendered:", out_path)
    return out_path

if __name__ == "__main__":
    p_a = render_style_a()
    p_b = render_style_b()
    print("Both prototype images rendered successfully!")
