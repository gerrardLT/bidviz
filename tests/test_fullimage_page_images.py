from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import fitz
from PIL import Image

import bidviz_fullimage as fi


class PageImageExportTests(unittest.TestCase):
    def test_entries_use_real_pdf_page_prefix_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "work" / "fullimage" / "S001_p1.png"
            src.parent.mkdir(parents=True)
            Image.new("RGB", (120, 80), "red").save(src)
            pdf = root / "output" / "册.pdf"
            pdf.parent.mkdir(parents=True)
            doc = fitz.open()
            doc.new_page(width=595, height=842)
            doc.save(pdf)
            doc.close()

            summary = fi.export_page_images(
                root, pdf, force_copy=True,
                entries=[{"pdf_page": 1, "sid": "S001", "section_page": 1,
                          "source": str(src), "source_kind": "generated"}],
            )

            dst = root / "output" / "page-images" / "册" / "001_S001_p1.png"
            self.assertTrue(dst.is_file())
            self.assertEqual(dst.read_bytes(), src.read_bytes())
            self.assertEqual(summary["generated"], 1)
            manifest = json.loads((summary["manifest"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["pages"][0]["file"], "001_S001_p1.png")
            self.assertEqual(manifest["pages"][0]["pdf_page"], 1)

    def test_vector_page_is_rendered_with_pdfrender_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf = root / "output" / "册.pdf"
            pdf.parent.mkdir(parents=True)
            doc = fitz.open()
            doc.new_page(width=595, height=842)
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 72), "vector page")
            doc.save(pdf)
            doc.close()

            summary = fi.export_page_images(
                root, pdf,
                entries=[{"pdf_page": 2, "sid": "S002", "section_page": 1,
                          "source": None, "source_kind": "pdf-render"}],
            )

            dst = root / "output" / "page-images" / "册" / "002_S002_p1_pdfrender.png"
            self.assertTrue(dst.is_file())
            self.assertEqual(summary["rendered"], 1)
            with Image.open(dst) as im:
                self.assertGreater(im.width, 100)


if __name__ == "__main__":
    unittest.main()
