#!/usr/bin/env python3
"""Durable, local memory primitives for BidViz.

The module deliberately has no CLI or third-party dependencies.  CLI and Web callers
may compose these functions without importing either application surface.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
import tomllib

MEMORY_SCHEMA = "bidviz.memory/v1"
EVENT_SCHEMA = "bidviz.event/v1"
RUN_SCHEMA = "bidviz.run/v1"

STATUSES = frozenset(
    {
        "candidate",
        "corroborated",
        "machine_verified",
        "human_signed",
        "rejected",
        "stale",
        "superseded",
        "expired",
    }
)
ACTIVE_STATUSES = frozenset({"corroborated", "machine_verified", "human_signed"})
TERMINAL_STATUSES = frozenset({"rejected", "stale", "superseded", "expired"})
SCOPE_FIELDS = ("project", "engine", "operation", "section", "page", "provider", "model")
REQUIRED_FIELDS = (
    "schema",
    "id",
    "revision",
    "kind",
    "status",
    "title",
    "claim",
    "tags",
    "scope",
    "created_at",
    "updated_at",
    "last_verified_at",
    "valid_until",
    "supersedes",
    "conflict_key",
    "evidence",
    "depends_on",
)

_ROOT_ORDER = (
    "schema",
    "id",
    "revision",
    "kind",
    "status",
    "title",
    "claim",
    "tags",
    "created_at",
    "updated_at",
    "last_verified_at",
    "valid_until",
    "supersedes",
    "superseded_by",
    "conflict_key",
    "scope",
    "authority",
    "invalidation",
    "evidence",
    "depends_on",
    "promotions",
)
_TABLE_ORDERS = {
    "scope": SCOPE_FIELDS,
    "authority": ("signed_by", "signed_at", "content_sha256"),
    "invalidation": ("reason", "by", "at", "previous_status"),
    "evidence": (
        "kind",
        "ref",
        "hash",
        "outcome",
        "episode_id",
        "run_id",
        "observed_at",
    ),
    "depends_on": ("path", "subject", "expected_hash"),
    "promotions": ("from", "to", "at", "by"),
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHORT_CJK_RE = re.compile(r"^[\u3400-\u9fff]{1,2}$")
_PASS_OUTCOMES = frozenset({"pass", "passed", "success", "succeeded", "ok", "verified", "approved"})
_FAIL_OUTCOMES = frozenset(
    {"fail", "failed", "failure", "error", "rejected", "contradicted", "counterexample", "unresolved"}
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x_api_key",
        "xapikey",
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "access-token",
        "accesstoken",
        "refresh_token",
        "refresh-token",
        "refreshtoken",
        "auth_token",
        "auth-token",
        "authtoken",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "client-secret",
        "clientsecret",
        "cookie",
        "set-cookie",
        "headers",
        "request_headers",
        "prompt",
        "full_prompt",
        "source_text",
        "original_text",
        "raw_text",
        "reasoning",
        "chain_of_thought",
    }
)
_URL_SECRET_QUERY_KEY_RE = re.compile(
    r"(?i)([?&](?:(?:x[_-]?(?:amz|goog|oss)[_-]?)?(?:token|security[_-]?token|credential|access[_-]?key(?:id)?|sig|signature)|api[_-]?key|x[_-]?api[_-]?key|auth[_-]?token|key)=)[^&#\s]+"
)
_TEXT_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|x[_-]?api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)=)[^&\s]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    # 冒号形式的凭据头/赋值：X-Api-Key: …、token: …
    re.compile(
        r"(?i)\b(x-?api-?key|authorization|api-?key|access[_-]?token|auth[_-]?token|secret|password|token)\s*[:=]\s*[A-Za-z0-9._~+/=-]+"
    ),
    # 常见平台令牌前缀：GitHub / Slack / AWS / GitLab
    re.compile(r"\b(?:ghp_|gho_|ghu_|github_pat_|xox[baprs]-|AKIA[0-9A-Z]{16}|glpat-)[A-Za-z0-9_.\-]{8,}"),
)


class MemoryErrorBase(Exception):
    """Base class for memory-kernel errors."""


class RecordValidationError(MemoryErrorBase, ValueError):
    """A durable record does not satisfy the v1 contract."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class RecordConflictError(MemoryErrorBase, ValueError):
    """A save would create an unresolved conflict."""


class PromotionError(MemoryErrorBase, ValueError):
    """A requested promotion does not pass its evidence gate."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def isoformat(value: dt.datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_datetime(value: dt.datetime | str | None) -> dt.datetime:
    if value is None:
        return utc_now()
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
    parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def safe_relative_path(root: str | Path, relative: str | Path) -> Path:
    """Resolve *relative* below *root*, rejecting traversal and absolute paths."""
    root_path = Path(root).resolve()
    candidate_arg = Path(relative)
    if candidate_arg.is_absolute():
        raise ValueError(f"absolute path is not allowed: {relative}")
    candidate = (root_path / candidate_arg).resolve()
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {relative}") from exc
    return candidate


def atomic_write(path: str | Path, data: str | bytes, *, encoding: str = "utf-8") -> Path:
    """Write a file through a same-directory temporary and ``os.replace``."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode(encoding) if isinstance(data, str) else bytes(data)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise
    return destination


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:  # Windows does not permit opening every directory this way.
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: str | Path, value: Any) -> Path:
    return atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _ordered_keys(table: Mapping[str, Any], table_name: str = "") -> list[str]:
    preferred = _ROOT_ORDER if not table_name else _TABLE_ORDERS.get(table_name, ())
    return [key for key in preferred if key in table] + sorted(key for key in table if key not in preferred)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        serialized = json.dumps(value, ensure_ascii=False)
        # json 不转义 U+007F，而 TOML 基本串禁止裸 DEL；不转义会产生无法重载的毒记录。
        return serialized.replace("\x7f", "\\u007f")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("TOML serializer does not accept non-finite floats")
        return repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, Mapping) for item in value):
            raise TypeError("arrays of tables are emitted structurally, not as values")
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def toml_dumps(document: Mapping[str, Any]) -> str:
    """Serialize the narrow TOML subset used by durable memory records.

    Supported values are strings, booleans, finite numbers, dates, scalar arrays,
    nested tables, and arrays of flat tables.  Keys and table order are stable.
    """
    if not isinstance(document, Mapping):
        raise TypeError("TOML document must be a mapping")
    lines: list[str] = []

    def emit_table(table: Mapping[str, Any], path: tuple[str, ...], *, header: bool) -> None:
        name = path[-1] if path else ""
        if header:
            lines.append("[" + ".".join(_toml_key(part) for part in path) + "]")
        children: list[tuple[str, Mapping[str, Any]]] = []
        arrays: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
        for key in _ordered_keys(table, name):
            value = table[key]
            if value is None:
                raise TypeError(f"TOML has no null value: {'.'.join((*path, key))}")
            if isinstance(value, Mapping):
                children.append((key, value))
            elif isinstance(value, (list, tuple)) and value and all(
                isinstance(item, Mapping) for item in value
            ):
                arrays.append((key, value))
            else:
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
        for key, child in children:
            if lines and lines[-1] != "":
                lines.append("")
            emit_table(child, (*path, key), header=True)
        for key, items in arrays:
            for item in items:
                if any(isinstance(value, Mapping) for value in item.values()):
                    raise TypeError("nested tables inside arrays of tables are not supported")
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append("[[" + ".".join(_toml_key(part) for part in (*path, key)) + "]]" )
                for item_key in _ordered_keys(item, key):
                    item_value = item[item_key]
                    if item_value is None:
                        raise TypeError(f"TOML has no null value: {key}.{item_key}")
                    lines.append(f"{_toml_key(item_key)} = {_toml_value(item_value)}")

    emit_table(document, (), header=False)
    return "\n".join(lines).rstrip() + "\n"


def toml_load(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        return tomllib.load(handle)


def redact(value: Any) -> Any:
    """Return a recursively redacted, JSON-compatible copy of *value*."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in _SENSITIVE_KEYS:
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact(item)
        return redacted
    if isinstance(value, (list, tuple, set)):
        return [redact(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"[BYTES sha256={sha256_bytes(value)} size={len(value)}]"
    if isinstance(value, str):
        result = _URL_SECRET_QUERY_KEY_RE.sub(
            lambda match: f"{match.group(1)}[REDACTED]", value
        )
        for pattern in _TEXT_SECRET_PATTERNS:
            if pattern.groups:
                result = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", result)
            else:
                result = pattern.sub("[REDACTED]", result)
        return result
    return value


def recover_jsonl(path: str | Path) -> dict[str, Any]:
    """Validate JSONL and repair only a malformed/truncated final line.

    A malformed interior line is corruption and raises ``ValueError``.  A valid
    final object lacking its newline is completed rather than discarded.
    """
    events_path = Path(path)
    if not events_path.exists():
        return {"events": 0, "last_seq": 0, "recovered": False, "truncated_bytes": 0}
    data = events_path.read_bytes()
    if not data:
        return {"events": 0, "last_seq": 0, "recovered": False, "truncated_bytes": 0}
    lines = data.splitlines(keepends=True)
    valid_end = 0
    last_seq = 0
    count = 0
    for index, raw in enumerate(lines):
        final = index == len(lines) - 1
        body = raw.rstrip(b"\r\n")
        try:
            event = json.loads(body.decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            seq = event.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                raise ValueError(f"non-monotonic seq {seq!r}")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            if not final:
                raise ValueError(f"malformed JSONL line {index + 1}: {exc}") from exc
            truncated = len(data) - valid_end
            with events_path.open("r+b") as handle:
                handle.truncate(valid_end)
                handle.flush()
                os.fsync(handle.fileno())
            return {
                "events": count,
                "last_seq": last_seq,
                "recovered": True,
                "truncated_bytes": truncated,
            }
        count += 1
        last_seq = seq
        valid_end += len(raw)
    if data and not data.endswith((b"\n", b"\r")):
        fd = os.open(str(events_path), os.O_WRONLY | os.O_APPEND)
        try:
            written = os.write(fd, b"\n")
            if written != 1:
                raise OSError("short JSONL newline write")
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"events": count, "last_seq": last_seq, "recovered": True, "truncated_bytes": 0}
    return {"events": count, "last_seq": last_seq, "recovered": False, "truncated_bytes": 0}


def read_events(path: str | Path, *, recover: bool = True) -> list[dict[str, Any]]:
    events_path = Path(path)
    if recover:
        recover_jsonl(events_path)
    if not events_path.exists():
        return []
    result: list[dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed JSONL line {line_number}: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"JSONL line {line_number} is not an object")
            result.append(event)
    return result


class _EventFileLock:
    """Bounded cross-process lock backed by a one-byte stdlib file lock."""

    def __init__(self, path: Path, *, timeout: float = 10.0, retry: float = 0.01) -> None:
        self.path = path
        self.timeout = timeout
        self.retry = retry
        self._handle: Any = None

    def __enter__(self) -> "_EventFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._handle = handle
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"timed out locking {self.path}") from exc
                time.sleep(self.retry)

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        handle = self._handle
        if handle is None:
            return False
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None
        return False


class RunRecorder:
    """Append redacted run events with one ``os.write`` call per JSONL record."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        fsync: bool = True,
    ) -> None:
        supplied = Path(path)
        if supplied.suffix.casefold() == ".jsonl":
            self.events_path = supplied
            self.run_dir = supplied.parent
        else:
            self.run_dir = supplied
            self.events_path = supplied / "events.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._event_lock_path = self.events_path.with_name(f".{self.events_path.name}.lock")
        with _EventFileLock(self._event_lock_path):
            recovery = recover_jsonl(self.events_path)
        self._seq = int(recovery["last_seq"])
        self._lock = threading.Lock()
        self._fsync = fsync
        existing_run = _read_json(self.run_dir / "run.json", default={})
        self.run_id = str(run_id or existing_run.get("run_id") or self.run_dir.name or uuid.uuid4())
        if not _ID_RE.fullmatch(self.run_id):
            # UUIDs and filesystem-safe run names pass; arbitrary names become a stable UUID.
            self.run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.run_id))
        if not existing_run:
            run_document = {
                **redact(dict(metadata or {})),
                "schema": RUN_SCHEMA,
                "run_id": self.run_id,
                "status": "running",
                "started_at": isoformat(),
            }
            atomic_write_json(self.run_dir / "run.json", run_document)

    @property
    def next_seq(self) -> int:
        return self._seq + 1

    def record(self, event: str | Mapping[str, Any], /, **fields: Any) -> dict[str, Any]:
        if isinstance(event, Mapping):
            supplied = dict(event)
            event_name = str(supplied.pop("event", supplied.pop("type", "event")))
            supplied.update(fields)
        else:
            event_name = str(event)
            supplied = dict(fields)
        with self._lock, _EventFileLock(self._event_lock_path):
            recovery = recover_jsonl(self.events_path)
            sequence = int(recovery["last_seq"]) + 1
            payload = {
                **supplied,
                "schema": EVENT_SCHEMA,
                "run_id": self.run_id,
                "seq": sequence,
                "timestamp": isoformat(),
                "event": event_name,
            }
            payload = redact(payload)
            encoded = (canonical_json(payload) + "\n").encode("utf-8")
            fd = os.open(str(self.events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                written = os.write(fd, encoded)
                if written != len(encoded):
                    raise OSError(f"short JSONL write: {written}/{len(encoded)}")
                if self._fsync:
                    os.fsync(fd)
            finally:
                os.close(fd)
            self._seq = sequence
        return payload

    event = record

    def close(self, status: str = "success", **outcome: Any) -> dict[str, Any]:
        result = {
            "schema": RUN_SCHEMA,
            "run_id": self.run_id,
            "status": status,
            "finished_at": isoformat(),
            "last_seq": self._seq,
            **redact(outcome),
        }
        atomic_write_json(self.run_dir / "outcome.json", result)
        run_path = self.run_dir / "run.json"
        run_document = _read_json(run_path, default={})
        run_document.update({key: result[key] for key in ("status", "finished_at", "last_seq")})
        atomic_write_json(run_path, run_document)
        return result

    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> bool:
        if exc is None:
            self.close("success")
        else:
            self.record("error", error_type=exc_type.__name__, error=str(exc))
            self.close("error", error_type=exc_type.__name__, error=str(exc))
        return False


def _read_json(path: Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def make_record(
    record_id: str,
    *,
    kind: str,
    title: str,
    claim: str,
    status: str = "candidate",
    tags: Sequence[str] = (),
    scope: Mapping[str, Any] | None = None,
    evidence: Sequence[Mapping[str, Any]] = (),
    depends_on: Sequence[Mapping[str, Any]] = (),
    conflict_key: str = "",
    supersedes: Sequence[str] = (),
    now: dt.datetime | str | None = None,
    valid_until: dt.datetime | str | None = None,
) -> dict[str, Any]:
    timestamp = isoformat(_as_datetime(now))
    scope_document = {key: "" for key in SCOPE_FIELDS}
    scope_document.update(dict(scope or {}))
    record: dict[str, Any] = {
        "schema": MEMORY_SCHEMA,
        "id": record_id,
        "revision": 1,
        "kind": kind,
        "status": status,
        "title": title,
        "claim": claim,
        "tags": sorted(dict.fromkeys(str(tag) for tag in tags)),
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_verified_at": "",
        "valid_until": isoformat(_as_datetime(valid_until)) if valid_until else "",
        "supersedes": list(dict.fromkeys(str(item) for item in supersedes)),
        "conflict_key": conflict_key,
        "scope": scope_document,
        "evidence": [dict(item) for item in evidence],
        "depends_on": [dict(item) for item in depends_on],
    }
    if kind in {"policy", "decision"}:
        record["authority"] = {"signed_by": "", "signed_at": "", "content_sha256": ""}
    return record


def validate_record(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, Mapping):
        return ["record must be an object"]
    for field in REQUIRED_FIELDS:
        if field not in record:
            errors.append(f"missing required field: {field}")
    schema = record.get("schema")
    if schema != MEMORY_SCHEMA:
        errors.append(f"schema must be {MEMORY_SCHEMA!r}")
    record_id = record.get("id")
    if not isinstance(record_id, str) or not _ID_RE.fullmatch(record_id):
        errors.append("id must be a filesystem-safe identifier of at most 128 characters")
    revision = record.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append("revision must be a positive integer")
    if not isinstance(record.get("kind"), str) or not str(record.get("kind", "")).strip():
        errors.append("kind must be a non-empty string")
    if record.get("status") not in STATUSES:
        errors.append(f"status must be one of: {', '.join(sorted(STATUSES))}")
    for field in ("title", "claim"):
        if not isinstance(record.get(field), str) or not str(record.get(field, "")).strip():
            errors.append(f"{field} must be a non-empty string")
    tags = record.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag for tag in tags):
        errors.append("tags must be an array of non-empty strings")
    scope = record.get("scope")
    if not isinstance(scope, Mapping):
        errors.append("scope must be a table")
    else:
        for field in SCOPE_FIELDS:
            if field not in scope:
                errors.append(f"missing required scope field: {field}")
            elif isinstance(scope[field], (Mapping, bool)) or scope[field] is None:
                errors.append(f"scope.{field} must be a scalar or scalar array")
    for field in ("created_at", "updated_at", "last_verified_at", "valid_until"):
        value = record.get(field)
        if not isinstance(value, str):
            errors.append(f"{field} must be an ISO-8601 string (empty is allowed)")
        elif value:
            try:
                _as_datetime(value)
            except (TypeError, ValueError):
                errors.append(f"{field} is not valid ISO-8601")
    supersedes = record.get("supersedes")
    if not isinstance(supersedes, list) or any(not isinstance(item, str) for item in supersedes):
        errors.append("supersedes must be an array of record IDs")
    if not isinstance(record.get("conflict_key"), str):
        errors.append("conflict_key must be a string")
    evidence = record.get("evidence")
    if not isinstance(evidence, list):
        errors.append("evidence must be an array of tables")
    else:
        for index, item in enumerate(evidence):
            if not isinstance(item, Mapping):
                errors.append(f"evidence[{index}] must be a table")
                continue
            for field in ("kind", "ref", "hash", "outcome"):
                if not isinstance(item.get(field), str) or not item.get(field):
                    errors.append(f"evidence[{index}].{field} is required")
            digest = item.get("hash")
            if isinstance(digest, str) and digest and not _SHA256_RE.fullmatch(digest.casefold()):
                errors.append(f"evidence[{index}].hash must be a lowercase SHA-256 digest")
    dependencies = record.get("depends_on")
    if not isinstance(dependencies, list):
        errors.append("depends_on must be an array of tables")
    else:
        for index, item in enumerate(dependencies):
            if not isinstance(item, Mapping):
                errors.append(f"depends_on[{index}] must be a table")
                continue
            if bool(item.get("path")) == bool(item.get("subject")):
                errors.append(f"depends_on[{index}] requires exactly one of path or subject")
            expected = item.get("expected_hash", item.get("expected_sha256"))
            if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected.casefold()):
                errors.append(f"depends_on[{index}].expected_hash must be a SHA-256 digest")
    if record.get("kind") in {"policy", "decision"}:
        authority = record.get("authority")
        if not isinstance(authority, Mapping):
            errors.append("policy/decision records require an authority table")
        else:
            for field in ("signed_by", "signed_at", "content_sha256"):
                if field not in authority or not isinstance(authority[field], str):
                    errors.append(f"authority.{field} must be present as a string")
    if record.get("status") == "human_signed":
        authority = record.get("authority")
        if not isinstance(authority, Mapping):
            errors.append("human_signed records require authority")
        else:
            if not str(authority.get("signed_by", "")).strip():
                errors.append("human_signed records require authority.signed_by")
            if not str(authority.get("signed_at", "")).strip():
                errors.append("human_signed records require authority.signed_at")
            digest = str(authority.get("content_sha256", ""))
            if not _SHA256_RE.fullmatch(digest.casefold()):
                errors.append("human_signed records require authority.content_sha256")
            elif digest.casefold() != record_content_hash(record):
                errors.append("authority.content_sha256 does not match record content")
    return errors


def assert_valid_record(record: Mapping[str, Any]) -> None:
    errors = validate_record(record)
    if errors:
        raise RecordValidationError(errors)


def record_content_hash(record: Mapping[str, Any]) -> str:
    """Hash all authoritative content except the signature digest itself."""
    signed_fields = {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in {"updated_at", "last_verified_at"} and not str(key).startswith("_")
    }
    authority = signed_fields.get("authority")
    if isinstance(authority, Mapping):
        signed_fields["authority"] = {
            key: copy.deepcopy(value)
            for key, value in authority.items()
            if key != "content_sha256"
        }
    return sha256_text(canonical_json(signed_fields))


def iter_record_paths(memory_root: str | Path) -> list[Path]:
    root = Path(memory_root)
    if not root.exists():
        return []
    return sorted(
        (path for path in root.rglob("*.toml") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )


def load_record(path: str | Path) -> dict[str, Any]:
    record = toml_load(path)
    if not isinstance(record, dict):
        raise RecordValidationError(["record must be a TOML table"])
    return record


def _record_directory(kind: str) -> str:
    return {
        "policy": "policies",
        "decision": "decisions",
        "procedure": "procedures",
        "knowledge": "knowledge",
    }.get(kind, "knowledge")


def find_record_path(memory_root: str | Path, record_id: str) -> Path | None:
    for path in iter_record_paths(memory_root):
        try:
            if load_record(path).get("id") == record_id:
                return path
        except (OSError, tomllib.TOMLDecodeError):
            continue
    return None


def save_record(
    memory_root: str | Path,
    record: Mapping[str, Any],
    *,
    relative_path: str | Path | None = None,
    strict_conflicts: bool = False,
) -> Path:
    document = copy.deepcopy(dict(record))
    assert_valid_record(document)
    root = Path(memory_root)
    with _EventFileLock(root / ".records.lock"):
        existing = find_record_path(root, str(document["id"]))
        if existing is not None:
            current = load_record(existing)
            if document == current:
                return existing
            if int(document["revision"]) <= int(current.get("revision", 1)):
                raise RecordValidationError(["changed record revision must strictly increase"])
            destination = existing
        else:
            relative_path = relative_path or Path(_record_directory(str(document["kind"]))) / f"{document['id']}.toml"
            destination = safe_relative_path(root, relative_path)
        if strict_conflicts:
            conflicts = find_conflicts(document, list_records(root, effective=True))
            if conflicts:
                raise RecordConflictError(
                    f"conflict_key {document['conflict_key']!r} is already active in: "
                    + ", ".join(item["id"] for item in conflicts)
                )
        atomic_write(destination, toml_dumps(document))
        return destination


def save_record_path(path: str | Path, record: Mapping[str, Any]) -> Path:
    """Validate and atomically write an explicit record with revision checks."""
    destination = Path(path)
    document = copy.deepcopy(dict(record))
    assert_valid_record(document)
    with _EventFileLock(destination.with_name(f".{destination.name}.lock")):
        if destination.exists():
            current = load_record(destination)
            if document == current:
                return destination
            if int(document["revision"]) <= int(current.get("revision", 1)):
                raise RecordValidationError(["changed record revision must strictly increase"])
        return atomic_write(destination, toml_dumps(document))


def _project_base(memory_root: str | Path) -> Path:
    root = Path(memory_root).resolve()
    return root.parent if root.name.casefold() == "memory" else root


def _subject_hash(value: Any) -> str:
    if isinstance(value, bytes):
        return sha256_bytes(value)
    if isinstance(value, Path):
        return sha256_file(value)
    if isinstance(value, str) and _SHA256_RE.fullmatch(value.casefold()):
        return value.casefold()
    if isinstance(value, str):
        return sha256_text(value)
    return sha256_text(canonical_json(value))


def status_details(
    record: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stored = str(record.get("status", "candidate"))
    if stored in TERMINAL_STATUSES:
        return {"stored_status": stored, "effective_status": stored, "reasons": []}
    current_time = _as_datetime(now)
    valid_until = record.get("valid_until")
    if isinstance(valid_until, str) and valid_until:
        try:
            if _as_datetime(valid_until) <= current_time:
                return {
                    "stored_status": stored,
                    "effective_status": "expired",
                    "reasons": [f"valid_until reached: {valid_until}"],
                }
        except ValueError:
            return {
                "stored_status": stored,
                "effective_status": "stale",
                "reasons": ["invalid valid_until"],
            }
    reasons: list[str] = []
    base = Path(base_dir).resolve() if base_dir is not None else None
    for dependency in record.get("depends_on", []):
        expected = str(dependency.get("expected_hash", dependency.get("expected_sha256", ""))).casefold()
        if dependency.get("path"):
            if base is None:
                reasons.append(f"dependency base unavailable: {dependency['path']}")
                continue
            try:
                path = safe_relative_path(base, str(dependency["path"]))
            except ValueError:
                reasons.append(f"dependency escapes base: {dependency['path']}")
                continue
            if not path.is_file():
                reasons.append(f"dependency missing: {dependency['path']}")
                continue
            actual = sha256_file(path)
        else:
            subject = str(dependency.get("subject", ""))
            if subjects is None or subject not in subjects:
                reasons.append(f"dependency subject unavailable: {subject}")
                continue
            actual = _subject_hash(subjects[subject])
        if actual != expected:
            target = dependency.get("path", dependency.get("subject", ""))
            reasons.append(f"dependency hash changed: {target}")
    return {
        "stored_status": stored,
        "effective_status": "stale" if reasons else stored,
        "reasons": reasons,
    }


def effective_status(
    record: Mapping[str, Any],
    *,
    base_dir: str | Path | None = None,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> str:
    return str(status_details(record, base_dir=base_dir, now=now, subjects=subjects)["effective_status"])


def list_records(
    memory_root: str | Path,
    *,
    statuses: Iterable[str] | None = None,
    effective: bool = False,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    root = Path(memory_root)
    allowed = set(statuses) if statuses is not None else None
    result: list[dict[str, Any]] = []
    for path in iter_record_paths(root):
        record = load_record(path)
        details = status_details(record, base_dir=_project_base(root), now=now, subjects=subjects)
        selected_status = details["effective_status"] if effective else record.get("status")
        if allowed is not None and selected_status not in allowed:
            continue
        item = copy.deepcopy(record)
        item["_path"] = path.relative_to(root).as_posix()
        if effective:
            item["effective_status"] = details["effective_status"]
            item["effective_reasons"] = details["reasons"]
        result.append(item)
    result.sort(key=lambda item: (str(item.get("id", "")), int(item.get("revision", 0))))
    return result


def validate_memory(
    memory_root: str | Path,
    *,
    fix: bool = False,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(memory_root)
    errors: dict[str, list[str]] = {}
    records: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    fixed: list[str] = []
    for path in iter_record_paths(root):
        relative = path.relative_to(root).as_posix()
        try:
            record = load_record(path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors[relative] = [str(exc)]
            continue
        path_errors = validate_record(record)
        record_id = str(record.get("id", ""))
        if record_id in seen:
            path_errors.append(f"duplicate id also found at {seen[record_id]}")
        else:
            seen[record_id] = relative
        if path_errors:
            errors[relative] = path_errors
            continue
        details = status_details(record, base_dir=_project_base(root), now=now, subjects=subjects)
        if fix and details["effective_status"] in {"stale", "expired"} and record["status"] != details["effective_status"]:
            record["status"] = details["effective_status"]
            record["revision"] += 1
            record["updated_at"] = isoformat(_as_datetime(now))
            record["invalidation"] = {
                "reason": "; ".join(details["reasons"]),
                "by": "memory.validate",
                "at": isoformat(_as_datetime(now)),
                "previous_status": details["stored_status"],
            }
            save_record(root, record)
            fixed.append(record_id)
        item = copy.deepcopy(record)
        item["_path"] = relative
        item["effective_status"] = details["effective_status"]
        records.append(item)
    conflict_list = conflict_groups(records)
    return {
        "ok": not errors and not conflict_list,
        "records": len(records),
        "errors": errors,
        "conflicts": conflict_list,
        "fixed": sorted(fixed),
    }


def _scope_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item).casefold() for item in value}
    return {str(value).casefold()}


def _wildcard_scope(value: Any) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(item in (None, "", "*", "all") for item in value)
    return value in (None, "", "*", "all")


def scope_matches(record_scope: Mapping[str, Any], requested_scope: Mapping[str, Any] | None) -> bool:
    if not requested_scope:
        return True
    for field, requested in requested_scope.items():
        if field not in SCOPE_FIELDS or _wildcard_scope(requested):
            continue
        stored = record_scope.get(field, "")
        if _wildcard_scope(stored):
            continue
        if not (_scope_values(stored) & _scope_values(requested)):
            return False
    return True


def scope_specificity(record_scope: Mapping[str, Any], requested_scope: Mapping[str, Any] | None) -> int:
    if not requested_scope:
        return 0
    score = 0
    for field, requested in requested_scope.items():
        stored = record_scope.get(field, "")
        if field in SCOPE_FIELDS and not _wildcard_scope(requested) and not _wildcard_scope(stored):
            if _scope_values(stored) & _scope_values(requested):
                score += 1
    return score


def _status_priority(record: Mapping[str, Any], status: str, scope: Mapping[str, Any] | None) -> int:
    if record.get("kind") in {"source_of_truth", "authoritative"}:
        return 7000
    base = {"human_signed": 6000, "machine_verified": 5000, "corroborated": 3000, "candidate": 2000}.get(status, 0)
    return base + scope_specificity(record.get("scope", {}), scope) * 10


def detect_fts5(connection: sqlite3.Connection | None = None) -> bool:
    owned = connection is None
    connection = connection or sqlite3.connect(":memory:")
    table = f"__bidviz_fts5_{uuid.uuid4().hex}"
    try:
        connection.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(value)")
        connection.execute(f"DROP TABLE {table}")
        return True
    except sqlite3.DatabaseError:
        return False
    finally:
        if owned:
            connection.close()


def default_index_path(memory_root: str | Path) -> Path:
    root = Path(memory_root)
    base = root.parent if root.name.casefold() == "memory" else root
    return base / "work" / "memory-index.sqlite3"


def rebuild_index(
    memory_root: str | Path,
    index_path: str | Path | None = None,
    *,
    use_fts: bool | None = None,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(memory_root)
    destination = Path(index_path) if index_path is not None else default_index_path(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    connection = sqlite3.connect(temporary)
    fts_enabled = detect_fts5(connection) if use_fts is None else bool(use_fts and detect_fts5(connection))
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            """
            CREATE TABLE records (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                effective_status TEXT NOT NULL,
                title TEXT NOT NULL,
                claim TEXT NOT NULL,
                tags TEXT NOT NULL,
                scope_project TEXT NOT NULL,
                scope_engine TEXT NOT NULL,
                scope_operation TEXT NOT NULL,
                scope_section TEXT NOT NULL,
                scope_page TEXT NOT NULL,
                scope_provider TEXT NOT NULL,
                scope_model TEXT NOT NULL,
                conflict_key TEXT NOT NULL,
                search_text TEXT NOT NULL,
                record_json TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if fts_enabled:
            connection.execute(
                "CREATE VIRTUAL TABLE records_fts USING fts5(id UNINDEXED, title, claim, tags, tokenize='unicode61')"
            )
        count = 0
        for path in iter_record_paths(root):
            record = load_record(path)
            assert_valid_record(record)
            details = status_details(record, base_dir=_project_base(root), now=now, subjects=subjects)
            scope = record.get("scope", {})
            tags_text = " ".join(str(tag) for tag in record.get("tags", []))
            search_text = "\n".join((str(record["id"]), str(record["title"]), str(record["claim"]), tags_text))
            values = (
                record["id"],
                path.relative_to(root).as_posix(),
                record["revision"],
                record["kind"],
                record["status"],
                details["effective_status"],
                record["title"],
                record["claim"],
                tags_text,
                *(str(scope.get(field, "")) for field in SCOPE_FIELDS),
                record.get("conflict_key", ""),
                search_text,
                canonical_json(record),
            )
            connection.execute(
                "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
            if fts_enabled:
                connection.execute(
                    "INSERT INTO records_fts(id,title,claim,tags) VALUES (?,?,?,?)",
                    (record["id"], record["title"], record["claim"], tags_text),
                )
            count += 1
        metadata = {
            "schema": "bidviz.memory-index/v1",
            "fts5": "1" if fts_enabled else "0",
            "record_count": str(count),
        }
        connection.executemany("INSERT INTO meta(key,value) VALUES (?,?)", sorted(metadata.items()))
        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)
    return {"path": destination, "records": count, "fts5": fts_enabled}


def _fts_query(query: str) -> str:
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _query_index(index_path: Path, query: str) -> tuple[list[tuple[dict[str, Any], float]], str]:
    connection = sqlite3.connect(index_path)
    connection.row_factory = sqlite3.Row
    try:
        meta = dict(connection.execute("SELECT key,value FROM meta"))
        fts_enabled = meta.get("fts5") == "1"
        stripped_query = query.strip()
        short_query = bool(_SHORT_CJK_RE.fullmatch(stripped_query)) or (
            len(stripped_query) <= 2 and bool(stripped_query)
        )
        if not query.strip():
            rows = connection.execute("SELECT record_json, 0.0 AS rank FROM records ORDER BY id").fetchall()
            mode = "all"
        elif fts_enabled and not short_query:
            try:
                rows = connection.execute(
                    """
                    SELECT records.record_json, bm25(records_fts) AS rank
                    FROM records_fts JOIN records ON records.id = records_fts.id
                    WHERE records_fts MATCH ? ORDER BY rank, records.id
                    """,
                    (_fts_query(query),),
                ).fetchall()
            except sqlite3.DatabaseError:
                rows = []
            mode = "fts5"
            if not rows:
                rows = _like_rows(connection, query)
                mode = "like"
        else:
            rows = _like_rows(connection, query)
            mode = "like"
        return [(json.loads(row["record_json"]), float(row["rank"])) for row in rows], mode
    finally:
        connection.close()


def _like_rows(connection: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    escaped = query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return connection.execute(
        "SELECT record_json, 0.0 AS rank FROM records WHERE lower(search_text) LIKE ? ESCAPE '\\' ORDER BY id",
        (f"%{escaped}%",),
    ).fetchall()


def _record_matches_query(record: Mapping[str, Any], query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    search_text = "\n".join(
        (
            str(record.get("id", "")),
            str(record.get("title", "")),
            str(record.get("claim", "")),
            " ".join(str(tag) for tag in record.get("tags", [])),
        )
    )
    return needle in search_text.casefold()


def search_records(
    memory_root: str | Path,
    query: str = "",
    *,
    scope: Mapping[str, Any] | None = None,
    statuses: Iterable[str] | None = None,
    include_inactive: bool = False,
    limit: int = 20,
    index_path: str | Path | None = None,
    rebuild_if_missing: bool = True,
    refresh_index: bool = False,
    use_fts: bool | None = None,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if limit < 0:
        raise ValueError("limit cannot be negative")
    root = Path(memory_root)
    chosen_index = Path(index_path) if index_path is not None else default_index_path(root)
    if refresh_index or (rebuild_if_missing and not chosen_index.exists()):
        rebuild_index(root, chosen_index, use_fts=use_fts, now=now, subjects=subjects)
    indexed: list[tuple[dict[str, Any], float]]
    mode = "scan"
    if chosen_index.exists():
        try:
            indexed, mode = _query_index(chosen_index, query)
        except (sqlite3.DatabaseError, OSError, ValueError):
            indexed = []
    else:
        indexed = []
    if not indexed and (not query.strip() or mode == "scan"):
        indexed = [
            (record, 0.0)
            for record in list_records(root)
            if _record_matches_query(record, query)
        ]
    requested_statuses = set(statuses) if statuses is not None else None
    result: list[dict[str, Any]] = []
    base = _project_base(root)
    for record, rank in indexed:
        details = status_details(record, base_dir=base, now=now, subjects=subjects)
        status = str(details["effective_status"])
        if requested_statuses is not None:
            if status not in requested_statuses:
                continue
        elif not include_inactive and status not in ACTIVE_STATUSES:
            continue
        if not scope_matches(record.get("scope", {}), scope):
            continue
        item = copy.deepcopy(record)
        item["effective_status"] = status
        item["effective_reasons"] = details["reasons"]
        item["search_mode"] = mode
        item["score"] = _status_priority(record, status, scope) - rank
        path = find_record_path(root, str(record["id"]))
        if path is not None:
            item["_path"] = path.relative_to(root).as_posix()
        result.append(item)
    result.sort(
        key=lambda item: (
            -_status_priority(item, str(item["effective_status"]), scope),
            -float(item.get("score", 0.0)),
            str(item["id"]),
        )
    )
    return result[:limit] if limit else []


search = search_records


def _format_context_record(record: Mapping[str, Any]) -> str:
    scope = record.get("scope", {})
    specific = ", ".join(
        f"{key}={scope[key]}" for key in SCOPE_FIELDS if not _wildcard_scope(scope.get(key, ""))
    )
    descriptor = f"[{record['id']} | {record['effective_status']} | {record['kind']}"
    if specific:
        descriptor += f" | {specific}"
    return f"{descriptor}]\n{record['title']}\n{record['claim']}"


def materialize_context(
    memory_root: str | Path,
    query: str = "",
    *,
    scope: Mapping[str, Any] | None = None,
    char_budget: int = 4000,
    statuses: Iterable[str] | None = None,
    limit: int = 100,
    index_path: str | Path | None = None,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if char_budget < 0:
        raise ValueError("char_budget cannot be negative")
    matches = search_records(
        memory_root,
        query,
        scope=scope,
        statuses=statuses,
        include_inactive=False,
        limit=limit,
        index_path=index_path,
        now=now,
        subjects=subjects,
    )
    chunks: list[str] = []
    selected: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for record in matches:
        chunk = _format_context_record(record)
        separator = "\n\n" if chunks else ""
        available = char_budget - used - len(separator)
        if available <= 0:
            truncated = True
            break
        if len(chunk) > available:
            if not chunks and available > 0:
                chunk = chunk[:available]
                chunks.append(chunk)
                selected.append(record)
                used += len(chunk)
            truncated = True
            break
        chunks.append(chunk)
        selected.append(record)
        used += len(separator) + len(chunk)
    if len(selected) < len(matches):
        truncated = True
    text = "\n\n".join(chunks)
    hash_payload = {
        "schema": "bidviz.memory-context/v1",
        "scope": dict(scope or {}),
        "records": [
            {"id": item["id"], "revision": item["revision"], "status": item["effective_status"]}
            for item in selected
        ],
        "text": text,
    }
    return {
        "text": text,
        "records": selected,
        "memory_ids": [item["id"] for item in selected],
        "context_hash": sha256_text(canonical_json(hash_payload)),
        "characters": len(text),
        "char_budget": char_budget,
        "truncated": truncated,
    }


context = materialize_context
build_context = materialize_context


def find_conflicts(record: Mapping[str, Any], records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    key = str(record.get("conflict_key", ""))
    if not key:
        return []
    supersedes = set(str(item) for item in record.get("supersedes", []))
    result = []
    for item in records:
        item_id = str(item.get("id", ""))
        status = str(item.get("effective_status", item.get("status", "")))
        if (
            item_id != record.get("id")
            and item.get("conflict_key") == key
            and status in ACTIVE_STATUSES | {"candidate"}
            and item_id not in supersedes
            and str(record.get("id")) not in set(str(value) for value in item.get("supersedes", []))
        ):
            result.append(dict(item))
    return sorted(result, key=lambda item: str(item.get("id", "")))


def conflict_groups(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        key = str(record.get("conflict_key", ""))
        status = str(record.get("effective_status", record.get("status", "")))
        if key and status in ACTIVE_STATUSES | {"candidate"}:
            grouped[key].append(record)
    result: list[dict[str, Any]] = []
    for key, items in grouped.items():
        ids = {str(item.get("id", "")) for item in items}
        superseded = {
            str(value)
            for item in items
            for value in item.get("supersedes", [])
            if str(value) in ids
        }
        unresolved = sorted(ids - superseded)
        if len(unresolved) > 1:
            result.append({"conflict_key": key, "ids": unresolved})
    return sorted(result, key=lambda item: item["conflict_key"])


def invalidate_record(
    memory_root: str | Path,
    record_id: str,
    reason: str,
    *,
    actor: str = "",
    status: str = "stale",
    now: dt.datetime | str | None = None,
) -> dict[str, Any]:
    if status not in {"stale", "rejected"}:
        raise ValueError("invalidation status must be stale or rejected")
    if not reason.strip():
        raise ValueError("invalidation reason is required")
    path = find_record_path(memory_root, record_id)
    if path is None:
        raise KeyError(record_id)
    record = load_record(path)
    previous = str(record["status"])
    record["status"] = status
    record["revision"] += 1
    record["updated_at"] = isoformat(_as_datetime(now))
    record["invalidation"] = {
        "reason": reason,
        "by": actor,
        "at": isoformat(_as_datetime(now)),
        "previous_status": previous,
    }
    save_record(memory_root, record)
    return record


invalidate = invalidate_record


def supersede_records(
    memory_root: str | Path,
    new_record: Mapping[str, Any],
    old_ids: Sequence[str] | None = None,
    *,
    actor: str = "",
    reason: str = "superseded by a newer record",
    now: dt.datetime | str | None = None,
) -> dict[str, Any]:
    replacement = copy.deepcopy(dict(new_record))
    targets = list(dict.fromkeys(old_ids or replacement.get("supersedes", [])))
    if not targets:
        raise ValueError("at least one superseded record ID is required")
    if replacement.get("id") in targets:
        raise ValueError("a record cannot supersede itself")
    missing = [record_id for record_id in targets if find_record_path(memory_root, record_id) is None]
    if missing:
        raise KeyError(", ".join(missing))
    replacement["supersedes"] = targets
    if replacement.get("status") == "human_signed":
        # supersedes 属于签认内容：补写目标后须在同一签认人下重算内容哈希。
        authority = replacement.setdefault("authority", {})
        authority["content_sha256"] = record_content_hash(replacement)
    save_record(memory_root, replacement)
    timestamp = isoformat(_as_datetime(now))
    for record_id in targets:
        path = find_record_path(memory_root, record_id)
        assert path is not None
        previous = load_record(path)
        old_status = previous["status"]
        previous["status"] = "superseded"
        previous["superseded_by"] = replacement["id"]
        previous["revision"] += 1
        previous["updated_at"] = timestamp
        previous["invalidation"] = {
            "reason": reason,
            "by": actor,
            "at": timestamp,
            "previous_status": old_status,
        }
        save_record(memory_root, previous)
    return replacement


supersede = supersede_records


def _evidence_identity(evidence: Mapping[str, Any]) -> str:
    return str(
        evidence.get("episode_id")
        or evidence.get("run_id")
        or str(evidence.get("ref", "")).split("#", 1)[0]
    )


def promotion_eligibility(record: Mapping[str, Any], target_status: str) -> dict[str, Any]:
    current = str(record.get("status", ""))
    reasons: list[str] = []
    evidence = [item for item in record.get("evidence", []) if isinstance(item, Mapping)]
    if current in TERMINAL_STATUSES:
        reasons.append(f"terminal status cannot be promoted: {current}")
    if target_status == "corroborated":
        if current != "candidate":
            reasons.append("corroborated promotion requires candidate status")
        episodes = {
            _evidence_identity(item)
            for item in evidence
            if str(item.get("kind", "")).casefold() in {"episode", "run", "observation"}
            and _evidence_identity(item)
        }
        if len(episodes) < 2:
            reasons.append("corroborated promotion requires at least 2 independent episodes")
    elif target_status == "machine_verified":
        if current != "corroborated":
            reasons.append("machine_verified promotion requires corroborated status")
        passing = [
            item
            for item in evidence
            if str(item.get("kind", "")).casefold() in {"test", "audit"}
            and str(item.get("outcome", "")).casefold() in _PASS_OUTCOMES
        ]
        if not passing:
            reasons.append("machine_verified promotion requires passing test or audit evidence")
        counterevidence = [
            item
            for item in evidence
            if str(item.get("kind", "")).casefold() in {"counterexample", "contradiction", "refutation"}
            or (
                str(item.get("kind", "")).casefold() in {"test", "audit"}
                and str(item.get("outcome", "")).casefold() in _FAIL_OUTCOMES
            )
        ]
        if counterevidence:
            reasons.append("machine_verified promotion is blocked by counterevidence")
    elif target_status == "human_signed":
        if current not in {"candidate", "corroborated", "machine_verified"}:
            reasons.append("human_signed promotion requires an active promotable status")
    else:
        reasons.append(f"unsupported promotion target: {target_status}")
    return {"eligible": not reasons, "from": current, "to": target_status, "reasons": reasons}


def promoted_record(
    record: Mapping[str, Any],
    target_status: str,
    *,
    actor: str = "",
    explicit_authority: bool = False,
    signed_by: str = "",
    signed_at: dt.datetime | str | None = None,
    content_sha256: str = "",
    now: dt.datetime | str | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(dict(record))
    eligibility = promotion_eligibility(result, target_status)
    if not eligibility["eligible"]:
        raise PromotionError(eligibility["reasons"])
    timestamp = isoformat(_as_datetime(now))
    previous = result["status"]
    result["status"] = target_status
    result["revision"] += 1
    result["updated_at"] = timestamp
    if target_status in {"machine_verified", "human_signed"}:
        result["last_verified_at"] = timestamp
    result.setdefault("promotions", []).append(
        {"from": previous, "to": target_status, "at": timestamp, "by": actor or signed_by}
    )
    if target_status == "human_signed":
        if not explicit_authority:
            raise PromotionError("human_signed promotion requires explicit_authority=True")
        signer = signed_by.strip() or actor.strip()
        if not signer:
            raise PromotionError("human_signed promotion requires signed_by")
        authority_time = isoformat(_as_datetime(signed_at or now))
        result["authority"] = {
            "signed_by": signer,
            "signed_at": authority_time,
            "content_sha256": "",
        }
        expected_digest = record_content_hash(result)
        digest = content_sha256.casefold() if content_sha256 else expected_digest
        if not _SHA256_RE.fullmatch(digest) or digest != expected_digest:
            raise PromotionError("human_signed content_sha256 must match final record content")
        result["authority"]["content_sha256"] = digest
    assert_valid_record(result)
    return result


def promote_record(
    memory_root: str | Path,
    record_id: str,
    target_status: str,
    **kwargs: Any,
) -> dict[str, Any]:
    path = find_record_path(memory_root, record_id)
    if path is None:
        raise KeyError(record_id)
    promoted = promoted_record(load_record(path), target_status, **kwargs)
    save_record(memory_root, promoted)
    return promoted


promote = promote_record


def _event_paths(source: str | Path | Iterable[str | Path]) -> list[Path]:
    sources = [source] if isinstance(source, (str, Path)) else list(source)
    paths: set[Path] = set()
    for item in sources:
        path = Path(item)
        if path.is_file():
            paths.add(path)
        elif path.is_dir():
            direct = path / "events.jsonl"
            if direct.is_file():
                paths.add(direct)
            else:
                paths.update(candidate for candidate in path.rglob("events.jsonl") if candidate.is_file())
    return sorted(paths, key=lambda path: path.as_posix())


def _event_signature(text: str) -> str:
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27,}", "<uuid>", text, flags=re.I)
    text = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:500]


def _proposal_scope(events: Sequence[Mapping[str, Any]], project: str) -> dict[str, Any]:
    result: dict[str, Any] = {field: "" for field in SCOPE_FIELDS}
    result["project"] = project
    for field in SCOPE_FIELDS:
        values = {
            str(event.get("scope", {}).get(field, event.get(field, "")))
            for event in events
            if event.get("scope", {}).get(field, event.get(field, "")) not in (None, "")
        }
        if len(values) == 1:
            result[field] = values.pop()
    return result


def propose_from_run_events(
    memory_root: str | Path,
    runs: str | Path | Iterable[str | Path],
    *,
    project: str = "",
    min_occurrences: int = 2,
    save: bool = False,
    now: dt.datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Extract deterministic candidate records from explicit lessons and repeated events."""
    if min_occurrences < 1:
        raise ValueError("min_occurrences must be positive")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for path in _event_paths(runs):
        for event in read_events(path, recover=True):
            item = copy.deepcopy(event)
            item["_events_path"] = path.as_posix()
            event_name = str(item.get("event", item.get("type", ""))).casefold()
            proposal = item.get("memory_candidate") or item.get("proposal")
            if isinstance(proposal, Mapping) and proposal.get("claim"):
                item["_proposal"] = dict(proposal)
                grouped[("explicit", str(proposal["claim"]))].append(item)
                continue
            if event_name in {"memory_candidate", "lesson", "observation"} and item.get("claim"):
                item["_proposal"] = item
                grouped[("explicit", str(item["claim"]))].append(item)
                continue
            outcome = str(item.get("outcome", item.get("status", ""))).casefold()
            if item.get("workaround") and outcome in _PASS_OUTCOMES:
                grouped[("workaround", str(item["workaround"]))].append(item)
                continue
            signature = item.get("error_signature")
            if not signature and (event_name in {"error", "failure", "failed"} or outcome in {"error", "failed", "failure"}):
                signature = item.get("error") or item.get("message")
            if signature:
                grouped[("error", _event_signature(str(signature)))].append(item)
                continue
            if event_name in {"approve", "reject", "waive", "decision"} and item.get("reason"):
                grouped[("decision", f"{event_name}: {item['reason']}")].append(item)
    proposals: list[dict[str, Any]] = []
    timestamp = _as_datetime(now)
    for (proposal_kind, key), events in sorted(grouped.items()):
        by_episode: dict[str, dict[str, Any]] = {}
        for event in events:
            episode = str(event.get("run_id") or Path(str(event["_events_path"])).parent.name)
            by_episode.setdefault(episode, event)
        if proposal_kind in {"error", "workaround"} and len(by_episode) < min_occurrences:
            continue
        first = events[0]
        explicit = first.get("_proposal", {}) if isinstance(first.get("_proposal"), Mapping) else {}
        if proposal_kind == "error":
            title = f"Recurring run error: {key[:80]}"
            claim = f"The error signature {key!r} recurred in {len(by_episode)} independent episodes."
            tags = ["run-error", "proposed"]
            conflict_key = f"run-error:{sha256_text(key)[:16]}"
        elif proposal_kind == "workaround":
            title = f"Repeated successful workaround: {key[:80]}"
            claim = f"The workaround {key!r} succeeded in {len(by_episode)} independent episodes."
            tags = ["workaround", "proposed"]
            conflict_key = f"workaround:{sha256_text(key)[:16]}"
        elif proposal_kind == "decision":
            title = str(explicit.get("title") or "Run review decision")
            claim = str(explicit.get("claim") or key)
            tags = list(explicit.get("tags", ["review-decision", "proposed"]))
            conflict_key = str(explicit.get("conflict_key", ""))
        else:
            title = str(explicit.get("title") or key[:100])
            claim = str(explicit.get("claim") or key)
            tags = list(explicit.get("tags", ["proposed"]))
            conflict_key = str(explicit.get("conflict_key", ""))
        evidence: list[dict[str, Any]] = []
        for episode, event in sorted(by_episode.items()):
            clean_event = {key: value for key, value in event.items() if not key.startswith("_")}
            event_path = str(event["_events_path"])
            sequence = event.get("seq", "")
            evidence.append(
                {
                    "kind": "episode",
                    "ref": f"{event_path}#{sequence}",
                    "hash": sha256_text(canonical_json(clean_event)),
                    "outcome": str(event.get("outcome", event.get("status", "observed"))),
                    "episode_id": episode,
                    "run_id": str(event.get("run_id", episode)),
                    "observed_at": str(event.get("timestamp", "")),
                }
            )
        record_id = "candidate-" + sha256_text(f"{proposal_kind}\0{key}")[:16]
        candidate = make_record(
            record_id,
            kind=str(explicit.get("kind", "knowledge")),
            title=title,
            claim=claim,
            tags=tags,
            scope=_proposal_scope(events, project),
            evidence=evidence,
            conflict_key=conflict_key,
            now=timestamp,
        )
        existing_path = find_record_path(memory_root, record_id)
        if existing_path is not None:
            existing = load_record(existing_path)
            known = {
                (item.get("kind"), item.get("ref"), item.get("hash"))
                for item in existing.get("evidence", [])
            }
            additions = [
                item for item in candidate["evidence"] if (item["kind"], item["ref"], item["hash"]) not in known
            ]
            if additions:
                existing["evidence"].extend(additions)
                existing["revision"] += 1
                existing["updated_at"] = isoformat(timestamp)
            candidate = existing
        assert_valid_record(candidate)
        if save:
            save_record(memory_root, candidate)
        proposals.append(candidate)
    return proposals


propose = propose_from_run_events


def memory_health(
    memory_root: str | Path,
    *,
    now: dt.datetime | str | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    validation = validate_memory(memory_root, now=now, subjects=subjects)
    counts: dict[str, int] = defaultdict(int)
    for record in list_records(memory_root, effective=True, now=now, subjects=subjects):
        counts[str(record["effective_status"])] += 1
    return {**validation, "status_counts": dict(sorted(counts.items()))}


# Concise aliases for CLI adapters.
validate = validate_memory
save = save_record
list_memory = list_records
reindex = rebuild_index
doctor = memory_health

__all__ = [
    "ACTIVE_STATUSES",
    "EVENT_SCHEMA",
    "MEMORY_SCHEMA",
    "RUN_SCHEMA",
    "SCOPE_FIELDS",
    "STATUSES",
    "MemoryErrorBase",
    "PromotionError",
    "RecordConflictError",
    "RecordValidationError",
    "RunRecorder",
    "assert_valid_record",
    "atomic_write",
    "atomic_write_json",
    "build_context",
    "canonical_json",
    "conflict_groups",
    "context",
    "default_index_path",
    "detect_fts5",
    "doctor",
    "effective_status",
    "find_conflicts",
    "find_record_path",
    "invalidate",
    "invalidate_record",
    "isoformat",
    "iter_record_paths",
    "list_memory",
    "list_records",
    "load_record",
    "make_record",
    "materialize_context",
    "memory_health",
    "promote",
    "promote_record",
    "promoted_record",
    "promotion_eligibility",
    "propose",
    "propose_from_run_events",
    "read_events",
    "rebuild_index",
    "recover_jsonl",
    "redact",
    "reindex",
    "safe_relative_path",
    "save",
    "save_record",
    "save_record_path",
    "scope_matches",
    "scope_specificity",
    "search",
    "search_records",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "status_details",
    "supersede",
    "supersede_records",
    "toml_dumps",
    "toml_load",
    "utc_now",
    "validate",
    "validate_memory",
    "validate_record",
]
