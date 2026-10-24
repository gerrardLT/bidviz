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
import bidviz_local_edit as localedit  # noqa: E402
import bidviz_memory as memory  # noqa: E402
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

# 同一页的生成/改字必须串行：候选版本号在生图前分配，并发会写同一个 vNNNN.png。
_PAGE_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_PAGE_LOCKS_GUARD = threading.Lock()


def _page_lock(sid: str, page_no: int) -> threading.Lock:
    key = (sid, int(page_no))
    with _PAGE_LOCKS_GUARD:
        return _PAGE_LOCKS.setdefault(key, threading.Lock())


# 全局长任务互斥：rebuild / replan / illustrations 会重排或重写共享文件，
# 同一时刻只允许一个；非阻塞获取失败 → 409。锁由任务线程结束时释放。
_TASK_LOCK = threading.Lock()
# edits.json / figure-plan.json 的读-改-写串行化，防止跨请求丢失更新
_EDITS_LOCK = threading.Lock()
_PLAN_LOCK = threading.Lock()


def _run_exclusive(fn, *args, **kwargs):
    """在后台线程里执行互斥任务，无论成败都释放 _TASK_LOCK。"""
    try:
        fn(*args, **kwargs)
    finally:
        _TASK_LOCK.release()


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
RUNTIME_ENGINE = ROOT / "work" / "runtime-engine.json"
_VALID_ENGINES = {"legacy", "v13", "fullimage", "html"}


def current_engine() -> str:
    """运行时引擎：work/runtime-engine.json 优先，否则读 bidviz.toml。"""
    if RUNTIME_ENGINE.exists():
        try:
            value = json.loads(RUNTIME_ENGINE.read_text(encoding="utf-8")).get("engine")
            if isinstance(value, str) and value.strip().lower() in _VALID_ENGINES:
                return value.strip().lower()
        except (OSError, json.JSONDecodeError):
            pass
    return _cfg().layout_engine


def set_current_engine(engine: str) -> str:
    value = str(engine or "").strip().lower()
    if value not in _VALID_ENGINES:
        raise ValueError(f"不支持的引擎：{engine}")
    RUNTIME_ENGINE.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_ENGINE.write_text(json.dumps({"engine": value}, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8")
    return value


def fullimage_mode() -> bool:
    return current_engine() == "fullimage"


def html_mode() -> bool:
    return current_engine() == "html"


def _require_store():
    if fullstore is None:
        raise RuntimeError("bidviz_fullimage_store 未安装")
    return fullstore


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _memory_roots() -> list[Path]:
    return [BASE / "memory", ROOT / "memory"]


class _NullRun:
    run_id = ""
    def record(self, *_args, **_kwargs):
        return {}
    event = record
    def close(self, *_args, **_kwargs):
        return {}


def _new_run(operation: str, **scope):
    """Create best-effort telemetry; recording failure must never change the action result."""
    run_id = time.strftime("run-%Y%m%d-%H%M%S-") + hashlib.sha256(
        f"{operation}:{time.time_ns()}".encode()).hexdigest()[:8]
    try:
        return memory.RunRecorder(
            ROOT / "work" / "runs" / run_id, run_id=run_id,
            metadata={"project": ROOT.name, "command": "web", "operation": operation,
                      "scope": {"project": ROOT.name, "engine": current_engine(),
                                "operation": operation, **scope}},
        )
    except Exception as exc:
        print(f"[WARN] 运行事件记录不可用：{exc}", file=sys.stderr)
        return _NullRun()


def _record(run, event: str, **fields) -> None:
    try:
        run.record(event, **fields)
    except Exception:
        traceback.print_exc()


def _close_run(run, status: str, **fields) -> None:
    try:
        run.close(status, **fields)
    except Exception:
        traceback.print_exc()


def _artifact(path: Path) -> dict:
    try:
        return {"path": path.relative_to(ROOT).as_posix(),
                "sha256": memory.sha256_file(path)}
    except Exception as exc:
        return {"path": str(path), "hash_error": str(exc)[:160]}


_MEM_FINGERPRINTS: dict[Path, tuple] = {}
_MEM_LOCK = threading.Lock()


def _memory_fingerprint(memory_root: Path) -> tuple:
    """记录目录的 mtime/size 指纹：未变化则跳过 FTS 索引全量重建。"""
    entries = []
    for dirpath, _dirs, files in os.walk(memory_root):
        for name in files:
            if name.endswith(".toml"):
                full = Path(dirpath) / name
                try:
                    st = full.stat()
                except OSError:
                    continue
                entries.append((str(full.relative_to(memory_root)).replace("\\", "/"),
                                st.st_mtime_ns, st.st_size))
    return tuple(sorted(entries))


def _memory_records(query: str = "", *, scope: dict | None = None,
                    include_inactive: bool = False, statuses=None, limit: int = 50) -> list[dict]:
    out = []
    # 重建与查询串行：避免两个请求并发 os.replace 同一个 sqlite 索引（Windows PermissionError）
    with _MEM_LOCK:
        for memory_root in _memory_roots():
            if not memory_root.exists():
                continue
            suffix = "global" if memory_root == BASE / "memory" else "project"
            index = ROOT / "work" / f"memory-index-{suffix}.sqlite3"
            fp = _memory_fingerprint(memory_root)
            refresh = fp != _MEM_FINGERPRINTS.get(memory_root) or not index.exists()
            out.extend(memory.search_records(
                memory_root, query, scope=scope, include_inactive=include_inactive,
                statuses=statuses, limit=limit, refresh_index=refresh,
                index_path=index))
            if refresh:
                _MEM_FINGERPRINTS[memory_root] = fp
    out.sort(key=lambda item: (-float(item.get("score", 0)), item["id"]))
    return out[:limit]


def _memory_health() -> dict:
    reports = []
    counts: dict[str, int] = {}
    for root in _memory_roots():
        if not root.exists():
            continue
        report = memory.memory_health(root)
        reports.append({"root": str(root), **report})
        for status, count in report.get("status_counts", {}).items():
            counts[status] = counts.get(status, 0) + int(count)
    return {"ok": all(x.get("ok") for x in reports), "reports": reports,
            "counts": counts,
            "records": sum(int(x.get("records", 0)) for x in reports),
            "conflicts": sum(len(x.get("conflicts", [])) for x in reports)}


def _memory_root_for(record_id: str) -> Path:
    for root in _memory_roots():
        if memory.find_record_path(root, record_id) is not None:
            return root
    raise KeyError(record_id)


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
    ctx = localedit.page_context(ROOT, sid, page_no, sec.title)
    ov = store.get_prompt_override(ROOT, sid, page_no, ctx["auto_prompt"], ctx["source_hash"])
    chapter = (sec.chapter or "").split(" / ")[0]
    return {**ctx, "section": sec, "chapter": chapter,
            "resolved_prompt": ov["resolved_prompt"], "custom": ov["custom"],
            "stale": ov["stale"],
            "prompt_hash": store.sha256_text(ov["resolved_prompt"])}


# ---------------------------------------------------------------- 数据

def load_edits() -> dict:
    if EDITS.exists():
        try:
            return json.loads(EDITS.read_text(encoding="utf-8"))
        except Exception:
            # 损坏不再静默清空：留档 .corrupt-* 供人工恢复，界面按空编辑起步
            backup = EDITS.with_name(EDITS.name + f".corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            try:
                shutil.copy2(EDITS, backup)
                EDITS.unlink(missing_ok=True)
            except OSError:
                pass
            print(f"[WARN] edits.json 损坏，已备份到 {backup.name}", flush=True)
            return {}
    return {}


def save_edits(d: dict) -> None:
    import bidviz_ioutil
    bidviz_ioutil.atomic_write_json(EDITS, d)


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


def html_illustration_progress() -> dict:
    fp = plan.load_plan(ROOT) or {}
    planned = generated = 0
    for sid, entry in (fp.get("sections") or {}).items():
        for f in entry.get("figures", []):
            planned += 1
            no = int(f.get("no", 1))
            path = ROOT / "work" / "illustrations" / plan.figure_filename(sid, no)
            if path.is_file() and path.stat().st_size > 2000:
                generated += 1
    return {"planned": planned, "generated": generated}


def _html_illustration_payload(sid: str) -> list[dict]:
    """figure-plan 的每节 1–2 张局部图 + work/illustrations 落地状态。"""
    fp = plan.load_plan(ROOT) or {}
    entry = ((fp.get("sections") or {}).get(sid) or {})
    out = []
    for f in entry.get("figures", []):
        no = int(f.get("no", 1))
        path = ROOT / "work" / "illustrations" / plan.figure_filename(sid, no)
        exists = path.is_file() and path.stat().st_size > 2000
        g = f.get("gen") or {}
        out.append({
            "no": no, "kind": f.get("kind"), "name": f.get("name"),
            "why": f.get("why"), "ratio": f.get("ratio"),
            "prompt": "",
            "image": f"/api/html/illustration/{sid}/{no}" if exists else None,
            "image_ts": int(path.stat().st_mtime) if exists else 0,
            "status": "generated" if exists else "missing",
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
    mode = current_engine()
    if mode in ("legacy", "html") and plan.load_plan(ROOT) is None:
        with _PLAN_LOCK:
            if plan.load_plan(ROOT) is None:
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
            if mode == "fullimage":
                figs = _fullimage_payload(sid, secs)
                expected.extend((sid, f["page"]) for f in figs)
            elif mode == "html":
                figs = _html_illustration_payload(sid)
            else:
                figs = figure_payload(sid, plan.section_figures(ROOT, sid, vis))
            sects.append({"id": sid, "title": ed.get("title", vis.title),
                          "chars": sum(len(b["text"]) for b in blocks), "blocks": blocks,
                          "edited": bool(ed), "figures": figs, "facts": facts_of(vis),
                          "dims": layout.dim_profile(vis)})
        out.append({"name": cname, "sections": sects})
    gates = _require_store().gate_summary(ROOT, expected) if mode == "fullimage" else None
    html_progress = None
    if mode == "html":
        planned = sum(len(s.get("figures", [])) for c in out for s in c["sections"])
        generated = sum(1 for c in out for s in c["sections"]
                        for f in s.get("figures", []) if f.get("image"))
        html_progress = {"planned": planned, "generated": generated}
    return {"chapters": out, "edits": len(edits), "max_figures": _cfg().max_figures,
            "mode": mode, "engine": current_engine(),
            "provider": _cfg().provider, "model": _cfg().model, "size": _cfg().image_size,
            "gates": gates,
            "progress": (gc.figure_progress(ROOT) if mode == "legacy"
                         else (html_progress if mode == "html" else gates))}


# ---------------------------------------------------------------- 动作

def save_prompts(sid: str, prompts: dict) -> int:
    """把网页上改过的提示词写回 figure-plan.json 并标 custom，重算时不被覆盖。"""
    with _PLAN_LOCK:
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
    finally:
        # 台账写入与状态复位必须在 finally：写失败也不允许永久卡「处理中」
        try:
            led = gc.load_ledger(ROOT)
            led[f"{sid}#{no}"] = rec
            gc.save_ledger(ROOT, led)
        except Exception:
            traceback.print_exc()
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
    with _page_lock(sid, page_no):
        _do_fullimage_generate(job_id, sid, page_no, expected_source_hash, prompt)


def _do_fullimage_generate(job_id: str, sid: str, page_no: int,
                           expected_source_hash: str, prompt: str) -> None:
    store = _require_store()
    run = _new_run("fullimage.generate", section=sid, page=page_no)
    _record(run, "generation.start", scope={"project": ROOT.name, "engine": "fullimage",
               "operation": "generate", "section": sid, "page": page_no},
               source_hash=expected_source_hash, prompt_hash=_hash_text(prompt))
    run_status = "error"
    try:
        store.update_job(ROOT, job_id, run_id=run.run_id)
        _chapters, secs = _fullimage_sections()
        ctx = _fullimage_page_context(sid, page_no, secs)
        if ctx["source_hash"] != expected_source_hash:
            raise RuntimeError("原文已变化，请刷新页面后重新提交")
        version = store.allocate_candidate_version(ROOT, sid, page_no)
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
        _record(run, "candidate.generated", version=version, status=status,
                   artifact=_artifact(candidate),
                   audit={"status": audit.get("status"),
                          "checks": [c.get("code") + ":" + c.get("level", "")
                                     for c in audit.get("checks", [])]})
        run_status = "success"
        msg = f"{sid} 第{page_no}页候选完成（{audit['status']}）"
    except Exception as exc:
        traceback.print_exc()
        store.update_job(ROOT, job_id, status="failed", error=str(exc)[:300],
                         msg=f"生成失败：{str(exc)[:150]}")
        _record(run, "generation.error", error_type=type(exc).__name__, error=str(exc)[:300])
        msg = f"{sid} 第{page_no}页生成失败: {str(exc)[:120]}"
    finally:
        _close_run(run, run_status)
        with _lock:
            _state.update(running=False, msg=msg)


def _local_edit_base(sid: str, page_no: int, base_version: int | None):
    """返回 (基准图路径, 基准元数据, 基准类型)。"""
    store = _require_store()
    rec = store.get_page(ROOT, sid, page_no)
    current = (rec.get("published") or {}).get("current") or {}
    if base_version and int(current.get("version", -1)) == int(base_version):
        path = ROOT / current.get("published_path", "")
        if not path.is_file():
            path = store.published_path(ROOT, sid, page_no)
        if path.is_file():
            return path, current, "published"
    if base_version:
        meta = store.get_candidate(ROOT, sid, page_no, int(base_version))
        if meta is None:
            raise KeyError(f"候选不存在：{sid} p{page_no} v{int(base_version):04d}")
        path = ROOT / meta["path"]
        if not path.is_file():
            raise FileNotFoundError(path)
        return path, meta, "candidate"
    if current:
        path = ROOT / current.get("published_path", "")
        if not path.is_file():
            path = store.published_path(ROOT, sid, page_no)
        if path.is_file():
            return path, current, "published"
    candidates = store.list_candidates(ROOT, sid, page_no)
    if candidates:
        return ROOT / candidates[-1]["path"], candidates[-1], "candidate"
    legacy = ROOT / "work" / "fullimage" / f"{sid}_p{page_no}.png"
    if legacy.is_file():
        return legacy, None, "legacy"
    raise FileNotFoundError(f"{sid} 第{page_no}页没有可编辑的基准图")


def do_fullimage_local_edit(job_id: str, sid: str, page_no: int,
                            expected_source_hash: str, base_version: int | None,
                            box: list[float], text: str, note: str = "") -> None:
    with _page_lock(sid, page_no):
        _do_fullimage_local_edit(job_id, sid, page_no, expected_source_hash,
                                 base_version, box, text, note)


def _do_fullimage_local_edit(job_id: str, sid: str, page_no: int,
                             expected_source_hash: str, base_version: int | None,
                             box: list[float], text: str, note: str = "") -> None:
    store = _require_store()
    run = _new_run("fullimage.local-edit", section=sid, page=page_no)
    _record(run, "local-edit.start", scope={"project": ROOT.name, "engine": "fullimage",
               "operation": "local-edit", "section": sid, "page": page_no},
               source_hash=expected_source_hash, base_version=base_version,
               box=box, replacement_hash=_hash_text(text), note_hash=_hash_text(note))
    msg = ""
    run_status = "error"
    try:
        store.update_job(ROOT, job_id, run_id=run.run_id)
        _chapters, secs = _fullimage_sections()
        ctx = _fullimage_page_context(sid, page_no, secs)
        if ctx["source_hash"] != expected_source_hash:
            raise RuntimeError("原文已变化，请刷新页面后重新提交")
        source, base_meta, base_kind = _local_edit_base(sid, page_no, base_version)
        version = store.allocate_candidate_version(ROOT, sid, page_no)
        candidate = store.candidate_path(ROOT, sid, page_no, version)
        store.update_job(ROOT, job_id, status="generating", version=version,
                         msg=f"局部改字 {sid} 第{page_no}页 v{version:04d} …")
        with _lock:
            _state.update(running=True, section=sid, fig=page_no,
                          msg=f"局部改字：{sid} 第{page_no}页 v{version:04d} …")

        edit = localedit.apply_local_edit(source, box, text, candidate, note=note)
        from PIL import Image
        with Image.open(source) as src, Image.open(candidate) as out:
            checks = localedit.manual_checks(src, out, tuple(edit["box"]),
                                             user_box=tuple(edit["user_box"]))
        status = "blocked" if checks["status"] == "block" else "review"
        reference_count = int(base_meta.get("reference_count", 0)) if base_meta else \
            (0 if ctx.get("cover") else 1)
        metadata = {
            "sid": sid, "page": page_no, "version": version, "status": status,
            "kind": "local-edit", "base_version": int(base_version) if base_version else None,
            "base_kind": base_kind,
            "source_hash": ctx["source_hash"], "template_hash": ctx["template_hash"],
            "prompt_hash": store.sha256_text(edit["prompt"]), "prompt": edit["prompt"],
            "provider": edit.get("provider"), "model": edit.get("model"),
            "requested_size": edit.get("requested_size"), "actual_size": edit.get("actual_size"),
            "reference_count": reference_count,
            "sec": edit.get("sec"), "kb": round(candidate.stat().st_size / 1024),
            "checks": checks,
            "edit": {"box": edit.get("box"), "user_box": edit.get("user_box"),
                     "crop": edit.get("crop"), "patch": edit.get("patch"),
                     "text": edit.get("text"), "note": note,
                     "color_fit": edit.get("color_fit"),
                     "raw_size": edit.get("raw_size"), "image_sha256": edit.get("image_sha256"),
                     "mask_sha256": edit.get("mask_sha256"),
                     "output_sha256": edit.get("output_sha256")},
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_ts": time.time(), "decision": None,
        }
        try:
            store.register_candidate(ROOT, metadata)
        except ValueError:
            # 并发下版本被占用：换下一个版本，保留已生成图片
            new_version = store.allocate_candidate_version(ROOT, sid, page_no)
            new_path = store.candidate_path(ROOT, sid, page_no, new_version)
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, new_path)
            try:
                candidate.unlink()
            except OSError:
                pass
            version, candidate = new_version, new_path
            metadata["version"] = version
            metadata["kb"] = round(candidate.stat().st_size / 1024)
            store.register_candidate(ROOT, metadata)
        store.update_job(ROOT, job_id, status=status, version=version,
                         msg=f"局部改字候选 v{version:04d} 已生成，待人工复核",
                         result=metadata)
        _record(run, "candidate.generated", version=version, status=status,
                   base_kind=base_kind,
                   artifact=_artifact(candidate),
                   audit={"status": checks.get("status"),
                          "outside_changed": checks.get("outside", {}).get("changed_pixels", 0),
                          "inside_changed": checks.get("inside", {}).get("changed_pixels", 0)})
        run_status = "success"
        msg = f"{sid} 第{page_no}页局部改字完成（v{version:04d}，{checks['status']}）"
    except (Exception, SystemExit) as exc:
        traceback.print_exc()
        store.update_job(ROOT, job_id, status="failed", error=str(exc)[:300],
                         msg=f"局部改字失败：{str(exc)[:150]}")
        _record(run, "local-edit.error", error_type=type(exc).__name__, error=str(exc)[:300])
        msg = f"{sid} 第{page_no}页局部改字失败: {str(exc)[:120]}"
    finally:
        _close_run(run, run_status)
        with _lock:
            _state.update(running=False, msg=msg)


def do_replan(section: str | None, force: bool) -> None:
    only = [x.strip() for x in (section or "").split(",") if x.strip()] or None
    run = _new_run("plan", section=section or "")
    _record(run, "plan.start", sections=only or [], force=force)
    status = "error"
    with _lock:
        _state.update(running=True, msg="重新分析内容并规划图位 …")
    try:
        p = plan.ensure_plan(ROOT, force=force, only=only)
        s = plan.plan_summary(p)
        _record(run, "plan.result", outcome="success", summary=s,
                   plan_hash=memory.sha256_file(plan.plan_path(ROOT)))
        status = "success"
        msg = f"规划完成：{s['sections']} 节 / {s['figures']} 张图（重算 {p.get('changed', 0)} 节）"
    except Exception as exc:
        traceback.print_exc()
        _record(run, "plan.error", error_type=type(exc).__name__, error=str(exc)[:300])
        msg = f"规划失败: {str(exc)[:160]}"
    finally:
        _close_run(run, status)
        with _lock:
            _state.update(msg=msg, running=False)


def do_illustrations(section: str | None = None, force: bool = False) -> None:
    run = _new_run("html.illustrations", section=section or "")
    _record(run, "illustrations.start", section=section or "", force=force)
    status = "error"
    with _lock:
        _state.update(running=True, msg="生成 HTML 局部插图 …")
    try:
        import bidviz_html as htmlengine
        if section:
            ids = [x.strip().upper() for x in section.split(",") if x.strip()]
        else:
            pp = plan.load_page_plan(ROOT) or plan.build_page_plan(ROOT)
            ids = sorted((pp.get("sections") or {}).keys())
        summary = htmlengine.generate_illustrations(ROOT, ids, force=force)
        _record(run, "illustrations.result", outcome="success",
                   summary={k: summary.get(k) for k in ("planned", "generated", "skipped", "failed")})
        status = "success"
        msg = (f"插图完成：生成 {summary['generated']} / 跳过 {summary['skipped']} / "
               f"失败 {summary['failed']}")
    except Exception as exc:
        traceback.print_exc()
        _record(run, "illustrations.error", error_type=type(exc).__name__, error=str(exc)[:300])
        msg = f"插图失败: {str(exc)[:160]}"
    finally:
        _close_run(run, status)
        with _lock:
            _state.update(msg=msg, running=False)


def _expected_fullimage_pages() -> list[tuple[str, int]]:
    chapters, secs = _fullimage_sections()
    return [(sid, page_no) for ids in chapters.values() for sid in ids
            for page_no in range(1, len(fullimage.split_pages([
                (getattr(b, "kind", "body"), getattr(b, "text", "")) for b in secs[sid].blocks
                if getattr(b, "text", "").strip() and getattr(b, "text", "").strip() != secs[sid].title])) + 1)]


def do_rebuild(draft: bool = False) -> None:
    run = _new_run("build")
    _record(run, "build.start", engine=current_engine(), draft=draft)
    status = "error"
    with _lock:
        _state.update(running=True, msg="汇编 PDF …")
    try:
        _cfg().output.mkdir(parents=True, exist_ok=True)
        engine = current_engine()
        if engine == "html":
            import bidviz_html as htmlengine
            out_pdf = _cfg().output / (
                "施工组织设计-图册-A3-html-DRAFT.pdf" if draft
                else "施工组织设计-图册-A3-html.pdf")
            result = htmlengine.build(ROOT, None, out_pdf, draft=draft,
                                      emit_png=_cfg().html_emit_png)
            pages = result["pages"]
        elif engine == "fullimage":
            store = _require_store()
            _chapters, sections = _fullimage_sections()
            expected = _expected_fullimage_pages()
            for sid, page_no in expected:
                ctx = _fullimage_page_context(sid, page_no, sections)
                store.ensure_page_hashes(ROOT, sid, page_no, {
                    **ctx,
                    "prompt_hash": ctx["prompt_hash"],
                })
            summary = store.gate_summary(ROOT, expected, verify_artifacts=True)
            if not draft and not summary.get("can_build_final"):
                raise RuntimeError("正式汇编被门禁阻断：仍有 stale/missing/blocked 或未发布页面")
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
                    candidates = store.list_candidates(ROOT, sid, page)
                    if candidates:
                        return ROOT / candidates[-1]["path"]
                    published = store.published_path(ROOT, sid, page)
                    return published if published.exists() else None
            else:
                out_pdf = _cfg().output / "施工组织设计-图册-A3-fullimage.pdf"
                resolver = lambda sid, page: store.published_path(ROOT, sid, page)
            fullimage.build_fullimage_pdf(ROOT, sids, out_pdf, image_resolver=resolver, draft=draft)
            pages = len(expected)
        else:
            out_pdf = _cfg().output / "施工组织设计-图册-A3.pdf"
            pages, _ = be.build_final(ROOT, out_pdf)
        _record(run, "build.result", outcome="success", engine=engine, draft=draft,
                   pages=pages, artifact=_artifact(out_pdf))
        status = "success"
        with _lock:
            _state.update(msg=f"PDF 已重建（{pages} 页）", pdf=f"/api/pdf{'?draft=1' if draft else ''}")
    except Exception as exc:
        traceback.print_exc()
        _record(run, "build.error", error_type=type(exc).__name__, error=str(exc)[:300])
        with _lock:
            _state.update(msg=f"重建失败: {str(exc)[:160]}")
    finally:
        _close_run(run, status)
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

    def _authorized_post(self) -> bool:
        """Require a same-machine JSON request with an explicit non-simple header."""
        host = self.headers.get("Host", "")
        host_name = host.rsplit(":", 1)[0].strip("[]").lower()
        if host_name not in {"127.0.0.1", "localhost", "::1"}:
            self._json({"error": "invalid Host"}, 403)
            return False
        if self.headers.get("X-BidViz-Request") != "1":
            self._json({"error": "missing X-BidViz-Request"}, 403)
            return False
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json({"error": "Content-Type must be application/json"}, 415)
            return False
        origin = self.headers.get("Origin", "").strip()
        if origin:
            parsed = urlparse(origin)
            origin_host = (parsed.hostname or "").lower()
            origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            request_port = int(host.rsplit(":", 1)[1]) if ":" in host and host.rsplit(":", 1)[1].isdigit() else 80
            loopback = {"127.0.0.1", "localhost", "::1"}
            if (parsed.scheme != "http" or origin_host not in loopback or
                    host_name not in loopback or origin_port != request_port):
                self._json({"error": "cross-origin request denied"}, 403)
                return False
        return True

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
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        p = parsed.path
        if p == "/api/memory/health":
            return self._json(_memory_health())
        if p == "/api/memory/candidates":
            return self._json(_memory_records("", include_inactive=True,
                              statuses={"candidate"}, limit=100))
        if p == "/api/memory/context":
            q = parse_qs(parsed.query)
            scope = {"project": ROOT.name,
                     "engine": (q.get("engine") or [current_engine()])[0],
                     "operation": (q.get("operation") or [""])[0],
                     "section": (q.get("section") or [""])[0],
                     "page": (q.get("page") or [""])[0]}
            query = (q.get("query") or [""])[0]
            budget = min(12000, max(200, int((q.get("budget") or ["4000"])[0])))
            records = _memory_records(query, scope=scope, limit=100)
            chunks, selected, used = [], [], 0
            for record in records:
                chunk = f"[{record['id']} | {record['effective_status']}]\n{record['title']}\n{record['claim']}"
                sep = "\n\n" if chunks else ""
                if used + len(sep) + len(chunk) > budget:
                    break
                chunks.append(chunk); selected.append(record); used += len(sep) + len(chunk)
            text = "\n\n".join(chunks)
            return self._json({"scope": scope, "text": text,
                               "memory_ids": [x["id"] for x in selected],
                               "context_hash": memory.sha256_text(text)})
        if p == "/api/doc":
            return self._json(doc_payload())
        if p == "/api/status":
            with _lock:
                st = dict(_state)
            if fullimage_mode():
                st["progress"] = None
            elif html_mode():
                st["progress"] = html_illustration_progress()
            else:
                st["progress"] = gc.figure_progress(ROOT)
            return self._json(st)
        if p == "/api/engine":
            return self._json({"engine": current_engine(), "config": _cfg().layout_engine,
                               "supported": sorted(_VALID_ENGINES)})
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
        m = re.fullmatch(r"/api/html/illustration/(S\d+)/(\d+)", p)
        if m:
            path = ROOT / "work" / "illustrations" / plan.figure_filename(m.group(1), int(m.group(2)))
            return self._file(path, "image/png")
        if p == "/api/pdf":
            from urllib.parse import parse_qs
            draft = any(v not in ("0", "false", "")
                        for v in parse_qs(urlparse(self.path).query).get("draft", []))
            engine = current_engine()
            names = {
                "fullimage": ("施工组织设计-图册-A3-fullimage-DRAFT.pdf"
                              if draft else "施工组织设计-图册-A3-fullimage.pdf"),
                "html": ("施工组织设计-图册-A3-html-DRAFT.pdf"
                         if draft else "施工组织设计-图册-A3-html.pdf"),
                "v13": "施工组织设计-图册-A3-v13.pdf",
                "legacy": "施工组织设计-图册-A3.pdf",
            }
            return self._file(_cfg().output / names.get(engine, names["legacy"]),
                              "application/pdf")
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
        if not self._authorized_post():
            return
        p = urlparse(self.path).path
        try:
            m_mem = re.fullmatch(r"/api/memory/([^/]+)/(promote|reject|invalidate)", p)
            if m_mem:
                record_id, action = m_mem.group(1), m_mem.group(2)
                body = self._body()
                memory_root = _memory_root_for(record_id)
                if action == "promote":
                    target = str(body.get("to") or "")
                    kwargs = {"actor": str(body.get("actor") or "web-user")}
                    if target == "human_signed":
                        signer = str(body.get("signed_by") or "").strip()
                        if not signer:
                            return self._json({"error": "human_signed 必须提供 signed_by"}, 400)
                        kwargs.update({"explicit_authority": True, "signed_by": signer})
                    result = memory.promote_record(memory_root, record_id, target, **kwargs)
                else:
                    reason = str(body.get("reason") or "").strip()
                    if not reason:
                        return self._json({"error": "必须填写原因"}, 400)
                    result = memory.invalidate_record(
                        memory_root, record_id, reason,
                        actor=str(body.get("actor") or "web-user"),
                        status="rejected" if action == "reject" else "stale")
                return self._json({"ok": True, "record": result})

            if p == "/api/rebuild":
                body = self._body()
                if not _TASK_LOCK.acquire(blocking=False):
                    return self._json({"error": "已有汇编/重规划任务在进行中，请稍候"}, 409)
                with _lock:
                    _state.update(running=True, msg="汇编 PDF …")
                threading.Thread(target=_run_exclusive, args=(do_rebuild,),
                                 kwargs={"draft": bool(body.get("draft"))},
                                 daemon=True).start()
                return self._json({"ok": True})

            if p == "/api/replan":
                body = self._body()
                if not _TASK_LOCK.acquire(blocking=False):
                    return self._json({"error": "已有汇编/重规划任务在进行中，请稍候"}, 409)
                with _lock:
                    _state.update(running=True, msg="重新分析内容并规划图位 …")
                threading.Thread(target=_run_exclusive, args=(do_replan,),
                                 kwargs={"section": body.get("section"),
                                         "force": bool(body.get("force"))},
                                 daemon=True).start()
                return self._json({"ok": True})

            if p == "/api/engine":
                body = self._body()
                return self._json({"ok": True, "engine": set_current_engine(body.get("engine"))})

            if p == "/api/illustrations/generate":
                body = self._body()
                if not _TASK_LOCK.acquire(blocking=False):
                    return self._json({"error": "已有汇编/重规划任务在进行中，请稍候"}, 409)
                with _lock:
                    _state.update(running=True, msg="生成 HTML 局部插图 …")
                threading.Thread(target=_run_exclusive, args=(do_illustrations,),
                                 kwargs={"section": body.get("section"),
                                         "force": bool(body.get("force"))},
                                 daemon=True).start()
                return self._json({"ok": True})

            m = re.fullmatch(r"/api/section/(S\d+)/pages/(\d+)/(generate|prompt-reset|approve|reject|waive|rollback|local-edit)", p)
            if m and fullimage_mode():
                sid, page_no, action = m.group(1), int(m.group(2)), m.group(3)
                body = self._body()
                store = _require_store()
                if action == "generate":
                    ctx = _fullimage_page_context(sid, page_no)
                    expected = str(body.get("expected_source_hash") or "")
                    if expected != ctx["source_hash"]:
                        return self._json({"error": "原文已变化，请刷新后重试"}, 409)
                    if ctx.get("stale"):
                        return self._json({"error": "自定义提示词对应旧原文，请先重置或重新保存提示词"}, 409)
                    prompt = str(body.get("prompt") or ctx["resolved_prompt"]).strip()
                    job = store.create_job(ROOT, sid=sid, page=page_no)
                    threading.Thread(target=do_fullimage_generate,
                                     args=(job["job_id"], sid, page_no, expected, prompt),
                                     daemon=True).start()
                    return self._json({"ok": True, "job_id": job["job_id"]})
                if action == "local-edit":
                    ctx = _fullimage_page_context(sid, page_no)
                    expected = str(body.get("expected_source_hash") or "")
                    if expected != ctx["source_hash"]:
                        return self._json({"error": "原文已变化，请刷新后重试"}, 409)
                    text = str(body.get("text") or "").strip()
                    if not text:
                        return self._json({"error": "缺少替换文字"}, 400)
                    if len(text) > localedit.MAX_TEXT:
                        return self._json({"error": f"替换文字最多 {localedit.MAX_TEXT} 字"}, 400)
                    box = body.get("box")
                    if not isinstance(box, list) or len(box) != 4:
                        return self._json({"error": "框必须是 [x0,y0,x1,y1]"}, 400)
                    try:
                        base_version = int(body["base_version"]) if body.get("base_version") else None
                    except (TypeError, ValueError):
                        return self._json({"error": "基准版本无效"}, 400)
                    try:
                        _local_edit_base(sid, page_no, base_version)
                    except (KeyError, FileNotFoundError) as exc:
                        return self._json({"error": str(exc)}, 404)
                    job = store.create_job(ROOT, sid=sid, page=page_no, kind="local-edit",
                                           base_version=base_version, text=text)
                    threading.Thread(target=do_fullimage_local_edit,
                                     args=(job["job_id"], sid, page_no, expected,
                                           base_version, box, text, str(body.get("note") or "")),
                                     daemon=True).start()
                    return self._json({"ok": True, "job_id": job["job_id"]})
                if action == "prompt-reset":
                    ctx = _fullimage_page_context(sid, page_no)
                    store.reset_prompt_override(
                        ROOT, sid, page_no, ctx["auto_prompt"], ctx["source_hash"])
                    return self._json({"ok": True})
                version = str(body.get("version") or "")
                if action in ("approve", "reject", "waive"):
                    if not version:
                        return self._json({"error": "缺少候选版本"}, 400)
                    version_no = int(version[1:]) if version.startswith("v") else int(version)
                    decided_by = str(body.get("decided_by") or "web-user")
                    comment = str(body.get("comment") or "")
                    meta = store.record_decision(ROOT, sid, page_no, version_no, action,
                                                 decided_by, comment)
                    if action in ("approve", "waive"):
                        store.publish_candidate(ROOT, sid, page_no, version_no)
                    run = _new_run(f"fullimage.{action}", section=sid, page=page_no)
                    _record(run, action, scope={"project": ROOT.name, "engine": "fullimage",
                               "operation": action, "section": sid, "page": page_no},
                               version=version_no, decided_by=decided_by,
                               reason=comment, outcome="approved" if action != "reject" else "rejected")
                    _close_run(run, "success")
                    return self._json({"ok": True, "candidate": meta})
                if action == "rollback":
                    result = store.rollback_published(ROOT, sid, page_no)
                    run = _new_run("fullimage.rollback", section=sid, page=page_no)
                    _record(run, "rollback", scope={"project": ROOT.name, "engine": "fullimage",
                               "operation": "rollback", "section": sid, "page": page_no},
                               outcome="success")
                    _close_run(run, "success")
                    return self._json({"ok": True, "page": result})

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

                with _EDITS_LOCK:
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
                    run = _new_run("document.edit", section=sid)
                    _record(run, "document.edited", scope={"project": ROOT.name,
                               "engine": current_engine(), "operation": "edit", "section": sid},
                               fields=fields, edits_sha256=memory.sha256_file(EDITS))
                    _close_run(run, "success")
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
                    run = _new_run("prompt.edit", section=sid)
                    _record(run, "prompt.edited", scope={"project": ROOT.name,
                               "engine": current_engine(), "operation": "prompt-edit", "section": sid},
                               pages=sorted(str(k) for k in prompts),
                               prompt_hashes={str(k): _hash_text(str(v)) for k, v in prompts.items()})
                    _close_run(run, "success")
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
