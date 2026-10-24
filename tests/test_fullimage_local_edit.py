import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_fullimage_store as store  # noqa: E402
import bidviz_local_edit as le  # noqa: E402


def _page(size=(256, 362), color="white") -> Image.Image:
    return Image.new("RGB", size, color)


def _with_text(size=(2048, 2896)) -> Image.Image:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.text((120, 200), "作业面开控深度超过 2m", fill=(30, 30, 30))
    return im


class BoxTests(unittest.TestCase):
    def test_normalize_box_pads_and_swaps(self):
        self.assertEqual(le.normalize_box([10, 20, 50, 70], 100, 100), (4, 14, 56, 76))
        self.assertEqual(le.normalize_box([50, 70, 10, 20], 100, 100), (4, 14, 56, 76))

    def test_normalize_box_clamps_to_image(self):
        self.assertEqual(le.normalize_box([-20, -20, 5, 5], 100, 100), (0, 0, 11, 11))
        self.assertEqual(le.normalize_box([90, 90, 200, 200], 100, 100), (84, 84, 100, 100))

    def test_normalize_box_rejects_tiny_or_bad(self):
        with self.assertRaises(ValueError):
            le.normalize_box([10, 10, 11, 11], 100, 100)
        with self.assertRaises(ValueError):
            le.normalize_box([1, 2, 3], 100, 100)
        with self.assertRaises(ValueError):
            le.normalize_box(["a", "b", "c", "d"], 100, 100)

    def test_crop_window_has_min_context(self):
        box = (900, 1200, 960, 1260)
        crop = le.crop_window(box, 2048, 2896)
        self.assertGreaterEqual(crop[2] - crop[0], 1024)
        self.assertGreaterEqual(crop[3] - crop[1], 512)
        self.assertLessEqual(crop[0], box[0])
        self.assertLessEqual(crop[1], box[1])
        self.assertGreaterEqual(crop[2], box[2])
        self.assertGreaterEqual(crop[3], box[3])

    def test_crop_window_shifts_at_edge(self):
        crop = le.crop_window((2000, 2800, 2048, 2896), 2048, 2896)
        self.assertEqual(crop[2], 2048)
        self.assertEqual(crop[3], 2896)
        self.assertGreaterEqual(crop[0], 0)
        self.assertGreaterEqual(crop[1], 0)

    def test_mask_marks_only_box(self):
        mask = le.build_mask((100, 100), (20, 30, 60, 70), (0, 0, 100, 100))
        self.assertEqual(mask.getpixel((40, 50)), 255)
        self.assertEqual(mask.getpixel((5, 5)), 0)


class CompositeTests(unittest.TestCase):
    def test_composite_keeps_outside_pixels(self):
        source = _page()
        ImageDraw.Draw(source).rectangle((40, 40, 120, 60), fill=(30, 30, 30))
        edited = source.copy()
        ImageDraw.Draw(edited).rectangle((40, 40, 120, 60), fill=(180, 20, 20))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((36, 36, 124, 64), fill=255)
        out = le.composite(source, edited, mask)
        self.assertEqual(out.size, source.size)
        max_diff, changed = le._outside_diff(source, out, (36, 36, 124, 64), margin=12)
        self.assertEqual(max_diff, 0)
        self.assertEqual(changed, 0)

    def test_composite_works_for_white_text_on_dark(self):
        source = Image.new("RGB", (256, 362), (20, 34, 78))
        d = ImageDraw.Draw(source)
        d.rectangle((40, 40, 200, 90), fill=(30, 60, 130))
        edited = source.copy()
        ImageDraw.Draw(edited).rectangle((40, 40, 200, 90), fill=(30, 60, 130))
        ImageDraw.Draw(edited).text((60, 55), "额定电压", fill=(250, 250, 250))
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((56, 48, 190, 84), fill=255)
        out = le.composite(source, edited, mask)
        max_diff, changed = le._outside_diff(source, out, (56, 48, 190, 84), margin=12)
        self.assertEqual(max_diff, 0)
        self.assertEqual(changed, 0)
        # 框内应出现明显变亮的文字像素，而不是被整体刷白
        inside = out.crop((56, 48, 190, 84))
        self.assertGreater(inside.convert("L").getextrema()[1], 200)

    def test_manual_checks_pass_and_warn(self):
        source = _page()
        out = source.copy()
        ImageDraw.Draw(out).rectangle((45, 45, 70, 70), fill=(0, 0, 0))
        box = (40, 40, 80, 80)
        checks = le.manual_checks(source, out, box, min_width=64)
        self.assertNotEqual(checks["status"], "block")
        codes = {c["code"]: c["level"] for c in checks["checks"]}
        self.assertEqual(codes["manual-review"], "warn")
        self.assertEqual(codes["outside-unchanged"], "pass")
        self.assertEqual(codes["inside-change"], "pass")

    def test_manual_checks_blocks_noop(self):
        source = _page()
        out = source.copy()
        checks = le.manual_checks(source, out, (40, 40, 80, 80), min_width=64)
        codes = {c["code"]: c["level"] for c in checks["checks"]}
        self.assertEqual(codes["inside-change"], "block")
        self.assertEqual(checks["status"], "block")

    def test_color_ring_uses_model_background_outside_patch(self):
        ring = le._color_ring((20, 20, 40, 40), (10, 10, 60, 60), (100, 100))
        self.assertIsNotNone(ring)
        self.assertEqual(ring.getpixel((14, 14)), 255)   # 遮罩内、补丁外 3px 以上
        self.assertEqual(ring.getpixel((30, 30)), 0)     # 补丁内
        self.assertEqual(ring.getpixel((5, 5)), 0)       # 遮罩外
        self.assertEqual(ring.getpixel((18, 30)), 0)     # 补丁外紧邻 3px 也排除

    def test_manual_checks_block_wrong_size_or_outside_change(self):
        source = _page()
        wrong = source.resize((128, 181))
        checks = le.manual_checks(source, wrong, (40, 40, 80, 80), min_width=64)
        codes = {c["code"]: c["level"] for c in checks["checks"]}
        self.assertEqual(checks["status"], "block")
        self.assertEqual(codes["dimensions"], "block")

        changed = source.copy()
        ImageDraw.Draw(changed).rectangle((0, 0, 5, 5), fill="black")
        checks = le.manual_checks(source, changed, (40, 40, 80, 80), min_width=64)
        codes = {c["code"]: c["level"] for c in checks["checks"]}
        self.assertEqual(codes["outside-unchanged"], "block")


class ApplyEditTests(unittest.TestCase):
    class FakeAPI:
        def edit(self, prompt, image, mask, output, size=None, recorder=None):
            im = Image.open(image).convert("RGB")
            d = ImageDraw.Draw(im)
            d.text((8, 8), "挖", fill=(20, 20, 20))
            im.save(output)
            if recorder is not None:
                recorder.update({"image_sha256": "i", "mask_sha256": "m",
                                 "output_sha256": "o"})
            return Path(output)

    def test_apply_local_edit_end_to_end_with_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = _with_text()
            source = root / "src.png"
            out = root / "out.png"
            src.save(source)
            with patch.object(le, "_api", return_value=(self.FakeAPI(), "test", "fake")):
                meta = le.apply_local_edit(source, [900, 190, 1200, 260], "挖", out)
            self.assertTrue(out.is_file())
            with Image.open(out) as im:
                self.assertEqual(im.size, (2048, 2896))
            self.assertEqual(meta["text"], "挖")
            self.assertEqual(meta["actual_size"], "2048x2896")
            self.assertEqual(meta["provider"], "test")

    def test_apply_local_edit_rejects_bad_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.png"
            _page().save(src)
            with self.assertRaises(ValueError):
                le.apply_local_edit(src, [10, 10, 60, 60], "   ", Path(tmp) / "o.png")
            with self.assertRaises(ValueError):
                le.apply_local_edit(src, [10, 10, 60, 60], "字" * 201, Path(tmp) / "o.png")

    def test_apply_local_edit_pastes_at_model_coordinates(self):
        class FakeAt:
            def edit(self, prompt, image, mask, output, size=None, recorder=None):
                im = Image.open(image).convert("RGB")
                ImageDraw.Draw(im).rectangle((380, 200, 392, 216), fill=(255, 255, 255))
                im.save(output)
                if recorder is not None:
                    recorder.update({"image_sha256": "i", "mask_sha256": "m",
                                     "output_sha256": "o"})
                return Path(output)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src.png"
            out = root / "out.png"
            Image.new("RGB", (2048, 2896), (5, 8, 12)).save(source)
            with patch.object(le, "_api", return_value=(FakeAt(), "test", "fake")):
                le.apply_local_edit(source, [900, 190, 1200, 260], "挖", out)
            with Image.open(out) as im:
                # 模型在裁剪图 (380,200) 画的字，原图坐标应为 crop x=538 + 382
                self.assertGreater(im.getpixel((924, 208))[0], 200)
                # 旧实现会把它重新居中到用户框中心 (1050,225)，这里必须仍是背景
                self.assertLess(im.getpixel((1050, 225))[0], 40)


class PageContextTests(unittest.TestCase):
    def test_cli_base_version_can_use_pruned_published_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.ensure_page(root, "S001", 1, "src", "tpl", "prm")
            image = root / "legacy.png"
            _page((100, 141)).save(image)
            store.import_published(root, "S001", 1, image, {"kind": "legacy-import"})
            store.compact_candidates(root)
            output = root / "out.png"
            with patch.object(le, "apply_local_edit", return_value={"ok": True}) as apply:
                code = le.main([
                    "edit", "--root", str(root), "--sid", "S001", "--page", "1",
                    "--base-version", "1", "--box", "10,10,50,50", "--text", "挖",
                    "--out", str(output),
                ])
            self.assertEqual(code, 0)
            self.assertEqual(apply.call_args.args[0], store.published_path(root, "S001", 1))

    def test_page_context_delegates_to_fullimage(self):
        expected = {"source_hash": "shared"}
        with patch("bidviz_fullimage.page_context", return_value=expected) as shared:
            actual = le.page_context(Path("project"), "S001", 2, "section")
        self.assertIs(actual, expected)
        shared.assert_called_once_with(Path("project"), "S001", 2, "section")


class StoreImportTests(unittest.TestCase):
    def test_import_published_marks_page_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store.ensure_page(root, "S001", 1, "src", "tpl", "prm")
            image = root / "legacy.png"
            _page((100, 141)).save(image)
            rec = store.import_published(root, "S001", 1, image, {"kind": "legacy-import"})
            self.assertEqual(rec["status"], "published")
            self.assertTrue(store.published_path(root, "S001", 1).is_file())
            gates = store.gate_summary(root, [("S001", 1)])
            self.assertEqual(gates["published"], 1)
            self.assertTrue(gates["can_build_final"])

    def test_import_existing_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_dir = root / "work" / "fullimage"
            image_dir.mkdir(parents=True)
            _page((100, 141)).save(image_dir / "S001_p1.png")

            sections = [SimpleNamespace(section_id="S001", title="封面及总述", blocks=[])]
            ctx = {"source_hash": "s", "template_hash": "t", "auto_prompt": "p",
                   "cover": True}
            cfg = SimpleNamespace(provider="test", model="fake", image_size="2048x2896")
            with patch.object(le, "_sections", return_value=sections), \
                 patch("bidviz_fullimage.split_pages", return_value=[[]]), \
                 patch.object(le, "page_context", return_value=ctx), \
                 patch("bidviz_fullimage_store.get_prompt_override",
                       return_value={"resolved_prompt": "p"}), \
                 patch("bidviz_config.get", return_value=cfg):
                first = le.import_existing(root)
                second = le.import_existing(root)
            self.assertEqual(first["imported"], 1)
            self.assertEqual(second["imported"], 0)
            self.assertEqual(second["skipped"], 1)
            self.assertTrue(store.published_path(root, "S001", 1).is_file())
            self.assertEqual(len(store.list_candidates(root, "S001", 1)), 1)


if __name__ == "__main__":
    unittest.main()
