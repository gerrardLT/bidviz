"""端到端冒烟：配置 → 图位规划 → 编译 → 覆盖率反抽 → 编辑投影 → 生图 dry-run。

不花 API 钱：规划与提示词在本地算，生图只走 --dry-run。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build_e1
import bidviz_e1 as e1
import bidviz_plan as plan
import bidviz_v2 as v2
import generate_chapter as gc
from bidviz_config import load

LIMIT = 3      # 三节 = 3 图版页 + 至少 3 正文页；且能同时命中有图与缺图占位两条路径


def check_plan(project: Path) -> dict:
    """规划先于生图：每节 1~N 张主图，没有主/辅之分，提示词与生图参数齐全。"""
    cfg = load(project)
    doc = plan.ensure_plan(project)
    summary = plan.plan_summary(doc)
    assert summary["sections"] > 0
    assert summary["figures"] >= summary["sections"], "每节至少一张主图"
    allowed = {str(i) for i in range(1, cfg.max_figures + 1)}
    assert set(summary["per_section"]) <= allowed, summary["per_section"]
    for sid, entry in doc["sections"].items():
        figs = entry["figures"]
        assert 1 <= len(figs) <= cfg.max_figures, (sid, len(figs))
        assert [f["no"] for f in figs] == list(range(1, len(figs) + 1))
        for f in figs:
            assert f["kind"] in plan.KINDS, (sid, f["kind"])
            assert f["name"] and f["why"] and f["ratio"] > 0
            g = f["gen"]
            assert g["provider"] and g["model"] and g["size"]
            p = f["prompt"]
            for frag in ("逐字取自", "禁止绘制", "宽高比"):
                assert frag in p, (sid, f["no"], frag)
    return doc


def check_boundary(project: Path) -> None:
    """节边界修复：节标题必为本节首块；任一节不得混入别节的节标题。"""
    blocks = [v2.SourceBlock(**x) for x in
              json.loads((project / "work" / "blocks.json").read_text(encoding="utf-8"))]
    secs = e1.group_sections(blocks)
    sec_re = re.compile(r"^第[一二三四五六七八九十百\d]+节")
    chap_re = re.compile(r"^第[一二三四五六七八九十百\d]+章")
    assert secs, "至少一节"
    for s in secs:
        if not s.blocks:
            continue
        name = s.chapter.split(" / ")[-1]
        for h in (b.text for b in s.blocks if b.kind == "heading" and sec_re.match(b.text)):
            assert h == name, f"{s.section_id} 混入别节标题 {h!r}（本节 {name!r}）"
        first = s.blocks[0]
        if first.kind == "heading":
            assert sec_re.match(first.text) or chap_re.match(first.text), (s.section_id, first.text)


def main() -> None:
    project = ROOT / "projects" / "anke-35kv"
    cfg = load(project)
    assert cfg.model == "gpt-image-2-2K/4K"
    assert cfg.image_dir.name == "visuals-2k-yoshub"

    check_plan(project)
    check_boundary(project)

    pdf = ROOT / "_smoke.pdf"
    # 双遍编译：首遍测页数，次遍写准确总页码
    pages, regions = build_e1.build_final(project, pdf, limit=LIMIT)
    audit = build_e1.audit_coverage(pdf, project, regions, limit=LIMIT)
    doc = fitz.open(pdf)
    text = "".join(page.get_text() for page in doc)
    doc.close()
    assert pages >= LIMIT * 2, pages                      # 每节 1 图版页 + ≥1 正文页
    assert audit["coverage_pct"] == 100.0, json.dumps(audit, ensure_ascii=False)
    assert "图版页" in text and "本节图目录与原文溯源" in text
    assert "待生成" in text                               # 缺图只画占位框，不回退程序矢量绘图
    # 内容报表落地：提炼表至少出现其一；页眉面包屑保留；不再有任何维度着色/互指页码
    assert any(k in text for k in ("施工工序", "参数与指标", "风险与控制要求", "材料与设备", "内容摘录"))
    assert "›" in text, "页眉面包屑"
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
        cdoc = fitz.open(changed)
        text = "".join(page.get_text() for page in cdoc)
        cdoc.close()
        assert "编辑后标题" in text and "编辑后正文标记" in text
        assert build_e1.audit_coverage(changed, edited, changed_regions, limit=1)["coverage_pct"] == 100.0
        # 正文改了要重算规划；改过的提示词不能被覆盖
        entry = plan.ensure_plan(edited, only=["S001"])["sections"]["S001"]
        assert "编辑后正文标记" in entry["figures"][0]["prompt"]

    cfg = load(project)      # 临时项目会把全局配置指过去，这里切回真项目
    stat = gc.generate_figures(project, ["S002"], dry_run=True)
    assert stat["planned"] >= 1 and stat["generated"] == 0 and stat["failed"] == 0, stat

    # 生图参数分流：同一节的两张图可以走不同的生图 API，图序覆盖比图型覆盖更具体
    cfg.raw["image"]["figure_overrides"] = {
        "2": {"provider": "taomu", "size": "1536x1024"},
        "assembly": {"provider": "gemini", "size": "2048x1448"},
    }
    g = plan.gen_config(2, "assembly")
    assert g["provider"] == "taomu" and g["size"] == "1536x1024", g
    assert plan.gen_config(1, "hero_scene")["provider"] == cfg.provider

    print("smoke ok: plan(1~N 主图) + 占位图版页 + 内容报表 + edits projection "
          "+ 节边界 + 提炼内容覆盖 + dry-run")


if __name__ == "__main__":
    main()
