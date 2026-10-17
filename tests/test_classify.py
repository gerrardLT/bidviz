#!/usr/bin/env python3
"""bidviz_classify 回归单测（V1.3 阶段 3.1b / 3.1c）。

golden 来源：docs/layout-md/layout_classification.md（57 节人审拆分）。
覆盖：
- md 解析：57 节 / 章级统计 16-3-6-9-6-6-6-5、表 47、图 60；
- golden 集：第一章第十一节（S012 候选，2 块）、第四章第三节五块拆分、
  第三章第四节三块拆分（规划 3.1c 点名样例）；
- md → json → md round-trip 一致；
- 3.1b 门禁：五字段缺失/主类非法/起始页非单调可检出；
- P1 LLM 通道：mock 掉网络后按章产出矩阵。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_classify as bc  # noqa: E402

GOLDEN = BASE / "docs" / "layout-md" / "layout_classification.md"


def _fake_blocks(root: Path) -> None:
    """合成最小 blocks.json / page-media-map.json（2 章 × 2 节 + 总述）。"""
    blocks = []
    for i, (sp, kind, text) in enumerate([
            ("封面及总述", "heading", "封面及总述"),
            ("封面及总述", "body", "项目概况正文" * 30),
            ("第一章 甲 / 第一节 a", "heading", "第一节 a"),
            ("第一章 甲 / 第一节 a", "body", "a 正文" * 60),
            ("第一章 甲 / 第二节 b", "heading", "第二节 b"),
            ("第一章 甲 / 第二节 b", "body", "b 正文" * 60),
            ("第二章 乙 / 第一节 c", "heading", "第一节 c"),
            ("第二章 乙 / 第一节 c", "body", "c 正文" * 60),
            ("第二章 乙 / 第二节 d", "heading", "第二节 d"),
            ("第二章 乙 / 第二节 d", "body", "d 正文" * 60)], start=1):
        blocks.append({"block_id": f"B-{i:04d}", "source_page": i, "order": i,
                       "kind": kind, "section_path": sp, "text": text,
                       "sha256": "0" * 64})
    (root / "work").mkdir(parents=True, exist_ok=True)
    (root / "work" / "blocks.json").write_text(
        json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    (root / "work" / "page-media-map.json").write_text("{}", encoding="utf-8")


def _fake_cls(monotonic: bool = True, drop_field: str | None = None,
              bad_main: bool = False) -> dict:
    def sec(sid, ch, title, page, main, aux=()):
        d = {"section_id": sid, "chapter": ch, "title": title, "source_page": page,
             "blocks_source_page": page, "main_class": main, "aux_classes": list(aux),
             "suggested_figures": "图", "suggested_tables": "表", "combo": "组",
             "production": "矢量", "content_blocks": [], "note": ""}
        if drop_field:
            d[drop_field] = ""
        return d
    pages = [1, 2, 3, 4] if monotonic else [3, 2, 4, 1]
    return {
        "schema": "bidviz-layout-classification/1.0", "version": "t", "generated_at": "t",
        "p0_chapter_stats": [], "p1_mode": "md",
        "sections": [
            sec("S002", "第一章 甲", "第一节 a", pages[0], "X" if bad_main else "B"),
            sec("S003", "第一章 甲", "第二节 b", pages[1], "C"),
            sec("S004", "第二章 乙", "第一节 c", pages[2], "D"),
            sec("S005", "第二章 乙", "第二节 d", pages[3], "H"),
        ],
        "p2_global": {}, "p3_rules": {},
    }


class GoldenParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parsed = bc.parse_classification_md(GOLDEN)

    def test_57_sections_and_chapter_stats(self):
        self.assertEqual(len(self.parsed["sections"]), 57)
        stats = self.parsed["p0_chapter_stats"]
        self.assertEqual([s["sections"] for s in stats],
                         [16, 3, 6, 9, 6, 6, 6, 5])
        self.assertEqual(sum(s["tables"] for s in stats), 47)
        self.assertEqual(sum(s["images"] for s in stats), 60)

    def test_class_field_parse(self):
        self.assertEqual(bc.parse_class_field("D＋G、B"), ("D", ["G", "B"]))
        self.assertEqual(bc.parse_class_field("F"), ("F", []))
        for bad in ("X＋G", "D＋X", "", "DD"):
            with self.assertRaises(ValueError, msg=bad):
                bc.parse_class_field(bad)

    def test_golden_ch1_s11_cable_head(self):
        """第一章第十一节 电缆头制作安装：B＋C、D、G，2 内容块（终端/中间接头）。"""
        sec = next(s for s in self.parsed["sections"]
                   if s["chapter"].startswith("第一章") and s["title"].startswith("第十一节"))
        self.assertEqual((sec["main_class"], sec["aux_classes"]), ("B", ["C", "D", "G"]))
        names = [b["name"] for b in sec["content_blocks"]]
        self.assertEqual(names, ["一、电缆终端", "二、中间接头"])

    def test_golden_ch4_s3_five_blocks(self):
        """第四章第三节 五块拆分（3.1c 点名 golden）：逐块类 F＋G/B＋C/G＋D/D＋H/B＋G。"""
        sec = next(s for s in self.parsed["sections"] if s.get("mixed_split_example"))
        self.assertTrue(sec["title"].startswith("第三节"), sec["title"])
        self.assertIn("安全文明", sec["chapter"])
        blocks = sec["content_blocks"]
        self.assertEqual(len(blocks), 5)
        self.assertEqual([b["class"] for b in blocks],
                         ["F＋G", "B＋C", "G＋D", "D＋H", "B＋G"])
        self.assertTrue(all(b["slots"] for b in blocks))

    def test_golden_ch3_s4_three_blocks(self):
        """第三章第四节 质量保证体系建立：F＋A、G，3 内容块（3.1c 点名 golden）。"""
        sec = next(s for s in self.parsed["sections"] if "质量保证体系建立" in s["title"])
        self.assertEqual((sec["main_class"], sec["aux_classes"]), ("F", ["A", "G"]))
        self.assertEqual(len(sec["content_blocks"]), 3)

    def test_named_redraw_pages(self):
        self.assertEqual(sorted(self.parsed["named_redraw_pages"]), [84, 108, 155, 256])

    def test_cross_chapter_objects_clean(self):
        p2 = bc.global_pass({"sections": self.parsed["sections"]})
        objs = [c["object"] for c in p2["cross_chapter_objects"]]
        self.assertTrue(objs, "应检出至少一组跨章同对象")
        for o in objs:                      # 无序号词/标点碎片
            self.assertRegex(o, r"^[\u4e00-\u9fff]{3,}$", o)
        self.assertIn("电缆头制作", objs)    # golden §五 的接头三处复用

    def test_render_round_trip(self):
        """json → md → json：57 节主辅类与内容块（含五块类）逐项一致。"""
        cls = {"project": {"name": "安科生物项目"},
               "p0_chapter_stats": self.parsed["p0_chapter_stats"],
               "sections": self.parsed["sections"],
               "p2_global": bc.global_pass({"sections": self.parsed["sections"]})}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rt.md"
            p.write_text(bc.render_md(cls), encoding="utf-8")
            back = bc.parse_classification_md(p)
        key = lambda ss: [(s["chapter"], s["title"], s["main_class"], tuple(s["aux_classes"]),
                           [(b["name"], b.get("class", ""), b.get("slots", ""))
                            for b in s["content_blocks"]]) for s in ss]
        self.assertEqual(key(back["sections"]), key(self.parsed["sections"]))


class ValidateGateTests(unittest.TestCase):
    """3.1b：schema 五字段 / 主辅类合法 / 覆盖与顺序 / 起始页单调。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _fake_blocks(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_gate_pass(self):
        ok, problems, _ = bc.validate(self.root, _fake_cls())
        self.assertTrue(ok, problems)

    def test_gate_missing_field(self):
        ok, problems, _ = bc.validate(self.root, _fake_cls(drop_field="combo"))
        self.assertFalse(ok)
        self.assertTrue(any("五字段缺失：组合" in p for p in problems), problems)

    def test_gate_bad_main_class(self):
        ok, problems, _ = bc.validate(self.root, _fake_cls(bad_main=True))
        self.assertFalse(ok)
        self.assertTrue(any("主类非法" in p for p in problems), problems)

    def test_gate_non_monotonic_pages(self):
        ok, problems, _ = bc.validate(self.root, _fake_cls(monotonic=False))
        self.assertFalse(ok)
        self.assertTrue(any("非单调" in p for p in problems), problems)

    def test_gate_missing_section(self):
        cls = _fake_cls()
        cls["sections"] = cls["sections"][:-1]
        ok, problems, _ = bc.validate(self.root, cls)
        self.assertFalse(ok)
        self.assertTrue(any("无分类条目" in p for p in problems), problems)

    def test_sample_checklist_two_per_chapter(self):
        cls = _fake_cls()
        checks = bc.sample_checklist(cls)
        per = {}
        for c in checks:
            per[c["chapter"]] = per.get(c["chapter"], 0) + 1
        self.assertEqual(sorted(per.values()), [2, 2])


class LlmChannelTests(unittest.TestCase):
    """P1 LLM 通道：mock 网络，验证模板替换与 JSON 解析。"""

    def test_llm_chapter_matrix_mocked(self):
        reply = json.dumps({"sections": [{
            "title": "第一节 a", "main_class": "B", "aux_classes": ["C"],
            "suggested_figures": "工序图", "suggested_tables": "要求表",
            "combo": "上图下表", "production": "矢量",
            "content_blocks": [{"name": "一、x", "class": "", "slots": "", "note": ""}],
            "note": ""}]}, ensure_ascii=False)
        captured = {}

        def fake_chat(prompt, provider, model, timeout=300):
            captured["prompt"] = prompt
            captured["model"] = model
            return f"```json\n{reply}\n```"

        with mock.patch.object(bc, "_chat_completion", side_effect=fake_chat):
            rows = bc.llm_chapter_matrix(
                "第一章 甲", {"sections": 1, "tables": 2, "images": 3},
                [{"title": "第一节 a", "sub_topics": ["一、x"], "excerpt": "正文"}],
                provider="p", model="m1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["main_class"], "B")
        self.assertIn("第一章 甲", captured["prompt"])
        self.assertNotIn("{{", captured["prompt"])       # 占位符全部替换
        self.assertIn("8 种内容形态", captured["prompt"])   # 模板核心段在位
        self.assertEqual(captured["model"], "m1")

    def test_llm_bad_json_raises(self):
        with mock.patch.object(bc, "_chat_completion", return_value="不是 JSON"):
            with self.assertRaises(ValueError):
                bc.llm_chapter_matrix("x", {"sections": 0, "tables": 0, "images": 0},
                                      [], provider="p", model="m")


if __name__ == "__main__":
    unittest.main()
