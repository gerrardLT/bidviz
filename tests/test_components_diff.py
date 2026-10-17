#!/usr/bin/env python3
"""视觉回归基线测试（V1.3 阶段 1.4）。

机制：
- `COMPONENT_CASES` 列出 (name, render_fn)；render_fn 接收输出路径，用固定数据渲染 PNG。
- 首次跑：如基线不存在，render_fn 会写入 `tests/baseline-components/<name>.png` 作为基线。
- 后续跑：重新渲染到临时 PNG，与基线做 Pillow `ImageChops.difference` + bbox 像素 diff；
  超过 `MAX_DIFF_RATIO`（默认 2%）则测试失败。

阶段 1 只提供机制自检（`demo-rect`）；阶段 2 起加入真实组件基线。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = ROOT / "tests" / "baseline-components"
MAX_DIFF_RATIO = 0.02   # 2% 像素差即失败

sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------- demo 渲染器


def render_demo_rect(path: Path) -> None:
    """固定数据渲染：200x100 蓝底 + 白色 40x40 方块 + 红色对角线。

    此函数**必须确定性**：不依赖时间、随机数、系统字体、外部资源。
    """
    img = Image.new("RGB", (200, 100), color=(31, 78, 121))   # tokens.header_bg
    d = ImageDraw.Draw(img)
    d.rectangle([(20, 20), (60, 60)], fill=(255, 255, 255))
    d.line([(0, 0), (199, 99)], fill=(192, 57, 43), width=2)  # tokens.warn_red
    img.save(path, format="PNG")


# ---------------------------------------------------------------- 组件用例清单

# 阶段 2 起在此追加真实组件：("t-b1-kpi-strip", render_kpi_strip), ...
COMPONENT_CASES: list[tuple[str, callable]] = [
    ("demo-rect", render_demo_rect),
]


# ---------------------------------------------------------------- diff 工具


def pixel_diff_ratio(a: Path, b: Path) -> tuple[float, tuple[int, int, int, int] | None]:
    """返回 (diff 像素比例, diff bbox)；bbox=None 表示完全一致。"""
    ia = Image.open(a).convert("RGB")
    ib = Image.open(b).convert("RGB")
    if ia.size != ib.size:
        # 尺寸不一致 → 100% diff
        return 1.0, (0, 0, max(ia.width, ib.width), max(ia.height, ib.height))
    diff = ImageChops.difference(ia, ib)
    bbox = diff.getbbox()
    if bbox is None:
        return 0.0, None
    # 统计非零像素数（任一通道差 >0 即计）
    hist = diff.convert("L").histogram()
    nonzero = sum(hist[1:])
    total = ia.width * ia.height
    return nonzero / total, bbox


# ---------------------------------------------------------------- 测试


class ComponentsDiffTests(unittest.TestCase):
    def test_baseline_mechanism_self_check(self):
        """机制自检：同一渲染器两次输出应完全一致（0 diff）。"""
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "a.png"
            p2 = Path(tmp) / "b.png"
            render_demo_rect(p1)
            render_demo_rect(p2)
            ratio, bbox = pixel_diff_ratio(p1, p2)
            self.assertEqual(ratio, 0.0)
            self.assertIsNone(bbox)

    def test_diff_detects_small_change(self):
        """机制自检：修改 1% 像素仍应通过（<2% 阈值）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.png"
            changed = Path(tmp) / "changed.png"
            render_demo_rect(base)
            img = Image.open(base).convert("RGB")
            # 修改 200 像素（200x100=20000，200/20000=1%）
            for i in range(200):
                img.putpixel((i % 200, i // 200), (0, 0, 0))
            img.save(changed)
            ratio, _ = pixel_diff_ratio(base, changed)
            self.assertLess(ratio, MAX_DIFF_RATIO, f"1% 修改应通过，实际 {ratio:.4f}")

    def test_diff_detects_large_change(self):
        """机制自检：修改 5% 像素应失败（>2% 阈值）。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.png"
            changed = Path(tmp) / "changed.png"
            render_demo_rect(base)
            img = Image.open(base).convert("RGB")
            # 修改 1000 像素（1000/20000=5%）
            for i in range(1000):
                img.putpixel((i % 200, i // 200), (0, 0, 0))
            img.save(changed)
            ratio, _ = pixel_diff_ratio(base, changed)
            self.assertGreater(ratio, MAX_DIFF_RATIO, f"5% 修改应失败，实际 {ratio:.4f}")

    def test_all_components_match_baseline(self):
        """所有登记组件：与基线 PNG 比对，diff ≤ 阈值。

        基线不存在时自动写入（首次跑），存在时严格比对。
        """
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []
        created: list[str] = []
        for name, render_fn in COMPONENT_CASES:
            baseline = BASELINE_DIR / f"{name}.png"
            with tempfile.TemporaryDirectory() as tmp:
                actual = Path(tmp) / f"{name}.png"
                render_fn(actual)
                if not baseline.exists():
                    # 首次跑：写入基线（用 copy 避免 Windows 跨盘 replace 失败）
                    shutil.copy(str(actual), str(baseline))
                    created.append(name)
                    continue
                ratio, bbox = pixel_diff_ratio(actual, baseline)
                if ratio > MAX_DIFF_RATIO:
                    failures.append(
                        f"{name}: diff={ratio:.4%} > {MAX_DIFF_RATIO:.2%}, bbox={bbox}"
                    )
        if created:
            print(f"[baseline] 新建基线：{created}（请肉眼确认后提交）")
        self.assertFalse(failures, "视觉回归失败：\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main()
