"""全图候选页自动质量门禁。

纯规则层不发网络请求；调用方传入 OCR/VLM 回读文本。OCR 不可用时 block，
可在 Web 中人工豁免并留痕，但绝不自动发布。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

A3_RATIO = 297 / 420
NUMBER_UNIT_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:至|到|~|～|-)?\s*\d*(?:\.\d+)?\s*"
    r"(?:kV|mm²|mm|cm|km|m³|m²|m|kN|KN|kW|h|小时|日历天|天|%|‰|t|吨|台|套|组|处|人)?",
    re.I,
)
MODEL_RE = re.compile(r"[A-Z]{2,}[A-Z0-9./×x\-]{3,}", re.I)
CONDITION_RE = re.compile(r"[^。；\n]*(?:严禁|不得|必须|检测合格后|验收合格后|方可|若|当|一旦)[^。；\n]*")
FORBIDDEN_RE = re.compile(r"(?:第\s*\d+\s*页|\d+\s*/\s*\d+|—\s*\d+\s*—|设计单位|审核人|批准人|图号|二维码)")


def norm(text: str) -> str:
    return re.sub(r"[\s，,。；;：:（）()\[\]【】]", "", str(text or "")).lower()


def expected_key_strings(chapter: str, section: str, source_lines: Iterable[str]) -> dict[str, list[str]]:
    text = "\n".join(str(x) for x in source_lines)
    nums = []
    for m in NUMBER_UNIT_RE.finditer(text):
        s = m.group(0).strip()
        if s and any(ch.isdigit() for ch in s) and s not in nums:
            nums.append(s)
    models = []
    for m in MODEL_RE.finditer(text):
        s = m.group(0).strip()
        if any(ch.isdigit() for ch in s) and s not in models:
            models.append(s)
    conditions = []
    for m in CONDITION_RE.finditer(text):
        s = m.group(0).strip()
        if len(s) >= 6 and s not in conditions:
            conditions.append(s)
    return {
        "titles": [x for x in (chapter, section) if x],
        "numbers": nums,
        "models": models,
        "conditions": conditions,
    }


def _plain_samples(source_lines: list[str]) -> list[str]:
    bodies = [re.sub(r"\s+", "", x) for x in source_lines if len(re.sub(r"\s+", "", x)) >= 18]
    if not bodies:
        return []
    picks = {0, len(bodies) // 2, len(bodies) - 1}
    return [bodies[i][:24] for i in sorted(picks)]


def audit_candidate(image: Path, *, chapter: str, section: str,
                    source_lines: list[str], ocr_texts: list[str] | None,
                    reference_count: int, min_width: int = 2048,
                    is_cover: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        with Image.open(image) as im:
            w, h = im.size
            im.verify()
    except Exception as exc:
        return {"status": "block", "checks": [{"code": "invalid-image", "level": "block", "message": str(exc)}]}

    def add(code: str, level: str, message: str, evidence: Any = None) -> None:
        checks.append({"code": code, "level": level, "message": message, "evidence": evidence})

    add("dimensions", "pass" if w >= min_width else "block",
        f"实际尺寸 {w}×{h}；宽度下限 {min_width}", {"width": w, "height": h})
    ratio = w / h
    add("portrait", "pass" if h > w else "block", "必须为竖版")
    add("aspect-ratio", "pass" if abs(ratio - A3_RATIO) <= 0.035 else "block",
        f"宽高比 {ratio:.4f}；目标 {A3_RATIO:.4f}")
    # 封面有意不传参考图（避免照抄样板章节），豁免 reference 检查
    add("reference", "pass" if (reference_count >= 1 or is_cover) else "block",
        f"参考图数量 {reference_count}" + ("（封面豁免）" if is_cover else ""))

    if ocr_texts is None:
        add("ocr-unavailable", "block", "OCR/VLM 回读不可用，关键串未核验")
    else:
        visible = norm("\n".join(ocr_texts))
        forbidden = FORBIDDEN_RE.findall("\n".join(ocr_texts))
        add("forbidden", "block" if forbidden else "pass",
            "检测 AI 页码/编造署名", forbidden)
        expected = expected_key_strings(chapter, section, source_lines)
        for group in ("titles", "numbers", "models", "conditions"):
            missing = [x for x in expected[group] if norm(x) not in visible]
            add(f"key-{group}", "block" if missing else "pass",
                f"{group} 关键串全检", {"expected": expected[group], "missing": missing})
        samples = _plain_samples(source_lines)
        missing_plain = [x for x in samples if norm(x) not in visible]
        add("plain-text", "block" if len(missing_plain) >= 2 else ("warn" if missing_plain else "pass"),
            "普通正文首/中/尾抽样", {"expected": samples, "missing": missing_plain})

    levels = {c["level"] for c in checks}
    status = "block" if "block" in levels else ("warn" if "warn" in levels else "pass")
    return {"status": status, "checks": checks}
