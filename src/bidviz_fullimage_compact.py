#!/usr/bin/env python3
"""AI 生图压缩版：按项目 [compact] 页数目标合并小节并生成 AI 页。

和全图路线的区别：
- 内容源用 work/page-plan.json 的精简 prose + 策展表格（不是 10 万字全文）；
- 连续小节按项目 [compact] 的字数/节数上限合并，避免每节强制起新页；
- 每页仍由 AI 生成整张 A3 设计稿；正文在提示词中用「」包住，提升逐字准确率；
- 封面复用已生成 S001_p1.png，不再重画；
- 隐藏文字层写入每页的合并正文，保证 Ctrl+F / 反抽审计。

产物：
  work/fullimage-compact/P001.png …              每个 AI 页
  work/fullimage-compact/ledger.json             台账
  work/fullimage-compact-plan.json               分页方案
  output/施工组织设计-图册-A3-fullimage-50p.pdf   汇编成品

用法：
  python -m bidviz_fullimage_compact projects/anke-35kv --plan
  python -m bidviz_fullimage_compact projects/anke-35kv --generate --limit 1
  python -m bidviz_fullimage_compact projects/anke-35kv --generate
  python -m bidviz_fullimage_compact projects/anke-35kv --pdf
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz  # noqa: E402
import bidviz_fullimage as fi  # noqa: E402
import bidviz_plan as plan_mod  # noqa: E402
import bidviz_vector as bv  # noqa: E402
from bidviz_config import load as load_cfg, provider_credentials  # noqa: E402

OUT_DIR = "work/fullimage-compact"
PLAN_NAME = "fullimage-compact-plan.json"
LEDGER = "ledger.json"
MAX_CHARS = 2000
MAX_SECTIONS = 2
MIN_WIDTH = 2048
MIN_HEIGHT = 2800
HEADER_PROJECT = "安科生物项目35kV电力接入工程"


@dataclass
class SectionContent:
    sid: str
    chapter: str
    title: str
    text: str
    chars: int
    main_class: str = "B"


@dataclass
class PageGroup:
    no: int
    chapter: str
    titles: list[str]
    sections: list[str]
    text: str                 # prompt 用：每节正文用「」包住
    plain_text: str           # 隐藏层用：不带引号
    chars: int
    main_classes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 分页方案


_TRACE_RE = re.compile(r"[（(]\s*逐字取自原文\s*[）)]|逐字取自原文")
_SEQ_RE = re.compile(r"^\s*(?:\d{1,3}|[①-⑩]|[一二三四五六七八九十]+)\s*[、.．)）]?\s*")


def _clean_text(value: Any) -> str:
    """删除 page-plan 里的编辑说明字样，避免被 AI 画进成品。"""
    text = str(value or "")
    text = _TRACE_RE.sub("", text)
    text = text.replace("（续）", "").replace("(续)", "")
    return re.sub(r"\s+", " ", text).strip()


def _clean_table(table: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    headers = [_clean_text(x) for x in (table.get("headers") or [])]
    rows = table.get("rows") or []
    seq_col = bool(headers) and any(k in headers[0] for k in
                                    ("序号", "阶段", "工序", "步骤", "环节"))
    cleaned: list[list[str]] = []
    for i, row in enumerate(rows, 1):
        cells = [_clean_text(x) for x in row]
        if seq_col and cells:
            label = _SEQ_RE.sub("", cells[0]).strip()
            cells[0] = f"{i}. {label}" if label else str(i)
        if not seq_col and cells and len(cells[0]) < 2:
            continue
        cleaned.append(cells)
    return headers, cleaned


def load_sections(root: Path) -> list[SectionContent]:
    pp = plan_mod.load_page_plan(root)
    if not pp:
        pp = plan_mod.build_page_plan(root)
    out: list[SectionContent] = []
    for sid in sorted((pp.get("sections") or {}).keys()):
        entry = pp["sections"][sid]
        pages = entry.get("pages") or []
        title = pages[0].get("title") or {} if pages else {}
        chapter = title.get("chapter") or entry.get("title", "")
        section = title.get("section") or entry.get("title", "")
        main = (pages[0].get("main_class") if pages else "") or entry.get("main_class") or "B"
        parts: list[str] = []
        chars = 0
        for page in pages:
            for p in page.get("prose", []):
                text = _clean_text(p.get("text", ""))
                if text:
                    parts.append(text)
                    chars += len(text)
            for table in page.get("tables", []):
                caption = _clean_text(table.get("caption", ""))
                headers, rows = _clean_table(table)
                lines = []
                if caption:
                    lines.append(caption)
                if headers:
                    lines.append("｜".join(headers))
                for row in rows:
                    lines.append("｜".join(row))
                block = "\n".join(lines)
                if block:
                    parts.append(block)
                    chars += len(block)
        text = "\n".join(parts)
        out.append(SectionContent(sid, chapter, section, text, chars, str(main)))
    return out


def group_sections(secs: list[SectionContent], max_chars: int = MAX_CHARS,
                   max_sections: int = MAX_SECTIONS) -> list[PageGroup]:
    groups: list[PageGroup] = []
    cur: list[SectionContent] = []
    chars = 0

    def flush() -> None:
        nonlocal cur, chars
        if not cur:
            return
        no = len(groups) + 1
        quoted = "\n".join(
            f"【{s.title}】\n「\n{s.text}\n」" for s in cur)
        plain = "\n".join(f"【{s.title}】\n{s.text}" for s in cur)
        groups.append(PageGroup(
            no=no, chapter=cur[0].chapter,
            titles=[s.title for s in cur],
            sections=[s.sid for s in cur],
            text=quoted, plain_text=plain, chars=chars,
            main_classes=sorted({s.main_class for s in cur})))
        cur, chars = [], 0

    for sec in secs:
        if sec.sid == "S001":       # 封面单独复用
            continue
        if cur and (sec.chapter != cur[0].chapter or
                    chars + sec.chars > max_chars or
                    len(cur) >= max_sections):
            flush()
        cur.append(sec)
        chars += sec.chars
    flush()
    return groups


def plan_for_target(secs: list[SectionContent], min_pages: int = 0,
                    max_pages: int = 0, max_chars: int = MAX_CHARS,
                    max_sections: int = MAX_SECTIONS
                    ) -> tuple[list[PageGroup], int, int, int]:
    """按项目页数目标自适应合并粒度；0 表示该方向不限制。"""
    chars, sections = max_chars, max_sections
    groups = group_sections(secs, chars, sections)
    total = len(groups) + 1
    for _ in range(30):
        if max_pages and total > max_pages:
            if chars < 3200:
                chars = min(3200, int(chars * 1.12) + 20)
            elif sections < 4:
                sections += 1
            else:
                break
        elif min_pages and total < min_pages:
            if sections > 1:
                sections = 1
            elif chars > 900:
                chars = max(900, int(chars * 0.9) - 20)
            else:
                break
        else:
            break
        groups = group_sections(secs, chars, sections)
        total = len(groups) + 1
    if max_pages and total > max_pages:
        raise SystemExit(
            f"当前内容量无法压到 {max_pages} 页（最少 {total} 页）；"
            "请放宽 [compact].max_pages 或删减内容。")
    if min_pages and total < min_pages:
        print(f"[WARN] 当前内容量只能排到 {total} 页，低于 min_pages={min_pages}",
              flush=True)
    return groups, chars, sections, total


def plan_doc(groups: list[PageGroup], max_chars: int = MAX_CHARS,
             max_sections: int = MAX_SECTIONS,
             min_pages: int = 0, max_pages: int = 0) -> dict[str, Any]:
    return {
        "schema": "bidviz-fullimage-compact-plan/1",
        "max_chars": max_chars,
        "max_sections": max_sections,
        "min_pages": min_pages,
        "max_pages": max_pages,
        "total_pages": len(groups) + 1,
        "cover": "work/fullimage/S001_p1.png",
        "pages": [
            {"no": g.no, "chapter": g.chapter, "titles": g.titles,
             "sections": g.sections, "chars": g.chars,
             "image": f"{OUT_DIR}/P{g.no:03d}.png"}
            for g in groups
        ],
    }


# ---------------------------------------------------------------- 提示词


def build_prompt(group: PageGroup, cfg) -> str:
    tpl = fi.PROMPT_TEMPLATE.read_text(encoding="utf-8")
    hierarchy = (f"章：{group.chapter}\n"
                 f"节：{' / '.join(group.titles)}\n"
                 f"本页主题：{'、'.join(group.titles)}")
    if len(group.sections) > 1:
        hierarchy += (f"\n本页包含 {len(group.sections)} 个小节："
                      "请用小标题/分区把各小节清楚分开，不要遗漏任何小节。")
    imagery = "、".join(dict.fromkeys(
        fi.CLASS_IMAGERY.get(c, fi.CLASS_IMAGERY["B"]) for c in group.main_classes))
    page_note = (
        "\n下面「」内是正文原文，必须逐字保留，技术参数、型号、数字不得改写；"
        "「」只是正文边界标记，不要绘制到页面中。"
        "\n只编排以下内容，不要补写、不要重复、不要页码，严禁出现任何（续）字样。"
        "\n【页面文字禁令】页面只出现正文、表格和必要的图注；"
        "不要出现任何编辑说明、校核说明、来源说明、生成说明或排版标记字样。"
        "\n表格行号必须从 1 开始连续编号，不得跳号、重号。")
    return (tpl
            .replace("{{chapter_hierarchy}}", hierarchy)
            .replace("{{project_name}}", HEADER_PROJECT)
            .replace("{{class_imagery}}", imagery)
            .replace("{{page_note}}", page_note)
            .replace("{{source_text}}", group.text))


# ---------------------------------------------------------------- 生图


_DENY_PROVIDERS = {"taomu", "gptimage2"}


def _providers(cfg) -> list[tuple[str, str]]:
    raw = cfg.raw.get("image", {})
    pairs = [(cfg.provider, cfg.model)]
    fb_p = raw.get("fallback_provider") or "grokimg"
    fb_m = raw.get("fallback_model") or "gpt-image-2-2K/4K"
    if fb_p:
        pairs.append((fb_p, fb_m))
    out, seen = [], set()
    for pair in pairs:
        provider = str(pair[0] or "").strip().lower()
        if provider in _DENY_PROVIDERS:
            continue
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    if not out:
        out.append(("grokimg", "gpt-image-2-2K/4K"))
    return out


def _api(provider: str, model: str):
    try:
        cred = provider_credentials(provider, env_first=False)
        return bidviz.ImageAPI(base_url=cred["baseUrl"], api_key=cred["apiKey"], model=model)
    except RuntimeError:
        return bidviz.ImageAPI(provider_name=provider, model=model)


def _sizes() -> list[str]:
    chain = fi.size_chain()
    preferred = [s for s in chain if s == "2048x2896"]
    return (preferred + [s for s in chain if s != "2048x2896"]) or ["2048x2896"]


def _ledger_path(root: Path, name: str = LEDGER) -> Path:
    return root / OUT_DIR / name


def _load_ledger(root: Path, name: str = LEDGER) -> dict[str, Any]:
    p = _ledger_path(root, name)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(root: Path, led: dict[str, Any], name: str = LEDGER) -> None:
    p = _ledger_path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(led, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def generate_group(root: Path, group: PageGroup, force: bool = False,
                   dry_run: bool = False, size_override: str | None = None,
                   ledger_name: str = LEDGER) -> dict[str, Any]:
    cfg = load_cfg(root)
    out_dir = root / OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"P{group.no:03d}.png"
    prompt = build_prompt(group, cfg)
    key = f"P{group.no:03d}"
    if out.exists() and out.stat().st_size > 2000 and not force:
        return {"key": key, "skip": True, "path": out}
    if dry_run:
        return {"key": key, "dry_run": True, "chars": len(prompt), "path": out,
                "prompt": prompt}
    rec: dict[str, Any] = {
        "key": key, "chapter": group.chapter, "titles": group.titles,
        "sections": group.sections, "chars": group.chars,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    last_err = ""
    ok = False
    t0 = time.time()
    sizes = ([size_override] if size_override else []) + _sizes()
    sizes = list(dict.fromkeys(sizes))
    for provider, model in _providers(cfg):
        try:
            api = _api(provider, model)
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for attempt in range(1, 4):
            for size in sizes:
                try:
                    gen_rec: dict[str, Any] = {}
                    api.generate(prompt, out, size=size, recorder=gen_rec)
                    rec.update(gen_rec)
                    rec.update({"provider": provider, "model": model,
                                "requested": size, "attempts": attempt})
                    from PIL import Image
                    with Image.open(out) as im:
                        w, h = im.size
                    if w < MIN_WIDTH or h < MIN_HEIGHT:
                        out.unlink(missing_ok=True)
                        last_err = f"分辨率不足 {w}x{h}，拒绝低清图"
                        rec["low_res"] = f"{w}x{h}"
                        continue
                    rec.update({"actual": f"{w}x{h}", "sec": round(time.time() - t0, 1),
                                "kb": round(out.stat().st_size / 1024),
                                "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest()})
                    ok = True
                    break
                except Exception as exc:
                    last_err = f"{type(exc).__name__}: {exc}"[:200]
                    time.sleep(2 ** (attempt - 1))
            if ok:
                break
        if ok:
            break
    rec.update({"ok": ok, "error": "" if ok else last_err,
                "sec": round(time.time() - t0, 1)})
    if ok:
        (out_dir / f"_prompt_P{group.no:03d}.txt").write_text(prompt, encoding="utf-8")
        rec.pop("error", None)
    led = _load_ledger(root, ledger_name)
    led[key] = rec
    _save_ledger(root, led, ledger_name)
    return {"key": key, "ok": ok, "path": out, "error": last_err,
            "kb": rec.get("kb", 0), "sec": rec.get("sec", 0)}


# ---------------------------------------------------------------- 汇编


def _fixed_frame(sheet, chapter: str, page_no: int, project: str) -> None:
    """统一页眉页脚：覆盖 AI 自由绘制的头部/尾部，再画固定矢量版式。"""
    W, H = sheet.w, sheet.h
    sheet.rect(0, 0, W, 64, fill="#ffffff", stroke=None)
    sheet.rect(0, H - 32, W, 32, fill="#ffffff", stroke=None)
    sheet.text(36, 14, chapter, 13.0, True, "#103164", max_w=W - 235)
    sheet.text(W - 72, 14, project, 10.5, False, "#727272", align="right")
    for i, color in enumerate(("#b0c3df", "#4a7fb5", "#004890")):
        x = W - 62 + i * 9
        sheet.polygon([(x, 16), (x + 7, 16), (x + 3, 28), (x - 4, 28)], fill=color)
    sheet.line(36, 44, W - 36, 44, color="#004890", lw=1.0)
    sheet.line(36, H - 30, W - 36, H - 30, color="#c1d0d9", lw=0.6)
    sheet.text(W / 2, H - 24, f"— {page_no} —", 9.0, False, "#5a6e7c", align="center")


def assemble(root: Path, out_pdf: Path | None = None) -> Path:
    root = Path(root)
    cfg = load_cfg(root)
    plan_path = root / "work" / PLAN_NAME
    if not plan_path.exists():
        raise SystemExit("缺少分页方案，先运行 --plan")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    groups = plan["pages"]
    from bidviz_fullimage_store import published_path
    cover = published_path(root, "S001", 1)
    if not cover.exists():
        legacy = root / "work" / "fullimage" / "S001_p1.png"
        cover = legacy if legacy.exists() else cover
    if not cover.exists():
        raise SystemExit(f"封面图不存在：{cover}")
    out_pdf = Path(out_pdf) if out_pdf else root / "output" / "施工组织设计-图册-A3-fullimage-50p.pdf"
    out_pdf.parent.mkdir(parents=True, exist_ok=True)

    bv.set_page("A3", "portrait")
    sheet = bv.Sheet(out_pdf)
    footer_h = 24.0
    n = 0

    def add_page(img_path: Path, hidden_lines: list[str], chapter: str = "") -> None:
        nonlocal n
        n += 1
        sheet.image(img_path, 0, 0, sheet.w, sheet.h - footer_h, dpi=200, fit="contain")
        if chapter:
            _fixed_frame(sheet, chapter, n, HEADER_PROJECT)
        else:
            sheet.text(sheet.w / 2, sheet.h - footer_h + 7, f"— {n} —",
                       9.0, False, "#5a6e7c", align="center")
        hy = sheet.h - footer_h - 12 - len(hidden_lines) * 8.4
        for line in hidden_lines:
            sheet.hidden_text(20, max(hy, 20), line[:160], 6.0)
            hy += 8.4
        sheet.c.showPage()

    add_page(cover, [f"{cfg.name} {cfg.voltage} 电力接入工程 {cfg.doc_type}"], "")
    for g in groups:
        img = root / g["image"]
        if not img.exists():
            raise SystemExit(f"缺图：{img}")
        plain = (root / OUT_DIR / f"_text_P{g['no']:03d}.txt")
        if plain.exists():
            text = plain.read_text(encoding="utf-8")
        else:
            text = "\n".join(
                [f"【{t}】" for t in g.get("titles", [])] + [""])
        chunks: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            for i in range(0, len(line), 150):
                chunks.append(line[i:i + 150])
        add_page(img, chunks, str(g.get("chapter", "")))
    sheet.c.save()
    print(f"[OK] 压缩全图版：{n} 页 → {out_pdf}")
    return out_pdf


# ---------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI 生图压缩版（页数目标按项目配置）")
    ap.add_argument("root", nargs="?", default="projects/anke-35kv")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--generate", action="store_true")
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=None)
    ap.add_argument("--start", type=int, default=None, help="起始页号（含）")
    ap.add_argument("--end", type=int, default=None, help="结束页号（含）")
    ap.add_argument("--size", default=None, help="覆盖生图尺寸，如 2048x2896")
    ap.add_argument("--tag", default="", help="并发任务的台账后缀")
    ap.add_argument("--min-pages", type=int, default=None,
                    help="覆盖 [compact].min_pages；0 = 不要求下限")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="覆盖 [compact].max_pages；0 = 不限页数")
    ap.add_argument("--max-chars", type=int, default=None,
                    help="覆盖 [compact].max_chars_per_page")
    ap.add_argument("--max-sections", type=int, default=None,
                    help="覆盖 [compact].max_sections_per_page")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_absolute():
        root = (BASE / root).resolve()
    cfg = load_cfg(root)
    secs = load_sections(root)
    min_pages = (args.min_pages if args.min_pages is not None
                 else cfg.compact_min_pages)
    max_pages = (args.max_pages if args.max_pages is not None
                 else cfg.compact_max_pages)
    max_chars = (args.max_chars if args.max_chars is not None
                 else cfg.compact_max_chars)
    max_sections = (args.max_sections if args.max_sections is not None
                    else cfg.compact_max_sections)
    # 命令行只给一侧时，另一侧按显式要求处理，避免 45–40 这种矛盾目标
    if args.max_pages is not None and args.min_pages is None:
        min_pages = 0
    if args.min_pages is not None and args.max_pages is None and max_pages and min_pages > max_pages:
        max_pages = 0
    if max_pages and min_pages > max_pages:
        raise SystemExit(f"min_pages={min_pages} 不能大于 max_pages={max_pages}")
    groups, max_chars, max_sections, total = plan_for_target(
        secs, min_pages=min_pages, max_pages=max_pages,
        max_chars=max_chars, max_sections=max_sections)
    plan_path = root / "work" / PLAN_NAME
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan = plan_doc(groups, max_chars, max_sections, min_pages, max_pages)
    if not args.generate or args.plan:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    target = (f"{min_pages}–{max_pages}" if max_pages and min_pages
              else (f"≤{max_pages}" if max_pages
                    else (f"≥{min_pages}" if min_pages else "不限")))
    print(f"页数目标：{target}；实际：{len(groups)} 个 AI 内容页 + 封面 = {total} 页")
    for g in groups:
        print(f"  P{g.no:03d} {g.chapter} | {' / '.join(g.titles)} | {g.chars} 字")

    if args.plan and not (args.generate or args.pdf):
        return 0
    if args.generate:
        start = (args.start or 1) - 1
        end = args.end if args.end else len(groups)
        todo = groups[start:end]
        if args.limit:
            todo = todo[:args.limit]
        ledger_name = f"ledger-{args.tag}.json" if args.tag else LEDGER
        ok = fail = skip = 0
        for g in todo:
            # 写隐藏层文本，供汇编使用
            (root / OUT_DIR).mkdir(parents=True, exist_ok=True)
            (root / OUT_DIR / f"_text_P{g.no:03d}.txt").write_text(g.plain_text, encoding="utf-8")
            r = generate_group(root, g, force=args.force, dry_run=args.dry_run,
                               size_override=args.size, ledger_name=ledger_name)
            if r.get("skip"):
                skip += 1
                print(f"{r['key']} 已存在，跳过", flush=True)
            elif r.get("dry_run"):
                print(f"{r['key']} dry-run prompt {r['chars']} 字", flush=True)
            elif r.get("ok"):
                ok += 1
                print(f"{r['key']} OK {r.get('kb', 0)}KB {r.get('sec', 0)}s", flush=True)
            else:
                fail += 1
                print(f"{r['key']} FAIL {r.get('error', '')}", flush=True)
        print(f"完成：生成 {ok}，跳过 {skip}，失败 {fail}", flush=True)
    if args.pdf:
        assemble(root, Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
