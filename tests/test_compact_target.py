from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_config as cfgmod
import bidviz_fullimage_compact as compact

_ANKE = BASE / "projects" / "anke-35kv"
_HAS_BLOCKS = (_ANKE / "work" / "blocks.json").is_file()


class CompactConfigTests(unittest.TestCase):
    def test_project_declares_45_to_50_not_global(self):
        cfg = cfgmod.load(_ANKE)
        self.assertEqual(cfg.compact_min_pages, 45)
        self.assertEqual(cfg.compact_max_pages, 50)
        cfgmod.reset()

    def test_default_is_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfgmod.load(tmp)
            self.assertEqual(cfg.compact_min_pages, 0)
            self.assertEqual(cfg.compact_max_pages, 0)
        cfgmod.reset()


@unittest.skipIf(not _HAS_BLOCKS,
                 "需要 projects/anke-35kv/work/blocks.json（本机数据，不入库）")
class CompactPlannerTests(unittest.TestCase):
    def test_planner_respects_project_range(self):
        root = _ANKE
        secs = compact.load_sections(root)
        groups, _chars, _sections, total = compact.plan_for_target(
            secs, min_pages=45, max_pages=50)
        self.assertGreaterEqual(total, 45)
        self.assertLessEqual(total, 50)
        self.assertEqual(total, len(groups) + 1)

    def test_planner_can_tighten_to_max(self):
        root = BASE / "projects" / "anke-35kv"
        secs = compact.load_sections(root)
        _groups, _chars, _sections, total = compact.plan_for_target(
            secs, min_pages=0, max_pages=40)
        self.assertLessEqual(total, 40)

    def test_planner_unlimited_uses_content_budget(self):
        root = BASE / "projects" / "anke-35kv"
        secs = compact.load_sections(root)
        _groups, _chars, _sections, total = compact.plan_for_target(
            secs, min_pages=0, max_pages=0)
        self.assertGreater(total, 1)


class CompactCleaningTests(unittest.TestCase):
    def test_trace_words_removed(self):
        self.assertEqual(
            compact._clean_text("作业要点（逐字取自原文）"),
            "作业要点")
        self.assertEqual(
            compact._clean_text("控制要求(逐字取自原文)"),
            "控制要求")

    def test_sequence_rows_renumbered_and_garbage_dropped(self):
        headers, rows = compact._clean_table({
            "headers": ["阶段 / 工序", "作业要点（逐字取自原文）"],
            "rows": [["1", "甲"], ["报告...", "乙"], ["3）", "丙"]],
        })
        self.assertEqual(headers, ["阶段 / 工序", "作业要点"])
        self.assertEqual([r[0] for r in rows], ["1", "2. 报告...", "3"])
        _h, rows2 = compact._clean_table({
            "headers": ["名称 / 部位", "指标 · 要求"],
            "rows": [["目", "x"], ["所有进场", "y"]],
        })
        self.assertEqual(rows2, [["所有进场", "y"]])

    def test_taomu_is_denied(self):
        class Cfg:
            provider = "taomu"
            model = "gpt-image-2.5"
            raw = {"image": {"fallback_provider": "grokimg",
                             "fallback_model": "gpt-image-2-2K/4K"}}

        self.assertEqual(compact._providers(Cfg()),
                         [("grokimg", "gpt-image-2-2K/4K")])


if __name__ == "__main__":
    unittest.main()
