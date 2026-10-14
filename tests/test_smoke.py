"""最小回归检查：配置、编辑投影和完整正文覆盖。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build_e1
from bidviz_config import load


def main() -> None:
    project = ROOT / "projects" / "anke-35kv"
    cfg = load(project)
    assert cfg.model == "gpt-image-2-2K/4K"
    assert cfg.image_dir.name == "visuals-2k-yoshub"

    pdf = ROOT / "_smoke.pdf"
    pages, regions = build_e1.build(project, pdf, limit=1)
    audit = build_e1.audit_coverage(pdf, project, regions, limit=1)
    assert pages >= 3
    assert audit["coverage_pct"] == 100.0, json.dumps(audit, ensure_ascii=False)
    pdf.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        edited = Path(tmp)
        (edited / "work").mkdir()
        shutil.copy(project / "bidviz.toml", edited / "bidviz.toml")
        for name in ("blocks.json", "page-media-map.json"):
            shutil.copy(project / "work" / name, edited / "work" / name)
        (edited / "work" / "edits.json").write_text(json.dumps({
            "S001": {"title": "编辑后标题", "body": "编辑后正文标记。"}
        }, ensure_ascii=False), encoding="utf-8")
        changed = edited / "changed.pdf"
        _, changed_regions = build_e1.build(edited, changed, limit=1)
        text = "".join(page.get_text() for page in fitz.open(changed))
        assert "编辑后标题" in text and "编辑后正文标记" in text
        assert build_e1.audit_coverage(changed, edited, changed_regions, limit=1)["coverage_pct"] == 100.0

    print("smoke ok: config + vector body + edits projection + 100% coverage")


if __name__ == "__main__":
    main()
