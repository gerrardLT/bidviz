from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_ioutil as ioutil
import bidviz_plan as plan
import generate_chapter as gc
import bidviz_fullimage as fullimage


class IoutilTests(unittest.TestCase):
    def test_atomic_write_json_roundtrip_and_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "work" / "data.json"
            ioutil.atomic_write_json(p, {"a": 1})
            ioutil.atomic_write_json(p, {"a": 2, "b": "中文"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["b"], "中文")
            # 不残留 .tmp 临时文件
            self.assertEqual(list(Path(tmp).rglob("*.tmp")), [])

    def test_atomic_write_text_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x" / "y" / "f.txt"
            ioutil.atomic_write_text(p, "hello")
            self.assertEqual(p.read_text(encoding="utf-8"), "hello")

    def test_atomic_replace_from_keeps_target_on_missing_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / "final.bin"
            ioutil.atomic_write_text(final, "old")
            with self.assertRaises(OSError):
                ioutil.atomic_replace_from(Path(tmp) / "no-such.tmp", final)
            # 替换失败不破坏既有目标
            self.assertEqual(final.read_text(encoding="utf-8"), "old")


class LedgerAtomicTests(unittest.TestCase):
    """工作区 JSON 一律 tmp+fsync+os.replace：写一半崩溃不再留下半写文件。"""

    def _assert_no_tmp(self, root: Path):
        self.assertEqual([p for p in root.rglob("*.tmp") if p.is_file()], [])

    def test_figure_plan_save_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan.save_plan(root, {"sections": {"S001": {}}})
            plan.save_plan(root, {"sections": {"S001": {}, "S002": {}}})
            data = json.loads((root / "work" / "figure-plan.json").read_text(encoding="utf-8"))
            self.assertIn("S002", data["sections"])
            self._assert_no_tmp(root)

    def test_generation_ledger_save_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gc.save_ledger(root, {"S001#1": {"ok": True}})
            self.assertTrue(gc.load_ledger(root)["S001#1"]["ok"])
            self._assert_no_tmp(root)

    def test_fullimage_ledger_save_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fullimage.save_ledger(root, {"pages": 3})
            self.assertEqual(fullimage.load_ledger(root)["pages"], 3)
            self._assert_no_tmp(root)


if __name__ == "__main__":
    unittest.main()
