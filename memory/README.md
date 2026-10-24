# BidViz Memory

BidViz memory is a repository-backed control plane, not a second source of truth. Source documents, project configuration, plans, tests, audit outputs, and published state remain authoritative. Memory records retain compact claims plus references and hashes; they do not copy full source text, prompts, request headers, credentials, or model reasoning.

## Storage

Durable records are UTF-8 TOML under repository or project `memory/` trees. Runtime run logs and the derived SQLite index belong under project `work/` and may be rebuilt or deleted:

```text
memory/
├─ policies/
├─ procedures/
├─ knowledge/
└─ schemas/

projects/<project>/memory/
├─ decisions/
├─ policies/
├─ procedures/
└─ knowledge/

projects/<project>/work/
├─ runs/<run_id>/run.json
├─ runs/<run_id>/events.jsonl
└─ memory-index.sqlite3
```

Writes use a same-directory temporary file, `fsync`, and `os.replace`. `RunRecorder` appends one redacted JSON object per `os.write`; a bounded cross-process lock serializes sequence recovery and append for each events file. Recovery repairs only a malformed final line. Interior corruption is an error.

## Record lifecycle

Statuses are:

- `candidate`: proposed only; excluded from execution context by default.
- `corroborated`: observed in at least two independent episodes.
- `machine_verified`: corroborated and backed by a passing `test` or `audit`, with no unresolved counterevidence.
- `human_signed`: explicitly approved by a named authority. It can never be inferred from a run.
- `rejected`, `stale`, `superseded`, `expired`: inactive and excluded from execution context.

Promotion is gated. Automatic event processing may create a `candidate`, but never a policy/decision approval and never `human_signed`. A newer claim supersedes an older record; it does not overwrite or delete history.

## Scope and retrieval

Every record declares `project`, `engine`, `operation`, `section`, `page`, `provider`, and `model`. Empty or `*` values are broad; populated values restrict retrieval. Default context admits only active statuses and ranks signed authority above machine verification, corroboration, and candidates. Exact scope raises priority. Context output has a caller-provided character budget and a stable SHA-256 `context_hash` over selected IDs, revisions, effective statuses, scope, and rendered text.

SQLite FTS5 is detected at runtime. When unavailable—or for short Chinese queries where tokenizer behavior is unsuitable—search uses escaped `LIKE`. The database is derived state and `rebuild_index()` is deterministic.

## Invalidation and conflicts

`valid_until` yields effective `expired` status. A changed/missing `depends_on.path` hash or changed `depends_on.subject` hash yields effective `stale` status without first rewriting the durable record. `validate_memory(..., fix=True)` may persist that effective status.

Records sharing a non-empty `conflict_key` are unresolved conflicts unless one explicitly names the other in `supersedes`. `supersede_records()` saves the replacement, then marks prior records `superseded` with provenance. `invalidate_record()` requires a reason and records actor/time/previous status.

## Python API

The stdlib-only module is `bidviz_memory`:

- TOML/files: `toml_load`, `toml_dumps`, `atomic_write`, `sha256_file`, `safe_relative_path`
- events: `RunRecorder`, `read_events`, `recover_jsonl`, `redact`
- durable records: `make_record`, `validate_record`, `save_record`, `load_record`, `list_records`, `validate_memory`
- retrieval: `detect_fts5`, `rebuild_index`, `search_records`, `materialize_context`
- lifecycle: `effective_status`, `invalidate_record`, `supersede_records`, `promotion_eligibility`, `promote_record`
- proposals: `propose_from_run_events`

Short aliases (`validate`, `save`, `search`, `context`, `reindex`, `promote`, `invalidate`, `supersede`, `propose`, `doctor`) are also available.

## CLI and Web integration

The integration is live rather than prospective:

- `bidviz memory validate|reindex|search|context|list|doctor|propose|promote|invalidate` operates across repository and project memory as appropriate. CLI project commands record redacted run events and artifact hashes under `projects/<project>/work/runs/`.
- The Web server records build, plan, edit, and candidate events through `RunRecorder`. It exposes memory health, candidates, and scoped context through `GET /api/memory/health`, `/api/memory/candidates`, and `/api/memory/context`; promotion, rejection, and invalidation use `POST /api/memory/<id>/<action>`.
- Both surfaces combine repository-wide and project-specific records and keep separate disposable SQLite indexes under the selected project's `work/` directory. Context assembly is currently performed by the adapters after kernel search, so their response shape and context hash calculation are adapter contracts rather than direct `materialize_context()` output.
