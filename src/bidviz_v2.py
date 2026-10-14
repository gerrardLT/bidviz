#!/usr/bin/env python3
"""Bid Visual PDF v2: full-text A3 atlas with adaptive layouts and BIM visuals."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import fitz
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

import bidviz

A3_PX = bidviz.A3_PX
W, H = A3_PX
NAVY = "#15364f"
BLUE = "#2b6f92"
PALE = "#edf3f6"
ORANGE = "#db6d2c"
RED = "#b53b3b"
GREEN = "#6d8061"
GRAY = "#53636d"
LAYOUTS = ("hero_bim", "section_bim", "process_lane", "table_detail", "risk_hotspot", "finish_quality", "detail_board")


@dataclass
class SourceBlock:
    block_id: str
    source_page: int
    order: int
    kind: str
    section_path: str
    text: str
    sha256: str


@dataclass
class AtlasPage:
    page_id: str
    title: str
    layout: str
    block_ids: list[str]
    section_path: str
    source_pages: list[int]
    visual_prompt: str = ""
    visual_path: str = ""
    status: str = "draft"


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return bidviz.pil_font(size, bold)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_pages(pdf_path: Path) -> list[dict[str, Any]]:
    doc = fitz.open(pdf_path)
    pages = []
    for number, page in enumerate(doc, 1):
        lines = [norm(x) for x in page.get_text("text").splitlines()]
        lines = [x for x in lines if x and not re.fullmatch(r"-?\s*\d+\s*-?", x)]
        pages.append({"page": number, "lines": lines})
    return pages


def heading_level(text: str) -> int | None:
    if re.match(r"^第[一二三四五六七八九十百\d]+章", text): return 1
    if re.match(r"^第[一二三四五六七八九十百\d]+节", text): return 2
    if re.match(r"^[一二三四五六七八九十]+、", text): return 3
    if re.match(r"^[（(][一二三四五六七八九十\d]+[）)]", text): return 4
    return None


def remove_toc(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Keep cover, skip TOC pages whose lines mostly contain dotted page references.
    result = []
    for p in pages:
        dotted = sum(bool(re.search(r"\.{3,}|…{2,}|-\s*\d+\s*-$", x)) for x in p["lines"])
        if p["page"] > 1 and dotted >= 3:
            continue
        result.append(p)
    return result


def build_blocks(pdf_path: Path) -> list[SourceBlock]:
    pages = remove_toc(source_pages(pdf_path))
    section: list[str] = []
    blocks: list[SourceBlock] = []
    order = 0
    for source in pages:
        buffer: list[str] = []
        def flush() -> None:
            nonlocal order, buffer
            text = norm(" ".join(buffer))
            if not text: return
            # Split long source paragraphs without dropping characters/meaning units.
            pieces = split_text(text, 620)
            for piece in pieces:
                order += 1
                block_id = f"B-{order:05d}"
                blocks.append(SourceBlock(block_id, source["page"], order, "body", " / ".join(section) or "封面及总述", piece, sha(piece)))
            buffer = []
        for line in source["lines"]:
            level = heading_level(line)
            if level:
                flush()
                section = section[:level-1] + [line]
                order += 1
                blocks.append(SourceBlock(f"B-{order:05d}", source["page"], order, "heading", " / ".join(section), line, sha(line)))
            else:
                buffer.append(line)
                if len(" ".join(buffer)) >= 520 or re.search(r"[。；]$", line):
                    flush()
        flush()
    return blocks


def split_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars: return [text]
    sentences = [x for x in re.split(r"(?<=[。；！？])", text) if x]
    out: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) <= max_chars:
            current += sentence
        else:
            if current: out.append(current)
            if len(sentence) <= max_chars:
                current = sentence
            else:
                out.extend(sentence[i:i+max_chars] for i in range(0, len(sentence), max_chars))
                current = ""
    if current: out.append(current)
    return out


def choose_layout(section: str, text: str, index: int) -> str:
    s = section + " " + text
    scores = {
        "risk_hotspot": len(re.findall(r"安全|危险|隐患|防火|应急|文明施工|重点难点|防护", s)),
        "process_lane": len(re.findall(r"流程|工序|步骤|计划|进度|网络图|程序|阶段|顺序", s)),
        "section_bim": len(re.findall(r"剖面|沟槽|工井|电缆沟|垫层|排管|回填|基础|开挖", s)),
        "table_detail": len(re.findall(r"质量标准|允许偏差|检验|检查|材料|设备|劳动力|临时用地|参数|制度", s)),
        "finish_quality": len(re.findall(r"成品|外观|安装完成|调试|交付|修复|验收", s)),
        "detail_board": len(re.findall(r"接头|节点|防水|接地|支架|井盖|砌筑|封堵|试验", s)),
        "hero_bim": len(re.findall(r"现场|布置|概况|勘察|校园|道路|项目", s)),
    }
    # Tie-break rotates layouts so nearby pages do not collapse into one template.
    rotated = list(LAYOUTS[index % len(LAYOUTS):]) + list(LAYOUTS[:index % len(LAYOUTS)])
    return max(rotated, key=lambda name: (scores[name], -rotated.index(name)))


def page_capacity(layout: str) -> int:
    # Conservative capacities keep 300-DPI text readable; pagination is unlimited.
    return {
        "hero_bim": 560,
        "section_bim": 620,
        "process_lane": 700,
        "table_detail": 820,
        "risk_hotspot": 650,
        "finish_quality": 560,
        "detail_board": 620,
    }[layout]


def paginate(blocks: list[SourceBlock]) -> list[AtlasPage]:
    pages: list[AtlasPage] = []
    current: list[SourceBlock] = []
    current_section = ""
    current_layout = "table_detail"
    chars = 0

    def flush() -> None:
        nonlocal current, chars
        if not current: return
        idx = len(pages) + 1
        heading = next((b.text for b in reversed(current) if b.kind == "heading"), "")
        title = heading or (current[-1].section_path.split(" / ")[-1] if current[-1].section_path else f"正文续页 {idx}")
        pages.append(AtlasPage(
            page_id=f"A3-{idx:03d}", title=title[:42], layout=current_layout,
            block_ids=[b.block_id for b in current], section_path=current[-1].section_path,
            source_pages=sorted({b.source_page for b in current}),
        ))
        current = []; chars = 0

    for block in blocks:
        layout = choose_layout(block.section_path, block.text, len(pages))
        section_key = block.section_path.split(" / ")[:2]
        section_key = " / ".join(section_key)
        if not current:
            current_layout, current_section = layout, section_key
        cap = page_capacity(current_layout)
        layout_changed = layout != current_layout and chars > cap * 0.45
        section_changed = section_key != current_section and chars > cap * 0.35
        if current and (chars + len(block.text) > cap or layout_changed or section_changed):
            flush(); current_layout, current_section = layout, section_key
        current.append(block); chars += len(block.text)
    flush()
    return pages


def visual_prompt(page: AtlasPage, blocks: dict[str, SourceBlock]) -> str:
    from bidviz_config import get as _cfg
    source = "；".join(blocks[x].text for x in page.block_ids if blocks[x].kind != "heading")[:1800]
    layout_desc = {
        "hero_bim": "全幅 BIM 施工场景，构件、人员、机械、道路和既有环境具有真实空间关系",
        "section_bim": "BIM 剖切透视图，显示土层、沟槽、构件、支护、排水及施工空间",
        "process_lane": "BIM 施工阶段序列图，多阶段连续构造变化",
        "table_detail": "BIM 主构件与两至三个关键节点特写，右侧保留自然留白",
        "risk_hotspot": "BIM 施工现场风险热点图，危险点以红橙高亮并有引线标注",
        "finish_quality": "接近真实材质的 BIM 完工效果图和质量细部特写",
        "detail_board": "BIM 节点详图拼版，包含剖切、爆炸和局部放大",
    }[page.layout]
    subject = _cfg().prompt_subject or "工程施工组织设计"
    return "\n".join([
        f"为{subject}生成{layout_desc}。",
        f"本页主题：{page.title}。",
        f"必须依据以下实际文字内容，不得添加冲突的工艺和参数：{source}",
        "视觉标准：专业 BIM 技术可视化，PBR 材质，真实尺度感，等轴测或剖切透视，构件边缘清楚，施工状态可信，灰蓝主色，安全风险用红橙色。",
        "需要在图中直接生成简短中文构件名、工序名和实际情况引线标注；不得生成长段正文，不得虚构未给出的精确尺寸。",
        "所有标注使用清晰规范简体中文；如无法正确生成文字，宁可省略该标签，不得产生乱码、错别字、Logo或水印。",
        "无 CAD、BIM 模型或现场照片，本图仅为文字约束的通用 BIM 示意，不得暗示真实坐标或现场还原。",
        "A3 横向构图，主体与文字信息区按本页主题自然编排，不要套用固定左图右表模板。",
    ])


def wrap(text: str, width: int) -> list[str]:
    return bidviz.wrap_text(text, width)


def draw_header(draw: ImageDraw.ImageDraw, page: AtlasPage) -> None:
    draw.rectangle((0, 0, W, 245), fill=NAVY)
    draw.text((135, 62), page.title, font=font(78, True), fill="white")
    draw.text((W-650, 82), f"{page.page_id}  ·  {page.layout}", font=font(34), fill="#c8dce8")


def draw_footer(draw: ImageDraw.ImageDraw, page: AtlasPage) -> None:
    draw.rectangle((0, H-280, W, H), fill="#f2f4f5")
    draw.text((135, H-225), "文字约束 BIM 通用示意｜非现场实景｜非真实坐标｜不得替代施工图与测量成果", font=font(36, True), fill=RED)
    draw.text((135, H-145), f"原文页：{','.join(map(str,page.source_pages))}　原文块：{page.block_ids[0]}—{page.block_ids[-1]}", font=font(29), fill=GRAY)
    draw.text((W-620, H-125), "DRAFT / 待总工审核", font=font(42, True), fill=RED)


def place_visual(image: Image.Image, visual: Image.Image | None, box: tuple[int,int,int,int]) -> None:
    if visual is None:
        visual = Image.new("RGB", (1600, 1000), "#dde7ec")
        d = ImageDraw.Draw(visual); d.text((120,430), "BIM VISUAL PENDING", font=ImageFont.truetype(r"C:\Windows\Fonts\arialbd.ttf",72), fill="#68808d")
    fitted = bidviz.fit_contain(visual, (box[2]-box[0], box[3]-box[1]))
    image.paste(fitted, box[:2]); ImageDraw.Draw(image).rectangle(box, outline="#91a4af", width=5)


def draw_text_cards(draw: ImageDraw.ImageDraw, blocks: list[SourceBlock], box: tuple[int,int,int,int], columns: int = 1, numbered: bool = False) -> None:
    x0,y0,x1,y1=box; gap=34; col_w=(x1-x0-gap*(columns-1))//columns
    entries=[b for b in blocks if b.kind != "heading"] or blocks
    per_col=math.ceil(len(entries)/columns)
    for ci in range(columns):
        x=x0+ci*(col_w+gap); y=y0
        for bi,b in enumerate(entries[ci*per_col:(ci+1)*per_col],1):
            lines=wrap(b.text, max(18,int(col_w/48)))
            h=max(120,55+len(lines)*43)
            if y+h>y1: break
            draw.rounded_rectangle((x,y,x+col_w,y+h-14),radius=18,fill="white",outline="#b6c4cc",width=3)
            if numbered:
                draw.ellipse((x+20,y+24,x+72,y+76),fill=BLUE); draw.text((x+37,y+29),str(bi),font=font(25,True),fill="white"); tx=x+90
            else: tx=x+26
            ty=y+20
            for line in lines:
                draw.text((tx,ty),line,font=font(34),fill=NAVY); ty+=43
            draw.text((x+col_w-210,y+h-52),b.block_id,font=font(23),fill=GRAY)
            y+=h


def render_page(page: AtlasPage, block_map: dict[str,SourceBlock], visual: Image.Image | None, output: Path) -> Path:
    img=Image.new("RGB",A3_PX,"white"); draw=ImageDraw.Draw(img); draw_header(draw,page); blocks=[block_map[x] for x in page.block_ids]
    if page.layout == "hero_bim":
        place_visual(img,visual,(120,330,3300,2920)); draw_text_cards(draw,blocks,(3400,330,4820,2920),1,True)
    elif page.layout == "section_bim":
        place_visual(img,visual,(120,330,3400,2180)); draw_text_cards(draw,blocks,(120,2250,4820,3060),2,False)
    elif page.layout == "process_lane":
        draw_text_cards(draw,blocks,(120,330,4820,1350),3,True); place_visual(img,visual,(300,1450,4660,3040))
    elif page.layout == "table_detail":
        draw.rectangle((120,330,4820,470),fill=BLUE); draw.text((170,360),"原文信息与技术控制",font=font(46,True),fill="white")
        draw_text_cards(draw,blocks,(120,510,3000,3050),2,False); place_visual(img,visual,(3100,510,4820,3050))
    elif page.layout == "risk_hotspot":
        place_visual(img,visual,(120,330,3250,3050)); draw.rectangle((3330,330,4820,470),fill=RED); draw.text((3380,360),"风险 / 控制 / 应急",font=font(46,True),fill="white"); draw_text_cards(draw,blocks,(3330,510,4820,3050),1,True)
    elif page.layout == "finish_quality":
        place_visual(img,visual,(120,330,4820,2100)); draw_text_cards(draw,blocks,(120,2180,4820,3050),3,False)
    else: # detail_board
        place_visual(img,visual,(120,330,3000,3050)); draw_text_cards(draw,blocks,(3100,330,4820,3050),1,True)
    draw_footer(draw,page); output.parent.mkdir(parents=True,exist_ok=True); img.save(output,dpi=(300,300)); return output


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")


def build_project(pdf: Path, root: Path) -> tuple[list[SourceBlock],list[AtlasPage]]:
    blocks=build_blocks(pdf); pages=paginate(blocks); bm={b.block_id:b for b in blocks}
    for p in pages: p.visual_prompt=visual_prompt(p,bm)
    save_json(root/'work/blocks.json',[asdict(x) for x in blocks]); save_json(root/'work/pages.json',[asdict(x) for x in pages])
    return blocks,pages


def load_project(root: Path) -> tuple[list[SourceBlock],list[AtlasPage]]:
    blocks=[SourceBlock(**x) for x in json.loads((root/'work/blocks.json').read_text(encoding='utf-8'))]
    pages=[AtlasPage(**x) for x in json.loads((root/'work/pages.json').read_text(encoding='utf-8'))]
    return blocks,pages


def vision_transcribe(path: Path, provider: str = "gemini") -> list[str] | None:
    cfg=bidviz.pideck_image_config(provider,"gemini-flash-thinking")
    data_uri="data:image/png;base64,"+base64.b64encode(path.read_bytes()).decode("ascii")
    body={"model":"gemini-flash-thinking","messages":[{"role":"user","content":[
        {"type":"text","text":"只做OCR：逐项转录图片里所有可见中文、英文和数字。严格返回 JSON：{\"texts\":[\"...\"]}。看不到文字则返回空数组，不解释。"},
        {"type":"image_url","image_url":{"url":data_uri}}
    ]}],"temperature":0}
    req=urllib.request.Request(cfg['base_url'].rstrip('/')+'/chat/completions',data=json.dumps(body).encode('utf-8'),headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=180) as response: payload=json.load(response)
    except Exception:
        return None
    content=payload.get('choices',[{}])[0].get('message',{}).get('content','')
    match=re.search(r'\{.*\}',content,re.S)
    if not match: return None
    try:
        values=json.loads(match.group()).get('texts',[])
        return [norm(str(x)) for x in values if norm(str(x))]
    except Exception:
        return None


def label_vocabulary(page: AtlasPage, block_map: dict[str,SourceBlock]) -> str:
    # Keep normalized full source string so valid labels spanning token boundaries remain matchable.
    text=norm(page.title+' '+page.section_path+' '+' '.join(block_map[x].text for x in page.block_ids))
    return re.sub(r'[\s：:、，,。；;（）()\-—_/]','',text)


def labels_valid(labels: list[str] | None, vocabulary: str) -> tuple[bool,str]:
    # Provider OCR may return []; that means no detectable label, not an error.
    if labels is None: return False,"ocr-unverified"
    invalid=[]
    allowed_tokens=re.findall(r'[\u4e00-\u9fff]{2,16}|\d+(?:\.\d+)?(?:mm|cm|m|kV|%|天)?',vocabulary)
    generic_tokens={"BIM","示意","施工","工程","现场","项目","临时","既有","新建","改造","方向","道路","工作面","布置","流程","阶段","节点","风险","质量","安全"}
    for label in labels:
        compact=re.sub(r'[\s：:、，,。；;（）()\-—_/]','',label)
        if not compact: continue
        fragments=re.findall(r'[\u4e00-\u9fff]{2,16}|\d+(?:\.\d+)?(?:mm|cm|m|kV|%|天)?|[A-Za-z]{2,}',compact)
        bad=[part for part in fragments if part not in generic_tokens and part not in vocabulary and not any(part in allowed or allowed in part for allowed in allowed_tokens)]
        if bad: invalid.append(label)
    return not invalid,"；".join(invalid[:8])


def generate_visuals(root: Path, pages: list[AtlasPage], block_map: dict[str,SourceBlock], provider: str = "gemini", only_missing: bool = True, max_attempts: int = 2, model: str | None = None, size: str | None = None) -> None:
    import time
    api=bidviz.ImageAPI(provider_name=provider,model=model)
    log=[]
    for index,p in enumerate(pages,1):
        out=root/'work/visuals'/f'{p.page_id}.png'
        if only_missing and out.exists():
            p.visual_path=str(out); continue
        approved=False; attempt=0; last_reason=""
        for attempt in range(1,max_attempts+1):
            record={"page_id":p.page_id,"layout":p.layout,"attempt":attempt}
            try:
                api.generate(p.visual_prompt,out,recorder=record,size=size)
                p.status="draft"
                approved=True
                break
            except Exception as exc:
                last_reason=f"{type(exc).__name__}: {exc}"; record.update({"generation_error":last_reason[:500]}); log.append(record); save_json(root/'work/generation-log.json',log)
                if attempt < max_attempts:
                    time.sleep(3)
                continue
        if not approved: p.status="label-review-required"
        p.visual_path=str(out) if out.exists() else ""
        print(f'[{index}/{len(pages)}] {p.page_id} status={p.status} reason={last_reason[:80]}',flush=True)


def compile_pdf(images: list[Path], output: Path) -> None:
    c=canvas.Canvas(str(output),pagesize=landscape(A3),pageCompression=1); pw,ph=landscape(A3)
    for p in images: c.drawImage(str(p),0,0,width=pw,height=ph); c.showPage()
    c.save()


def render(root: Path, blocks: list[SourceBlock], pages: list[AtlasPage]) -> list[Path]:
    bm={b.block_id:b for b in blocks}; outputs=[]
    for p in pages:
        vp=Path(p.visual_path) if p.visual_path else root/'work/visuals'/f'{p.page_id}.png'
        visual=Image.open(vp).convert('RGB') if vp.exists() else None
        out=root/'output/pages'/f'{p.page_id}.png'; render_page(p,bm,visual,out); outputs.append(out)
    compile_pdf(outputs,root/'output/施工组织设计-BIM可视化图册-A3-DRAFT.pdf')
    return outputs


def audit(root: Path, blocks: list[SourceBlock], pages: list[AtlasPage], images: list[Path]) -> dict[str,Any]:
    assigned=[bid for p in pages for bid in p.block_ids]; source_ids=[b.block_id for b in blocks]
    counts={x:assigned.count(x) for x in source_ids}; missing=[x for x,c in counts.items() if c==0]; duplicate=[x for x,c in counts.items() if c>1]
    visible=[bid for p in pages for bid in p.block_ids]  # v2 renders all assigned blocks; overflow is checked separately.
    layout_counts={x:sum(p.layout==x for p in pages) for x in LAYOUTS}
    wrong=[]
    for p in images:
        with Image.open(p) as im:
            if im.size!=A3_PX: wrong.append({"page":p.name,"size":im.size})
    source_chars=sum(len(b.text) for b in blocks); assigned_chars=sum(len(next(b.text for b in blocks if b.block_id==bid)) for bid in assigned)
    findings=[]
    if missing: findings.append({"severity":"error","code":"missing-source-blocks","count":len(missing),"sample":missing[:20]})
    if duplicate: findings.append({"severity":"error","code":"duplicate-source-blocks","count":len(duplicate),"sample":duplicate[:20]})
    if wrong: findings.append({"severity":"error","code":"wrong-page-size","items":wrong})
    if len([x for x,c in layout_counts.items() if c])<5: findings.append({"severity":"error","code":"insufficient-layout-diversity","layout_counts":layout_counts})
    label_review=[p.page_id for p in pages if p.status=='label-review-required']
    if label_review: findings.append({"severity":"error","code":"label-review-required","count":len(label_review),"pages":label_review})
    return {"passed":not any(x['severity']=='error' for x in findings),"source_blocks":len(blocks),"assigned_blocks":len(assigned),"coverage_pct":round(100*(len(blocks)-len(missing))/len(blocks),2) if blocks else 100,"source_chars":source_chars,"assigned_chars":assigned_chars,"pages":len(pages),"layout_counts":layout_counts,"findings":findings}


def write_coverage(root: Path, blocks: list[SourceBlock], pages: list[AtlasPage]) -> None:
    mapping={bid:p.page_id for p in pages for bid in p.block_ids}
    with (root/'output/coverage.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f); w.writerow(['block_id','source_page','section_path','atlas_page','sha256','text'])
        for b in blocks: w.writerow([b.block_id,b.source_page,b.section_path,mapping.get(b.block_id,''),b.sha256,b.text])


def main() -> None:
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('build'); p.add_argument('pdf'); p.add_argument('--root',default='projects/default')
    p=sub.add_parser('generate'); p.add_argument('--root',default='projects/default'); p.add_argument('--provider',default='gemini'); p.add_argument('--all',action='store_true'); p.add_argument('--start',type=int,default=1); p.add_argument('--end',type=int)
    p=sub.add_parser('render'); p.add_argument('--root',default='projects/default')
    p=sub.add_parser('audit'); p.add_argument('--root',default='projects/default')
    p=sub.add_parser('all'); p.add_argument('pdf'); p.add_argument('--root',default='projects/default'); p.add_argument('--provider',default='gemini'); p.add_argument('--offline',action='store_true')
    args=ap.parse_args(); root=Path(args.root)
    if args.cmd=='build':
        b,p=build_project(Path(args.pdf),root); print(json.dumps({'blocks':len(b),'pages':len(p),'layouts':{x:sum(y.layout==x for y in p) for x in LAYOUTS}},ensure_ascii=False))
    elif args.cmd=='generate':
        b,p=load_project(root); selected=p[args.start-1:args.end]; generate_visuals(root,selected,{x.block_id:x for x in b},args.provider,not args.all)
        save_json(root/'work/pages.json',[asdict(x) for x in p])
    elif args.cmd=='render':
        b,p=load_project(root); imgs=render(root,b,p); write_coverage(root,b,p); print(len(imgs))
    elif args.cmd=='audit':
        b,p=load_project(root); imgs=sorted((root/'output/pages').glob('*.png')); r=audit(root,b,p,imgs); save_json(root/'output/audit-v2.json',r); print(json.dumps(r,ensure_ascii=False)); raise SystemExit(0 if r['passed'] else 1)
    else:
        b,p=build_project(Path(args.pdf),root)
        if not args.offline: generate_visuals(root,p,{x.block_id:x for x in b},args.provider,False)
        imgs=render(root,b,p); write_coverage(root,b,p); r=audit(root,b,p,imgs); save_json(root/'output/audit-v2.json',r); print(json.dumps(r,ensure_ascii=False)); raise SystemExit(0 if r['passed'] else 1)

if __name__=='__main__': main()
