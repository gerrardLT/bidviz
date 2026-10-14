#!/usr/bin/env python3
"""施工组织设计 A3 可视化全自动端到端流水线 (One-Click Pipeline)

使用方法：
    python run_pipeline.py <doc/docx/pdf路径> [选项]

示例：
    # 完整全自动（含 AI 在线 BIM 生图）
    python run_pipeline.py "D:\\bid文件\\参照文档\\施组50页.doc"

    # 离线极速模式（不调用生图 API，5秒出 50 页完整排版 PDF）
    python run_pipeline.py "D:\\bid文件\\参照文档\\施组50页.doc" --offline

    # 指定项目输出目录
    python run_pipeline.py "D:\\bid文件\\参照文档\\施组50页.doc" --project-dir my-project
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import fitz
from PIL import Image

import bidviz

def log(step: str, msg: str):
    print(f"\n{'='*70}\n[步骤 {step}] {msg}\n{'='*70}", flush=True)

def convert_to_pdf_and_docx(src_file: Path, inputs_dir: Path) -> tuple[Path, Path]:
    ext = src_file.suffix.lower()
    pdf_path = inputs_dir / f"{src_file.stem}-source.pdf"
    docx_path = inputs_dir / f"{src_file.stem}.docx"

    if ext == ".pdf":
        if src_file != pdf_path:
            shutil.copy(src_file, pdf_path)
        return pdf_path, docx_path

    if ext in [".doc", ".docx"]:
        print(f"正在调用 Word COM 将 {src_file.name} 转为标准 DOCX 与高精度 PDF...", flush=True)
        import win32com.client
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        try:
            doc = word.Documents.Open(str(src_file.resolve()), ReadOnly=True, AddToRecentFiles=False)
            if ext == ".doc":
                doc.SaveAs2(str(docx_path.resolve()), FileFormat=16)
            elif src_file != docx_path:
                shutil.copy(src_file, docx_path)
            pdf_target = str(pdf_path.resolve())
            doc.ExportAsFixedFormat(pdf_target, ExportFormat=17, OpenAfterExport=False, OptimizeFor=0)
            if not pdf_path.exists():
                # 部分 Word/pywin32 组合会无异常返回却不落盘；用同一原生 PDF 格式补一次。
                doc.SaveAs2(FileName=pdf_target, FileFormat=17, AddToRecentFiles=False)
            doc.Close(False)
        finally:
            word.Quit()
        if not pdf_path.exists():
            raise RuntimeError(f"Word 未生成 PDF：{pdf_path}")
        print(f"转换成功: PDF ({pdf_path.stat().st_size} bytes), DOCX ({docx_path.stat().st_size} bytes)", flush=True)
        return pdf_path, docx_path

    raise ValueError(f"不支持的文件格式: {ext}")

def _extract_image_with_mask(doc, xref: int, smask_xref: int):
    """PDF 图像常带 SMask 软掩膜；只取底图会得到黑底，必须把掩膜应用为 alpha。"""
    base_img = doc.extract_image(xref)
    base = Image.open(io.BytesIO(base_img["image"])).convert("RGB")
    if smask_xref:
        try:
            mask_img = doc.extract_image(smask_xref)
            mask = Image.open(io.BytesIO(mask_img["image"])).convert("L")
            if mask.size != base.size:
                mask = mask.resize(base.size)
            base.putalpha(mask)
        except Exception:
            pass
    return base


def extract_illustrations(pdf_path: Path, work_dir: Path) -> Path:
    print("正在从源文档各页中精确提取工程插图与现场照片...", flush=True)
    out_dir = work_dir / "page-media"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.*"):
        old.unlink()
    doc = fitz.open(pdf_path)
    page_media_map = {}

    for p_idx in range(len(doc)):
        p_num = p_idx + 1
        imgs = doc[p_idx].get_images(full=True)
        valid_files = []
        for i_idx, img_info in enumerate(imgs):
            xref = img_info[0]
            smask_xref = img_info[1] if len(img_info) > 1 else 0
            try:
                pil_im = _extract_image_with_mask(doc, xref, smask_xref)
                w, h = pil_im.size
                if w >= 150 and h >= 120:
                    fname = f"src-p{p_num:02d}-img{i_idx+1:02d}.png"
                    fpath = out_dir / fname
                    pil_im.save(fpath)
                    valid_files.append({"file": str(fpath), "w": w, "h": h, "area": w * h})
            except Exception:
                pass
        valid_files.sort(key=lambda x: x["area"], reverse=True)
        page_media_map[p_num] = [x["file"] for x in valid_files]

    map_path = work_dir / "page-media-map.json"
    map_path.write_text(json.dumps(page_media_map, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for v in page_media_map.values())
    print(f"成功提取 {total} 张原始工程图（含透明掩膜），已建立逐页关联台账: {map_path}", flush=True)
    return map_path

def main():
    parser = argparse.ArgumentParser(description="施工组织设计 A3 高端工程画册级全自动工作流")
    parser.add_argument("source", help="施工组织设计原文件路径 (.doc, .docx, 或 .pdf)")
    parser.add_argument("--project-dir", default="projects/default", help="项目生成目录")
    parser.add_argument("--provider", default=None, help="生图供应商；默认读 bidviz.toml")
    parser.add_argument("--model", default=None, help="生图模型；默认读 bidviz.toml")
    parser.add_argument("--offline", action="store_true", help="离线模式：跳过在线生图，使用占位快速排版导出")
    parser.add_argument("--skip-generation", action="store_true", help="跳过生图步骤（沿用已有图像直接重新排版合成）")
    parser.add_argument("--ingest-only", action="store_true", help="只解析输入并生成 blocks.json/pages.json")
    args = parser.parse_args()

    src = Path(args.source).resolve()
    if not src.exists():
        print(f"错误：文件不存在: {src}")
        sys.exit(1)

    project_root = Path(args.project_dir).resolve()
    from bidviz_config import load as load_cfg
    cfg = load_cfg(project_root)
    provider = args.provider or cfg.provider
    model = args.model or cfg.model
    inputs_dir = project_root / "inputs"
    work_dir = project_root / "work"
    output_dir = project_root / "output"
    for d in [inputs_dir, work_dir, output_dir]:
        d.mkdir(parents=True, exist_ok=True)

    start_all = time.time()

    # Step 1: 文档格式标准化与解析
    log("1/5", f"解析与标准化源文件: {src.name}")
    pdf_path, docx_path = convert_to_pdf_and_docx(src, inputs_dir)

    # Step 2: 提取插图与结构化切分
    log("2/5", "提取源文档插图 & 全文切分为原子事实块")
    extract_illustrations(pdf_path, work_dir)

    import bidviz_v2
    blocks, pages = bidviz_v2.build_project(pdf_path, project_root)
    print(f"全文结构化完毕：共提取 {len(blocks)} 个文本块，规划为 {len(pages)} 页 A3 画册。", flush=True)
    if args.ingest_only:
        return

    # Step 3: AI 生图 (若未选择跳过或离线)
    log("3/5", "生成写实 BIM 施工工况主视觉")
    if args.offline:
        print("已启用 --offline 模式，跳过在线生图。", flush=True)
    elif args.skip_generation:
        print("已启用 --skip-generation，沿用已有视觉素材。", flush=True)
    else:
        print(f"调用 {provider} ({model}) 逐页生成写实 BIM 工况图...", flush=True)
        bidviz_v2.generate_visuals(project_root, pages, {b.block_id: b for b in blocks},
                                  provider=provider, model=model, size=cfg.image_size,
                                  only_missing=True)

    # Step 4: 方案 A 画册排版合成
    log("4/5", "执行方案 A 高端工程画册级复合编排 (双表+多图联动+工序流水)")
    import bidviz_album
    b_map = {b.block_id: b for b in blocks}
    
    pm_path = work_dir / "page-media-map.json"
    page_media_map = {}
    if pm_path.exists():
        page_media_map = {int(k): v for k, v in json.loads(pm_path.read_text(encoding="utf-8")).items()}
    global_media = sorted([str(p) for p in (work_dir / "page-media").glob("*.*")])

    out_pages_dir = output_dir / "album-pages"
    out_pages_dir.mkdir(parents=True, exist_ok=True)
    rendered_pages = []
    
    for i, p in enumerate(pages, 1):
        target = out_pages_dir / f"{p.page_id}.png"
        bidviz_album.render_album_page(bidviz_v2.asdict(p), b_map, page_media_map, global_media, target, project_root=project_root)
        rendered_pages.append(target)
        if i % 10 == 0 or i == len(pages):
            print(f"已渲染 [{i}/{len(pages)}] 页...", flush=True)

    pdf_out = output_dir / "施工组织设计-高端工程画册图册-A3-DRAFT.pdf"
    print("正在汇编 300 DPI A3 印刷级 PDF...", flush=True)
    bidviz_album.compile_pdf(rendered_pages, pdf_out)
    print(f"PDF 汇编成功: {pdf_out} ({round(pdf_out.stat().st_size / (1024*1024), 2)} MB)", flush=True)

    # Step 5: 生成审阅缩略图与看板
    log("5/5", "生成全景审阅看板与高清联系表")
    preview_dir = output_dir / "album-previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    for p in rendered_pages:
        im = Image.open(p).convert("RGB")
        im.thumbnail((1600, 1131))
        im.save(preview_dir / f"{p.stem}.jpg", quality=88)

    sheets_dir = output_dir / "album-layout-sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    font = bidviz.pil_font(22, False)
    for batch in range((len(rendered_pages) + 11) // 12):
        tiles = []
        for p in rendered_pages[batch * 12 : (batch + 1) * 12]:
            im = Image.open(p).convert("RGB")
            im.thumbnail((440, 310))
            tile = Image.new("RGB", (460, 345), "white")
            tile.paste(im, ((460 - im.width) // 2, 28))
            from PIL import ImageDraw
            ImageDraw.Draw(tile).text((8, 3), p.stem, font=font, fill="#0c2333")
            tiles.append(tile)
        rows = (len(tiles) + 3) // 4
        sheet = Image.new("RGB", (1840, 345 * rows), "#d5dde2")
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % 4) * 460, (i // 4) * 345))
        sheet.save(sheets_dir / f"album-sheet-{batch+1}.jpg", quality=90)

    total_sec = round(time.time() - start_all, 1)
    print("\n" + "=" * 70)
    print(f"🎉 全流程执行完毕！总耗时: {total_sec} 秒")
    print(f"📄 最终 PDF: {pdf_out}")
    print(f"🌐 交互看板: {Path('full_album_viewer.html').resolve()}")
    print("=" * 70 + "\n")

if __name__ == "__main__":
    main()
