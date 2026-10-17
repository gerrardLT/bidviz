#!/usr/bin/env python3
"""施工组织设计图册 · 文档编辑器后端。

把项目当作一份可编辑的文档来提供：
  GET  /api/doc                     整份文档结构（章 → 节 → 正文块 + 本节规划的 1~N 张图）
  POST /api/section/<sid>           保存某节的编辑（标题 / 正文 / 每张图的提示词）
  POST /api/section/<sid>/regen     重生某节的某一张图（body: {"fig": 2}）
  POST /api/section/<sid>/reset     撤销该节的正文编辑，回到原文
  POST /api/replan                  重新分析内容并规划图位（body: {"section":"S006","force":false}）
  POST /api/rebuild                 按当前编辑重新汇编 PDF
  GET  /api/status                  生成进度 / 图位进度

正文编辑存到 <root>/work/edits.json，提示词存到 <root>/work/figure-plan.json（标 custom），
两者都不污染原文 blocks.json。
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

import bidviz_e1 as e1  # noqa: E402
import bidviz_layout as layout  # noqa: E402
import bidviz_plan as plan  # noqa: E402
import build_e1 as be  # noqa: E402
import generate_chapter as gc  # noqa: E402
from bidviz_config import load as _load_cfg, get as _cfg  # noqa: E402

# 项目目录：命令行第 2 个参数 > 环境变量 BIDVIZ_PROJECT > 默认
_proj = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(os.getenv("BIDVIZ_PROJECT", "projects/anke-35kv"))
ROOT = (_proj if _proj.is_absolute() else BASE / _proj).resolve()
_load_cfg(ROOT)

EDITS = ROOT / "work" / "edits.json"


def _image_dirs() -> list[Path]:
    """主视觉目录候选，由项目配置决定优先级。"""
    return _cfg().image_dirs


def find_image(sid: str, no: int = 1):
    name = plan.figure_filename(sid, no)
    for d in _image_dirs():
        f = d / name
        if f.exists() and f.stat().st_size > 2000:
            return f
    return None

_state = {"running": False, "section": "", "fig": 0, "done": 0, "total": 0, "msg": ""}
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


def facts_of(vis) -> dict:
    """规划依据：只统计原文里真出现的内容，供前端核对图里有没有编造。"""
    try:
        f = plan.analyze(vis)
    except Exception:
        return {}
    return {"items": f.items, "proc": f.proc, "param": f.param, "risk": f.risk,
            "mat": f.mat, "time": f.time, "org": f.org, "safe": f.safe, "qa": f.qa,
            "layout": f.layout, "style": f.style,
            "steps": f.steps, "params": f.param_sents[:8],
            "risks": f.risk_sents[:6], "mats": f.mat_names[:12]}


def figure_payload(sid: str, figs: list[dict]) -> list[dict]:
    imgs = plan.resolve_images(ROOT, sid, figs)
    out = []
    for f in figs:
        no = int(f.get("no", 1))
        img = imgs.get(no)
        g = f.get("gen") or {}
        out.append({
            "no": no,
            "kind": f.get("kind"),
            "name": f.get("name"),
            "why": f.get("why"),
            "prompt": f.get("prompt", ""),
            "custom": bool(f.get("custom")),
            "image": f"/api/image/{sid}/{no}" if img else None,
            "image_ts": int(img.stat().st_mtime) if img else 0,
            "gen": " / ".join(str(g.get(k, "")) for k in ("provider", "model", "size")),
        })
    return out


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
    plan.ensure_plan(ROOT)          # 正文改过的节自动重算图位与提示词
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
            figs = plan.section_figures(ROOT, sid, vis)
            sects.append({
                "id": sid,
                "title": ed.get("title", vis.title),
                "chars": sum(len(b["text"]) for b in blocks),
                "blocks": blocks,
                "edited": bool(ed),
                "figures": figure_payload(sid, figs),
                "facts": facts_of(vis),
                "dims": layout.dim_profile(vis),
            })
        out.append({"name": cname, "sections": sects})
    return {"chapters": out, "edits": len(edits), "max_figures": _cfg().max_figures,
            "progress": gc.figure_progress(ROOT)}


# ---------------------------------------------------------------- 动作

def save_prompts(sid: str, prompts: dict) -> int:
    """把网页上改过的提示词写回 figure-plan.json 并标 custom，重算时不被覆盖。"""
    p = plan.load_plan(ROOT)
    entry = ((p or {}).get("sections") or {}).get(sid)
    if not entry:
        return 0
    n = 0
    for f in entry.get("figures", []):
        txt = prompts.get(str(f.get("no")))
        if isinstance(txt, str) and txt.strip() and txt.strip() != str(f.get("prompt", "")).strip():
            f["prompt"], f["custom"] = txt.strip(), True
            n += 1
    if n:
        plan.save_plan(ROOT, p)
    return n


def do_regen(sid: str, no: int = 1) -> None:
    """重生某节的第 no 张图：提示词与生图参数全部取自图位规划。"""
    _, secs = chapter_map()
    if sid not in secs:
        with _lock:
            _state.update(msg=f"{sid} 不存在")
        return
    plan.ensure_plan(ROOT, only=[sid])
    figs = plan.section_figures(ROOT, sid, secs[sid].as_visual_item())
    fig = next((f for f in figs if int(f.get("no", 1)) == no), None)
    if fig is None:
        with _lock:
            _state.update(msg=f"{sid} 没有图 {no}")
        return
    g = fig.get("gen") or plan.gen_config(no, fig.get("kind") or plan.DEFAULT_KIND)
    with _lock:
        _state.update(running=True, section=sid, fig=no, msg=f"生成 {sid} 图{no} …")
    rec = {"section_id": sid, "no": no, "kind": fig.get("kind"), "name": fig.get("name"),
           "provider": g["provider"], "model": g["model"], "size": g["size"],
           "custom": bool(fig.get("custom")), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    t0 = time.time()
    try:
        api = plan.make_api({**fig, "gen": g})
        out = _cfg().image_dir / plan.figure_filename(sid, no)
        out.parent.mkdir(parents=True, exist_ok=True)
        api.generate(fig.get("prompt", ""), out, recorder=rec, size=g["size"])
        rec.update({"ok": True, "attempts": 1, "sec": round(time.time() - t0, 1),
                    "kb": round(out.stat().st_size / 1024)})
        msg = f"{sid} 图{no} 完成"
    except Exception as exc:
        rec.update({"ok": False, "attempts": 1, "sec": round(time.time() - t0, 1),
                    "error": str(exc)[:200]})
        msg = f"{sid} 图{no} 失败: {str(exc)[:160]}"
    led = gc.load_ledger(ROOT)
    led[f"{sid}#{no}"] = rec
    gc.save_ledger(ROOT, led)
    with _lock:
        _state.update(msg=msg, running=False)


def do_replan(section: str | None, force: bool) -> None:
    only = [x.strip() for x in (section or "").split(",") if x.strip()] or None
    with _lock:
        _state.update(running=True, msg="重新分析内容并规划图位 …")
    try:
        p = plan.ensure_plan(ROOT, force=force, only=only)
        s = plan.plan_summary(p)
        msg = f"规划完成：{s['sections']} 节 / {s['figures']} 张图（重算 {p.get('changed', 0)} 节）"
    except Exception as exc:
        traceback.print_exc()
        msg = f"规划失败: {str(exc)[:160]}"
    finally:
        with _lock:
            _state.update(msg=msg, running=False)


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
                st = dict(_state)
            st["progress"] = gc.figure_progress(ROOT)
            return self._json(st)
        m = re.fullmatch(r"/api/image/(S\d+)(?:/(\d+))?", p)
        if m:
            image = find_image(m.group(1), int(m.group(2) or 1))
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

            if p == "/api/replan":
                body = self._body()
                threading.Thread(target=do_replan,
                                 args=(body.get("section"), bool(body.get("force"))),
                                 daemon=True).start()
                return self._json({"ok": True})

            m = re.match(r"^/api/section/(\w+)(?:/(\w+))?$", p)
            if m:
                sid, action = m.group(1), m.group(2)
                if action == "regen":
                    body = self._body()
                    threading.Thread(target=do_regen,
                                     args=(sid, int(body.get("fig") or 1)), daemon=True).start()
                    return self._json({"ok": True})

                edits = load_edits()
                if action == "reset":
                    edits.pop(sid, None)
                    save_edits(edits)
                    return self._json({"ok": True})

                body = self._body()
                fields = []
                if "title" in body or "body" in body:
                    cur = edits.setdefault(sid, {})
                    for k in ("title", "body"):
                        if k in body:
                            cur[k] = body[k]
                            fields.append(k)
                    save_edits(edits)
                prompts = body.get("prompts")
                if isinstance(prompts, dict) and prompts:
                    n = save_prompts(sid, {str(k): v for k, v in prompts.items()})
                    fields.append(f"prompts×{n}")
                return self._json({"ok": True, "saved": sid, "fields": fields})

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
