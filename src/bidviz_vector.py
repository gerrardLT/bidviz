#!/usr/bin/env python3
"""矢量排版引擎：A3 横向报告排版，文字/表格/线框全部矢量，仅图片为位图。

设计要点：
- 坐标系对外使用「左上角原点、y 向下」，内部转换为 PDF 左下角原点。
- 折行按实测字宽（pdfmetrics.stringWidth），而不是按字符数估算。
- 表格行高由实测折行数决定；超出页高时返回剩余行，由调用方续排到下一页。
- 图片按目标打印尺寸压缩为 JPEG 后嵌入，避免整页栅格化。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas

MM = 2.834645669291339
PAGE_W = 420 * MM   # A3 横向宽 1190.55pt
PAGE_H = 297 * MM   # A3 横向高 841.89pt

REG = "CN"
BOLD = "CN-Bold"

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\.\-+±~%×x/:：]*|\s+|.", re.S)


def _font_file(bold: bool) -> str:
    for cand in ([r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\simhei.ttf"]
                 if bold else [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]):
        if Path(cand).exists():
            return cand
    raise RuntimeError("未找到可用中文字体")


_registered = False


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    pdfmetrics.registerFont(TTFont(REG, _font_file(False), subfontIndex=0))
    pdfmetrics.registerFont(TTFont(BOLD, _font_file(True), subfontIndex=0))
    _registered = True


def text_width(text: str, size: float, bold: bool = False) -> float:
    return pdfmetrics.stringWidth(text, BOLD if bold else REG, size)


def tokenize(text: str) -> list[str]:
    """把文本切成不希望在行内被拆开的单元：连续英文/数字算一个单元，中文逐字。"""
    return [t for t in _TOKEN_RE.findall(text) if t]


def wrap(text: str, size: float, max_w: float, bold: bool = False) -> list[str]:
    """按实测宽度折行。保留数字+单位不被拆开。"""
    out: list[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue
        line = ""
        for tok in tokenize(para):
            candidate = line + tok
            if line and text_width(candidate, size, bold) > max_w:
                out.append(line.rstrip())
                line = tok.lstrip() if tok.isspace() else tok
            else:
                line = candidate
        out.append(line.rstrip())
    return out or [""]


def truncate(text: str, size: float, max_w: float, bold: bool = False) -> str:
    """截断到一行放得下，末尾加省略号。"""
    if text_width(text, size, bold) <= max_w:
        return text
    ell = "…"
    budget = max_w - text_width(ell, size, bold)
    acc = ""
    for tok in tokenize(text):
        if text_width(acc + tok, size, bold) > budget:
            break
        acc += tok
    return acc.rstrip() + ell


@dataclass
class Cell:
    text: str = ""
    bold: bool = False
    size: float = 9.0
    color: str = "#1a2c38"
    align: str = "left"          # left | center
    pill: tuple[str, str, str] | None = None   # (label, bg, fg)


@dataclass
class TableSpec:
    headers: Sequence[str]
    rows: Sequence[Sequence[Cell | str]]
    col_w: Sequence[float]
    header_bg: str = "#13334c"
    font_size: float = 9.0
    pad: float = 5.0
    header_size: float = 9.5


class Sheet:
    """A3 横向页面。所有对外坐标以左上角为原点、y 向下，单位为 pt。"""

    def __init__(self, path: Path):
        register_fonts()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.c = rl_canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
        self.c.setTitle(path.stem)

    # ---------- 基础绘制 ----------
    def _y(self, y_top: float) -> float:
        return PAGE_H - y_top

    def rect(self, x: float, y: float, w: float, h: float, fill: str | None = None,
             stroke: str | None = None, lw: float = 0.6, radius: float = 0.0) -> None:
        c = self.c
        if fill:
            c.setFillColor(fill)
        if stroke:
            c.setStrokeColor(stroke)
            c.setLineWidth(lw)
        mode = 0
        if fill and stroke:
            mode = 1
        elif fill:
            mode = 1
            stroke = None
        if radius > 0:
            c.roundRect(x, self._y(y + h), w, h, radius, stroke=1 if stroke else 0, fill=1 if fill else 0)
        else:
            c.rect(x, self._y(y + h), w, h, stroke=1 if stroke else 0, fill=1 if fill else 0)

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = "#c1d0d9", lw: float = 0.6) -> None:
        self.c.setStrokeColor(color)
        self.c.setLineWidth(lw)
        self.c.line(x1, self._y(y1), x2, self._y(y2))

    def text(self, x: float, y: float, s: str, size: float = 9.5, bold: bool = False,
             color: str = "#1a2c38", align: str = "left", max_w: float | None = None) -> None:
        if max_w is not None:
            s = truncate(s, size, max_w, bold)
        self.c.setFillColor(color)
        self.c.setFont(BOLD if bold else REG, size)
        baseline = self._y(y + size * 0.82)
        if align == "center":
            self.c.drawCentredString(x, baseline, s)
        elif align == "right":
            self.c.drawRightString(x, baseline, s)
        else:
            self.c.drawString(x, baseline, s)

    def para(self, x: float, y: float, text: str, max_w: float, size: float = 9.5,
             leading: float | None = None, color: str = "#1a2c38", bold: bool = False,
             max_lines: int | None = None) -> float:
        """排一段文字，返回占用高度。"""
        leading = leading or size * 1.45
        lines = wrap(text, size, max_w, bold)
        if max_lines is not None:
            lines = lines[:max_lines]
        cy = y
        for ln in lines:
            self.text(x, cy, ln, size, bold, color)
            cy += leading
        return cy - y

    def para_height(self, text: str, max_w: float, size: float = 9.5, leading: float | None = None) -> float:
        leading = leading or size * 1.45
        return len(wrap(text, size, max_w)) * leading

    def pill(self, x: float, y: float, label: str, bg: str = "#e0f2f1", fg: str = "#00695c",
             size: float = 7.5, pad_x: float = 5.0, pad_y: float = 2.5) -> float:
        w = text_width(label, size, True) + pad_x * 2
        h = size * 1.5 + pad_y * 2
        self.rect(x, y, w, h, fill=bg, radius=h / 2)
        self.text(x + w / 2, y + pad_y + size * 0.16, label, size, True, fg, align="center")
        return w

    # ---------- 图片 ----------
    def image(self, src: Path | Image.Image, x: float, y: float, w: float, h: float,
              dpi: float = 200, cache_dir: Path | None = None, fit: str = "contain") -> None:
        """把图片按目标尺寸压缩为 JPEG 后嵌入（contain 留白 / cover 裁切铺满）。"""
        img = src if isinstance(src, Image.Image) else Image.open(src)
        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")

        target_px_w = max(64, int(w / 72.0 * dpi))
        target_px_h = max(64, int(h / 72.0 * dpi))
        if fit == "cover":
            ratio = max(target_px_w / img.width, target_px_h / img.height)
        else:
            ratio = min(target_px_w / img.width, target_px_h / img.height)
        if ratio < 1.0:
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.Resampling.LANCZOS)

        cache_dir = cache_dir or self.path.parent / "_imgcache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = f"{abs(hash((getattr(src, 'name', 'mem'), img.size, dpi, fit, round(w, 1), round(h, 1)))):x}"
        jpg = cache_dir / f"{key}.jpg"
        if not jpg.exists():
            img.save(jpg, "JPEG", quality=85, optimize=True)

        if fit == "cover":
            self.c.drawImage(ImageReader(str(jpg)), x, self._y(y + h), w, h)
        else:
            iw, ih = img.size
            scale = min(w / iw, h / ih)
            dw, dh = iw * scale, ih * scale
            self.c.drawImage(ImageReader(str(jpg)), x + (w - dw) / 2, self._y(y + (h - dh) / 2 + dh), dw, dh)

    # ---------- 表格 ----------
    def table_height(self, spec: TableSpec, max_w: float | None = None) -> float:
        h = self._header_h(spec)
        for row in spec.rows:
            h += self._row_h(spec, row)
        return h

    def _header_h(self, spec: TableSpec) -> float:
        return spec.header_size * 1.6 + spec.pad * 2

    def _row_h(self, spec: TableSpec, row: Sequence[Cell | str]) -> float:
        lines = 1
        for i, raw in enumerate(row):
            cell = raw if isinstance(raw, Cell) else Cell(text=str(raw), size=spec.font_size)
            if cell.pill:
                continue
            cw = spec.col_w[i] - spec.pad * 2
            lines = max(lines, len(wrap(cell.text, cell.size, cw, cell.bold)))
        return max(spec.font_size * 1.45 * lines + spec.pad * 2, spec.font_size * 1.45 + spec.pad * 2)

    def table(self, spec: TableSpec, x: float, y: float, max_h: float | None = None,
              draw_header: bool = True) -> tuple[float, int]:
        """画表；返回 (占用高度, 已画行数)。若 max_h 不足，只画放得下的行。"""
        w = sum(spec.col_w)
        cy = y
        used = 0.0
        if draw_header:
            hh = self._header_h(spec)
            self.rect(x, cy, w, hh, fill=spec.header_bg)
            cx = x
            for i, head in enumerate(spec.headers):
                self.text(cx + spec.pad, cy + spec.pad, head, spec.header_size, True, "white",
                          max_w=spec.col_w[i] - spec.pad * 2)
                cx += spec.col_w[i]
                if i < len(spec.headers) - 1:
                    self.line(cx, cy, cx, cy + hh, "#2e4d65", 0.7)
            cy += hh
            used += hh

        drawn = 0
        for r_idx, row in enumerate(spec.rows):
            rh = self._row_h(spec, row)
            if max_h is not None and used + rh > max_h:
                break
            self.rect(x, cy, w, rh, fill="white" if r_idx % 2 == 0 else "#f8fbfd", stroke="#c1d0d9", lw=0.5)
            cx = x
            for i, raw in enumerate(row):
                cell = raw if isinstance(raw, Cell) else Cell(text=str(raw), size=spec.font_size)
                cw = spec.col_w[i]
                if cell.pill:
                    label, bg, fg = cell.pill
                    self.pill(cx + spec.pad, cy + spec.pad, label, bg, fg)
                else:
                    lines = wrap(cell.text, cell.size, cw - spec.pad * 2, cell.bold)
                    ty = cy + spec.pad
                    for ln in lines:
                        if cell.align == "center":
                            self.text(cx + cw / 2, ty, ln, cell.size, cell.bold, cell.color, align="center")
                        else:
                            self.text(cx + spec.pad, ty, ln, cell.size, cell.bold, cell.color)
                        ty += cell.size * 1.45
                cx += cw
                if i < len(row) - 1:
                    self.line(cx, cy, cx, cy + rh, "#c1d0d9", 0.5)
            cy += rh
            used += rh
            drawn += 1

        self.rect(x, y, w, used, stroke=spec.header_bg, lw=1.0)
        return used, drawn

    def save(self) -> Path:
        self.c.showPage()
        self.c.save()
        return self.path
