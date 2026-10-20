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

import hashlib
import json
import os
import re
import shutil
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
import bidviz_fullimage as fullimage  # noqa: E402
import bidviz_fullimage_audit as fullaudit  # noqa: E402
try:
    import bidviz_fullimage_store as fullstore  # noqa: E402
except ImportError:  # 模块由 W0 阶段提供；缺失时 fullimage API 返回明确错误
    fullstore = None
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


def fullimage_mode() -> bool:
    return _cfg().layout_engine == "fullimage"


def _require_store():
    if fullstore is None:
        raise RuntimeError("bidviz_fullimage_store 未安装")
    return fullstore


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fullimage_sections():
    chapters, secs = chapter_map()
    return chapters, secs


def _fullimage_page_context(sid: str, page_no: int, secs: dict | None = None) -> dict:
    """构建页面提示词/哈希/原文上下文；正文 edits 已在 chapter_map 中投影。"""
    store = _require_store()
    if secs is None:
        _chapters, secs = _fullimage_sections()
    sec = secs.get(sid)
    if sec is None:
        raise KeyError(f"节不存在：{sid}")
    blocks = [(getattr(b, "kind", "body"), (getattr(b, "text", "") or "").strip())
              for b in sec.blocks if (getattr(b, "text", "") or "").strip() and
              (getattr(b, "text", "") or "").strip() != sec.title]
    pages = fullimage.split_pages(blocks)
    if page_no < 1 or page_no > len(pages):
        raise IndexError(f"{sid} 无第 {page_no} 页（共 {len(pages)} 页）")
    built = fullimage.build_fullpage_prompt(ROOT, sid, page_no, len(pages), pages[page_no - 1])
    source_text = "\n".join(x[1] for x in pages[page_no - 1])
    source_hash = store.sha256_text(sec.title + "\n" + source_text)
    template_hash = store.sha256_text(fullimage.PROMPT_TEMPLATE.read_text(encoding="utf-8"))
    ov = store.get_prompt_override(ROOT, sid, page_no, built["prompt"], source_hash)
    chapter = (sec.chapter or "").split(" / ")[0]
    return {"sid": sid, "page": page_no, "total_pages": len(pages), "section": sec,
            "chapter": chapter,
            "page_blocks": pages[page_no - 1], "built": built,
            "auto_prompt": built["prompt"], "resolved_prompt": ov["resolved_prompt"],
            "custom": ov["custom"], "stale": ov["stale"], "source_hash": source_hash,
            "template_hash": template_hash, "prompt_hash": store.sha256_text(ov["resolved_prompt"]),
            "source_text": source_text}


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


def _fullimage_payload(sid: str, secs: dict) -> list[dict]:
    store = _require_store()
    total = len(fullimage.split_pages([(getattr(b, "kind", "body"), getattr(b, "text", ""))
                                       for b in secs[sid].blocks
                                       if getattr(b, "text", "").strip() and
                                       getattr(b, "text", "").strip() != secs[sid].title]))
    pages = []
    for page_no in range(1, total + 1):
        ctx = _fullimage_page_context(sid, page_no, secs)
        state = store.ensure_page(ROOT, sid, page_no, ctx["source_hash"],
                                  ctx["template_hash"], ctx["prompt_hash"])
        published = (state.get("published") or {}).get("current")
        candidates = store.list_candidates(ROOT, sid, page_no)
        latest_meta = candidates[-1] if candidates else None
        latest = int(latest_meta["version"]) if latest_meta else None
        latest_label = f"v{latest:04d}" if latest else None
        published_path = store.published_path(ROOT, sid, page_no)
        pages.append({
            "no": page_no, "page": page_no, "kind": "fullimage",
            "name": f"第 {page_no} 页整页大样", "why": f"本节共 {total} 页",
            "prompt": ctx["resolved_prompt"], "auto_prompt": ctx["auto_prompt"],
            "custom": ctx["custom"], "stale": ctx["stale"] or bool(state.get("stale")),
            "source_hash": ctx["source_hash"], "prompt_hash": ctx["prompt_hash"],
            "image": f"/api/fullimage/published/{sid}/{page_no}" if published_path.exists() else None,
            "image_ts": int(published_path.stat().st_mtime) if published_path.exists() else 0,
            "candidate_image": (f"/api/fullimage/candidate/{sid}/{page_no}/{latest_label}"
                                if latest else None),
            "candidate_ts": int(latest_meta.get("created_ts", 0)) if latest_meta else 0,
            "candidate": latest_meta, "published": published,
            "status": ("published" if published else
                       (latest_meta.get("status", "pending") if latest_meta else "missing")),
            "gen": f"{_cfg().provider} / {_cfg().model} / {_cfg().image_size}",
        })
    return pages


def doc_payload() -> dict:
    chapters, secs = chapter_map()
    edits = load_edits()
    mode = "fullimage" if fullimage_mode() else "legacy"
    if mode == "legacy":
        plan.ensure_plan(ROOT)
    out = []
    expected = []
    for cname, ids in chapters.items():
        sects = []
        for sid in ids:
            s = secs[sid]
            vis = s.as_visual_item()
            ed = edits.get(sid, {})
            blocks = [{"kind": getattr(b, "kind", "para"), "text": getattr(b, "text", "")}
                      for b in s.blocks]
            figs = _fullimage_payload(sid, secs) if mode == "fullimage" else \
                figure_payload(sid, plan.section_figures(ROOT, sid, vis))
            if mode == "fullimage":
                expected.extend((sid, f["page"]) for f in figs)
            sects.append({"id": sid, "title": ed.get("title", vis.title),
                          "chars": sum(len(b["text"]) for b in blocks), "blocks": blocks,
                          "edited": bool(ed), "figures": figs, "facts": facts_of(vis),
                          "dims": layout.dim_profile(vis)})
        out.append({"name": cname, "sections": sects})
    gates = _require_store().gate_summary(ROOT, expected) if mode == "fullimage" else None
    return {"chapters": out, "edits": len(edits), "max_figures": _cfg().max_figures,
            "mode": mode, "engine": _cfg().layout_engine,
            "provider": _cfg().provider, "model": _cfg().model, "size": _cfg().image_size,
            "gates": gates,
            "progress": gc.figure_progress(ROOT) if mode == "legacy" else gates}


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


def _vision_ocr(path: Path) -> list[str] | None:
    try:
        import bidviz_v2
        return bidviz_v2.vision_transcribe(path)
    except Exception:
        return None


def do_fullimage_generate(job_id: str, sid: str, page_no: int,
                          expected_source_hash: str, prompt: str) -> None:
    store = _require_store()
    try:
        _chapters, secs = _fullimage_sections()
        ctx = _fullimage_page_context(sid, page_no, secs)
        if ctx["source_hash"] != expected_source_hash:
            raise RuntimeError("原文已变化，请刷新页面后重新提交")
        version = store.next_candidate_version(ROOT, sid, page_no)
        candidate = store.candidate_path(ROOT, sid, page_no, version)
        store.update_job(ROOT, job_id, status="generating", version=version,
                         msg=f"生成 {sid} 第{page_no}页候选 {version} …")
        with _lock:
            _state.update(running=True, section=sid, fig=page_no,
                          msg=f"生成候选：{sid} 第{page_no}页 {version} …")
        rec = fullimage.generate_page(ROOT, sid, page_no, ctx["total_pages"],
                                      ctx["page_blocks"], force=True,
                                      prompt_override=prompt, output_path=candidate)
        if not rec.get("ok"):
            raise RuntimeError(rec.get("error") or "生图失败")
        ocr = _vision_ocr(candidate)
        audit = fullaudit.audit_candidate(candidate, chapter=ctx["chapter"],
                                          section=ctx["section"].title,
                                          source_lines=[x[1] for x in ctx["page_blocks"]],
                                          ocr_texts=ocr,
                                          reference_count=int(rec.get("reference_count", 1)))
        status = "blocked" if audit["status"] == "block" else "review"
        metadata = {"sid": sid, "page": page_no, "version": version, "status": status,
                    "source_hash": ctx["source_hash"], "template_hash": ctx["template_hash"],
                    "prompt_hash": store.sha256_text(prompt), "prompt": prompt,
                    "provider": rec.get("provider"), "model": rec.get("model"),
                    "requested_size": rec.get("requested"), "actual_size": rec.get("actual"),
                    "reference_count": rec.get("reference_count", 1),
                    "sec": rec.get("sec"), "kb": rec.get("kb"), "checks": audit,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "created_ts": time.time(), "decision": None}
        store.register_candidate(ROOT, metadata)
        store.update_job(ROOT, job_id, status=status, version=version,
                         msg=f"候选 {version} 已生成，门禁：{audit['status']}", result=metadata)
        msg = f"{sid} 第{page_no}页候选完成（{audit['status']}）"
    except Exception as exc:
        traceback.print_exc()
        store.update_job(ROOT, job_id, status="failed", error=str(exc)[:300],
                         msg=f"生成失败：{str(exc)[:150]}")
        msg = f"{sid} 第{page_no}页生成失败: {str(exc)[:120]}"
    finally:
        with _lock:
            _state.update(running=False, msg=msg)


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


def _expected_fullimage_pages() -> list[tuple[str, int]]:
    chapters, secs = _fullimage_sections()
    return [(sid, page_no) for ids in chapters.values() for sid in ids
            for page_no in range(1, len(fullimage.split_pages([
                (getattr(b, "kind", "body"), getattr(b, "text", "")) for b in secs[sid].blocks
                if getattr(b, "text", "").strip() and getattr(b, "text", "").strip() != secs[sid].title])) + 1)]


def do_rebuild(draft: bool = False) -> None:
    with _lock:
        _state.update(running=True, msg="汇编 PDF …")
    try:
        _cfg().output.mkdir(parents=True, exist_ok=True)
        if fullimage_mode():
            store = _require_store()
            expected = _expected_fullimage_pages()
            summary = store.gate_summary(ROOT, expected)
            if not draft and not summary.get("can_build_final"):
                raise RuntimeError("正式汇编被门禁阻断：仍有 pending/stale/missing/blocked 页面")
            sids = []
            for sid, _page in expected:
                if sid not in sids:
                    sids.append(sid)
            if draft:
                usable = summary.get("published", 0) + summary.get("pending", 0) + summary.get("blocked", 0)
                if usable <= 0:
                    raise RuntimeError("DRAFT 汇编失败：尚无已发布或候选页面")
                out_pdf = _cfg().output / "施工组织设计-图册-A3-fullimage-DRAFT.pdf"
                def resolver(sid, page):
                    published = store.published_path(ROOT, sid, page)
                    if published.exists():
                        return published
                    candidates = store.list_candidates(ROOT, sid, page)
                    if candidates:
                        return store.candidate_path(ROOT, sid, page, candidates[-1]["version"])
                    return None
            else:
                out_pdf = _cfg().output / "施工组织设计-图册-A3-fullimage.pdf"
                resolver = lambda sid, page: store.published_path(ROOT, sid, page)
            fullimage.build_fullimage_pdf(ROOT, sids, out_pdf, image_resolver=resolver, draft=draft)
            pages = len(expected)
        else:
            out_pdf = _cfg().output / "施工组织设计-图册-A3.pdf"
            pages, _ = be.build_final(ROOT, out_pdf)
        with _lock:
            _state.update(msg=f"PDF 已重建（{pages} 页）", pdf=f"/api/pdf{'?draft=1' if draft else ''}")
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

    def _file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            return self.send_error(404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/doc":
            return self._json(doc_payload())
        if p == "/api/status":
            with _lock:
                st = dict(_state)
            st["progress"] = (None if fullimage_mode() else gc.figure_progress(ROOT))
            return self._json(st)
        m = re.fullmatch(r"/api/jobs/([\w-]+)", p)
        if m and fullimage_mode():
            job = _require_store().get_job(ROOT, m.group(1))
            return self._json(job or {"error": "job not found"}, 200 if job else 404)
        m = re.fullmatch(r"/api/fullimage/published/(S\d+)/(\d+)", p)
        if m:
            return self._file(_require_store().published_path(ROOT, m.group(1), int(m.group(2))), "image/png")
        m = re.fullmatch(r"/api/fullimage/candidate/(S\d+)/(\d+)/(v\d+)", p)
        if m:
            version = int(m.group(3)[1:])
            return self._file(_require_store().candidate_path(ROOT, m.group(1), int(m.group(2)), version), "image/png")
        if p == "/api/pdf":
            from urllib.parse import parse_qs
            draft = bool(parse_qs(urlparse(self.path).query).get("draft"))
            pdf = _cfg().output / ("施工组织设计-图册-A3-fullimage-DRAFT.pdf" if draft else
                                   "施工组织设计-图册-A3-fullimage.pdf")
            return self._file(pdf, "application/pdf")
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
                body = self._body()
                threading.Thread(target=do_rebuild, args=(bool(body.get("draft")),), daemon=True).start()
                return self._json({"ok": True})

            if p == "/api/replan":
                body = self._body()
                threading.Thread(target=do_replan,
                                 args=(body.get("section"), bool(body.get("force"))),
                                 daemon=True).start()
                return self._json({"ok": True})

            m = re.fullmatch(r"/api/section/(S\d+)/pages/(\d+)/(generate|prompt-reset|approve|reject|waive|rollback)", p)
            if m and fullimage_mode():
                sid, page_no, action = m.group(1), int(m.group(2)), m.group(3)
                body = self._body()
                store = _require_store()
                if action == "generate":
                    ctx = _fullimage_page_context(sid, page_no)
                    expected = str(body.get("expected_source_hash") or "")
                    if expected != ctx["source_hash"]:
                        return self._json({"error": "原文已变化，请刷新后重试"}, 409)
                    prompt = str(body.get("prompt") or ctx["resolved_prompt"]).strip()
                    job = store.create_job(ROOT, sid=sid, page=page_no)
                    threading.Thread(target=do_fullimage_generate,
                                     args=(job["job_id"], sid, page_no, expected, prompt),
                                     daemon=True).start()
                    return self._json({"ok": True, "job_id": job["job_id"]})
                if action == "prompt-reset":
                    store.reset_prompt_override(ROOT, sid, page_no)
                    return self._json({"ok": True})
                version = str(body.get("version") or "")
                if action in ("approve", "reject", "waive"):
                    if not version:
                        return self._json({"error": "缺少候选版本"}, 400)
                    version_no = int(version[1:]) if version.startswith("v") else int(version)
                    meta = store.record_decision(ROOT, sid, page_no, version_no, action,
                                                 str(body.get("decided_by") or "web-user"),
                                                 str(body.get("comment") or ""))
                    if action in ("approve", "waive"):
                        store.publish_candidate(ROOT, sid, page_no, version_no)
                    return self._json({"ok": True, "candidate": meta})
                if action == "rollback":
                    return self._json({"ok": True, "page": store.rollback_published(ROOT, sid, page_no)})

            m = re.match(r"^/api/section/(\w+)(?:/(\w+))?$", p)
            if m:
                sid, action = m.group(1), m.group(2)
                if action == "regen":
                    body = self._body()
                    if fullimage_mode():
                        page_no = int(body.get("page") or body.get("fig") or 1)
                        ctx = _fullimage_page_context(sid, page_no)
                        prompt = str(body.get("prompt") or ctx["resolved_prompt"])
                        job = _require_store().create_job(ROOT, sid, page_no)
                        threading.Thread(target=do_fullimage_generate,
                                         args=(job["job_id"], sid, page_no, ctx["source_hash"], prompt),
                                         daemon=True).start()
                        return self._json({"ok": True, "job_id": job["job_id"]})
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
                    if fullimage_mode():
                        n = 0
                        for raw_page, text in prompts.items():
                            page_no = int(raw_page)
                            if not isinstance(text, str) or not text.strip():
                                continue
                            ctx = _fullimage_page_context(sid, page_no)
                            _require_store().set_prompt_override(ROOT, sid, page_no, text.strip(),
                                                                 ctx["source_hash"])
                            n += 1
                    else:
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
