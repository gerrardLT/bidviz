"""Persistent review state for the full-image rendering pipeline."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_STORE = Path("work/fullimage")
_PROMPTS = Path("work/fullimage-prompts.json")
_PAGES = Path("work/fullimage-pages.json")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _page_no(page: int) -> int:
    page = int(page)
    if page < 1:
        raise ValueError("page must be positive")
    return page


def _sid(sid: str) -> str:
    if not _SID_RE.fullmatch(str(sid)):
        raise ValueError(f"invalid sid: {sid!r}")
    return str(sid)


def _key(sid: str, page: int) -> str:
    return f"{_sid(sid)}:p{_page_no(page)}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: str | Path, data: Any) -> Path:
    """Atomically write JSON; the temporary file shares the target volume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False) as fh:
            tmp_name = fh.name
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
    return path


def load_prompt_overrides(root: str | Path) -> dict[str, Any]:
    return load_json(Path(root) / _PROMPTS, {"schema": "bidviz-fullimage-prompts/1", "overrides": {}})


def save_prompt_overrides(root: str | Path, doc: dict[str, Any]) -> Path:
    doc.setdefault("schema", "bidviz-fullimage-prompts/1")
    doc.setdefault("overrides", {})
    return save_json(Path(root) / _PROMPTS, doc)


def get_prompt_override(root: str | Path, sid: str, page: int,
                        auto_prompt: str, source_hash: str) -> dict[str, Any]:
    rec = load_prompt_overrides(root)["overrides"].get(_key(sid, page))
    custom = bool(rec and "prompt" in rec)
    return {
        "auto_prompt": auto_prompt,
        "resolved_prompt": rec["prompt"] if custom else auto_prompt,
        "custom": custom,
        "stale": bool(custom and rec.get("source_hash") != source_hash),
        "source_hash": source_hash,
    }


def set_prompt_override(root: str | Path, sid: str, page: int,
                        prompt: str, source_hash: str) -> dict[str, Any]:
    doc = load_prompt_overrides(root)
    doc["overrides"][_key(sid, page)] = {
        "sid": _sid(sid), "page": _page_no(page), "prompt": prompt,
        "source_hash": source_hash, "updated_at": _now(),
    }
    save_prompt_overrides(root, doc)
    return get_prompt_override(root, sid, page, prompt, source_hash)


def reset_prompt_override(root: str | Path, sid: str, page: int,
                          auto_prompt: str = "", source_hash: str = "") -> dict[str, Any]:
    doc = load_prompt_overrides(root)
    doc["overrides"].pop(_key(sid, page), None)
    save_prompt_overrides(root, doc)
    return get_prompt_override(root, sid, page, auto_prompt, source_hash)


def load_pages_doc(root: str | Path) -> dict[str, Any]:
    return load_json(Path(root) / _PAGES, {"schema": "bidviz-fullimage-pages/1", "pages": {}})


def save_pages_doc(root: str | Path, doc: dict[str, Any]) -> Path:
    doc.setdefault("schema", "bidviz-fullimage-pages/1")
    doc.setdefault("pages", {})
    return save_json(Path(root) / _PAGES, doc)


def ensure_page(root: str | Path, sid: str, page: int, source_hash: str,
                template_hash: str, prompt_hash: str) -> dict[str, Any]:
    doc = load_pages_doc(root)
    key = _key(sid, page)
    hashes = {"source_hash": source_hash, "template_hash": template_hash,
              "prompt_hash": prompt_hash}
    rec = doc["pages"].get(key)
    if rec is None:
        rec = {"sid": _sid(sid), "page": _page_no(page), **hashes,
               "status": "missing", "stale": False, "candidates": [],
               "published": {"previous": None, "current": None},
               "created_at": _now(), "updated_at": _now()}
        doc["pages"][key] = rec
    else:
        changed = any(rec.get(name) != value for name, value in hashes.items())
        if changed and (rec.get("candidates") or rec.get("published", {}).get("current")):
            rec["stale"] = True
            rec["status"] = "stale"
        rec.update(hashes)
        rec["updated_at"] = _now()
    save_pages_doc(root, doc)
    return rec


def published_path(root: str | Path, sid: str, page: int) -> Path:
    return Path(root) / _STORE / "published" / f"{_sid(sid)}_p{_page_no(page)}.png"


def candidate_path(root: str | Path, sid: str, page: int, version: int) -> Path:
    version = int(version)
    if version < 1:
        raise ValueError("version must be positive")
    return (Path(root) / _STORE / "candidates" / _sid(sid) /
            f"p{_page_no(page)}" / f"v{version:04d}.png")


def next_candidate_version(root: str | Path, sid: str, page: int) -> int:
    versions: set[int] = set()
    rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
    versions.update(int(c["version"]) for c in rec.get("candidates", []) if "version" in c)
    folder = candidate_path(root, sid, page, 1).parent
    for path in folder.glob("v[0-9][0-9][0-9][0-9].png"):
        versions.add(int(path.stem[1:]))
    return max(versions, default=0) + 1


def _find_candidate(page_rec: dict[str, Any], version: int) -> dict[str, Any] | None:
    return next((c for c in page_rec.get("candidates", [])
                 if int(c.get("version", -1)) == int(version)), None)


def register_candidate(root: str | Path, metadata: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(metadata)
    try:
        sid, page = _sid(metadata.pop("sid")), _page_no(metadata.pop("page"))
    except KeyError as exc:
        raise ValueError(f"missing candidate field: {exc.args[0]}") from exc
    version = int(metadata.pop("version", next_candidate_version(root, sid, page)))
    path = candidate_path(root, sid, page, version)
    if not path.is_file():
        raise FileNotFoundError(path)

    doc = load_pages_doc(root)
    key = _key(sid, page)
    if key not in doc["pages"]:
        raise KeyError(f"page not ensured: {key}")
    page_rec = doc["pages"][key]
    if _find_candidate(page_rec, version):
        raise ValueError(f"candidate already registered: {key} v{version:04d}")
    rec = {"sid": sid, "page": page, "version": version,
           "path": path.relative_to(Path(root)).as_posix(), "status": "pending",
           "registered_at": _now(), "decisions": [], **metadata}
    page_rec["candidates"].append(rec)
    page_rec["status"] = "pending"
    page_rec["updated_at"] = _now()
    save_pages_doc(root, doc)
    return rec


def get_candidate(root: str | Path, sid: str, page: int,
                  version: int) -> dict[str, Any] | None:
    rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
    return _find_candidate(rec, version)


def list_candidates(root: str | Path, sid: str, page: int) -> list[dict[str, Any]]:
    rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
    return sorted(rec.get("candidates", []), key=lambda c: int(c["version"]))


def record_decision(root: str | Path, sid: str, page: int, version: int,
                    action: str, decided_by: str = "", comment: str = "") -> dict[str, Any]:
    if action not in {"approve", "reject", "waive"}:
        raise ValueError(f"invalid decision action: {action}")
    doc = load_pages_doc(root)
    page_rec = doc["pages"].get(_key(sid, page))
    candidate = _find_candidate(page_rec or {}, version)
    if candidate is None:
        raise KeyError(f"candidate not found: {_key(sid, page)} v{int(version):04d}")
    if action == "waive" and candidate.get("status") != "blocked":
        raise ValueError("waive requires a blocked candidate")
    if action == "approve" and candidate.get("status") != "review":
        raise ValueError("approve requires a review candidate; blocked candidates must be waived")
    decision = {"action": action, "decided_by": decided_by, "comment": comment,
                "decided_at": _now()}
    candidate.setdefault("decisions", []).append(decision)
    candidate["decision"] = decision
    candidate["status"] = {"approve": "review", "reject": "blocked",
                           "waive": "waived"}[action]
    if not page_rec.get("published", {}).get("current"):
        page_rec["status"] = candidate["status"]
    page_rec["updated_at"] = _now()
    save_pages_doc(root, doc)
    return candidate


def _publication(candidate: dict[str, Any], waived: bool = False) -> dict[str, Any]:
    return {"sid": candidate["sid"], "page": candidate["page"],
            "version": candidate["version"], "candidate_path": candidate["path"],
            "waived": waived, "published_at": _now()}


def publish_candidate(root: str | Path, sid: str, page: int,
                      version: int) -> dict[str, Any]:
    root = Path(root)
    doc = load_pages_doc(root)
    page_rec = doc["pages"].get(_key(sid, page))
    candidate = _find_candidate(page_rec or {}, version)
    if candidate is None:
        raise KeyError(f"candidate not found: {_key(sid, page)} v{int(version):04d}")
    if candidate.get("status") not in {"review", "waived"}:
        raise PermissionError("candidate must be approved, or blocked then waived")
    source = root / candidate["path"]
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = published_path(root, sid, page)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)

    publication = _publication(candidate, candidate["status"] == "waived")
    published = page_rec.setdefault("published", {"previous": None, "current": None})
    published["previous"] = published.get("current")
    published["current"] = publication
    candidate["status"] = "published"
    candidate["published_at"] = publication["published_at"]
    page_rec["status"] = "published"
    page_rec["stale"] = False
    page_rec["updated_at"] = _now()
    save_pages_doc(root, doc)
    return {"path": destination, "previous": published["previous"],
            "current": published["current"]}


def rollback_published(root: str | Path, sid: str, page: int) -> dict[str, Any]:
    root = Path(root)
    doc = load_pages_doc(root)
    page_rec = doc["pages"].get(_key(sid, page))
    if not page_rec:
        raise KeyError(f"page not found: {_key(sid, page)}")
    published = page_rec.get("published", {})
    previous, current = published.get("previous"), published.get("current")
    if not previous:
        raise ValueError("no previous publication to restore")
    source = root / previous["candidate_path"]
    if not source.is_file():
        raise FileNotFoundError(source)
    destination = published_path(root, sid, page)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    previous = {**previous, "published_at": _now(), "rolled_back": True}
    published["current"], published["previous"] = previous, current
    page_rec["status"] = "published"
    page_rec["stale"] = False
    page_rec["updated_at"] = _now()
    save_pages_doc(root, doc)
    return {"path": destination, "previous": published["previous"],
            "current": published["current"]}


def create_job(root: str | Path, metadata: dict[str, Any] | None = None,
               **fields: Any) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    job = {"job_id": job_id, "status": "generating", "created_at": _now(),
           "updated_at": _now(), **(metadata or {}), **fields}
    save_json(Path(root) / _STORE / "jobs" / f"{job_id}.json", job)
    return job


def get_job(root: str | Path, job_id: str) -> dict[str, Any] | None:
    return load_json(Path(root) / _STORE / "jobs" / f"{job_id}.json")


def update_job(root: str | Path, job_id: str,
               updates: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    job = get_job(root, job_id)
    if job is None:
        raise KeyError(f"job not found: {job_id}")
    job.update(updates or {})
    job.update(fields)
    job["updated_at"] = _now()
    save_json(Path(root) / _STORE / "jobs" / f"{job_id}.json", job)
    return job


def _expected_page(item: Any) -> tuple[str, int]:
    if isinstance(item, dict):
        return _sid(item["sid"]), _page_no(item["page"])
    if isinstance(item, str):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?::p|_p)(\d+)", item)
        if not match:
            raise ValueError(f"invalid expected page: {item!r}")
        return _sid(match.group(1)), _page_no(int(match.group(2)))
    sid, page = item
    return _sid(sid), _page_no(page)


def gate_summary(root: str | Path, expected_pages: Iterable[Any]) -> dict[str, Any]:
    expected_pages = list(expected_pages)
    pages = load_pages_doc(root)["pages"]
    lists = {name: [] for name in ("published", "pending", "stale", "missing",
                                   "blocked", "waived")}
    for item in expected_pages:
        sid, page = _expected_page(item)
        label = f"{sid}:p{page}"
        rec = pages.get(label)
        if rec is None:
            lists["missing"].append(label)
            continue
        publication = rec.get("published", {}).get("current")
        if rec.get("stale"):
            lists["stale"].append(label)
        elif publication:
            lists["published"].append(label)
            if publication.get("waived"):
                lists["waived"].append(label)
        else:
            candidates = rec.get("candidates", [])
            latest = candidates[-1] if candidates else None
            if not latest:
                lists["missing"].append(label)
            elif latest.get("status") == "blocked":
                lists["blocked"].append(label)
            elif latest.get("status") == "waived":
                lists["waived"].append(label)
                lists["pending"].append(label)
            else:
                lists["pending"].append(label)
    counts = {name: len(values) for name, values in lists.items()}
    summary: dict[str, Any] = dict(counts)
    summary["counts"] = counts
    summary["lists"] = lists
    summary.update({f"{name}_pages": values for name, values in lists.items()})
    summary["can_build_final"] = (
        summary["published"] == len(expected_pages)
        and not any(summary[name] for name in ("pending", "stale", "missing", "blocked")))
    return summary


def can_build_final(root: str | Path, expected_pages: Iterable[Any]) -> bool:
    expected_pages = list(expected_pages)
    return bool(gate_summary(root, expected_pages)["can_build_final"])
