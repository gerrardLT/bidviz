"""端到端冒烟（pytest 可收集版）：配置 → 图位规划 → 编译 → 覆盖率反抽 → 编辑投影 → 生图 dry-run。

不花 API 钱：规划与提示词在本地算，生图只走 dry-run。
依赖真项目数据（projects/anke-35kv/work/blocks.json 等，本机不入库）的测试
在缺数据环境（CI / fresh clone）自动 skip；纯配置断言无需数据。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
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

PROJECT = ROOT / "projects" / "anke-35kv"
HAVE_DATA = (PROJECT / "work" / "blocks.json").is_file()
LIMIT = 3      # 三节 = 3 图版页 + 至少 3 正文页；且能同时命中有图与缺图占位两条路径


class PlanStructureTests(unittest.TestCase):
    """图位规划结构：每节 1~N 张主图，提示词与生图参数齐全。"""

    @unittest.skipUnless(HAVE_DATA, "需要真项目 blocks.json")
    def test_plan_structure(self):
        # 在临时副本上跑规划，不改动真项目已提交的 work/figure-plan.json
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "work").mkdir()
            shutil.copy(PROJECT / "bidviz.toml", project / "bidviz.toml")
            shutil.copy(PROJECT / "work" / "blocks.json", project / "work" / "blocks.json")
            for name in ("page-media-map.json", "source-tables.json"):
                src = PROJECT / "work" / name
                if src.exists():
                    shutil.copy(src, project / "work" / name)
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
            load(PROJECT)   # 临时项目会把全局配置指过去，这里切回真项目

    @unittest.skipUnless(HAVE_DATA, "需要真项目 blocks.json")
    def test_section_boundary(self):
        """节边界：节标题必为本节首块；任一节不得混入别节的节标题。"""
        blocks = [v2.SourceBlock(**x) for x in
                  json.loads((PROJECT / "work" / "blocks.json").read_text(encoding="utf-8"))]
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


class LegacyBuildTests(unittest.TestCase):
    """双遍编译 → 覆盖率反抽 → 文本抽查（占位框 / 报表 / 面包屑）。"""

    @unittest.skipUnless(HAVE_DATA, "需要真项目数据与字体")
    def test_build_final_coverage_and_text(self):
        # 临时副本：build_final 内部会 ensure_plan，不能重写真项目的 figure-plan.json
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "work").mkdir()
            shutil.copy(PROJECT / "bidviz.toml", project / "bidviz.toml")
            for name in ("blocks.json", "page-media-map.json", "figure-plan.json",
                         "source-tables.json"):
                src = PROJECT / "work" / name
                if src.exists():
                    shutil.copy(src, project / "work" / name)
            load(project)
            pdf = project / "_smoke.pdf"
            try:
                pages, regions = build_e1.build_final(project, pdf, limit=LIMIT)
                audit = build_e1.audit_coverage(pdf, project, regions, limit=LIMIT)
                doc = fitz.open(pdf)
                text = "".join(page.get_text() for page in doc)
                doc.close()
                assert pages >= LIMIT * 2, pages                      # 每节 1 图版页 + ≥1 正文页
                assert audit["coverage_pct"] == 100.0, json.dumps(audit, ensure_ascii=False)
                assert "图版页" in text and "本节图目录与原文溯源" in text
                assert "待生成" in text                               # 缺图只画占位框，不回退程序矢量绘图
                # 内容报表落地：提炼表至少出现其一；页眉面包屑保留
                assert any(k in text for k in ("施工工序", "参数与指标", "风险与控制要求",
                                               "材料与设备", "内容摘录"))
                assert "›" in text, "页眉面包屑"
            finally:
                load(PROJECT)      # 临时项目会把全局配置指过去，这里切回真项目

    @unittest.skipUnless(HAVE_DATA, "需要真项目数据")
    def test_edits_projected_into_pdf_and_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            edited = Path(tmp)
            (edited / "work").mkdir()
            shutil.copy(PROJECT / "bidviz.toml", edited / "bidviz.toml")
            for name in ("blocks.json", "page-media-map.json"):
                src = PROJECT / "work" / name
                if src.exists():
                    shutil.copy(src, edited / "work" / name)
            (edited / "work" / "edits.json").write_text(json.dumps({
                "S001": {"title": "编辑后标题", "body": "编辑后正文标记。"}
            }, ensure_ascii=False), encoding="utf-8")
            changed = edited / "changed.pdf"
            try:
                _, changed_regions = build_e1.build(edited, changed, limit=1)
                cdoc = fitz.open(changed)
                text = "".join(page.get_text() for page in cdoc)
                cdoc.close()
                assert "编辑后标题" in text and "编辑后正文标记" in text
                assert build_e1.audit_coverage(
                    changed, edited, changed_regions, limit=1)["coverage_pct"] == 100.0
                # 正文改了要重算规划；改过的提示词不能被覆盖
                entry = plan.ensure_plan(edited, only=["S001"])["sections"]["S001"]
                assert "编辑后正文标记" in entry["figures"][0]["prompt"]
            finally:
                load(PROJECT)      # 临时项目会把全局配置指过去，这里切回真项目

    @unittest.skipUnless(HAVE_DATA, "需要真项目数据")
    def test_generate_dry_run_spends_nothing(self):
        # 临时副本：generate_figures 内部会 ensure_plan，不能让它重写真项目的 figure-plan.json
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "work").mkdir()
            shutil.copy(PROJECT / "bidviz.toml", project / "bidviz.toml")
            shutil.copy(PROJECT / "work" / "blocks.json", project / "work" / "blocks.json")
            for name in ("page-media-map.json", "source-tables.json"):
                src = PROJECT / "work" / name
                if src.exists():
                    shutil.copy(src, project / "work" / name)
            load(project)
            stat = gc.generate_figures(project, ["S002"], dry_run=True)
            assert stat["planned"] >= 1 and stat["generated"] == 0 and stat["failed"] == 0, stat
            load(PROJECT)

    def test_figure_overrides_route_by_no_and_kind(self):
        """生图参数分流：同一节的两张图可以走不同的生图 API。"""
        cfg = load(PROJECT)
        try:
            cfg.raw["image"]["figure_overrides"] = {
                "2": {"provider": "taomu", "size": "1536x1024"},
                "assembly": {"provider": "gemini", "size": "2048x1448"},
            }
            g = plan.gen_config(2, "assembly")
            assert g["provider"] == "taomu" and g["size"] == "1536x1024", g
            assert plan.gen_config(1, "hero_scene")["provider"] == cfg.provider
        finally:
            load(PROJECT)


if __name__ == "__main__":
    unittest.main()
