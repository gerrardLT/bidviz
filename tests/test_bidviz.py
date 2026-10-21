import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import bidviz


class BidVizTests(unittest.TestCase):
    def test_manifest_hashes_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("施工", encoding="utf-8")
            manifest = bidviz.build_manifest(root)
            self.assertEqual(manifest["sources"][0]["source_id"], "TXT-001")
            self.assertEqual(len(manifest["sources"][0]["sha256"]), 64)

    def test_claims_are_traceable_and_typed(self):
        claims = bidviz.extract_claims([
            "一、挖沟槽土方",
            "土方开挖采用自然放坡，放坡系数为1:1。",
            "基槽施工时严禁人员进入危险区域。",
        ], "DOC-001", "EXC")
        self.assertEqual([c.claim_type for c in claims], ["PARAMETER", "PROHIBITION"])
        self.assertTrue(all(c.source_id and c.locator and c.raw_quote for c in claims))

    def test_issue_detection(self):
        claims = [
            bidviz.Claim("C-1", "W", "s", "距离3、5~4、5米", "距离3、5~4、5米", "D", "p1", "PARAMETER", "ambiguous"),
            bidviz.Claim("C-2", "W", "s", "养护不少于7", "养护不少于7", "D", "p2", "PARAMETER"),
            bidviz.Claim("C-3", "W", "s", "防水上卷300mm", "防水上卷300mm", "D", "p3", "PARAMETER"),
            bidviz.Claim("C-4", "W", "s", "防水上卷250mm", "防水上卷250mm", "D", "p4", "PARAMETER"),
        ]
        categories = {i.category for i in bidviz.detect_issues(claims)}
        self.assertEqual(categories, {"ambiguous-number", "missing-unit", "conflict"})

    def test_visual_plan_is_adaptive(self):
        claims = [
            bidviz.Claim("C-1", "W", "s", "沟槽深3m", "沟槽深3m", "D", "p1", "PARAMETER"),
            bidviz.Claim("C-2", "W", "s", "厚200mm", "厚200mm", "D", "p2", "PARAMETER"),
            bidviz.Claim("C-3", "W", "s", "间距20m", "间距20m", "D", "p3", "PARAMETER"),
            bidviz.Claim("C-4", "W", "s", "发现裂缝立即停工", "发现裂缝立即停工", "D", "p4", "RISK_CONTROL"),
        ]
        pages = bidviz.visual_plan("W", "沟槽", claims, ["plan.png"])
        kinds = {p.visual_type for p in pages}
        self.assertTrue({"location-context", "construction-scene", "section-or-exploded", "parameter-control", "quality-safety"} <= kinds)

    def test_parameter_labels_and_dimensions(self):
        claims = [bidviz.Claim("C-1", "W", "s", "排水沟200mm×300mm，每隔20m设置集水井", "q", "D", "p", "PARAMETER")]
        self.assertEqual(bidviz.parameter_labels(claims), ["200mm×300mm", "20m"])

    def test_prompt_bans_final_text(self):
        claim = bidviz.Claim("C-1", "W", "s", "沟槽深3m", "沟槽深3m", "D", "p1", "PARAMETER")
        page = bidviz.PageSpec("P", "W", "沟槽", "construction-scene", ["C-1"])
        prompt = bidviz.compile_prompt(page, {"C-1": claim})
        self.assertIn("禁止生成任何中文", prompt)
        self.assertIn("禁止增加事实", prompt)
        self.assertIn("无项目平面图参考", prompt)
        self.assertIn("不要绘制页面模板", prompt)

    def test_pideck_provider_selection(self):
        config = {"providers": [{"id": "a", "name": "gemini", "baseUrl": "https://example.test/v1", "apiKey": "secret", "models": []}], "activeProviderId": "a", "activeModel": "other"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"APPDATA": tmp}, clear=False):
            path = Path(tmp) / "PiDeck" / "imagegen.json"
            path.parent.mkdir()
            path.write_text(json.dumps(config), encoding="utf-8")
            selected = bidviz.pideck_image_config("gemini", "gemini-flash-thinking")
        self.assertEqual(selected["model"], "gemini-flash-thinking")
        self.assertEqual(selected["base_url"], "https://example.test/v1")

    def test_named_provider_beats_active_provider(self):
        config = {"providers": [{"id": "g", "name": "gemini", "baseUrl": "https://gemini.test/v1", "apiKey": "g", "models": []}, {"id": "o", "name": "gptimage2", "baseUrl": "https://other.test", "apiKey": "o", "models": ["gpt-image-2.5"]}], "activeProviderId": "o", "activeModel": "gpt-image-2.5"}
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"APPDATA": tmp}, clear=False):
            path = Path(tmp) / "PiDeck" / "imagegen.json"; path.parent.mkdir(); path.write_text(json.dumps(config), encoding="utf-8")
            selected = bidviz.pideck_image_config("gemini", "gemini-flash-thinking")
        self.assertEqual(selected["base_url"], "https://gemini.test/v1")
        self.assertEqual(selected["model"], "gemini-flash-thinking")

    def test_api_uses_environment_and_multi_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            refs = []
            for n in range(2):
                p = root / f"r{n}.png"
                Image.new("RGB", (10, 10), "white").save(p)
                refs.append(p)
            output = root / "out.png"
            image_bytes = base64.b64encode(b"fake-image").decode()
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps({"data": [{"b64_json": image_bytes}]}).encode()
            record = {}
            with patch.dict("os.environ", {"CUSTOM_IMAGE_API_BASE": "https://example.test/v1", "CUSTOM_IMAGE_API_KEY": "secret"}, clear=False), patch.object(bidviz, "_safe_open", return_value=response) as safe_open:
                bidviz.ImageAPI().generate("prompt", output, refs, recorder=record)
            request = safe_open.call_args.args[0]
            body = json.loads(request.data)
            self.assertEqual(len(body["image"]), 2)
            self.assertEqual(request.headers["Authorization"], "Bearer secret")
            self.assertEqual(output.read_bytes(), b"fake-image")
            self.assertEqual(record["reference_count"], 2)
            self.assertNotIn("secret", json.dumps(record))

    def test_api_edit_posts_multipart_with_mask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "crop.png"
            mask = root / "mask.png"
            output = root / "out.png"
            Image.new("RGB", (32, 16), "white").save(image)
            Image.new("L", (32, 16), 0).save(mask)
            image_bytes = b"edited-image"
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = json.dumps(
                {"data": [{"b64_json": base64.b64encode(image_bytes).decode()}]}).encode()
            record = {}
            with patch.dict("os.environ", {"CUSTOM_IMAGE_API_BASE": "https://example.test/v1",
                                           "CUSTOM_IMAGE_API_KEY": "secret"}, clear=False), \
                 patch.object(bidviz, "_safe_open", return_value=response) as safe_open:
                bidviz.ImageAPI().edit("把文字改成挖", image, mask, output,
                                       size="32x16", recorder=record)
            request = safe_open.call_args.args[0]
            self.assertTrue(request.full_url.endswith("/v1/images/edits"))
            self.assertIn("multipart/form-data", request.get_header("Content-type"))
            self.assertIn(b'name="model"', request.data)
            self.assertIn(b'name="prompt"', request.data)
            self.assertIn(b'name="image"', request.data)
            self.assertIn(b'name="mask"', request.data)
            self.assertIn(image.read_bytes(), request.data)
            self.assertIn(mask.read_bytes(), request.data)
            self.assertEqual(output.read_bytes(), image_bytes)
            self.assertEqual(record["kind"], "edit")
            self.assertNotIn("secret", json.dumps(record))

    def test_online_visuals_require_ocr_or_human_review(self):
        project = bidviz.Project("x")
        project._visual_paths = [Path("generated.png")]
        with patch.object(bidviz, "OCR_AVAILABLE", False):
            report = bidviz.audit(project)
        self.assertIn("ocr-unavailable", {x["code"] for x in report["findings"]})

    def test_final_audit_blocks_unresolved_issue(self):
        project = bidviz.Project("x", claims=[bidviz.Claim("C", "W", "s", "t", "q", "D", "p", "ACTION")], issues=[bidviz.Issue("I", ["C"], "high", "conflict", "x")])
        self.assertFalse(bidviz.audit(project, final=True)["passed"])


if __name__ == "__main__":
    unittest.main()
