#!/usr/bin/env python3
"""bidviz_local_edit —— 全图局部改字（人工框选，OCR 门禁暂缓）。

流程：原图裁一块足够大的上下文 → 只对框内画遮罩 → 调 /images/edits →
把返回结果回缩到裁剪尺寸 → 只合成遮罩区域（含色彩迁移/羽化）→ 输出新图。

关键约束（2026-10-21 实测）：
- 整页 mask 会让接口重绘整张画布，绝不能整页替换；
- 裁剪窗口小于 1024x512 容易 400；返回尺寸可能是请求的 1.125 倍；
- 合成必须只取遮罩区域，遮罩外像素保持原样。

命令行：
  python -m src.bidviz_local_edit import --root projects/anke-35kv
  python -m src.bidviz_local_edit edit --root projects/anke-35kv \
      --sid S030 --page 1 --box 940,810,980,875 --text 挖
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat

MIN_CROP = (1024, 512)
MIN_BOX = 8
MASK_PAD = 6
ERODE = 3
FEATHER = 2
MAX_TEXT = 200
A3_RATIO = 297 / 420
OUTSIDE_MARGIN = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_box(box: Any, width: int, height: int, pad: int = MASK_PAD) -> tuple[int, int, int, int]:
    """把用户框规范成整数坐标，外扩 pad 并夹到图内。"""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("框必须是 [x0,y0,x1,y1]")
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in box)
    except (TypeError, ValueError):
        raise ValueError("框坐标必须是数字") from None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if x1 - x0 < MIN_BOX or y1 - y0 < MIN_BOX:
        raise ValueError(f"框太小（至少 {MIN_BOX}x{MIN_BOX} 像素）")
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    x1 = min(width, x1 + pad)
    y1 = min(height, y1 + pad)
    if x1 - x0 < MIN_BOX or y1 - y0 < MIN_BOX:
        raise ValueError(f"框太小（至少 {MIN_BOX}x{MIN_BOX} 像素）")
    return x0, y0, x1, y1


def crop_window(box: tuple[int, int, int, int], width: int, height: int,
                min_size: tuple[int, int] = MIN_CROP) -> tuple[int, int, int, int]:
    """以框为中心取至少 min_size 的裁剪窗；贴边时平移，不缩小（图本身更小除外）。"""
    min_w, min_h = min_size
    cw = min(width, max(min_w, box[2] - box[0]))
    ch = min(height, max(min_h, box[3] - box[1]))
    cx = (box[0] + box[2]) // 2
    cy = (box[1] + box[3]) // 2
    left = max(0, min(cx - cw // 2, width - cw))
    top = max(0, min(cy - ch // 2, height - ch))
    return left, top, left + cw, top + ch


def build_mask(crop_size: tuple[int, int], box: tuple[int, int, int, int],
               crop_box: tuple[int, int, int, int]) -> Image.Image:
    """裁剪图坐标系下的遮罩：白=可改。"""
    mask = Image.new("L", crop_size, 0)
    local = (box[0] - crop_box[0], box[1] - crop_box[1],
             box[2] - crop_box[0], box[3] - crop_box[1])
    ImageDraw.Draw(mask).rectangle(local, fill=255)
    return mask


def _color_transfer(original: Image.Image, edited: Image.Image, ring: Image.Image) -> Image.Image:
    """用环带的均值/中位数把返回值拉回原图色彩，兼容深底白字与白底深字。"""
    if ring.getbbox() is None:
        return edited
    try:
        orig = ImageStat.Stat(original, mask=ring)
        edit = ImageStat.Stat(edited, mask=ring)
    except Exception:
        return edited
    o_med, e_med = orig.median, edit.median
    o_std, e_std = orig.stddev, edit.stddev
    luts = []
    for i in range(3):
        std = e_std[i]
        if std < 1e-3:
            scale, offset = 1.0, o_med[i] - e_med[i]
        else:
            scale = max(0.5, min(2.0, o_std[i] / std))
            offset = o_med[i] - scale * e_med[i]
        luts.append([(int(round(o_med[i])) if abs(v - e_med[i]) <= 14
                      else max(0, min(255, int(round(scale * v + offset)))))
                     for v in range(256)])
    channels = edited.split()
    return Image.merge("RGB", [ch.point(lut) for ch, lut in zip(channels, luts)])


def composite(original: Image.Image, edited: Image.Image, mask: Image.Image,
              erode: int = ERODE, feather: int = FEATHER) -> Image.Image:
    """只把遮罩区域合成回原图；遮罩外不动。"""
    if original.size != edited.size or original.size != mask.size:
        raise ValueError("original/edited/mask 尺寸不一致")
    core = mask
    if erode > 0:
        k = erode * 2 + 1
        try:
            eroded = mask.filter(ImageFilter.MinFilter(k))
            if eroded.getbbox() is not None:
                core = eroded
        except Exception:
            core = mask
    # 优先用遮罩内侧的环带做色彩对齐：这里的返回值是模型生成的真实背景，
    # 遮罩外返回的黑色不会污染统计。
    ring = ImageChops.subtract(mask, core)
    if ring.getbbox() is None:
        try:
            ring = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(9)), mask)
        except Exception:
            ring = mask
    alpha = core.filter(ImageFilter.GaussianBlur(feather)) if feather > 0 else core
    adjusted = _color_transfer(original, edited, ring)
    return Image.composite(adjusted, original, alpha)


def _outside_diff(source: Image.Image, output: Image.Image,
                  box: tuple[int, int, int, int], margin: int = OUTSIDE_MARGIN) -> tuple[int, int]:
    """返回 (最大通道差, 变化像素数)，统计范围=框外 margin 像素以外的区域。"""
    diff = ImageChops.difference(source.convert("RGB"), output.convert("RGB")).convert("L")
    keep = Image.new("L", source.size, 255)
    x0, y0, x1, y1 = box
    ImageDraw.Draw(keep).rectangle((max(0, x0 - margin), max(0, y0 - margin),
                                    min(source.width, x1 + margin), min(source.height, y1 + margin)), fill=0)
    diff = ImageChops.multiply(diff, keep)
    hist = diff.histogram()
    changed = sum(hist[1:])
    return int(diff.getextrema()[1]), int(changed)


def manual_checks(source: Image.Image, output: Image.Image, box: tuple[int, int, int, int],
                  min_width: int = 2048) -> dict[str, Any]:
    """无 OCR 的人工门禁：几何、合成范围、待人工复核。"""
    checks: list[dict[str, Any]] = []

    def add(code: str, level: str, message: str, evidence: str = "") -> None:
        checks.append({"code": code, "level": level, "message": message, "evidence": evidence})

    sw, sh = source.size
    ow, oh = output.size
    if (sw, sh) == (ow, oh):
        add("dimensions", "pass", f"输出尺寸与源图一致 {ow}x{oh}")
    else:
        add("dimensions", "block", f"输出尺寸 {ow}x{oh} 与源图 {sw}x{sh} 不一致")
    if ow >= min_width:
        add("min-width", "pass", f"宽度 {ow} ≥ {min_width}")
    else:
        add("min-width", "block", f"宽度 {ow} < {min_width}")
    if oh > ow:
        add("portrait", "pass", f"竖版 {ow}x{oh}")
    else:
        add("portrait", "block", f"非竖版 {ow}x{oh}")
    ratio = ow / oh if oh else 0
    if abs(ratio - A3_RATIO) <= 0.035:
        add("aspect-ratio", "pass", f"宽高比 {ratio:.4f}")
    else:
        add("aspect-ratio", "block", f"宽高比 {ratio:.4f}，不是 A3 竖版")
    max_diff, changed = _outside_diff(source, output, box) if (sw, sh) == (ow, oh) else (999, -1)
    if (sw, sh) != (ow, oh):
        add("outside-unchanged", "block", "尺寸不一致，无法校验框外像素", f"{sw}x{sh} vs {ow}x{oh}")
    elif max_diff <= 1 and changed == 0:
        add("outside-unchanged", "pass", "框外像素未变化", f"max={max_diff}, changed={changed}")
    else:
        add("outside-unchanged", "block", "框外像素被改动",
            f"max={max_diff}, changed={changed}")
    add("edit-box", "pass", f"改字框 {box[2] - box[0]}x{box[3] - box[1]} 合法")
    add("manual-review", "warn", "OCR 门禁待办：请放大到 1:1/3x 人工复核改字结果")

    status = "block" if any(c["level"] == "block" for c in checks) else "warn"
    return {"status": status, "checks": checks}


def _api(provider: str | None = None, model: str | None = None):
    import bidviz
    from bidviz_config import get as cfg_get, provider_credentials
    cfg = cfg_get()
    prov = provider or cfg.provider
    mod = model or cfg.model
    cred = provider_credentials(prov, env_first=False)
    return bidviz.ImageAPI(base_url=cred["baseUrl"], api_key=cred["apiKey"], model=mod), prov, mod


def _patch_bbox(raw: Image.Image, local_box: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    """接口有时在遮罩内再留一圈黑边；取非黑内容的外接框。"""
    patch = raw.crop(local_box).convert("L")
    lit = patch.point(lambda v: 255 if v > 15 else 0)
    bbox = lit.getbbox()
    if not bbox:
        return None
    x0, y0, x1, y1 = bbox
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    # 去掉模型可能画在补丁边缘的细边框
    x0, y0, x1, y1 = x0 + 2, y0 + 2, x1 - 2, y1 - 2
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return (local_box[0] + x0, local_box[1] + y0,
            local_box[0] + x1, local_box[1] + y1)


def apply_local_edit(source_image: str | Path, box: Any, text: str, output: str | Path,
                     provider: str | None = None, model: str | None = None,
                     note: str = "") -> dict[str, Any]:
    """对一张全图做局部改字，返回元数据；输出图与源图同尺寸。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("替换文字不能为空")
    if len(text) > MAX_TEXT:
        raise ValueError(f"替换文字最多 {MAX_TEXT} 字")
    source_path, output_path = Path(source_image), Path(output)
    source = Image.open(source_path).convert("RGB")
    width, height = source.size
    user_box = normalize_box(box, width, height, pad=0)
    box2 = normalize_box(user_box, width, height, pad=MASK_PAD)
    crop_box = crop_window(box2, width, height)
    crop = source.crop(crop_box)
    mask = build_mask(crop.size, box2, crop_box)

    prompt = (
        f"保持图片其他部分完全不变。只把遮罩区域内的文字改写为「{text}」，"
        "字体、字号、颜色、粗细与周围文字完全一致，位置不变，不要添加任何其他内容，"
        "不要在遮罩边缘绘制边框、线条或色块。"
    )
    rec: dict[str, Any] = {}
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="bidviz-localedit-") as tmp:
        tmpdir = Path(tmp)
        crop_path = tmpdir / "crop.png"
        mask_path = tmpdir / "mask.png"
        raw_path = tmpdir / "edited_raw.png"
        crop.save(crop_path)
        mask.save(mask_path)
        api, prov, mod = _api(provider, model)
        api.edit(prompt, crop_path, mask_path, raw_path,
                 size=f"{crop.width}x{crop.height}", recorder=rec)
        raw = Image.open(raw_path).convert("RGB")
        raw_size = raw.size
        if raw.size != crop.size:
            ratio = raw.width / raw.height
            expected = crop.width / crop.height
            if abs(ratio - expected) > 0.03:
                raise ValueError(f"改字返回尺寸 {raw.size} 与裁剪比例不符 {crop.size}")
            raw = raw.resize(crop.size, Image.LANCZOS)

    user_local = (user_box[0] - crop_box[0], user_box[1] - crop_box[1],
                  user_box[2] - crop_box[0], user_box[3] - crop_box[1])
    patch_box = _patch_bbox(raw, user_local)
    if patch_box is None:
        raise ValueError("改字接口返回空白，请重试或换一个更大的框")
    content = raw.crop(patch_box)
    user_w, user_h = user_box[2] - user_box[0], user_box[3] - user_box[1]
    if content.width > user_w * 1.5 or content.height > user_h * 1.5:
        content = content.resize((user_w, user_h), Image.LANCZOS)
    paste_x = (user_box[0] + user_box[2]) // 2 - content.width // 2
    paste_y = (user_box[1] + user_box[3]) // 2 - content.height // 2
    edited_full = source.copy()
    edited_full.paste(content, (paste_x, paste_y))
    full_mask = Image.new("L", source.size, 0)
    full_mask.paste(mask, crop_box[:2])
    result = composite(source, edited_full, full_mask)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path, format="PNG")

    meta = {
        "box": list(box2), "user_box": [int(round(float(v))) for v in box],
        "crop": list(crop_box), "text": text, "note": note,
        "prompt": prompt, "provider": prov, "model": mod,
        "requested_size": f"{crop.width}x{crop.height}",
        "raw_size": f"{raw_size[0]}x{raw_size[1]}",
        "actual_size": f"{result.width}x{result.height}",
        "sec": round(time.time() - t0, 1),
        "image_sha256": rec.get("image_sha256"),
        "mask_sha256": rec.get("mask_sha256"),
        "output_sha256": rec.get("output_sha256"),
    }
    return meta


def page_context(root: Path, sid: str, page_no: int, section_title: str) -> dict[str, Any]:
    """统一的分页/哈希口径：Web 候选、回填、局部改字都从这里取。"""
    import bidviz_fullimage as fullimage

    root = Path(root)
    blocks = fullimage._section_blocks(root, sid)
    pages = fullimage.split_pages(blocks)
    if page_no < 1 or page_no > len(pages):
        raise IndexError(f"{sid} 无第 {page_no} 页（共 {len(pages)} 页）")
    page_blocks = pages[page_no - 1]
    built = fullimage.build_fullpage_prompt(root, sid, page_no, len(pages), page_blocks)
    source_text = "\n".join(text for _kind, text in page_blocks)
    return {"sid": sid, "page": page_no, "total_pages": len(pages),
            "page_blocks": page_blocks, "built": built, "auto_prompt": built["prompt"],
            "source_text": source_text,
            "source_hash": hashlib.sha256((section_title + "\n" + source_text).encode("utf-8")).hexdigest(),
            "template_hash": hashlib.sha256(fullimage.PROMPT_TEMPLATE.read_text(encoding="utf-8").encode("utf-8")).hexdigest(),
            "prompt_hash": hashlib.sha256(built["prompt"].encode("utf-8")).hexdigest(),
            "cover": bool(built.get("cover"))}


def _sections(root: Path):
    import bidviz_e1 as e1
    import build_e1 as be
    blocks, _pm = be.load_project(root)
    sections = e1.group_sections(blocks)
    be.apply_edits(root, sections)
    return sections


def import_existing(root: str | Path, only: list[str] | None = None) -> dict[str, Any]:
    """把 work/fullimage/Sxxx_pN.png 回填为已发布候选（幂等）。"""
    import bidviz_fullimage as fullimage
    import bidviz_fullimage_store as store
    from bidviz_config import get as cfg_get

    root = Path(root).resolve()
    cfg = cfg_get()
    pages_path = root / "work" / "fullimage-pages.json"
    if pages_path.exists():
        backup = pages_path.with_name(f"fullimage-pages.backup-{datetime.now():%Y%m%d-%H%M%S}.json")
        shutil.copy2(pages_path, backup)
    else:
        backup = None

    imported, skipped, missing = 0, 0, []
    expected: list[tuple[str, int]] = []
    for sec in _sections(root):
        sid = sec.section_id
        if only and sid not in only:
            continue
        blocks = [(getattr(b, "kind", "body"), (getattr(b, "text", "") or "").strip())
                  for b in sec.blocks
                  if (getattr(b, "text", "") or "").strip()
                  and (getattr(b, "text", "") or "").strip() != sec.title]
        try:
            page_count = len(fullimage.split_pages(blocks))
        except Exception:
            continue
        for page_no in range(1, page_count + 1):
            expected.append((sid, page_no))
            ctx = page_context(root, sid, page_no, sec.title)
            ov = store.get_prompt_override(root, sid, page_no, ctx["auto_prompt"], ctx["source_hash"])
            prompt = ov["resolved_prompt"]
            state = store.ensure_page(root, sid, page_no, ctx["source_hash"], ctx["template_hash"],
                                      store.sha256_text(prompt))
            if state.get("published", {}).get("current") or state.get("candidates"):
                skipped += 1
                continue
            image = root / "work" / "fullimage" / f"{sid}_p{page_no}.png"
            if not image.is_file():
                variants = sorted((root / "work" / "fullimage").glob(f"{sid}_p{page_no}_v*.png"),
                                  key=lambda p: p.stat().st_mtime, reverse=True)
                image = variants[0] if variants else image
            if not image.is_file():
                missing.append(f"{sid}:p{page_no}")
                continue
            try:
                with Image.open(image) as im:
                    w, h = im.size
            except Exception:
                missing.append(f"{sid}:p{page_no}")
                continue
            metadata = {
                "kind": "legacy-import", "legacy_import": True,
                "source_hash": ctx["source_hash"], "template_hash": ctx["template_hash"],
                "prompt_hash": store.sha256_text(prompt), "prompt": prompt,
                "provider": cfg.provider, "model": cfg.model,
                "requested_size": cfg.image_size, "actual_size": f"{w}x{h}",
                "reference_count": 0 if ctx["cover"] else 1,
                "checks": {"status": "warn", "checks": [{
                    "code": "manual-review", "level": "warn",
                    "message": "历史全图导入；OCR 门禁待办，按当前交付状态标记为已发布"}]},
                "created_at": _now(), "created_ts": time.time(), "decision": None,
            }
            store.import_published(root, sid, page_no, image, metadata)
            imported += 1

    gates = store.gate_summary(root, expected)
    summary = {"imported": imported, "skipped": skipped, "missing": missing,
               "backup": str(backup) if backup else None,
               "published": gates.get("published"), "expected": len(expected),
               "can_build_final": gates.get("can_build_final")}
    return summary


def _parse_box(raw: str) -> list[float]:
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise ValueError("--box 需要 x0,y0,x1,y1")
    return [float(p) for p in parts]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="全图局部改字（人工框选）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    imp = sub.add_parser("import", help="把现有全图回填为已发布候选")
    imp.add_argument("--root", default="projects/anke-35kv")
    imp.add_argument("--section", default="", help="只回填某几节，逗号分隔")

    ed = sub.add_parser("edit", help="离线局部改字")
    ed.add_argument("--root", default="projects/anke-35kv")
    ed.add_argument("--sid", required=True)
    ed.add_argument("--page", type=int, default=1)
    ed.add_argument("--box", required=True, help="x0,y0,x1,y1（原图像素）")
    ed.add_argument("--text", required=True)
    ed.add_argument("--base-version", type=int, default=0)
    ed.add_argument("--out", default="")

    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root
    from bidviz_config import load as load_cfg
    load_cfg(root)

    if args.cmd == "import":
        only = [x.strip().upper() for x in args.section.split(",") if x.strip()] or None
        print(json.dumps(import_existing(root, only), ensure_ascii=False, indent=2))
        return 0

    import bidviz_fullimage_store as store
    if args.base_version:
        meta = store.get_candidate(root, args.sid, args.page, args.base_version)
        if not meta:
            raise SystemExit(f"候选不存在：{args.sid} p{args.page} v{args.base_version:04d}")
        source = root / meta["path"]
    else:
        source = store.published_path(root, args.sid, args.page)
        if not source.is_file():
            candidates = store.list_candidates(root, args.sid, args.page)
            source = root / candidates[-1]["path"] if candidates else \
                root / "work" / "fullimage" / f"{args.sid}_p{args.page}.png"
    if not source.is_file():
        raise SystemExit(f"基准图不存在：{source}")
    out = Path(args.out) if args.out else \
        root / "work" / "fullimage" / f"{args.sid}_p{args.page}_localedit.png"
    meta = apply_local_edit(source, _parse_box(args.box), args.text, out)
    print(json.dumps({"output": str(out), **meta}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
