#!/usr/bin/env python3
"""施工组织设计图册 · 文档编辑器后端。

把项目当作一份可编辑的文档来提供：
  GET  /api/doc                     整份文档结构（章 → 节 → 正文块 + 图片）
  POST /api/section/<sid>           保存某节的编辑（标题 / 提示词 / 正文块）
  POST /api/section/<sid>/regen     重新生成某节的主视觉
  POST /api/section/<sid>/reset     撤销该节的编辑，回到原文
  POST /api/rebuild                 按当前编辑重新汇编 PDF
  GET  /api/status                  生成进度 / 运行状态

编辑内容存到 <root>/work/edits.json，不污染原文。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent.parent        # 代码库根（web/ 的上一级）
sys.path.insert(0, str(BASE / "src"))

import bidviz  # noqa: E402
import bidviz_e1 as e1  # noqa: E402
import build_e1 as be  # noqa: E402
from bidviz_config import load as _load_cfg, get as _cfg  # noqa: E402

# 项目目录：命令行第 2 个参数 > 环境变量 BIDVIZ_PROJECT > 默认
_proj = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(os.getenv("BIDVIZ_PROJECT", "projects/anke-35kv"))
ROOT = (_proj if _proj.is_absolute() else BASE / _proj).resolve()
_load_cfg(ROOT)

EDITS = ROOT / "work" / "edits.json"


def _image_dirs() -> list[Path]:
    """主视觉目录候选，由项目配置决定优先级。"""
    return _cfg().image_dirs


def find_image(sid: str):
    for d in _image_dirs():
        f = d / f"{sid}.png"
        if f.exists() and f.stat().st_size > 2000:
            return f
    return None

_state = {"running": False, "section": "", "done": 0, "total": 0, "msg": ""}
_lock = threading.Lock()


# ---------------------------------------------------------------- 数据

def load_edits() -> dict:
    if EDITS.exists():
        try:
            return json.loads(EDITS.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_edits(d: dict) -> None:
    EDITS.parent.mkdir(parents=True, exist_ok=True)
    EDITS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt(vis) -> str:
    facts = "；".join(b.text for b in vis.blocks[:12] if b.kind != "heading")[:1400]
    return "\n".join([
        f"为{_cfg().prompt_subject}生成：轴测爆炸分解图，构件按施工层级拆开悬浮，表现分层工艺与装配关系。",
        f"本页主题：{vis.title}。",
        f"必须依据以下实际文字内容，不得添加冲突的工艺与参数：{facts}",
        "视觉标准：专业工程技术图，尺度感真实，构件边缘清楚，灰蓝主色，安全风险用红橙。",
        "需要在图中直接生成简短中文构件名与实际情况引线标注；不得生成长段正文，不得虚构未给出的精确尺寸。",
        "所有标注使用规范简体中文；若无法正确生成文字则省略该标签，不得产生乱码、错别字、Logo 或水印。",
        "无 CAD、BIM 模型或现场照片，本图为文字约束的通用工程示意，不得暗示真实坐标。",
        "A3 横向构图，主体居中，四周留出自然空白。",
    ])


def chapter_map():
    """{章名: [section_id, ...]} 与 {section_id: Section}"""
    blocks, _ = be.load_project(ROOT)
    marks = []
    for i, b in enumerate(blocks):
        t = getattr(b, "text", "").strip()
        if re.match(r"^第[一二三四五六七八九十]+章", t):
            marks.append((i, t[:40]))
    sections = e1.group_sections(blocks)
    pos_of = {id(b): i for i, b in enumerate(blocks)}

    chapters: dict[str, list[str]] = {}
    for s in sections:
        bi = pos_of.get(id(s.blocks[0]), 0) if s.blocks else 0
        cur = "(无章)"
        for mi, mt in marks:
            if mi <= bi:
                cur = mt
            else:
                break
        chapters.setdefault(cur, []).append(s.section_id)
    be.apply_edits(ROOT, sections)
    return chapters, {s.section_id: s for s in sections}


def doc_payload() -> dict:
    chapters, secs = chapter_map()
    edits = load_edits()
    out = []
    for cname, ids in chapters.items():
        sects = []
        for sid in ids:
            s = secs[sid]
            vis = s.as_visual_item()
            ed = edits.get(sid, {})
            blocks = [
                {"kind": getattr(b, "kind", "para"), "text": getattr(b, "text", "")}
                for b in s.blocks
            ]
            img = find_image(sid)
            sects.append({
                "id": sid,
                "title": ed.get("title", vis.title),
                "chars": sum(len(b["text"]) for b in blocks),
                "image": f"/api/image/{sid}" if img else None,
                "image_ts": int(img.stat().st_mtime) if img else 0,
                "prompt": ed.get("prompt", build_prompt(vis)),
                "blocks": blocks,
                "edited": bool(ed),
                "params": [list(x) for x in e1.extract_params(vis)],
                "risks": e1.extract_risks(vis),
                "steps": [list(x) for x in e1.extract_steps(vis)],
                "itp": [list(x) for x in e1.extract_itp(vis)],
            })
        out.append({"name": cname, "sections": sects})
    return {"chapters": out, "edits": len(edits)}


# ---------------------------------------------------------------- 动作

def do_regen(sid: str, model: str, size: str) -> None:
    _, secs = chapter_map()
    if sid not in secs:
        return
    vis = secs[sid].as_visual_item()
    edits = load_edits()
    prompt = edits.get(sid, {}).get("prompt") or build_prompt(vis)
    with _lock:
        _state.update(running=True, section=sid, msg=f"生成 {sid} …")
    try:
        api = bidviz.ImageAPI(provider_name=_cfg().provider, model=model)
        out = _cfg().image_dir / f"{sid}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        api.generate(prompt, out, size=size)
        with _lock:
            _state.update(msg=f"{sid} 完成")
    except Exception as exc:
        with _lock:
            _state.update(msg=f"{sid} 失败: {str(exc)[:160]}")
    finally:
        with _lock:
            _state["running"] = False


def do_rebuild() -> None:
    with _lock:
        _state.update(running=True, msg="汇编 PDF …")
    try:
        _cfg().output.mkdir(parents=True, exist_ok=True)
        out_pdf = _cfg().output / "施工组织设计-图册-A3.pdf"
        pages, _ = be.build_final(ROOT, out_pdf)
        with _lock:
            _state.update(msg=f"PDF 已重建（{pages} 页）")
    except Exception as exc:
        traceback.print_exc()
        with _lock:
            _state.update(msg=f"重建失败: {str(exc)[:160]}")
    finally:
        with _lock:
            _state["running"] = False


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(BASE / "web" / "static"), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/doc":
            return self._json(doc_payload())
        if p == "/api/status":
            with _lock:
                return self._json(dict(_state))
        m = re.fullmatch(r"/api/image/(S\d+)", p)
        if m:
            image = find_image(m.group(1))
            if not image:
                return self.send_error(404)
            body = image.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return self.wfile.write(body)
        if p == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            if p == "/api/rebuild":
                threading.Thread(target=do_rebuild, daemon=True).start()
                return self._json({"ok": True})

            m = re.match(r"^/api/section/(\w+)(?:/(\w+))?$", p)
            if m:
                sid, action = m.group(1), m.group(2)
                if action == "regen":
                    body = self._body()
                    threading.Thread(
                        target=do_regen,
                        args=(sid, body.get("model", _cfg().model), body.get("size", _cfg().image_size)),
                        daemon=True).start()
                    return self._json({"ok": True})

                edits = load_edits()
                if action == "reset":
                    edits.pop(sid, None)
                    save_edits(edits)
                    return self._json({"ok": True})

                body = self._body()
                cur = edits.setdefault(sid, {})
                for k in ("title", "prompt", "body"):
                    if k in body:
                        cur[k] = body[k]
                save_edits(edits)
                return self._json({"ok": True, "saved": sid})

            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            traceback.print_exc()
            return self._json({"error": str(exc)}, 500)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8770
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"施工组织设计编辑器: http://localhost:{port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
