#!/usr/bin/env python3
"""用 gpt-image-2 以 2K（2048x1448，A3 比例）生成指定章节的全部节主视觉。

用法:
    python generate_chapter.py --chapter 第一章
    python generate_chapter.py --chapter 第一章 --model gpt-image-2 --size 2048x1448
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bidviz
import bidviz_e1 as e1
from bidviz_config import load as _load_cfg, get as _cfg
import build_e1 as be


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


def build_prompt(vis) -> str:
    """原始宏观提示词：给事实 + 视觉标准，让模型自主排版。"""
    facts = "；".join(b.text for b in vis.blocks[:12] if b.kind != "heading")[:1400]
    return "\n".join([
        f"为{_cfg().prompt_subject}生成：轴测爆炸分解图，构件按施工层级拆开悬浮，表现分层工艺与装配关系。",
        f"本页主题：{vis.title}。",
        f"必须依据以下实际文字内容，不得添加冲突的工艺与参数：{facts}",
        "视觉标准：专业工程技术图，尺度感真实，构件边缘清楚，灰蓝主色，安全风险用红橙。",
        "需要在图中直接生成简短中文构件名与实际情况引线标注；不得生成长段正文，不得虚构未给出的精确尺寸。",
        "所有标注使用规范简体中文；若无法正确生成文字则省略该标签，不得产生乱码、错别字、Logo 或水印。",
        "无 CAD、BIM 模型或现场照片，本图为文字约束的通用工程示意，不得暗示真实坐标。",
        "A3 横向构图，主体居中，四周留出自然空白。",
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="projects/anke-35kv")
    ap.add_argument("--chapter", default=None, help="只跑某一章（模糊匹配）")
    ap.add_argument("--all", action="store_true", help="跑全部章节")
    ap.add_argument("--provider", default=None, help="默认读 bidviz.toml")
    ap.add_argument("--model", default=None, help="默认读 bidviz.toml")
    ap.add_argument("--size", default=None, help="默认读 bidviz.toml")
    ap.add_argument("--outdir", default=None, help="输出子目录名，默认 visuals-2k")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    _load_cfg(root)                       # 载入项目配置
    chapmap, secs = chapter_of_sections(root)

    if args.all or not args.chapter:
        target = "（全部章节）"
        ids = [sid for v in chapmap.values() for sid in v]
    else:
        target = next((k for k in chapmap if args.chapter in k), None)
        if not target:
            print("可用章节:")
            for k, v in chapmap.items():
                print(f"  {k}  ({len(v)} 节)")
            raise SystemExit(f"未找到章节: {args.chapter}")
        ids = chapmap[target]

    provider = args.provider or _cfg().provider
    model = args.model or _cfg().model
    size = args.size or _cfg().image_size
    out_dir = root / "work" / (args.outdir or _cfg().image_dir.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = root / "work" / "generation-chapter.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []

    print(f"章节: {target}  共 {len(ids)} 节  模型={model}  尺寸={size}", flush=True)
    print(f"供应商: {provider}   输出: {out_dir}", flush=True)

    ok = fail = skip = 0
    for n, sid in enumerate(ids, 1):
        sec = secs[sid]
        vis = sec.as_visual_item()
        f = out_dir / f"{sid}.png"
        if f.exists() and not args.force and f.stat().st_size > 2000:
            skip += 1
            print(f"[{n}/{len(ids)}] {sid} 已存在，跳过", flush=True)
            continue

        rec = {"section_id": sid, "title": vis.title, "chapter": target,
               "model": model, "size": size}
        print(f"[{n}/{len(ids)}] {sid} {vis.title} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            api = bidviz.ImageAPI(provider_name=provider, model=model)
            api.generate(build_prompt(vis), f, recorder=rec, size=size)
            rec.update({"ok": True, "sec": round(time.time() - t0, 1),
                        "kb": round(f.stat().st_size / 1024)})
            ok += 1
            print(f"OK {rec['kb']}KB {rec['sec']}s", flush=True)
        except Exception as exc:
            rec.update({"ok": False, "error": str(exc)[:300], "sec": round(time.time() - t0, 1)})
            fail += 1
            print(f"FAIL {str(exc)[:120]}", flush=True)

        log.append(rec)
        log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(_cfg().pause)

    print(f"\n完成: 生成 {ok}  跳过 {skip}  失败 {fail}", flush=True)
    print(f"输出目录: {out_dir}", flush=True)


if __name__ == "__main__":
    main()
