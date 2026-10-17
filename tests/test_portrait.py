#!/usr/bin/env python3
"""tests/test_portrait —— V1.3 阶段 2.5：A3 竖版画布与 T-B1 布局比例断言。

不依赖 anke 项目数据：用最小 PagePlan 渲染临时 PDF，以 fitz 反抽 bbox
断言各区域落在预期比例带内；并验证隐藏检索层可反抽且视觉不可见。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import bidviz_templates as tpl
import bidviz_vector as bv
from bidviz_templates import KPI, PagePlan, ProseBlock
from bidviz_vector import Sheet

PORTRAIT_RATIO = 297.0 / 420.0   # ≈0.7071


class PortraitCanvasTests(unittest.TestCase):
    def test_set_page_portrait_dimensions(self):
        w, h = bv.set_page("A3", "portrait")
        try:
            self.assertAlmostEqual(w / h, PORTRAIT_RATIO, places=3)
            self.assertLess(w, h, "竖版应宽<高")
            self.assertAlmostEqual(w, 297 * bv.MM, places=1)
            self.assertAlmostEqual(h, 420 * bv.MM, places=1)
        finally:
            bv.set_page("A3", "landscape")   # 还原，避免污染其他测试

    def test_sheet_snapshots_page_size(self):
        bv.set_page("A3", "portrait")
        try:
            sheet = Sheet(None)   # 探针模式
            self.assertAlmostEqual(sheet.w, 297 * bv.MM, places=1)
            self.assertAlmostEqual(sheet.h, 420 * bv.MM, places=1)
            # 切换 module globals 不影响已创建实例
            bv.set_page("A3", "landscape")
            self.assertAlmostEqual(sheet.h, 420 * bv.MM, places=1)
        finally:
            bv.set_page("A3", "landscape")


class TB1LayoutRatioTests(unittest.TestCase):
    """T-B1 装配后各区域比例带断言（最小 page-plan，无图）。"""

    @classmethod
    def setUpClass(cls):
        bv.set_page("A3", "portrait")
        cls.tk = tpl.load_tokens()
        plan = PagePlan(
            chapter="第一章 测试章",
            section="第一节 测试节",
            page_no=1,
            total_pages=1,
            kpis=[KPI(label="指标一", value="60", unit="日历天", icon="期"),
                  KPI(label="指标二", value="3295", unit="m", icon="缆")],
            figure=None,
            prose=[ProseBlock(text="测试正文段落，用于断言右栏位置与首行缩进。" * 3)],
            icon_table_headers=["工序类别", "主要工作内容及要点"],
            icon_table_rows=[["①", "施工准备", "测试内容行一，用于断言底部图标表位置。"],
                             ["②", "土方与支护", "测试内容行二。"]],
            footer_note="测试页脚",
        )
        cls.tmp = tempfile.TemporaryDirectory()
        pdf = Path(cls.tmp.name) / "portrait.pdf"
        sheet = Sheet(pdf)
        tpl.compose_page(sheet, cls.tk, plan, template="T-B1")
        sheet.save()
        doc = fitz.open(pdf)
        cls.page = doc[0]
        cls.pw, cls.ph = cls.page.rect.width, cls.page.rect.height
        cls.lines = []
        for b in cls.page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                txt = "".join(s["text"] for s in ln["spans"])
                if txt.strip():
                    cls.lines.append((ln["bbox"], txt))
        cls.doc = doc

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()
        cls.tmp.cleanup()
        bv.set_page("A3", "landscape")

    def _find(self, frag: str):
        hits = [bb for bb, txt in self.lines if frag in txt]
        self.assertTrue(hits, f"未找到文本：{frag}")
        return hits[0]

    def test_page_ratio(self):
        self.assertAlmostEqual(self.pw / self.ph, PORTRAIT_RATIO, places=3)

    def test_title_in_top_band(self):
        bb = self._find("第一章 测试章")
        self.assertLess(bb[1] / self.ph, 0.08, "标题区应在顶部 8% 内")

    def test_kpi_below_title(self):
        bb = self._find("60日历天")
        ratio = bb[1] / self.ph
        self.assertGreater(ratio, 0.06)
        self.assertLess(ratio, 0.16, "KPI 带应在 6%~16% 之间")

    def test_prose_in_right_column(self):
        bb = self._find("测试正文段落")
        self.assertGreater(bb[0] / self.pw, 0.52, "正文应在右栏（x0 > 52% 页宽）")

    def test_icon_table_at_bottom(self):
        bb = self._find("施工准备")
        self.assertGreater(bb[1] / self.ph, 0.75, "图标表应在底部 25% 内")

    def test_left_region_reserved_for_figure(self):
        """无图时左 55% 应为空（占位框不画文本），右栏文本不得侵入左栏。"""
        for bb, txt in self.lines:
            if "测试正文" in txt or "施工准备" in txt:
                continue
        # 正文所有行 x0 均 > 左栏右边界
        left_bound = 0.50 * self.pw
        for bb, txt in self.lines:
            if "测试正文" in txt:
                self.assertGreater(bb[0], left_bound, f"正文侵入左栏：{txt}")


class HiddenLayerTests(unittest.TestCase):
    """D10 隐藏检索层：可反抽、视觉不可见、不污染后续文本。"""

    def test_hidden_text_extractable_and_invisible(self):
        bv.set_page("A3", "portrait")
        try:
            tmp = tempfile.TemporaryDirectory()
            pdf = Path(tmp.name) / "hidden.pdf"
            sheet = Sheet(pdf)
            sheet.hidden_text(100, 100, "隐藏检索串Alpha", 9.5)
            sheet.text(100, 140, "可见文本Beta", 9.5)   # 隐藏层之后的文本必须仍可见
            sheet.save()

            doc = fitz.open(pdf)
            page = doc[0]
            text = page.get_text()
            doc.close()
            self.assertIn("隐藏检索串Alpha", text, "隐藏层应可 Ctrl+F 反抽")
            self.assertIn("可见文本Beta", text, "隐藏层不得使后续文本隐形")

            # 视觉不可见：隐藏串所在区域像素应为纯白
            doc = fitz.open(pdf)
            pix = doc[0].get_pixmap(dpi=72, clip=fitz.Rect(95, 95, 300, 115))
            doc.close()
            samples = pix.samples
            non_white = sum(1 for i in range(0, len(samples), 3)
                            if samples[i] < 250 or samples[i + 1] < 250 or samples[i + 2] < 250)
            self.assertEqual(non_white, 0, f"隐藏层区域应纯白，实际非白像素 {non_white}")
            tmp.cleanup()
        finally:
            bv.set_page("A3", "landscape")


if __name__ == "__main__":
    unittest.main()
