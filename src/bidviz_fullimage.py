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
    sec = next((s for s in e1.group_sections(blocks) if s.section_id == sid), None)
    if not sec:
        raise SystemExit(f"blocks 中无节 {sid}")
    out = []
    for b in sec.blocks:
        t = (getattr(b, "text", "") or "").strip()
        if not t or t == sec.title:
            continue
        out.append((getattr(b, "kind", "body"), t))
    return out


def split_pages(blocks: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """按块边界与字数把节内容分成 1–3 页。"""
    total = sum(len(t) for _k, t in blocks)
    n_pages = 1 if total <= SPLIT_CHARS else (2 if total <= SPLIT_CHARS * 2 else MAX_PAGES)
    if n_pages == 1:
        return [blocks]
    pages: list[list[tuple[str, str]]] = [[] for _ in range(n_pages)]
    budget = total / n_pages
    acc = 0
    idx = 0
    for blk in blocks:
        pages[idx].append(blk)
        acc += len(blk[1])
        if acc >= budget * 0.92 and idx < n_pages - 1:
            idx += 1
            acc = 0
    return [p for p in pages if p]


def _hierarchy(root: Path, sid: str) -> list[str]:
    """章节层级三行（章 / 节 / 首个三级主题——对齐用户原始提示词格式）。"""
    pp = __import__("bidviz_plan").load_page_plan(root) or {}
    page = pp["sections"][sid]["pages"][0]
    lines = [f"- {page['title']['chapter']}", f"- {page['title']['section']}"]
    for kind, t in _section_blocks(root, sid):
        if kind == "heading" and t.startswith(("一、", "二、", "三、")):
            lines.append(f"- {t}")
            break
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
    page_note = ("\n本页为该节多页排版中的一页，只编排上面给出的原文分段；"
                 "不要编造分段之外的工序或参数，不要自行标注页码。" if total_pages > 1 else "")

    tpl = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    from bidviz_config import get as _cfg2
    project_name = _cfg2().name
    prompt = (tpl
              .replace("{{chapter_hierarchy}}", "\n".join(_hierarchy(root, sid)))
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
                  variant: int = 1, dry_run: bool = False) -> dict[str, Any]:
    import bidviz
    from bidviz_config import provider_credentials, get as _cfg

    root = Path(root)
    built = build_fullpage_prompt(root, sid, page_no, total_pages, page_blocks)
    stem = f"{sid}_p{page_no}" + (f"_v{variant}" if variant > 1 else "")
    out = out_dir(root) / f"{stem}.png"
    prompt_snap = out_dir(root) / f"_prompt_{stem}.txt"

    if dry_run:
        print(f"[dry-run] {stem} 提示词 {len(built['prompt'])} 字 / "
              f"原文 {built['meta']['chars']} 字")
        print(built["prompt"])
        return {"sid": sid, "dry_run": True}

    if out.exists() and out.stat().st_size > 2000 and not force:
        return {"sid": sid, "stem": stem, "skipped": True}

    cfg = _cfg()
    cred = provider_credentials(cfg.provider)
    api = bidviz.ImageAPI(base_url=cred["baseUrl"], api_key=cred["apiKey"], model=cfg.model)

    rec: dict[str, Any] = {"sid": sid, "stem": stem, "page": page_no, "variant": variant,
                           "chars": built["meta"]["chars"],
                           "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    t0 = time.time()
    last_err = ""
    refs = [ANCHOR_REF] if ANCHOR_REF.exists() else []
    rerolls = 0
    # 外层：供应商限流（503）退避 + 尺寸抽卡重掷；内层：尺寸回退链
    while True:
        for attempt in range(1, 4):
            for size in size_chain():
                try:
                    api.generate(built["prompt"], out, references=refs,
                                 recorder=rec, size=size)
                    from PIL import Image
                    w, h = Image.open(out).size
                    rec.update({"ok": True, "requested": size, "actual": f"{w}x{h}",
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
                wait = 90 * attempt          # 池子恢复需要时间，指数等待
                print(f"    [wait] 供应商限流，{wait}s 后重试（第 {attempt}/3 次）", flush=True)
                time.sleep(wait)
            else:
                break
        # 尺寸验证重掷（taomu 抽卡）：宽度不足则丢弃重画（按次收费，重掷=再来一次）
        from PIL import Image
        if rec.get("ok") and Image.open(out).size[0] < MIN_WIDTH and rerolls < REROLL_MAX:
            rerolls += 1
            print(f"    [reroll] {stem} 宽度不足 {MIN_WIDTH}，重掷 {rerolls}/{REROLL_MAX}", flush=True)
            out.unlink(missing_ok=True)
            rec["ok"] = False
            continue
        break
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


def build_fullimage_pdf(root: Path, sids: list[str], out_pdf: Path) -> Path:
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
            # 变体挑选：取台账里 actual 最大的（同请求档下分辨率最高者）
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
