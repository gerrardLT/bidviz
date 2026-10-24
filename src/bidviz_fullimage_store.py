"""Persistent review state for the full-image rendering pipeline."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

_STORE = Path("work/fullimage")
_PROMPTS = Path("work/fullimage-prompts.json")
_PAGES = Path("work/fullimage-pages.json")
_LOCK = Path("work/fullimage-pages.lock")
_TXN = Path("work/fullimage-artifact-transaction.json")
_SID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_NAMES = ("source_hash", "template_hash", "prompt_hash")
_UNRESOLVED_STATUSES = {"pending", "review", "blocked", "waived"}
_LOCK_POLL_SECONDS = 0.01
_LOCK_TIMEOUT_SECONDS = 30.0
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_STATE = threading.local()


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


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(root: Path, path: Path) -> str:
    root, path = root.resolve(), path.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path must be inside project root: {path}") from exc


def _stored_path(root: Path, raw_path: Any) -> Path | None:
    if not raw_path:
        return None
    root = root.resolve()
    path = (root / str(raw_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _lock_key(lock_path: Path) -> str:
    return os.path.normcase(os.path.abspath(lock_path))


def _thread_lock(lock_path: Path) -> threading.RLock:
    key = _lock_key(lock_path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _ledger_lock(root: str | Path) -> Iterator[None]:
    """Serialize full-ledger transactions across threads and processes."""
    lock_path = Path(root).resolve() / _LOCK
    key = _lock_key(lock_path)
    depths = getattr(_LOCK_STATE, "depths", None)
    if depths is None:
        depths = _LOCK_STATE.depths = {}
    thread_lock = _thread_lock(lock_path)
    with thread_lock:
        if depths.get(key, 0):
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
            return

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        if not lock_path.exists():
            try:
                lock_path.write_bytes(b"\0")
            except OSError:
                pass
        fh = lock_path.open("r+b")
        locked = False
        try:
            while not locked:
                try:
                    if os.name == "nt":
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - Windows is the production platform
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out waiting for publication lock: {lock_path}")
                    time.sleep(_LOCK_POLL_SECONDS)
            depths[key] = 1
            try:
                _recover_artifact_transaction(root)
                yield
            finally:
                depths.pop(key, None)
        finally:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Windows is the production platform
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


def load_json(path: str | Path, default: Any = None) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _json_text(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


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
            fh.write(_json_text(data))
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
    with _ledger_lock(root):
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


def _refresh_prompt_hash_unlocked(root: str | Path, sid: str, page: int,
                                  prompt_hash: str) -> None:
    pages = load_json(Path(root) / _PAGES, {"schema": "bidviz-fullimage-pages/1", "pages": {}})
    rec = pages.get("pages", {}).get(_key(sid, page))
    if rec is None or rec.get("prompt_hash") == prompt_hash:
        return
    if rec.get("candidates") or (rec.get("published") or {}).get("current"):
        rec["stale"] = True
        rec["status"] = "stale"
    rec["prompt_hash"] = prompt_hash
    rec["updated_at"] = _now()
    save_json(Path(root) / _PAGES, pages)


def set_prompt_override(root: str | Path, sid: str, page: int,
                        prompt: str, source_hash: str) -> dict[str, Any]:
    with _ledger_lock(root):
        doc = load_prompt_overrides(root)
        doc["overrides"][_key(sid, page)] = {
            "sid": _sid(sid), "page": _page_no(page), "prompt": prompt,
            "source_hash": source_hash, "updated_at": _now(),
        }
        save_prompt_overrides(root, doc)
        _refresh_prompt_hash_unlocked(root, sid, page, sha256_text(prompt))
        return get_prompt_override(root, sid, page, prompt, source_hash)


def reset_prompt_override(root: str | Path, sid: str, page: int,
                          auto_prompt: str = "", source_hash: str = "") -> dict[str, Any]:
    with _ledger_lock(root):
        doc = load_prompt_overrides(root)
        doc["overrides"].pop(_key(sid, page), None)
        save_prompt_overrides(root, doc)
        if auto_prompt:
            _refresh_prompt_hash_unlocked(root, sid, page, sha256_text(auto_prompt))
        return get_prompt_override(root, sid, page, auto_prompt, source_hash)


def _recover_artifact_transaction(root: str | Path) -> bool:
    """Recover a publish/rollback interrupted around the ledger commit.

    If the ledger still has its pre-transaction hash, restore old artifacts.  A
    different ledger hash means the metadata commit completed, so keep the new
    artifacts and only remove transaction debris.
    """
    root = Path(root).resolve()
    journal_path = root / _TXN
    journal = load_json(journal_path)
    if not isinstance(journal, dict):
        return False
    ledger_path = root / _PAGES
    before_hash = str(journal.get("ledger_before_sha256") or "")
    after_hash = str(journal.get("ledger_after_sha256") or "")
    current_hash = sha256_file(ledger_path) if ledger_path.is_file() else ""
    committed = bool(after_hash and current_hash == after_hash)
    if not committed:
        for item in journal.get("files", []):
            destination = _stored_path(root, item.get("destination"))
            backup = _stored_path(root, item.get("backup"))
            if destination is None:
                continue
            if item.get("existed"):
                if backup is None or not backup.is_file():
                    raise RuntimeError(f"transaction recovery backup missing: {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(backup, destination)
            else:
                destination.unlink(missing_ok=True)
    for item in journal.get("files", []):
        staged = _stored_path(root, item.get("staged"))
        backup = _stored_path(root, item.get("backup"))
        if staged is not None:
            staged.unlink(missing_ok=True)
        if backup is not None:
            backup.unlink(missing_ok=True)
    journal_path.unlink(missing_ok=True)
    return True


def load_pages_doc(root: str | Path) -> dict[str, Any]:
    with _ledger_lock(root):
        _recover_artifact_transaction(root)
        return load_json(Path(root) / _PAGES, {"schema": "bidviz-fullimage-pages/1", "pages": {}})


def save_pages_doc(root: str | Path, doc: dict[str, Any]) -> Path:
    with _ledger_lock(root):
        doc.setdefault("schema", "bidviz-fullimage-pages/1")
        doc.setdefault("pages", {})
        return save_json(Path(root) / _PAGES, doc)


def ensure_page(root: str | Path, sid: str, page: int, source_hash: str,
                template_hash: str, prompt_hash: str) -> dict[str, Any]:
    """Refresh current page hashes before generation, review, or a formal gate."""
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        key = _key(sid, page)
        hashes = {"source_hash": source_hash, "template_hash": template_hash,
                  "prompt_hash": prompt_hash}
        rec = doc["pages"].get(key)
        if rec is None:
            rec = {"sid": _sid(sid), "page": _page_no(page), **hashes,
                   "status": "missing", "stale": False, "candidates": [],
                   "version_high_watermark": 0,
                   "published": {"previous": None, "current": None},
                   "created_at": _now(), "updated_at": _now()}
            doc["pages"][key] = rec
        else:
            changed = any(rec.get(name) != value for name, value in hashes.items())
            if changed and (rec.get("candidates") or rec.get("published", {}).get("current")):
                rec["stale"] = True
                rec["status"] = "stale"
            rec["version_high_watermark"] = max(
                int(rec.get("version_high_watermark") or 0), *_recorded_versions(rec), 0)
            rec.update(hashes)
            rec["updated_at"] = _now()
        save_pages_doc(root, doc)
        return rec


def ensure_page_hashes(root: str | Path, sid: str, page: int,
                       hashes: dict[str, Any]) -> dict[str, Any]:
    """Caller-facing adapter for a freshly recomputed page context."""
    missing = [name for name in _HASH_NAMES if hashes.get(name) is None]
    if missing:
        raise ValueError(f"missing page hashes: {', '.join(missing)}")
    return ensure_page(root, sid, page, *(str(hashes[name]) for name in _HASH_NAMES))


def published_path(root: str | Path, sid: str, page: int) -> Path:
    return Path(root) / _STORE / "published" / f"{_sid(sid)}_p{_page_no(page)}.png"


def previous_published_path(root: str | Path, sid: str, page: int) -> Path:
    return (Path(root) / _STORE / "published" / "previous" /
            f"{_sid(sid)}_p{_page_no(page)}.png")


def candidate_path(root: str | Path, sid: str, page: int, version: int) -> Path:
    version = int(version)
    if version < 1:
        raise ValueError("version must be positive")
    return (Path(root) / _STORE / "candidates" / _sid(sid) /
            f"p{_page_no(page)}" / f"v{version:04d}.png")


def _recorded_versions(page_rec: dict[str, Any]) -> set[int]:
    versions = {int(c["version"]) for c in page_rec.get("candidates", [])
                if "version" in c}
    published = page_rec.get("published") or {}
    versions.update(int(publication["version"])
                    for publication in (published.get("current"), published.get("previous"))
                    if publication and "version" in publication)
    return versions


def _next_candidate_version_unlocked(root: str | Path, sid: str, page: int,
                                     page_rec: dict[str, Any] | None = None) -> int:
    rec = page_rec if page_rec is not None else load_pages_doc(root)["pages"].get(
        _key(sid, page), {})
    versions = _recorded_versions(rec)
    versions.add(int(rec.get("version_high_watermark") or 0))
    folder = candidate_path(root, sid, page, 1).parent
    for path in folder.glob("v[0-9][0-9][0-9][0-9].png"):
        versions.add(int(path.stem[1:]))
    return max(versions, default=0) + 1


def next_candidate_version(root: str | Path, sid: str, page: int) -> int:
    """Peek at the next version. Use ``allocate_candidate_version`` before writing."""
    with _ledger_lock(root):
        return _next_candidate_version_unlocked(root, sid, page)


def allocate_candidate_version(root: str | Path, sid: str, page: int) -> int:
    """Reserve a unique candidate version across threads and processes."""
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        key = _key(sid, page)
        page_rec = doc["pages"].get(key)
        if page_rec is None:
            raise KeyError(f"page not ensured: {key}")
        version = _next_candidate_version_unlocked(root, sid, page, page_rec)
        page_rec["version_high_watermark"] = version
        page_rec["updated_at"] = _now()
        save_pages_doc(root, doc)
        return version


def _find_candidate(page_rec: dict[str, Any], version: int) -> dict[str, Any] | None:
    return next((c for c in page_rec.get("candidates", [])
                 if int(c.get("version", -1)) == int(version)), None)


def register_candidate(root: str | Path, metadata: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(metadata)
    try:
        sid, page = _sid(metadata.pop("sid")), _page_no(metadata.pop("page"))
    except KeyError as exc:
        raise ValueError(f"missing candidate field: {exc.args[0]}") from exc
    requested_version = metadata.pop("version", None)

    with _ledger_lock(root):
        doc = load_pages_doc(root)
        key = _key(sid, page)
        if key not in doc["pages"]:
            raise KeyError(f"page not ensured: {key}")
        page_rec = doc["pages"][key]
        version = (int(requested_version) if requested_version is not None
                   else _next_candidate_version_unlocked(root, sid, page, page_rec))
        path = candidate_path(root, sid, page, version)
        if not path.is_file():
            raise FileNotFoundError(path)
        if _find_candidate(page_rec, version):
            raise ValueError(f"candidate already registered: {key} v{version:04d}")
        rec = {"sid": sid, "page": page, "version": version,
               "path": _relative_path(Path(root), path), "status": "pending",
               "registered_at": _now(), "decisions": [], **metadata}
        page_rec.setdefault("candidates", []).append(rec)
        page_rec["version_high_watermark"] = max(
            int(page_rec.get("version_high_watermark") or 0), version)
        page_rec["status"] = "pending"
        page_rec["updated_at"] = _now()
        save_pages_doc(root, doc)
        return rec


def import_published(root: str | Path, sid: str, page: int, source_image: str | Path,
                     metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """把一张既有全图导入为 vNNNN 候选并立即发布（回填历史交付用）。"""
    root = Path(root)
    source = Path(source_image)
    if not source.is_file():
        raise FileNotFoundError(source)
    with _ledger_lock(root):
        page_rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
        candidate_metadata = dict(metadata or {})
        for name in _HASH_NAMES:
            candidate_metadata.setdefault(name, page_rec.get(name))
        version = _next_candidate_version_unlocked(root, sid, page, page_rec)
        destination = candidate_path(root, sid, page, version)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        try:
            record = register_candidate(root, {
                "sid": sid, "page": page, "version": version, "status": "review",
                **candidate_metadata,
            })
            publish_candidate(root, sid, page, version)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return get_candidate(root, sid, page, version) or record


def _candidate_view(root: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    view = dict(candidate)
    path = _stored_path(root, view.get("path"))
    artifact_exists = bool(path and path.is_file())
    pruned = bool(view.get("artifact_pruned"))
    view["artifact_exists"] = artifact_exists and not pruned
    view["artifact_pruned"] = pruned or not artifact_exists
    return view


def get_candidate(root: str | Path, sid: str, page: int, version: int,
                  include_pruned: bool = True) -> dict[str, Any] | None:
    rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
    candidate = _find_candidate(rec, version)
    if candidate is None:
        return None
    view = _candidate_view(Path(root), candidate)
    return view if include_pruned or view["artifact_exists"] else None


def list_candidates(root: str | Path, sid: str, page: int,
                    include_pruned: bool = False) -> list[dict[str, Any]]:
    rec = load_pages_doc(root)["pages"].get(_key(sid, page), {})
    candidates = (_candidate_view(Path(root), candidate)
                  for candidate in rec.get("candidates", []))
    if not include_pruned:
        candidates = (candidate for candidate in candidates
                      if candidate["artifact_exists"])
    return sorted(candidates, key=lambda candidate: int(candidate["version"]))


def get_page(root: str | Path, sid: str, page: int) -> dict[str, Any]:
    return load_pages_doc(root)["pages"].get(_key(sid, page), {})


def record_decision(root: str | Path, sid: str, page: int, version: int,
                    action: str, decided_by: str = "", comment: str = "") -> dict[str, Any]:
    if action not in {"approve", "reject", "waive"}:
        raise ValueError(f"invalid decision action: {action}")
    with _ledger_lock(root):
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


def _publication(candidate: dict[str, Any], page_rec: dict[str, Any],
                 published_relpath: str, image_sha256: str,
                 waived: bool = False) -> dict[str, Any]:
    publication = {
        "sid": candidate["sid"], "page": candidate["page"],
        "version": candidate["version"], "published_path": published_relpath,
        "image_sha256": image_sha256, "waived": waived, "published_at": _now(),
        "kind": candidate.get("kind", "generate"),
    }
    for name in ("source_hash", "template_hash", "prompt_hash"):
        value = candidate.get(name, page_rec.get(name))
        if value is not None:
            publication[name] = value
    return publication


def _stage_copy(source: Path, destination: Path, suffix: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.{suffix}")
    try:
        shutil.copy2(source, staged)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _install_staged(staged: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, destination)


def _restore_file(destination: Path, backup: Path | None, existed: bool) -> None:
    if existed:
        if backup is None or not backup.is_file():
            raise RuntimeError(f"transaction backup missing: {destination}")
        _install_staged(backup, destination)
    else:
        destination.unlink(missing_ok=True)


def _backup_existing(path: Path) -> tuple[bool, Path | None]:
    if not path.is_file():
        return False, None
    return True, _stage_copy(path, path, "backup")


def _write_artifact_journal(root: Path, operation: str,
                            files: list[tuple[Path, Path | None, Path | None, bool]],
                            ledger_after_sha256: str = "") -> Path:
    ledger = root / _PAGES
    journal = {
        "schema": "bidviz-fullimage-artifact-transaction/1",
        "operation": operation,
        "created_at": _now(),
        "ledger_before_sha256": sha256_file(ledger) if ledger.is_file() else "",
        "ledger_after_sha256": ledger_after_sha256,
        "files": [
            {"destination": _relative_path(root, destination),
             "staged": _relative_path(root, staged) if staged is not None else "",
             "backup": _relative_path(root, backup) if backup is not None else "",
             "existed": existed}
            for destination, staged, backup, existed in files
        ],
    }
    return save_json(root / _TXN, journal)


def _finish_artifact_transaction(root: Path) -> None:
    (root / _TXN).unlink(missing_ok=True)


def _candidate_hash_mismatches(page_rec: dict[str, Any],
                               candidate: dict[str, Any]) -> list[str]:
    # 局部改字的 prompt 是编辑指令，不是整页提示词：只校验原文与模板。
    names = _HASH_NAMES
    if candidate.get("kind") == "local-edit":
        names = ("source_hash", "template_hash")
    return [name for name in names
            if not candidate.get(name) or candidate.get(name) != page_rec.get(name)]


def publish_candidate(root: str | Path, sid: str, page: int,
                      version: int) -> dict[str, Any]:
    root = Path(root).resolve()
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        page_rec = doc["pages"].get(_key(sid, page))
        candidate = _find_candidate(page_rec or {}, version)
        if candidate is None:
            raise KeyError(f"candidate not found: {_key(sid, page)} v{int(version):04d}")
        if candidate.get("status") not in {"review", "waived"}:
            raise PermissionError("candidate must be approved, or blocked then waived")
        if candidate.get("artifact_pruned"):
            raise PermissionError("pruned candidate cannot be published")
        mismatches = _candidate_hash_mismatches(page_rec, candidate)
        if mismatches:
            raise PermissionError(
                "stale candidate hashes do not match current page: " + ", ".join(mismatches))
        source = _stored_path(root, candidate.get("path"))
        if source is None or not source.is_file():
            raise FileNotFoundError(source or candidate.get("path", ""))

        destination = published_path(root, sid, page)
        previous_destination = previous_published_path(root, sid, page)
        published = page_rec.setdefault("published", {"previous": None, "current": None})
        old_current = published.get("current")
        old_current_source = _publication_source(root, old_current) if old_current else None
        staged_current = _stage_copy(source, destination, "publish")
        staged_previous = None
        if old_current and old_current_source is not None:
            staged_previous = _stage_copy(old_current_source, previous_destination, "publish")
        current_existed, current_backup = _backup_existing(destination)
        previous_existed, previous_backup = _backup_existing(previous_destination)
        if old_current and staged_previous is not None:
            previous = dict(old_current)
            previous["published_path"] = _relative_path(root, previous_destination)
            previous["image_sha256"] = sha256_file(staged_previous)
            previous.pop("candidate_path", None)
            published["previous"] = previous
        else:
            published["previous"] = None
        publication = _publication(
            candidate, page_rec, _relative_path(root, destination), sha256_file(staged_current),
            candidate["status"] == "waived")
        published["current"] = publication
        candidate["status"] = "published"
        candidate["published_at"] = publication["published_at"]
        page_rec["version_high_watermark"] = max(
            int(page_rec.get("version_high_watermark") or 0), int(candidate["version"]))
        # 局部改字不改变底图的提示词口径：页面此前的 stale 判定保持不变。
        if candidate.get("kind") == "local-edit":
            page_rec["stale"] = bool(page_rec.get("stale"))
        else:
            page_rec["stale"] = False
        page_rec["status"] = "stale" if page_rec["stale"] else "published"
        page_rec["updated_at"] = _now()

        ledger_after = sha256_text(_json_text(doc))
        installed = False
        try:
            _write_artifact_journal(root, "publish", [
                (destination, staged_current, current_backup, current_existed),
                (previous_destination, staged_previous, previous_backup, previous_existed),
            ], ledger_after)
            installed = True
            _install_staged(staged_current, destination)
            if staged_previous is not None:
                _install_staged(staged_previous, previous_destination)
            else:
                previous_destination.unlink(missing_ok=True)
            save_pages_doc(root, doc)
            _finish_artifact_transaction(root)
        except Exception:
            if installed:
                restore_error = None
                try:
                    _restore_file(destination, current_backup, current_existed)
                    current_backup = None
                    _restore_file(previous_destination, previous_backup, previous_existed)
                    previous_backup = None
                except Exception as exc:  # pragma: no cover - catastrophic disk failure
                    restore_error = exc
                if restore_error is not None:
                    raise RuntimeError("publish failed and artifact restore failed") from restore_error
            _finish_artifact_transaction(root)
            raise
        finally:
            for path in (staged_current, staged_previous, current_backup, previous_backup):
                if path is not None:
                    path.unlink(missing_ok=True)
        return {"path": destination, "previous": published["previous"],
                "current": published["current"]}


def _publication_source(root: Path, publication: dict[str, Any]) -> Path | None:
    for name in ("published_path", "candidate_path"):
        path = _stored_path(root, publication.get(name))
        if path and path.is_file():
            return path
    return None


def _publication_history_source(root: Path, page_rec: dict[str, Any],
                                publication: dict[str, Any] | None) -> Path | None:
    """Resolve history bytes without confusing current and previous fixed paths."""
    if not publication:
        return None
    current = (page_rec.get("published") or {}).get("current")
    if publication is current:
        return _publication_source(root, publication)
    fixed_current = published_path(root, page_rec["sid"], page_rec["page"])
    raw = _stored_path(root, publication.get("published_path"))
    if raw == fixed_current:
        candidate = _find_candidate(page_rec, int(publication.get("version", -1)))
        candidate_artifact = _stored_path(root, (candidate or {}).get("path"))
        if candidate_artifact and candidate_artifact.is_file():
            return candidate_artifact
        legacy_candidate = _stored_path(root, publication.get("candidate_path"))
        if legacy_candidate and legacy_candidate.is_file():
            return legacy_candidate
        return None
    return _publication_source(root, publication)


def _publication_from_existing(root: Path, page_rec: dict[str, Any],
                               publication: dict[str, Any], destination: Path,
                               source: Path | None = None) -> dict[str, Any]:
    candidate = _find_candidate(page_rec, int(publication.get("version", -1))) or {}
    source = source or _publication_source(root, publication)
    if source is None:
        raise FileNotFoundError(
            f"publication artifact missing: {page_rec.get('sid')}:p{page_rec.get('page')}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source != destination:
        shutil.copy2(source, destination)
    migrated = dict(publication)
    migrated.update({
        "sid": page_rec["sid"], "page": page_rec["page"],
        "version": int(publication.get("version") or candidate.get("version") or 0),
        "published_path": _relative_path(root, destination),
        "image_sha256": sha256_file(destination),
    })
    for name in ("source_hash", "template_hash", "prompt_hash"):
        value = publication.get(name, candidate.get(name, page_rec.get(name)))
        if value is not None:
            migrated[name] = value
    migrated.pop("candidate_path", None)
    return migrated


def migrate_publication_store(root: str | Path) -> dict[str, Any]:
    """Copy legacy publications into durable current/previous paths and add metadata."""
    root = Path(root).resolve()
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        migrated_pages = 0
        copied_current = 0
        copied_previous = 0
        for page_rec in doc["pages"].values():
            versions = _recorded_versions(page_rec)
            page_rec["version_high_watermark"] = max(
                int(page_rec.get("version_high_watermark") or 0), *versions, 0)
            published = page_rec.setdefault("published", {"previous": None, "current": None})
            current = published.get("current")
            previous = published.get("previous")
            if current:
                destination = published_path(root, page_rec["sid"], page_rec["page"])
                source = _publication_source(root, current)
                if source is None and destination.is_file():
                    source = destination
                needed_copy = source is not None and source != destination
                published["current"] = _publication_from_existing(
                    root, page_rec, current, destination, source)
                copied_current += int(needed_copy)
            if previous:
                destination = previous_published_path(root, page_rec["sid"], page_rec["page"])
                source = _publication_source(root, previous)
                if source is None and destination.is_file():
                    source = destination
                if source is not None:
                    published["previous"] = _publication_from_existing(
                        root, page_rec, previous, destination, source)
                    copied_previous += 1
                else:
                    published["previous"] = None
            page_rec["updated_at"] = _now()
            migrated_pages += 1
        save_pages_doc(root, doc)
        return {"pages": migrated_pages, "current": copied_current,
                "previous": copied_previous}


def compact_candidates(root: str | Path, retain: Iterable[Any] | None = None,
                       force: bool = False) -> dict[str, Any]:
    """Prune resolved candidate artifacts; unresolved artifacts require ``force``."""
    root = Path(root).resolve()
    explicit_retain = retain is not None
    requested = {_expected_candidate(item) for item in (retain or ())}
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        known = {
            (page_rec["sid"], int(page_rec["page"]), int(candidate["version"]))
            for page_rec in doc["pages"].values()
            for candidate in page_rec.get("candidates", [])
        }
        unknown = requested - known
        if unknown:
            labels = ", ".join(f"{sid}:p{page}:v{version:04d}"
                               for sid, page, version in sorted(unknown))
            raise KeyError(f"retained candidates not found: {labels}")
        unresolved = {
            (page_rec["sid"], int(page_rec["page"]), int(candidate["version"]))
            for page_rec in doc["pages"].values()
            for candidate in page_rec.get("candidates", [])
            if candidate.get("status") in _UNRESOLVED_STATUSES
        }
        if explicit_retain and not force:
            omitted = unresolved - requested
            if omitted:
                labels = ", ".join(f"{sid}:p{page}:v{version:04d}"
                                   for sid, page, version in sorted(omitted))
                raise ValueError(f"retain omits unresolved candidates: {labels}")
        retained = requested | (unresolved if not explicit_retain and not force else set())
        for sid, page, version in retained:
            candidate = _find_candidate(doc["pages"][_key(sid, page)], version)
            path = _stored_path(root, (candidate or {}).get("path"))
            if path is None or not path.is_file():
                raise FileNotFoundError(path or (candidate or {}).get("path", ""))

        to_unlink: list[Path] = []
        pruned = 0
        kept = 0
        already_missing = 0
        for page_rec in doc["pages"].values():
            page_rec["version_high_watermark"] = max(
                int(page_rec.get("version_high_watermark") or 0),
                *_recorded_versions(page_rec), 0)
            for candidate in page_rec.get("candidates", []):
                identity = (page_rec["sid"], int(page_rec["page"]),
                            int(candidate["version"]))
                path = _stored_path(root, candidate.get("path"))
                if identity in retained:
                    candidate.pop("artifact_pruned", None)
                    candidate.pop("artifact_pruned_at", None)
                    kept += 1
                    continue
                if path and path.is_file():
                    to_unlink.append(path)
                    pruned += 1
                else:
                    already_missing += 1
                candidate["artifact_pruned"] = True
                candidate.setdefault("artifact_pruned_at", _now())
            page_rec["updated_at"] = _now()
        save_pages_doc(root, doc)
        for path in to_unlink:
            path.unlink(missing_ok=True)
        return {"pruned": pruned, "retained": kept,
                "already_missing": already_missing}


def compact_publication_history(root: str | Path) -> dict[str, Any]:
    """Delete rollback images after atomically clearing their ledger records."""
    root = Path(root).resolve()
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        cleared = 0
        for page_rec in doc["pages"].values():
            page_rec["version_high_watermark"] = max(
                int(page_rec.get("version_high_watermark") or 0),
                *_recorded_versions(page_rec), 0)
            published = page_rec.setdefault("published", {"previous": None, "current": None})
            if published.get("previous"):
                published["previous"] = None
                cleared += 1
            page_rec["updated_at"] = _now()
        save_pages_doc(root, doc)

        previous_dir = root / _STORE / "published" / "previous"
        paths = list(previous_dir.glob("*.png")) if previous_dir.exists() else []
        for path in paths:
            path.unlink(missing_ok=True)
        try:
            previous_dir.rmdir()
        except OSError:
            pass
        return {"removed": len(paths), "cleared": cleared}


def _expected_candidate(item: Any) -> tuple[str, int, int]:
    if isinstance(item, dict):
        return _sid(item["sid"]), _page_no(item["page"]), int(item["version"])
    if isinstance(item, str):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?::p|_p)(\d+)(?::v|_v)(\d+)", item)
        if not match:
            raise ValueError(f"invalid retained candidate: {item!r}")
        return _sid(match.group(1)), _page_no(int(match.group(2))), int(match.group(3))
    sid, page, version = item
    return _sid(sid), _page_no(page), int(version)


def rollback_published(root: str | Path, sid: str, page: int) -> dict[str, Any]:
    root = Path(root).resolve()
    with _ledger_lock(root):
        doc = load_pages_doc(root)
        page_rec = doc["pages"].get(_key(sid, page))
        if not page_rec:
            raise KeyError(f"page not found: {_key(sid, page)}")
        published = page_rec.get("published", {})
        previous, current = published.get("previous"), published.get("current")
        if not previous:
            raise ValueError("no previous publication to restore")
        destination = published_path(root, sid, page)
        previous_destination = previous_published_path(root, sid, page)
        source = _publication_history_source(root, page_rec, previous)
        if source is None or not source.is_file():
            raise FileNotFoundError(previous.get("published_path") or
                                    previous.get("candidate_path") or "")
        if not destination.is_file():
            raise FileNotFoundError(destination)

        staged_current = _stage_copy(source, destination, "rollback")
        staged_previous = _stage_copy(destination, previous_destination, "rollback")
        current_existed, current_backup = _backup_existing(destination)
        previous_existed, previous_backup = _backup_existing(previous_destination)
        restored = {**previous, "published_path": _relative_path(root, destination),
                    "image_sha256": sha256_file(staged_current),
                    "published_at": _now(), "rolled_back": True}
        restored.pop("candidate_path", None)
        rollback = {**(current or {}),
                    "published_path": _relative_path(root, previous_destination),
                    "image_sha256": sha256_file(staged_previous)}
        rollback.pop("candidate_path", None)
        published["current"], published["previous"] = restored, rollback
        stale = bool(_candidate_hash_mismatches(page_rec, restored))
        page_rec["status"] = "stale" if stale else "published"
        page_rec["stale"] = stale
        page_rec["updated_at"] = _now()
        ledger_after = sha256_text(_json_text(doc))
        installed = False
        try:
            _write_artifact_journal(root, "rollback", [
                (destination, staged_current, current_backup, current_existed),
                (previous_destination, staged_previous, previous_backup, previous_existed),
            ], ledger_after)
            installed = True
            _install_staged(staged_current, destination)
            _install_staged(staged_previous, previous_destination)
            save_pages_doc(root, doc)
            _finish_artifact_transaction(root)
        except Exception:
            if installed:
                restore_error = None
                try:
                    _restore_file(destination, current_backup, current_existed)
                    current_backup = None
                    _restore_file(previous_destination, previous_backup, previous_existed)
                    previous_backup = None
                except Exception as exc:  # pragma: no cover - catastrophic disk failure
                    restore_error = exc
                if restore_error is not None:
                    raise RuntimeError("rollback failed and artifact restore failed") from restore_error
            _finish_artifact_transaction(root)
            raise
        finally:
            for path in (staged_current, staged_previous, current_backup, previous_backup):
                if path is not None:
                    path.unlink(missing_ok=True)
        return {"path": destination, "previous": published["previous"],
                "current": published["current"]}


def create_job(root: str | Path, metadata: dict[str, Any] | None = None,
               **fields: Any) -> dict[str, Any]:
    with _ledger_lock(root):
        job_id = str(uuid.uuid4())
        job = {"job_id": job_id, "status": "generating", "created_at": _now(),
               "updated_at": _now(), **(metadata or {}), **fields}
        save_json(Path(root) / _STORE / "jobs" / f"{job_id}.json", job)
        return job


def get_job(root: str | Path, job_id: str) -> dict[str, Any] | None:
    return load_json(Path(root) / _STORE / "jobs" / f"{job_id}.json")


def update_job(root: str | Path, job_id: str,
               updates: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
    with _ledger_lock(root):
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


def gate_summary(root: str | Path, expected_pages: Iterable[Any],
                 verify_artifacts: bool = False) -> dict[str, Any]:
    root = Path(root)
    expected_pages = list(expected_pages)
    pages = load_pages_doc(root)["pages"]
    lists = {name: [] for name in ("published", "pending", "stale", "missing",
                                   "blocked", "waived", "update_review")}
    for item in expected_pages:
        sid, page = _expected_page(item)
        label = f"{sid}:p{page}"
        rec = pages.get(label)
        if rec is None:
            lists["missing"].append(label)
            continue
        publication = rec.get("published", {}).get("current")
        publication_path = (_stored_path(root, publication.get("published_path"))
                            if publication else None)
        if publication and (publication_path is None or not publication_path.is_file()):
            legacy_path = published_path(root, sid, page)
            publication_path = legacy_path if legacy_path.is_file() else None
        if verify_artifacts and publication and publication_path:
            digest = str(publication.get("image_sha256") or "")
            if not _SHA256_RE.fullmatch(digest) or sha256_file(publication_path) != digest:
                publication_path = None
                rec["stale"] = True
        candidates = sorted(
            (_candidate_view(root, candidate) for candidate in rec.get("candidates", [])),
            key=lambda candidate: int(candidate.get("version", 0)))
        active_candidates = [candidate for candidate in candidates
                             if candidate["artifact_exists"]]
        latest = active_candidates[-1] if active_candidates else None
        if rec.get("stale"):
            lists["stale"].append(label)
        elif publication and publication_path:
            lists["published"].append(label)
            if publication.get("waived"):
                lists["waived"].append(label)
            if latest and int(latest.get("version", 0)) > int(publication.get("version", 0)):
                if latest.get("status") in {"pending", "review", "blocked", "waived"}:
                    lists["update_review"].append(label)
        elif not latest:
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


def can_build_final(root: str | Path, expected_pages: Iterable[Any],
                    verify_artifacts: bool = True) -> bool:
    """Safety predicate: formal output requires intact published artifacts."""
    expected_pages = list(expected_pages)
    return bool(gate_summary(root, expected_pages,
                             verify_artifacts=verify_artifacts)["can_build_final"])
