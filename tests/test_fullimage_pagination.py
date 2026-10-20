from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_fullimage as fi
from bidviz_config import load as load_cfg


class FullimagePaginationTests(unittest.TestCase):
    def test_blocks_are_preserved_once_in_order(self):
        blocks = [
            ("heading", "一、主题甲"), ("heading", "（一）子题"), ("body", "甲" * 900),
            ("heading", "二、主题乙"), ("body", "乙" * 1000),
            ("heading", "三、主题丙"), ("body", "丙" * 800),
        ]
        pages = fi.split_pages(blocks)
        flattened = [b for page in pages for b in page]
        self.assertEqual(flattened, blocks)
        self.assertLessEqual(len(pages), fi.MAX_PAGES)

    def test_primary_topics_are_not_forced_to_first_topic(self):
        blocks = [
            ("heading", "一、主题甲"), ("body", "甲" * 1000),
            ("heading", "二、主题乙"), ("body", "乙" * 1000),
        ]
        pages = fi.split_pages(blocks)
        topics = [fi.page_topic(blocks, page, i) for i, page in enumerate(pages, 1)]
        self.assertEqual(topics, ["一、主题甲", "二、主题乙"])

    def test_continuation_without_suffix(self):
        blocks = [("heading", "一、超长主题"), ("body", "甲" * 1000), ("body", "乙" * 1000)]
        pages = fi.split_pages(blocks)
        self.assertEqual(len(pages), 2)
        self.assertEqual(fi.page_topic(blocks, pages[0], 1), "一、超长主题")
        self.assertEqual(fi.page_topic(blocks, pages[1], 2), "一、超长主题")

    def test_secondary_topic_is_in_page_topic(self):
        blocks = [
            ("heading", "一、主题"), ("heading", "（一）前半"), ("body", "甲" * 1000),
            ("heading", "（二）后半"), ("body", "乙" * 1000),
        ]
        pages = fi.split_pages(blocks)
        self.assertEqual(len(pages), 2)
        self.assertIn("（一）前半", fi.page_topic(blocks, pages[0], 1))
        self.assertIn("（二）后半", fi.page_topic(blocks, pages[1], 2))

    def test_hierarchy_does_not_mark_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            (work / "page-plan.json").write_text(
                '{"sections":{"S001":{"pages":[{"title":{"chapter":"第一章","section":"第一节"}}]}}}',
                encoding="utf-8")
            old = fi._section_blocks
            fi._section_blocks = lambda _root, _sid: [("heading", "一、主题"), ("body", "正文")]
            try:
                hierarchy = fi._hierarchy(root, "S001", [("body", "正文")], 2)
            finally:
                fi._section_blocks = old
        self.assertEqual(hierarchy[1], "- 第一节")
        self.assertEqual(hierarchy[2], "- 一、主题")

    @unittest.skipUnless((BASE / "projects/anke-35kv/work/page-plan.json").exists(), "anke fixture missing")
    def test_cover_uses_dedicated_prompt_without_chapter_titles(self):
        root = BASE / "projects/anke-35kv"
        load_cfg(root)
        self.assertTrue(fi.is_cover(root, "S001"))
        self.assertFalse(fi.is_cover(root, "S002"))
        blocks = fi._section_blocks(root, "S001")
        built = fi.build_fullpage_prompt(root, "S001", 1, 1, fi.split_pages(blocks)[0])
        self.assertTrue(built["cover"])
        self.assertIn("封面页", built["prompt"])
        self.assertNotIn("第三章 实施方案", built["prompt"])
        self.assertNotIn("第一节 项目总体实施方案", built["prompt"])

    @unittest.skipUnless((BASE / "projects/anke-35kv/work/blocks.json").exists(), "anke fixture missing")
    def test_real_multipage_sections_are_bounded_and_distinct(self):
        root = BASE / "projects/anke-35kv"
        load_cfg(root)
        for sid in ("S002", "S006", "S017"):
            blocks = fi._section_blocks(root, sid)
            pages = fi.split_pages(blocks)
            self.assertLessEqual(len(pages), 3, sid)
            self.assertEqual([b for page in pages for b in page], blocks, sid)
            topics = [fi.page_topic(blocks, page, i) for i, page in enumerate(pages, 1)]
            self.assertTrue(all(topics), (sid, topics))
            self.assertGreater(len(set(topics)), 1, (sid, topics))


if __name__ == "__main__":
    unittest.main()
