#!/usr/bin/env python3
"""layout_audit —— 乱度门禁量化与报告。

功能：
- fitz page.get_drawings() → 填充色块数/不同填充色数
- fitz text('dict') → 行 bbox x0 离散度统计
- 对照 tokens.toml gate.thresholds 阈值输出超标页报告
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import fitz
import tomllib

MM = 2.834645669291339

# x0 聚类容差（pt）：相差 ≤ 该值的左边界视为同一带（消除居中/缩进噪声）
X0_CLUSTER_PT = 3.0


def _cluster_x0(sorted_x0: list[float], tol: float = X0_CLUSTER_PT) -> list[list[float]]:
    """把有序 x0 列表按容差聚成连续带；返回带列表。"""
    clusters: list[list[float]] = []
    for x0 in sorted_x0:
        if clusters and x0 - clusters[-1][-1] <= tol:
            clusters[-1].append(x0)
        else:
            clusters.append([x0])
    return clusters


def load_tokens(path: Path) -> dict:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return raw.get("gate", {})


def stats_from_page(page: fitz.Page) -> dict:
    """单页统计：填充色块数 / 色种数 / 左边界离散度.

    fitz text('dict') 的 span 结构：{bbox, origin, text, size, font, color, ...}；
    取 bbox[0] 作为行左边界 x0。空 spans 或非文本 block（image）直接跳过，避免 min() 崩溃。
    """
    drawings = page.get_drawings()
    fill_blocks = len([d for d in drawings if d.get("fill")])
    fill_colors = set(
        tuple(d["fill"])
        for d in drawings
        if d.get("fill") and isinstance(d["fill"], (tuple, list))
    )

    # 左边界离散度：提取所有文本行的 x0
    texts = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    x0_counter: Counter = Counter()
    for block in texts.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = [s for s in line.get("spans", []) if s.get("bbox")]
            if not spans:
                continue
            span_x0 = min(s["bbox"][0] for s in spans)
            x0_counter[round(span_x0, 1)] += 1

    # 聚类：相差 ≤ X0_CLUSTER_PT 的 x0 合并为同一左边界带。
    # 居中/缩进文本的 x0 连续分布，聚类后计为 1 带，消除多栏版式的噪声；
    # 真正散乱的页面 x0 跨度大，聚类后仍会多出带数。
    distinct_x0_count = len(_cluster_x0(sorted(x0_counter.keys()), X0_CLUSTER_PT))
    max_line_in_cluster = max(x0_counter.values()) if x0_counter else 0

    return {
        "fill_blocks": fill_blocks,
        "fill_color_variety": len(fill_colors),
        "distinct_x0_count": distinct_x0_count,
        "max_line_in_cluster": max_line_in_cluster,
    }


def audit_pdf(pdf: Path, tokens_path: Path, output: Path | None = None) -> dict:
    """对全 PDF 进行 layout 审计并生成报告 JSON。"""
    doc = fitz.open(pdf)
    threshold = load_tokens(tokens_path)
    threshold_max_fill = int(threshold.get("max_fill_blocks_per_page", 35))
    threshold_max_colors = int(threshold.get("max_color_variety", 5))
    threshold_max_x0 = int(threshold.get("max_left_boundary_count", 10))
    report = {
        "pdf_name": pdf.name,
        "tokens_path": str(tokens_path),
        "threshold_max_fill": threshold_max_fill,
        "threshold_max_colors": threshold_max_colors,
        "threshold_max_x0": threshold_max_x0,
        "pages": [],
        "total_fill_blocks": 0,
        "total_color_variety": 0,
        "failed_pages": [],
        "overall_passed": True,
    }

    for i, page in enumerate(doc):
        s = stats_from_page(page)
        page_info = {
            "page_no": i + 1,
            **s,
            "passed": (
                s["fill_blocks"] <= threshold_max_fill
                and s["fill_color_variety"] <= threshold_max_colors
                and s["distinct_x0_count"] <= threshold_max_x0
            ),
        }
        report["pages"].append(page_info)
        if not page_info["passed"]:
            report["failed_pages"].append(i + 1)
            report["overall_passed"] = False

        report["total_fill_blocks"] += s["fill_blocks"]
        # 全局色种数用最大单页近似
        report["total_color_variety"] = max(report["total_color_variety"], s["fill_color_variety"])

    doc.close()

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report
