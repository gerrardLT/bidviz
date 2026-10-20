#!/usr/bin/env python3
"""bidviz_fullimage —— 全图渲染路线（engine=fullimage，2026-10-17 用户拍板）。

提示词格式 = 用户原始样图提示词（通用段固定 + 原文全文注入），模型自由编排。
试验参数：屏显为主 / 暂不设文字门禁（准确率仅记录）/ 第一章整章 / 原文全文进图。

分页：节字数 > SPLIT_CHARS 时按块边界分 2–3 页（对齐 D4 ≤3 页预算），每页一次
生图调用，提示词标注「第 N/M 页」。多变体：--variants N 用多次调用实现（yoshub
gpt-image-2 实测不支持 n>1，报 "n currently supports 1 only"）。

保真兜底：隐藏层（render mode 3）写入该页对应原文分段，保证 Ctrl+F 与反抽审计。

产物：
  work/fullimage/Sxxx_pN[_vK].png     整页图
  work/fullimage/ledger.json          台账（提示词快照/尺寸/耗时/耗时）
  output/施工组织设计-图册-A3-fullimage.pdf   整图页 + 隐藏层汇编

用法：
  python -m bidviz_fullimage projects/anke-35kv --section S003 --dry-run
  python -m bidviz_fullimage projects/anke-35kv --chapter 第一章
  python -m bidviz_fullimage projects/anke-35kv --chapter 第一章 --variants 2 --pdf
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

PROMPT_TEMPLATE = BASE / "prompts" / "fullpage.md"
OUT_DIR_NAME = "work/fullimage"
LEDGER_NAME = "ledger.json"
SPLIT_CHARS = 1500          # 超过则分页（1225 字的 S003 单页效果已验证）
MAX_PAGES = 3               # D4：每节 ≤3 页
MIN_WIDTH = 2048            # 尺寸下限（taomu 抽卡重掷阈值，2026-10-17 拍板）
REROLL_MAX = 3              # 尺寸重掷上限
# 风格/框架锚定参考图（AI 图内+参考图锚定方案；样图 1 = 用户签认的框架基调）
ANCHOR_REF = BASE / "docs" / "example-image" / "ChatGPT_图像_2026年10月16日_10_21_30-1.png"


def size_chain() -> list[str]:
    """尺寸回退链：首选 bidviz.toml [image].size（如 4096x2896 走 taomu 4K），兜底降档。"""
    from bidviz_config import get as _cfg
    first = _cfg().image_size
    chain = [first] if first else []
    for s in ("2048x2896", "1536x2170", "1024x1536"):
        if s not in chain:
            chain.append(s)
    return chain

# 8 类图片语汇（D8 / layout-classification），注入「图片采用…」一句
CLASS_IMAGERY: dict[str, str] = {
    "A": "真实感工程场景总览图或范围关系示意",
    "B": "真实感工程场景、三维施工示意图",
    "C": "等轴测工程图、剖面图或构造分解示意",
    "D": "小幅识别用对象图（以通栏表格为主，不配大图）",
    "E": "阶段时序示意条（不出现具体日期）",
    "F": "组织架构与协同关系示意",
    "G": "检测点位或核查闭环示意（以检验表为主体）",
    "H": "真实感风险场景图与防护位置示意",
}


# ---------------------------------------------------------------- 原文与分页


def _section_blocks(root: Path, sid: str) -> list[tuple[str, str]]:
    """[(kind, text)] 该节块列表（去掉节标题自身，保留三级标题以下结构）。"""
    import bidviz_e1 as e1
    import build_e1 as be
    blocks, _pm = be.load_project(Path(root))
    sections = e1.group_sections(blocks)
    be.apply_edits(Path(root), sections)
    sec = next((s for s in sections if s.section_id == sid), None)
    if not sec:
        raise SystemExit(f"blocks 中无节 {sid}")
    out = []
    for b in sec.blocks:
        t = (getattr(b, "text", "") or "").strip()
        if not t or t == sec.title:
            continue
        out.append((getattr(b, "kind", "body"), t))
    return out


_CN_NUM = "一二三四五六七八九十百"
_PRIMARY_HEADING_RE = re.compile(rf"^[{_CN_NUM}]+[、.]\s*")
_SECONDARY_HEADING_RE = re.compile(rf"^[（(][{_CN_NUM}0-9]+[）)]\s*")


def _block_chars(blocks: list[tuple[str, str]]) -> int:
    return sum(len(text) for _kind, text in blocks)


def _split_chunk(blocks: list[tuple[str, str]], limit: int = SPLIT_CHARS) -> list[list[tuple[str, str]]]:
    """仅在单一主题过长时按二级标题、最终按段落边界拆分。"""
    if _block_chars(blocks) <= limit:
        return [blocks]
    secondary: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for block in blocks:
        is_secondary = block[0] == "heading" and bool(_SECONDARY_HEADING_RE.match(block[1]))
        if is_secondary and current and any(
                kind == "heading" and _SECONDARY_HEADING_RE.match(text)
                for kind, text in current):
            secondary.append(current)
            current = []
        current.append(block)
    if current:
        secondary.append(current)
    if len(secondary) > 1:
        out: list[list[tuple[str, str]]] = []
        for group in secondary:
            out.extend(_split_chunk(group, limit))
        return out

    out, current, chars = [], [], 0
    for block in blocks:
        length = len(block[1])
        if current and chars + length > limit:
            out.append(current)
            current, chars = [], 0
        current.append(block)
        chars += length
    if current:
        out.append(current)
    return out


def _semantic_chunks(blocks: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """一级主题优先成组；开头的章标题/前言归入首个主题。"""
    groups: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for block in blocks:
        is_primary = block[0] == "heading" and bool(_PRIMARY_HEADING_RE.match(block[1]))
        if is_primary and current and any(
                kind == "heading" and _PRIMARY_HEADING_RE.match(text)
                for kind, text in current):
            groups.append(current)
            current = []
        current.append(block)
    if current:
        groups.append(current)
    chunks: list[list[tuple[str, str]]] = []
    for group in groups:
        chunks.extend(_split_chunk(group))
    return chunks


def split_pages(blocks: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """按一级/二级主题语义分页，保持块顺序且每块恰好出现一次。"""
    blocks = list(blocks)
    if not blocks:
        return [[]]
    total = _block_chars(blocks)
    n_pages = 1 if total <= SPLIT_CHARS else (2 if total <= SPLIT_CHARS * 2 else MAX_PAGES)
    chunks = _semantic_chunks(blocks)
    if n_pages == 1:
        return [blocks]

    pages: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    remaining_chars = total
    for chunk in chunks:
        remaining_pages = n_pages - len(pages)
        target = remaining_chars / max(1, remaining_pages)
        chunk_chars = _block_chars(chunk)
        if current and len(pages) < n_pages - 1 and _block_chars(current) + chunk_chars > target:
            pages.append(current)
            remaining_chars -= _block_chars(current)
            current = []
        current.extend(chunk)
    if current:
        pages.append(current)
    while len(pages) > n_pages:
        tail = pages.pop()
        pages[-1].extend(tail)
    return pages


def _page_start_index(all_blocks: list[tuple[str, str]], page_blocks: list[tuple[str, str]]) -> int:
    if not page_blocks:
        return 0
    size = len(page_blocks)
    for index in range(len(all_blocks) - size + 1):
        if all_blocks[index:index + size] == page_blocks:
            return index
    return 0


def page_topic(all_blocks: list[tuple[str, str]], page_blocks: list[tuple[str, str]],
               page_index: int) -> str:
    """返回本页实际主题；同主题承接页标“（续）”。"""
    start = _page_start_index(all_blocks, page_blocks)
    primary = ""
    secondary = ""
    for kind, text in all_blocks[:start]:
        if kind == "heading" and _PRIMARY_HEADING_RE.match(text):
            primary, secondary = text, ""
        elif kind == "heading" and _SECONDARY_HEADING_RE.match(text):
            secondary = text

    page_primaries = [text for kind, text in page_blocks
                      if kind == "heading" and _PRIMARY_HEADING_RE.match(text)]
    page_primary = page_primaries[0] if page_primaries else ""
    page_secondary = next((text for kind, text in page_blocks
                           if kind == "heading" and _SECONDARY_HEADING_RE.match(text)), "")
    if page_primary:
        if len(page_primaries) > 1:
            return " / ".join(page_primaries)
        primary = page_primary
        secondary = page_secondary
        return f"{primary} / {secondary}" if secondary else primary
    if page_secondary:
        secondary = page_secondary
        return f"{primary} / {secondary}" if primary else secondary
    topic = f"{primary} / {secondary}" if primary and secondary else (primary or secondary)
    return f"{topic}（续）" if topic and page_index > 1 else topic


def _hierarchy(root: Path, sid: str, page_blocks: list[tuple[str, str]], page_index: int) -> list[str]:
    """章节层级：章 / 节 / 本页实际主题。"""
    pp = __import__("bidviz_plan").load_page_plan(root) or {}
    page = pp["sections"][sid]["pages"][0]
    section_title = page["title"]["section"] + ("（续）" if page_index > 1 else "")
    lines = [f"- {page['title']['chapter']}", f"- {section_title}"]
    topic = page_topic(_section_blocks(root, sid), page_blocks, page_index)
    if topic:
        lines.append(f"- {topic}")
    return lines


# ---------------------------------------------------------------- 提示词


def build_fullpage_prompt(root: Path, sid: str, page_no: int = 1,
                          total_pages: int = 1,
                          page_blocks: list[tuple[str, str]] | None = None) -> dict[str, Any]:
    """用户格式提示词：通用段（prompts/fullpage.md）+ 章节层级 + 按类语汇 + 原文分段。"""
    root = Path(root)
    if page_blocks is None:
        page_blocks = [b for p in split_pages(_section_blocks(root, sid)) for b in p]
    import bidviz_plan as plan_mod
    from bidviz_config import get as _cfg
    pp = plan_mod.load_page_plan(root) or {}
    entry = pp.get("sections", {}).get(sid)
    if not entry:
        raise SystemExit(f"page-plan 无 {sid}")
    page = entry["pages"][0]
    main = page.get("main_class") or entry.get("main_class") or "B"

    source_text = "\n".join(t for _k, t in page_blocks)
    # 多页节页注：不写"第N/M页"（会诱导模型画页码），只说明本页编排范围
    page_note = ("\n本页为该节多页排版中的一页，只编排上面给出的本页主题与原文分段；"
                 "不要重复上一页已经呈现的内容，不要编造分段之外的工序或参数，不要自行标注页码。"
                 if total_pages > 1 else "")

    tpl = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    from bidviz_config import get as _cfg2
    project_name = _cfg2().name
    prompt = (tpl
              .replace("{{chapter_hierarchy}}", "\n".join(_hierarchy(root, sid, page_blocks, page_no)))
              .replace("{{project_name}}", project_name)
              .replace("{{class_imagery}}", CLASS_IMAGERY.get(main, CLASS_IMAGERY["B"]))
              .replace("{{page_note}}", page_note)
              .replace("{{source_text}}", source_text))
    return {"prompt": prompt, "hidden": [t for _k, t in page_blocks],
            "meta": {"sid": sid, "main_class": main, "page": page_no,
                     "total": total_pages, "chars": len(source_text)}}


# ---------------------------------------------------------------- 生图


def out_dir(root: Path) -> Path:
    d = Path(root) / OUT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_ledger(root: Path) -> dict:
    p = out_dir(root) / LEDGER_NAME
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_ledger(root: Path, led: dict) -> None:
    (out_dir(root) / LEDGER_NAME).write_text(
        json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_page(root: Path, sid: str, page_no: int, total_pages: int,
                  page_blocks: list[tuple[str, str]], force: bool = False,
                  variant: int = 1, dry_run: bool = False,
                  prompt_override: str | None = None,
                  output_path: Path | None = None) -> dict[str, Any]:
    import bidviz
    from bidviz_config import provider_credentials, get as _cfg

    root = Path(root)
    built = build_fullpage_prompt(root, sid, page_no, total_pages, page_blocks)
    if prompt_override is not None:
        built["prompt"] = prompt_override.strip()
    stem = f"{sid}_p{page_no}" + (f"_v{variant}" if variant > 1 else "")
    out = Path(output_path) if output_path is not None else out_dir(root) / f"{stem}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    prompt_snap = out.with_name(f"_prompt_{out.stem}.txt")

    if dry_run:
        print(f"[dry-run] {stem} 提示词 {len(built['prompt'])} 字 / "
              f"原文 {built['meta']['chars']} 字")
        print(built["prompt"])
        return {"sid": sid, "dry_run": True}

    if out.exists() and out.stat().st_size > 2000 and not force:
        return {"sid": sid, "stem": stem, "skipped": True}

    cfg = _cfg()
    providers_to_try = [
        (cfg.provider, cfg.model),
    ]
    fb_p = getattr(cfg, "raw", {}).get("image", {}).get("fallback_provider")
    fb_m = getattr(cfg, "raw", {}).get("image", {}).get("fallback_model", "gpt-image-2-2K/4K")
    if fb_p and fb_p != cfg.provider:
        providers_to_try.append((fb_p, fb_m))

    rec: dict[str, Any] = {"sid": sid, "stem": stem, "page": page_no, "variant": variant,
                           "chars": built["meta"]["chars"],
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    t0 = time.time()
    last_err = ""
    refs = [ANCHOR_REF] if ANCHOR_REF.exists() else []
    rerolls = 0

    for prov_name, model_name in providers_to_try:
        try:
            cred = provider_credentials(prov_name, env_first=False)
            api = bidviz.ImageAPI(base_url=cred["baseUrl"], api_key=cred["apiKey"], model=model_name)
            print(f"  [通道] 使用 {prov_name} ({model_name}) ...", flush=True)
        except Exception as e:
            print(f"  [通道跳过] {prov_name}: {e}", flush=True)
            continue

        while True:
            for attempt in range(1, 4):
                for size in size_chain():
                    try:
                        api.generate(built["prompt"], out, references=refs,
                                     recorder=rec, size=size)
                        from PIL import Image
                        w, h = Image.open(out).size
                        rec.update({"ok": True, "provider": prov_name, "model": model_name,
                                    "requested": size, "actual": f"{w}x{h}",
                                    "sec": round(time.time() - t0, 1),
                                    "kb": round(out.stat().st_size / 1024),
                                    "attempts": attempt, "rerolls": rerolls})
                        if f"{w}x{h}" != size:
                            rec["downgraded"] = True
                        prompt_snap.write_text(built["prompt"], encoding="utf-8")
                        break
                    except Exception as exc:
                        last_err = f"{type(exc).__name__}: {str(exc)[:160]}"
                        out.unlink(missing_ok=True)
                        continue
                if rec.get("ok"):
                    break
                if "503" in last_err or "tokens" in last_err.lower():
                    wait = 30 * attempt
                    print(f"    [wait] {prov_name} 限流，{wait}s 后重试...", flush=True)
                    time.sleep(wait)
                else:
                    break

            if rec.get("ok") and Image.open(out).size[0] < MIN_WIDTH and rerolls < REROLL_MAX:
                rerolls += 1
                print(f"    [reroll] {stem} 宽度不足 {MIN_WIDTH}，重掷 {rerolls}/{REROLL_MAX}", flush=True)
                out.unlink(missing_ok=True)
                rec["ok"] = False
                continue
            break

        if rec.get("ok"):
            break
        print(f"  [通道回退] {prov_name} 失败: {last_err}，尝试下一通道...", flush=True)
    if not rec.get("ok"):
        rec.update({"ok": False, "error": last_err, "sec": round(time.time() - t0, 1)})

    led = load_ledger(root)
    led[stem] = rec
    save_ledger(root, led)
    print(f"[{'OK' if rec.get('ok') else 'FAIL'}] {stem} {rec.get('actual', '')} "
          f"{rec.get('sec', '')}s {last_err[:80]}", flush=True)
    return rec


def generate_section(root: Path, sid: str, force: bool = False, variants: int = 1,
                     dry_run: bool = False) -> list[dict]:
    """一节 → 分页 → 每页 × variants 张。"""
    pages = split_pages(_section_blocks(root, sid))
    recs = []
    for i, page_blocks in enumerate(pages, 1):
        for v in range(1, variants + 1):
            recs.append(generate_page(root, sid, i, len(pages), page_blocks,
                                      force=force, variant=v, dry_run=dry_run))
    return recs


def chapter_sids(root: Path, chapter_kw: str) -> list[str]:
    import bidviz_plan as plan_mod
    pp = plan_mod.load_page_plan(root) or {}
    return [sid for sid in sorted(pp["sections"].keys())
            if chapter_kw in pp["sections"][sid]["pages"][0]["title"]["chapter"]]


# ---------------------------------------------------------------- 汇编（整图页 + 隐藏层）


def build_fullimage_pdf(root: Path, sids: list[str], out_pdf: Path,
                        image_resolver=None, draft: bool = False) -> Path:
    """整图页（等比无损置入）+ 矢量全局页码 + 隐藏层原文 → A3 竖版 PDF。

    页码方案（2026-10-17 拍板）：AI 图内禁止画页码；汇编时在页面底部 24pt 白边
    写矢量页码「— N —」，全册按节顺序连续编号（1..N，跨章累计）。
    """
    import bidviz_vector as bv

    root = Path(root)
    bv.set_page("A3", "portrait")
    sheet = bv.Sheet(out_pdf)
    footer_h = 24.0                     # 底部页码白边高（图不进入此带）
    n_pages = 0
    global_no = 0
    for sid in sids:
        pages = split_pages(_section_blocks(root, sid))
        for i, page_blocks in enumerate(pages, 1):
            if image_resolver is not None:
                img = image_resolver(sid, i)
                if img is None or not Path(img).exists():
                    continue
                img = Path(img)
            else:
                # CLI 兼容路径：取台账里 actual 最大的候选；Web 正式汇编传 resolver，只读 published
                cands = sorted(out_dir(root).glob(f"{sid}_p{i}*.png"))
                if not cands:
                    continue
                img = max(cands, key=lambda p: p.stat().st_size)
            global_no += 1
            # 方案 A1：等比包含（contain），图完整放入标题区…页脚带之间
            sheet.image(img, 0, 0, sheet.w, sheet.h - footer_h, dpi=200, fit="contain")
            # 矢量全局页码：底部白带居中「— N —」
            muted = "#5a6e7c"
            sheet.text(sheet.w / 2, sheet.h - footer_h + 7, f"— {global_no} —",
                       9.0, False, muted, align="center")
            if draft:
                sheet.c.saveState()
                sheet.c.setFillColorRGB(0.75, 0.1, 0.1, alpha=0.18)
                sheet.c.setFont("Helvetica-Bold", 72)
                sheet.c.translate(sheet.w / 2, sheet.h / 2)
                sheet.c.rotate(35)
                sheet.c.drawCentredString(0, 0, "DRAFT")
                sheet.c.restoreState()
            built = build_fullpage_prompt(root, sid, i, len(pages), page_blocks)
            hy = sheet.h - footer_h - 12 - len(built["hidden"]) * 8.4
            for line in built["hidden"]:
                sheet.hidden_text(20, max(hy, 20), line[:160], 6.0)
                hy += 8.4
            sheet.c.showPage()
            n_pages += 1
    sheet.c.save()
    print(f"[OK] fullimage 汇编：{n_pages} 页（全局页码 1..{global_no}）→ {out_pdf}")
    return out_pdf


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    from bidviz_config import load as _load
    ap = argparse.ArgumentParser(description="全图渲染（用户原始提示词格式 + 原文全文进图）")
    ap.add_argument("root", nargs="?", default="projects/anke-35kv")
    ap.add_argument("--section", default=None)
    ap.add_argument("--chapter", default=None)
    ap.add_argument("--variants", type=int, default=1,
                    help="每页变体数（多次调用实现；yoshub 实测不支持 n>1）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pdf", action="store_true")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_absolute():
        root = (BASE / root).resolve()
    _load(root)

    if args.section:
        sids = [s.strip().upper() for s in args.section.split(",")]
    elif args.chapter:
        sids = chapter_sids(root, args.chapter)
    else:
        raise SystemExit("需要 --section 或 --chapter 之一")
    print(f"范围：{len(sids)} 节 × ≤{MAX_PAGES} 页 × {args.variants} 变体 "
          f"（{sids[0]}…{sids[-1]}）", flush=True)

    t0 = time.time()
    for sid in sids:
        generate_section(root, sid, force=args.force, variants=args.variants,
                         dry_run=args.dry_run)
    if args.dry_run:
        return 0

    led = load_ledger(root)
    ok = [v for v in led.values() if v.get("ok")]
    print(f"\n完成：台账累计 {len(ok)} 张成功；"
          f"image tokens ≈ {len(ok) * 1650}（实测均值 1650/张）")
    print(f"耗时 {time.time() - t0:.0f}s；台账 {out_dir(root) / LEDGER_NAME}")

    if args.pdf:
        out = root / "output" / "施工组织设计-图册-A3-fullimage.pdf"
        build_fullimage_pdf(root, sids, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
