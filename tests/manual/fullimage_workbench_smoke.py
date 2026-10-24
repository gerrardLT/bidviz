"""Dedicated UI smoke test for the viewer / local-edit modal.

Run with the repo venv while web/server.py is listening on 8771.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8771/"
OUT = Path(__file__).resolve().parent
errors: list[str] = []


def shot(page, name: str) -> None:
    page.screenshot(path=str(OUT / name), full_page=False)


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1600, "height": 1000})
    page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
            if m.type == "error" and "favicon" not in m.text else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector("#doc .fig", timeout=30000)

    # 1) 大图审阅布局
    page.click("#bigview")
    assert page.evaluate("document.body.classList.contains('bigview')")
    shot(page, "ui-01-bigview.png")

    # 2) 普通查看器：点击图片打开
    page.click('.fig[data-s="S001"] img[alt="已发布"]')
    page.wait_for_selector("#viewer", state="visible")
    page.wait_for_function("document.querySelector('#vwpct').textContent !== '100%'", timeout=8000)
    fit_pct = page.text_content("#vwpct")
    shot(page, "ui-02-viewer-fit.png")

    # 3) 100% 与滚轮缩放
    page.click('#viewer [data-vw="1"]')
    assert page.text_content("#vwpct") == "100%"
    before = page.evaluate("VIEW.zoom")
    page.mouse.move(700, 400)
    page.mouse.wheel(0, -300)
    after = page.evaluate("VIEW.zoom")
    assert after > before, (before, after)
    shot(page, "ui-03-viewer-100-zoom.png")
    page.click("#vwclose")
    page.wait_for_selector("#viewer", state="hidden")

    # 4) 局部改字编辑器：默认 100%
    page.click('.fig[data-s="S001"] .pane:first-child button[data-act="local-edit"]')
    page.wait_for_selector("#viewer", state="visible")
    assert page.evaluate("VIEW.mode") == "edit"
    assert page.text_content("#vwpct") == "100%"
    shot(page, "ui-04-editor-100.png")

    # 5) 适应后按比例拖框（框住封面标题区域）
    page.click('#viewer [data-vw="fit"]')
    rect = page.evaluate("""() => {
        const r = document.querySelector('#vwimg').getBoundingClientRect();
        return {left:r.left, top:r.top, width:r.width, height:r.height,
                nw:document.querySelector('#vwimg').naturalWidth,
                nh:document.querySelector('#vwimg').naturalHeight};
    }""")
    x0 = rect["left"] + rect["width"] * 0.27
    x1 = rect["left"] + rect["width"] * 0.73
    y0 = rect["top"] + rect["height"] * 0.24
    y1 = rect["top"] + rect["height"] * 0.33
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=15)
    loupe_visible = page.evaluate("getComputedStyle(document.querySelector('#vwloupe')).display")
    shot(page, "ui-05-editor-drag.png")
    page.mouse.up()
    box = page.evaluate("""() => ({
        box: VIEW.box,
        inputs: ['vwx0','vwy0','vwx1','vwy1'].map(id => document.getElementById(id).value),
        handles: document.querySelectorAll('#vwbox i').length,
        preview: (() => { const c = document.querySelector('#vwpreview');
            const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
            let n = 0; for (let i=0;i<d.length;i+=4) if (d[i]+d[i+1]+d[i+2] > 30) n++;
            return n; })()
    })""")
    assert loupe_visible == "block", loupe_visible
    assert box["box"] and box["box"]["x1"] - box["box"]["x0"] > 8
    assert all(box["inputs"]), box["inputs"]
    assert box["handles"] == 8, box["handles"]
    assert box["preview"] > 100, box["preview"]
    shot(page, "ui-06-editor-box.png")

    # 6) 方向键微调
    before_x = page.evaluate("VIEW.box.x0")
    page.keyboard.press("ArrowRight")
    after_x = page.evaluate("VIEW.box.x0")
    assert after_x == before_x + 1, (before_x, after_x)
    page.keyboard.press("Shift+ArrowDown")
    assert page.evaluate("VIEW.box.y0") == box["box"]["y0"] + 10

    # 7) 坐标输入回写
    page.fill("#vwx0", str(int(box["box"]["x0"]) - 5))
    page.dispatch_event("#vwx0", "change")
    assert page.evaluate("VIEW.box.x0") == int(box["box"]["x0"]) - 5

    # 8) 200% 后框随图缩放
    page.click('#viewer [data-vw="2"]')
    assert page.text_content("#vwpct") == "200%"
    assert page.evaluate("document.querySelector('#vwcanvas').style.width") == "4096px"
    shot(page, "ui-07-editor-200.png")

    # 9) 拖右下角手柄放大框
    page.click('#viewer [data-vw="fit"]')
    r = page.evaluate("""() => { const b = document.querySelector('#vwbox').getBoundingClientRect();
        return {right:b.right, bottom:b.bottom}; }""")
    before = page.evaluate("VIEW.box")
    page.mouse.move(r["right"], r["bottom"])
    page.mouse.down()
    page.mouse.move(r["right"] + 40, r["bottom"] + 30, steps=8)
    page.mouse.up()
    after = page.evaluate("VIEW.box")
    assert after["x1"] > before["x1"] and after["y1"] > before["y1"], (before, after)
    shot(page, "ui-08-editor-resized.png")

    # 10) 提交前拦截接口，验证 payload（不调用真实生图）
    captured = {}

    def handle_local_edit(route):
        captured.update(route.request.post_data_json or {})
        route.fulfill(status=200, content_type="application/json",
                      body='{"ok":true,"job_id":"fake-ui-test"}')

    page.route("**/api/section/S001/pages/1/local-edit", handle_local_edit)
    page.fill("#vwtext", "测试")
    page.fill("#vwnote", "ui smoke")
    page.click("#vwsubmit")
    page.wait_for_selector("#viewer", state="hidden", timeout=8000)
    assert captured.get("text") == "测试", captured
    assert len(captured.get("box") or []) == 4, captured
    assert captured.get("base_version") == 1, captured
    assert captured.get("expected_source_hash"), captured

    # 11) 退出大图
    page.click("#bigview")
    assert not page.evaluate("document.body.classList.contains('bigview')")

    print(json.dumps({
        "ok": True,
        "fit_pct": fit_pct,
        "box": box,
        "console_errors": errors,
    }, ensure_ascii=False, indent=2))
    browser.close()
