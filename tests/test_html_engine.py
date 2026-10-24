from __future__ import annotations

import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_config as cfgmod
import bidviz_html as h


class ConfigEngineTests(unittest.TestCase):
    def test_hybrid_alias_normalizes_to_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bidviz.toml").write_text('[layout]\nengine = "hybrid"\n', encoding="utf-8")
            cfg = cfgmod.load(root)
            self.assertEqual(cfg.layout_engine, "html")
            self.assertIn("html", cfg.supported_engines)
            self.assertEqual(cfg.html_table_cell_max, 400)
        cfgmod.reset()

    def test_unknown_engine_falls_back_to_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bidviz.toml").write_text('[layout]\nengine = "nope"\n', encoding="utf-8")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                cfg = cfgmod.load(root)
                self.assertEqual(cfg.layout_engine, "legacy")
            self.assertTrue(any("未知 layout.engine" in str(w.message) for w in caught))
        cfgmod.reset()


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.tokens = h.load_tokens()

    def test_prose_blocks_flow_to_next_page(self):
        blocks = [h.Block(f"b{i}", "prose", {"text": "x", "level": 1}, height=300.0)
                  for i in range(4)]
        pages = h.paginate_section("S001", blocks, [], "章", "节", "", self.tokens)
        self.assertEqual(len(pages), 2)
        self.assertEqual(len(pages[0].blocks), 3)
        self.assertEqual(len(pages[1].blocks), 1)
        self.assertEqual(pages[1].section_page, 2)

    def test_table_rows_split_and_header_repeats(self):
        block = h.Block("S001:t1", "table", {
            "table_id": "S001:t1", "caption": "测试表",
            "headers": ["A", "B"],
            "rows": [[str(i), f"row{i}"] for i in range(10)],
            "col_ratios": [0.4, 0.6],
        })
        block.header_height = 40.0
        block.row_heights = [100.0] * 10
        pages = h.paginate_section("S001", [block], [], "章", "节", "", self.tokens)
        self.assertEqual(len(pages), 2)
        first = pages[0].blocks[0]
        second = pages[1].blocks[0]
        self.assertIsInstance(first, h.TableChunk)
        self.assertTrue(first.first)
        self.assertFalse(second.first)
        self.assertEqual(first.headers, ["A", "B"])
        self.assertEqual(second.headers, ["A", "B"])
        self.assertEqual(len(first.rows) + len(second.rows), 10)
        self.assertEqual(sum(len(p.overflow) for p in pages), 0)

    def test_html_contains_a3_page_and_number(self):
        pages = [h.HtmlPage("S001", 1, "第一章", "第一节", "", [])]
        doc = h.render_album_html(pages, self.tokens, project_name="测试项目")
        self.assertIn("size: 297mm 420mm", doc)
        self.assertIn("— 1 —", doc)
        self.assertIn("测试项目", doc)

    def test_metrics_parser_returns_points(self):
        self.assertEqual(h.parse_metrics_text('{"a": 96}'), {"a": 72.0})


class RendererTests(unittest.TestCase):
    @unittest.skipUnless(h.playwright_available(), "playwright 未安装")
    def test_playwright_pdf_is_a3_with_vector_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            html_path = root / "page.html"
            html_path.write_text(
                "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
                "@page{size:297mm 420mm;margin:0}.page{width:297mm;height:420mm;"
                "font-family:'Microsoft YaHei'}</style></head><body>"
                "<div class='page'>中文测试 ABC 123</div></body></html>",
                encoding="utf-8")
            pdf = root / "page.pdf"
            h._playwright_pdf(html_path, pdf)
            doc = h.fitz.open(pdf)
            try:
                self.assertEqual(doc.page_count, 1)
                self.assertAlmostEqual(doc[0].rect.width, 841.89, delta=2)
                self.assertAlmostEqual(doc[0].rect.height, 1190.55, delta=2)
                self.assertIn("中文测试", doc[0].get_text())
            finally:
                doc.close()


class CliDispatchTests(unittest.TestCase):
    def test_build_html_uses_html_output_name(self):
        import bidviz_cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = cfgmod.load(root)
            args = type("A", (), {"project": str(root), "out": None, "engine": "html",
                                  "png": False})()
            fake = {"pdf": root / "out.pdf", "pages": 3,
                    "audit": {"ok": True, "coverage": 1.0}, "png": None}
            with patch.object(bidviz_cli, "load_cfg", return_value=cfg), \
                 patch("bidviz_html.build", return_value=fake) as build:
                bidviz_cli.cmd_build(args)
            self.assertTrue(build.called)
            self.assertEqual(build.call_args.args[2].name, h.OUT_SUFFIX + ".pdf")
        cfgmod.reset()

    def test_generate_html_dispatches_illustrations(self):
        import bidviz_cli
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = cfgmod.load(root)
            args = type("A", (), {"project": str(root), "engine": "html", "section": "S002",
                                  "chapter": None, "all": False, "fig": None, "limit": None,
                                  "force": False, "dry_run": True, "outdir": None})()
            fake = {"planned": 2, "generated": 0, "skipped": 0, "failed": 0, "out_dir": str(root)}
            with patch.object(bidviz_cli, "load_cfg", return_value=cfg), \
                 patch("bidviz_html.generate_illustrations", return_value=fake) as gen:
                bidviz_cli.cmd_generate(args)
            self.assertTrue(gen.called)
            self.assertEqual(gen.call_args.args[1], ["S002"])
        cfgmod.reset()


if __name__ == "__main__":
    unittest.main()
