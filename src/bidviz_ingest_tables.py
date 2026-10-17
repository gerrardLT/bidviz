#!/usr/bin/env python3
"""bidviz_ingest_tables —— 原表初判前置（V1.3 阶段 3.0）。

扫 docx 顶层表格对象 → `work/source-tables.json`（轻量初判版）：
- 逐表记录：所在节（S 编号）、表头、行列数、预览、数值密度、流程标记；
- keep / redraw 初判规则（确定性、可复核）：
    * 表头命中参数/资源语素（型号/规格/数量/人数/周期…）或数值密度 ≥ 0.30 → keep（D/G 类素材）；
    * 含流程标记（→/⇒/流程/步骤）或低数值密度且行数少 → redraw（矢量化候选）；
    * 其余 → keep（保真默认：原表优先保留，不为了统一版式转图）。
- 本初判供阶段 3 批 2（A/D 模板）设计参考；**最终 keep/redraw/replace 决策在阶段 5
  source-assets.json 由人工签认（签认点⑤）**，本文件不替代该决策。

用法：
    python -m bidviz_ingest_tables projects/anke-35kv
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

SCHEMA_VERSION = "1.0"
OUT_NAME = "source-tables.json"

# 目录条目特征：点线引导 + 末尾页码（避免目录行污染节追踪）
_TOC_LINE = re.compile(r"[.·…]{4,}\s*\d+\s*$")
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百\d]+章")
_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百\d]+节")

# 参数/资源语素（D/G 类素材证据）
_PARAM_HEADERS = ("型号", "规格", "数量", "单位", "参数", "电压", "截面", "人数", "人数配置",
                  "工种", "机械", "设备", "材料", "名称", "周期", "频次", "时间", "天数",
                  "尺寸", "厚度", "等级", "配比", "责任人", "岗位", "检查项目", "判定")
# 流程/关系语素（矢量化候选证据）
_FLOW_MARKERS = ("→", "⇒", "➜", "流程", "步骤顺序", "先后", "衔接")


def _is_toc(para: DocxParagraph) -> bool:
    """目录条目：样式名 TOC* / 目录，或点线引导页码行。"""
    style = (getattr(para.style, "name", "") or "").lower()
    if style.startswith("toc") or "目录" in style:
        return True
    text = para.text.strip()
    return bool(_TOC_LINE.search(text))


def walk_docx(doc: Document) -> list[tuple[str, DocxTable]]:
    """按 body 顺序遍历，返回 [(「章 / 节」键, Table)]；表前无节标题时键为空串。"""
    chapter = section = ""
    out: list[tuple[str, DocxTable]] = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = DocxParagraph(child, doc)
            text = para.text.strip()
            if not text or _is_toc(para):
                continue
            if _CHAPTER_RE.match(text):
                chapter = text[:40]
                section = ""          # 章标题后节标题重置
            elif _SECTION_RE.match(text):
                section = text[:40]
        elif child.tag.endswith("}tbl"):
            key = f"{chapter} / {section}" if chapter and section else (chapter or section)
            out.append((key, DocxTable(child, doc)))
    return out


def _cell_texts(tbl: DocxTable) -> list[list[str]]:
    rows: list[list[str]] = []
    for r in tbl.rows:
        cells = []
        for c in r.cells:
            t = re.sub(r"\s+", " ", c.text or "").strip()
            cells.append(t)
        rows.append(cells)
    return rows


def _numeric_density(rows: list[list[str]]) -> float:
    cells = [c for r in rows for c in r if c]
    if not cells:
        return 0.0
    hits = sum(1 for c in cells if re.search(r"\d", c))
    return round(hits / len(cells), 3)


def _has_flow(rows: list[list[str]]) -> bool:
    sample = " ".join(c for r in rows[:4] for c in r)
    return any(m in sample for m in _FLOW_MARKERS)


def initial_decision(headers: list[str], rows: list[list[str]]) -> tuple[str, str]:
    """keep / redraw 初判；返回 (决策, 理由)。保真默认：证据不足 → keep。"""
    density = _numeric_density(rows)
    head_text = " ".join(headers)
    param_hit = [w for w in _PARAM_HEADERS if w in head_text]
    flow = _has_flow(rows)
    n_rows = len(rows)

    if param_hit:
        return "keep", f"表头命中参数/资源语素：{'、'.join(param_hit[:4])}"
    if density >= 0.30:
        return "keep", f"数值密度 {density} ≥ 0.30（参数资源型证据）"
    if flow:
        return "redraw", "含流程/衔接标记，关系类内容适合矢量重绘"
    if density < 0.15 and n_rows <= 8:
        return "redraw", f"数值密度 {density} 低且仅 {n_rows} 行，轻量关系表可矢量重绘"
    return "keep", f"证据不足，保真默认保留原表（数值密度 {density}）"


def scan(root: Path, docx_path: Path | None = None) -> dict[str, Any]:
    """扫 docx 表格并生成 source-tables.json 结构（不落盘）。"""
    root = Path(root)
    if docx_path is None:
        docx_path = next(iter(sorted(root.glob("inputs/*.docx"))), None)   # type: ignore[assignment]
    if not docx_path or not Path(docx_path).exists():
        raise SystemExit(f"未找到输入 docx：{docx_path or root / 'inputs'}")

    # blocks.json → 节键 → S 编号（文档顺序，与 e1.group_sections 一致）
    import bidviz_e1 as e1
    import build_e1 as be
    blocks, _pm = be.load_project(root)
    sections = e1.group_sections(blocks)
    key_to_sid: dict[str, str] = {}
    for s in sections:
        k = e1.section_key(s.blocks[0]) if s.blocks else ""
        if k and k not in key_to_sid:
            key_to_sid[k] = s.section_id

    doc = Document(str(docx_path))
    tables: list[dict[str, Any]] = []
    by_chapter: dict[str, dict[str, int]] = {}
    for idx, (key, tbl) in enumerate(walk_docx(doc), 1):
        rows = _cell_texts(tbl)
        if not rows:
            continue
        headers = rows[0]
        decision, reason = initial_decision(headers, rows)
        sid = key_to_sid.get(key, "")
        chapter = key.split(" / ")[0] if " / " in key else (key or "（无节归属）")
        stat = by_chapter.setdefault(chapter, {"tables": 0, "keep": 0, "redraw": 0})
        stat["tables"] += 1
        stat[decision] += 1
        tables.append({
            "index": idx,
            "section_id": sid,
            "section_path": key,
            "n_rows": len(rows),
            "n_cols": max(len(r) for r in rows),
            "headers": headers[:12],
            "preview": [c[:40] for c in (rows[1] if len(rows) > 1 else rows[0])][:12],
            "numeric_density": _numeric_density(rows),
            "flow_markers": _has_flow(rows),
            "initial_decision": decision,
            "reason": reason,
            "final_decision": None,      # 阶段 5 签认点⑤人工定案
            "final_reason": None,
        })

    n_keep = sum(1 for t in tables if t["initial_decision"] == "keep")
    return {
        "schema": f"bidviz-source-tables/{SCHEMA_VERSION}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(Path(docx_path).relative_to(root)) if Path(docx_path).is_relative_to(root) else str(docx_path),
        "note": "阶段 3.0 轻量初判；最终决策见阶段 5 source-assets.json（签认点⑤）",
        "summary": {"tables": len(tables), "keep": n_keep, "redraw": len(tables) - n_keep,
                    "by_chapter": by_chapter},
        "tables": tables,
    }


def run(root: Path) -> Path:
    out = Path(root) / "work" / OUT_NAME
    doc = scan(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    s = doc["summary"]
    print(f"[OK] 原表初判：{s['tables']} 表 → keep {s['keep']} / redraw {s['redraw']}，写入 {out}")
    for ch, st in s["by_chapter"].items():
        print(f"  {ch[:30]:<32} 表 {st['tables']:>2}  keep {st['keep']:>2}  redraw {st['redraw']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="docx 顶层表格初判 → work/source-tables.json")
    ap.add_argument("root", nargs="?", default="projects/anke-35kv")
    ap.add_argument("--docx", default=None, help="指定 docx 路径（默认取 inputs/ 第一份）")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if not root.is_absolute():
        root = (Path(__file__).resolve().parent.parent / root).resolve()
    run(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
