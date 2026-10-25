import tempfile
import unittest
from pathlib import Path

import bidviz_v2 as v2

_SOURCE_PDF = next((Path(__file__).parents[1] / "projects" / "anke-35kv" / "inputs").glob("*-source.pdf"), None)


@unittest.skipIf(_SOURCE_PDF is None,
                 "需要 projects/anke-35kv/inputs/*-source.pdf（本机数据，不入库）")
class BidVizV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = _SOURCE_PDF

    def test_full_document_blocks_are_unique_and_nonempty(self):
        blocks = v2.build_blocks(self.pdf)
        self.assertGreater(len(blocks), 250)
        self.assertEqual(len({b.block_id for b in blocks}), len(blocks))
        self.assertTrue(all(b.text and b.sha256 for b in blocks))

    def test_unlimited_pagination_assigns_every_block_once(self):
        blocks = v2.build_blocks(self.pdf)
        pages = v2.paginate(blocks)
        assigned = [bid for p in pages for bid in p.block_ids]
        self.assertEqual(assigned, [b.block_id for b in blocks])
        self.assertGreater(len(pages), 40)

    def test_all_seven_layouts_are_used(self):
        blocks = v2.build_blocks(self.pdf)
        pages = v2.paginate(blocks)
        self.assertEqual({p.layout for p in pages}, set(v2.LAYOUTS))

    def test_prompt_requests_bim_and_model_labels(self):
        block = v2.SourceBlock("B", 1, 1, "body", "电缆沟", "沟槽开挖后砌筑电缆沟", "x")
        page = v2.AtlasPage("P", "电缆沟", "section_bim", ["B"], "电缆沟", [1])
        prompt = v2.visual_prompt(page, {"B": block})
        self.assertIn("BIM", prompt)
        self.assertIn("简短中文构件名", prompt)
        self.assertIn("实际情况引线标注", prompt)

    def test_label_gate_rejects_unknown_model_text(self):
        ok, reason = v2.labels_valid(["基槽开控"], "基槽开挖电缆沟")
        self.assertFalse(ok)
        self.assertIn("基槽开控", reason)
        self.assertTrue(v2.labels_valid(["基槽开挖"], "基槽开挖电缆沟")[0])
        self.assertTrue(v2.labels_valid([], "基槽开挖电缆沟")[0])


if __name__ == "__main__":
    unittest.main()
