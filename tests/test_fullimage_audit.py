from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import bidviz_fullimage_audit as audit


class FullimageAuditTests(unittest.TestCase):
    def image(self, root: Path, size=(2048, 2896)) -> Path:
        p = root / "page.png"
        Image.new("RGB", size, "white").save(p)
        return p

    def test_key_strings_extract_numbers_models_conditions(self):
        got = audit.expected_key_strings("第一章", "第一节", [
            "采用 ZC-YJV22-26/35kV-3×300mm² 电缆，预留150mm。",
            "检测合格后方可开展下一层作业。",
        ])
        self.assertIn("150mm", [x.replace(" ", "") for x in got["numbers"]])
        self.assertTrue(any("ZC-YJV22" in x for x in got["models"]))
        self.assertTrue(any("方可" in x for x in got["conditions"]))

    def test_ocr_missing_blocks_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = audit.audit_candidate(self.image(Path(tmp)), chapter="第一章", section="第一节",
                                        source_lines=["采用150mm厚垫层。"], ocr_texts=None,
                                        reference_count=1)
        self.assertEqual(rep["status"], "block")
        self.assertTrue(any(x["code"] == "ocr-unavailable" for x in rep["checks"]))

    def test_exact_key_strings_pass(self):
        source = ["采用150mm厚垫层，检测合格后方可进入下一步。"]
        ocr = ["第一章 第一节", source[0]]
        with tempfile.TemporaryDirectory() as tmp:
            rep = audit.audit_candidate(self.image(Path(tmp)), chapter="第一章", section="第一节",
                                        source_lines=source, ocr_texts=ocr, reference_count=1)
        self.assertEqual(rep["status"], "pass", rep)

    def test_wrong_number_blocks(self):
        source = ["开挖深度2至3米，预留150mm。"]
        ocr = ["第一章 第一节 开挖深度2至3米，预留130mm。"]
        with tempfile.TemporaryDirectory() as tmp:
            rep = audit.audit_candidate(self.image(Path(tmp)), chapter="第一章", section="第一节",
                                        source_lines=source, ocr_texts=ocr, reference_count=1)
        self.assertEqual(rep["status"], "block")
        number_check = next(x for x in rep["checks"] if x["code"] == "key-numbers")
        self.assertTrue(number_check["evidence"]["missing"])

    def test_ai_page_number_and_signature_block(self):
        source = ["普通正文内容用于抽验，不含任何签名字段。"]
        ocr = ["第一章 第一节 第1页 设计单位 某公司", source[0]]
        with tempfile.TemporaryDirectory() as tmp:
            rep = audit.audit_candidate(self.image(Path(tmp)), chapter="第一章", section="第一节",
                                        source_lines=source, ocr_texts=ocr, reference_count=1)
        self.assertEqual(rep["status"], "block")
        forbidden = next(x for x in rep["checks"] if x["code"] == "forbidden")
        self.assertGreaterEqual(len(forbidden["evidence"]), 2)

    def test_small_or_landscape_image_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            rep = audit.audit_candidate(self.image(Path(tmp), (1024, 768)), chapter="章", section="节",
                                        source_lines=["正文内容足够长以便抽样检查。"],
                                        ocr_texts=["章节正文内容足够长以便抽样检查。"],
                                        reference_count=1)
        self.assertEqual(rep["status"], "block")
        bad = {x["code"] for x in rep["checks"] if x["level"] == "block"}
        self.assertIn("dimensions", bad)
        self.assertIn("portrait", bad)


if __name__ == "__main__":
    unittest.main()
