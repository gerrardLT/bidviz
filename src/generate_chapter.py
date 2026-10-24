#!/usr/bin/env python3
"""按图位规划逐张生图：每节 1~N 张主图，每张可以走不同的生图 API。

规划由 bidviz_plan 产出并落盘 work/figure-plan.json（可人工审改）；本脚本只负责
按规划花钱生图，并把结果记进 work/generation-figures.json，按「节#图序」去重，
因此中断后重跑会自动跳过已生成的图。

用法:
    python generate_chapter.py --all                        # 整册
    python generate_chapter.py --chapter 第一章              # 只跑某一章
    python generate_chapter.py --section S006,S012          # 只跑指定节
    python generate_chapter.py --section S012 --fig 2 --force
    python generate_chapter.py --all --dry-run              # 只看提示词与生图参数
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import bidviz_e1 as e1
import bidviz_plan as plan
from bidviz_config import load as _load_cfg, get as _cfg
import build_e1 as be

LEDGER = "generation-figures.json"


def chapter_of_sections(root: Path):
    """把每个节映射到它所属的「第X章」。

    Section.chapter 实际存的是分项路径，不是章名；所以这里用 block 对象身份
    定位每个节的首个 block 下标，再和章标题的位置比较。
    """
    blocks, _ = be.load_project(root)
    marks = []
    for i, b in enumerate(blocks):
        t = getattr(b, "text", "").strip()
        if re.match(r"^第[一二三四五六七八九十]+章", t):
            marks.append((i, t[:40]))

    sections = e1.group_sections(blocks)
    pos_of = {id(b): i for i, b in enumerate(blocks)}

    out: dict[str, list[str]] = {}
    for s in sections:
        bi = pos_of.get(id(s.blocks[0]), 0) if s.blocks else 0
        cur = "(无章)"
        for mi, mt in marks:
            if mi <= bi:
                cur = mt
            else:
                break
        out.setdefault(cur, []).append(s.section_id)
    return out, {s.section_id: s for s in sections}


def target_sections(root: Path, chapter: str | None, section: str | None) -> tuple[list[str], str]:
    """解析范围参数，返回（节 id 列表, 范围描述）。"""
    chapmap, secs = chapter_of_sections(root)
    if section:
        wanted = {x.strip().upper() for x in section.split(",") if x.strip()}
        ids = [sid for sid in secs if sid.upper() in wanted]
        if not ids:
            raise SystemExit(f"未找到指定节：{section}")
        return ids, f"指定 {len(ids)} 节"
    if chapter:
        target = next((k for k in chapmap if chapter in k), None)
        if not target:
            print("可用章节:")
            for k, v in chapmap.items():
                print(f"  {k}  ({len(v)} 节)")
            raise SystemExit(f"未找到章节: {chapter}")
        return chapmap[target], target
    return [sid for v in chapmap.values() for sid in v], "（全部章节）"


def ledger_path(root: Path) -> Path:
    return root / "work" / LEDGER


def load_ledger(root: Path) -> dict[str, Any]:
    p = ledger_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_ledger(root: Path, led: dict[str, Any]) -> None:
    import bidviz_ioutil
    bidviz_ioutil.atomic_write_json(ledger_path(root), led)


def figure_progress(root: Path) -> dict[str, Any]:
    """规划了多少张图、已落地多少张（供 status / Web 显示）。

    「已生成」以图片文件为准，因此旧管线生的图也算数；台账只用来报失败。
    """
    secs = (plan.load_plan(root) or {}).get("sections") or {}
    led = load_ledger(root)
    planned = done = 0
    kinds: dict[str, int] = {}
    for sid, entry in secs.items():
        figs = entry.get("figures", [])
        imgs = plan.resolve_images(root, sid, figs)
        for f in figs:
            no = int(f.get("no", 1))
            kind = f.get("kind", "?")
            planned += 1
            kinds[kind] = kinds.get(kind, 0) + 1
            if imgs.get(no) or led.get(f"{sid}#{no}", {}).get("ok"):
                done += 1
    return {"sections": len(secs), "planned": planned, "generated": done,
            "failed": sum(1 for v in led.values() if not v.get("ok")),
            "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1]))}


def generate_figures(root: Path, ids: list[str], fig_no: int | None = None,
                     force: bool = False, outdir: str | None = None, attempts: int = 3,
                     pause: float | None = None, dry_run: bool = False,
                     limit: int | None = None) -> dict[str, Any]:
    """按规划逐张生图。生图参数以当前配置为准（[image.figure_overrides] 可按图序/图型分流）。"""
    plan.ensure_plan(root)                      # 正文变过的节自动重算提示词
    secs = (plan.load_plan(root) or {}).get("sections", {})
    out_dir = root / "work" / (outdir or _cfg().image_dir.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    led = load_ledger(root)
    pause = _cfg().pause if pause is None else pause

    todo: list[tuple[str, int, dict]] = []
    for sid in ids:
        for f in secs.get(sid, {}).get("figures", []):
            no = int(f.get("no", 1))
            if fig_no and no != fig_no:
                continue
            todo.append((sid, no, f))
    if limit:
        todo = todo[:limit]

    apis: dict[tuple[str, str], Any] = {}
    ok = fail = skip = 0
    for n, (sid, no, f) in enumerate(todo, 1):
        kind = f.get("kind") or plan.DEFAULT_KIND
        g = plan.gen_config(no, kind)           # 现算，避免规划落盘后改了配置不生效
        out = out_dir / plan.figure_filename(sid, no)
        tag = f"[{n}/{len(todo)}] {sid} 图{no} {kind}"
        if out.exists() and out.stat().st_size > 2000 and not force:
            skip += 1
            print(f"{tag} 已存在，跳过", flush=True)
            continue
        if dry_run:
            print(f"{tag} dry-run provider={g['provider']} model={g['model']} size={g['size']} "
                  f"提示词 {len(f.get('prompt', ''))} 字 custom={bool(f.get('custom'))}", flush=True)
            continue

        rec: dict[str, Any] = {
            "section_id": sid, "no": no, "kind": kind, "name": f.get("name"),
            "provider": g["provider"], "model": g["model"], "size": g["size"],
            "custom": bool(f.get("custom")), "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        key = (g["provider"], g["model"])
        api = apis.get(key)
        if api is None:
            apis[key] = api = plan.make_api({**f, "gen": g})
        last, done, attempt = "", False, 0
        t0 = time.time()
        for attempt in range(1, attempts + 1):
            try:
                api.generate(f.get("prompt", ""), out, recorder=rec, size=g["size"])
                done = True
                break
            except Exception as exc:            # 生图接口会间歇性断连，指数退避重试
                last = f"{type(exc).__name__}: {exc}"[:200]
                if attempt < attempts:
                    time.sleep(pause * (2 ** (attempt - 1)))
        rec.update({"ok": done, "attempts": attempt, "sec": round(time.time() - t0, 1)})
        if done:
            rec["kb"] = round(out.stat().st_size / 1024)
            rec.pop("error", None)
            ok += 1
        else:
            rec["error"] = last
            fail += 1
        led[f"{sid}#{no}"] = rec
        save_ledger(root, led)
        print(f"{tag} {'OK' if done else 'FAIL'} {rec.get('kb', '')}KB {rec['sec']}s "
              f"attempts={attempt} {last}", flush=True)
        time.sleep(pause)

    return {"planned": len(todo), "generated": ok, "skipped": skip, "failed": fail,
            "dry_run": dry_run, "out_dir": str(out_dir)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--chapter", default=None, help="只跑某一章（模糊匹配）")
    ap.add_argument("--section", default=None, help="只跑指定节，如 S006 或 S006,S012")
    ap.add_argument("--fig", type=int, default=None, help="只跑每节的第几张图（1/2）")
    ap.add_argument("--all", action="store_true", help="跑全部章节")
    ap.add_argument("--limit", type=int, default=None, help="最多生几张（试跑用）")
    ap.add_argument("--outdir", default=None, help="输出子目录名，默认取 [image].dir")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--pause", type=float, default=None)
    ap.add_argument("--force", action="store_true", help="已有图也重生")
    ap.add_argument("--dry-run", action="store_true", help="只打印提示词与生图参数，不调 API")
    args = ap.parse_args()

    root = Path(args.root)
    _load_cfg(root)
    ids, target = target_sections(root, args.chapter, args.section)
    print(f"范围: {target}  共 {len(ids)} 节   输出: {root / 'work'}", flush=True)

    t0 = time.time()
    stat = generate_figures(root, ids, fig_no=args.fig, force=args.force, outdir=args.outdir,
                            attempts=args.attempts, pause=args.pause, dry_run=args.dry_run,
                            limit=args.limit)
    print(f"\n完成: {stat}  耗时 {round(time.time() - t0, 1)}s", flush=True)


if __name__ == "__main__":
    main()
