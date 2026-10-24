#!/usr/bin/env python3
"""Tests for the stdlib-only BidViz durable memory kernel."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_memory as memory  # noqa: E402


NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
EVIDENCE_HASH = memory.sha256_text("evidence")


def evidence(
    kind: str,
    ref: str,
    outcome: str = "observed",
    *,
    episode_id: str = "",
) -> dict[str, str]:
    item = {
        "kind": kind,
        "ref": ref,
        "hash": memory.sha256_text(ref),
        "outcome": outcome,
    }
    if episode_id:
        item["episode_id"] = episode_id
    return item


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "project"
        self.root = self.project / "memory"
        self.index = self.project / "work" / "memory-index.sqlite3"

    def tearDown(self):
        self.temporary.cleanup()

    def record(
        self,
        record_id: str,
        *,
        status: str = "machine_verified",
        kind: str = "knowledge",
        title: str | None = None,
        claim: str | None = None,
        scope: dict | None = None,
        tags: tuple[str, ...] = (),
        evidence_items: tuple[dict, ...] = (),
        depends_on: tuple[dict, ...] = (),
        conflict_key: str = "",
        valid_until: dt.datetime | str | None = None,
    ) -> dict:
        return memory.make_record(
            record_id,
            kind=kind,
            status=status,
            title=title or f"Title {record_id}",
            claim=claim or f"Claim {record_id}",
            scope=scope or {"project": "demo"},
            tags=tags,
            evidence=evidence_items,
            depends_on=depends_on,
            conflict_key=conflict_key,
            valid_until=valid_until,
            now=NOW,
        )

    def save(self, record: dict) -> Path:
        return memory.save_record(self.root, record)


class TomlAndValidationTests(MemoryTestCase):
    def test_toml_round_trip_is_deterministic_and_schema_valid(self):
        source = self.project / "input.txt"
        source.parent.mkdir(parents=True)
        source.write_text("source", encoding="utf-8")
        record = self.record(
            "cn-layout-1",
            kind="procedure",
            title="中文排版",
            claim="页图采用真实 PDF 页码。",
            tags=("分页", "verified"),
            scope={"project": "demo", "engine": "html", "page": 2},
            evidence_items=(evidence("test", "tests/test_memory.py", "passed"),),
            depends_on=(
                {"path": "input.txt", "expected_hash": memory.sha256_file(source)},
            ),
        )
        path = self.save(record)
        first = path.read_text(encoding="utf-8")
        loaded = memory.load_record(path)
        self.assertEqual(memory.validate_record(loaded), [])
        self.assertEqual(loaded, record)
        memory.save_record(self.root, loaded)
        self.assertEqual(path.read_text(encoding="utf-8"), first)
        self.assertLess(first.index('schema = "bidviz.memory/v1"'), first.index("[scope]"))

    def test_policy_requires_authority_fields_and_human_signature(self):
        policy = self.record("policy-1", kind="policy", status="candidate")
        self.assertEqual(memory.validate_record(policy), [])
        policy["status"] = "human_signed"
        errors = memory.validate_record(policy)
        self.assertTrue(any("signed_by" in error for error in errors))
        self.assertTrue(any("content_sha256" in error for error in errors))

    def test_human_signed_validation_rejects_content_tamper(self):
        policy = memory.promoted_record(
            self.record("signed-policy", kind="policy", status="machine_verified"),
            "human_signed",
            explicit_authority=True,
            signed_by="chief",
            now=NOW,
        )
        self.assertEqual(memory.validate_record(policy), [])
        policy["claim"] = "tampered after signing"
        errors = memory.validate_record(policy)
        self.assertIn("authority.content_sha256 does not match record content", errors)

    def test_human_signature_covers_revision_validity_evidence_and_dependencies(self):
        policy = memory.promoted_record(
            self.record("signed-scope", kind="policy", status="machine_verified"),
            "human_signed", explicit_authority=True, signed_by="chief", now=NOW)
        for field, value in (
            ("revision", policy["revision"] + 1),
            ("valid_until", "2099-01-01T00:00:00Z"),
            ("evidence", policy["evidence"] + [evidence("audit", "audit.json", "pass")]),
            ("depends_on", [{"subject": "source", "expected_hash": "a" * 64}]),
        ):
            changed = copy.deepcopy(policy)
            changed[field] = value
            self.assertIn(
                "authority.content_sha256 does not match record content",
                memory.validate_record(changed),
            )

    def test_changed_record_requires_strictly_increasing_revision(self):
        original = self.record("revisioned", claim="original")
        path = self.save(original)

        changed = dict(original)
        changed["claim"] = "changed at equal revision"
        with self.assertRaisesRegex(memory.RecordValidationError, "strictly increase"):
            self.save(changed)
        self.assertEqual(memory.load_record(path)["claim"], "original")

        changed["revision"] += 1
        self.save(changed)
        self.assertEqual(memory.load_record(path)["claim"], "changed at equal revision")

    def test_identical_record_save_is_idempotent_at_equal_revision(self):
        record = self.record("idempotent")
        path = self.save(record)
        before = path.read_bytes()
        self.assertEqual(self.save(dict(record)), path)
        self.assertEqual(path.read_bytes(), before)

    def test_validation_rejects_bad_dependency_and_path_traversal(self):
        record = self.record(
            "bad-dep",
            depends_on=({"path": "../secret", "expected_hash": "not-a-hash"},),
        )
        errors = memory.validate_record(record)
        self.assertTrue(any("expected_hash" in error for error in errors))
        with self.assertRaises(ValueError):
            memory.safe_relative_path(self.project, "../outside")

    def test_atomic_write_replaces_file_without_temp_leftovers(self):
        target = self.project / "nested" / "value.txt"
        memory.atomic_write(target, "first")
        memory.atomic_write(target, "second")
        self.assertEqual(target.read_text(encoding="utf-8"), "second")
        self.assertEqual(list(target.parent.glob(".value.txt.*.tmp")), [])

    def test_json_schemas_are_well_formed_and_named(self):
        schema_dir = BASE / "memory" / "schemas"
        expected = {
            "memory-record-v1.schema.json": "bidviz.memory/v1",
            "event-v1.schema.json": "bidviz.event/v1",
            "run-v1.schema.json": "bidviz.run/v1",
        }
        for filename, schema_name in expected.items():
            document = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
            self.assertEqual(document["properties"]["schema"]["const"], schema_name)

    def test_explicit_path_save_api(self):
        target = self.root / "custom" / "record.toml"
        record = self.record("explicit")
        self.assertEqual(memory.save_record_path(target, record), target)
        self.assertEqual(memory.load_record(target), record)
        changed = dict(record)
        changed["claim"] = "changed without revision"
        with self.assertRaisesRegex(memory.RecordValidationError, "strictly increase"):
            memory.save_record_path(target, changed)
        changed["revision"] += 1
        memory.save_record_path(target, changed)
        self.assertEqual(memory.load_record(target)["claim"], "changed without revision")


class RunRecorderTests(MemoryTestCase):
    def test_run_recorder_orders_sequences_and_redacts_secrets(self):
        run_dir = self.project / "work" / "runs" / "run-1"
        recorder = memory.RunRecorder(
            run_dir,
            run_id="run-1",
            metadata={"project": "demo", "api_key": "metadata-secret"},
            fsync=False,
        )
        one = recorder.record(
            "start",
            authorization="Bearer top-secret-token",
            nested={"password": "p@ss", "safe": "yes"},
            headers={"X-Trace": "private-header"},
            prompt="complete private prompt",
        )
        two = recorder.record("result", message="api_key=abc123", binary=b"secret bytes")
        recorder.close(exit_code=0)
        events = memory.read_events(run_dir / "events.jsonl")
        self.assertEqual([event["seq"] for event in events], [1, 2])
        self.assertEqual(one["authorization"], "[REDACTED]")
        self.assertEqual(one["nested"]["password"], "[REDACTED]")
        self.assertEqual(one["headers"], "[REDACTED]")
        self.assertEqual(one["prompt"], "[REDACTED]")
        self.assertEqual(two["message"], "api_key=[REDACTED]")
        raw = (run_dir / "events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("top-secret-token", raw)
        self.assertNotIn("p@ss", raw)
        self.assertNotIn("private-header", raw)
        self.assertNotIn("complete private prompt", raw)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["api_key"], "[REDACTED]")
        self.assertEqual(run["status"], "success")

    def test_redaction_covers_api_headers_and_signed_url_keys_without_normal_field_false_positives(self):
        value = {
            "X-API-Key": "header-secret",
            "x_api_key": "alternate-secret",
            "monkey": "banana",
            "token_count": 42,
            "keynote": "slides",
            "url": (
                "https://example.test/file?token=one&key=two&sig=three&signature=four"
                "&api_key=five&x-api-key=six&X-Amz-Signature=seven&X-Goog-Credential=eight"
                "&monkey=banana&token_count=42#fragment"
            ),
        }
        redacted = memory.redact(value)
        self.assertEqual(redacted["X-API-Key"], "[REDACTED]")
        self.assertEqual(redacted["x_api_key"], "[REDACTED]")
        self.assertEqual(redacted["monkey"], "banana")
        self.assertEqual(redacted["token_count"], 42)
        self.assertEqual(redacted["keynote"], "slides")
        self.assertEqual(
            redacted["url"],
            "https://example.test/file?token=[REDACTED]&key=[REDACTED]&sig=[REDACTED]"
            "&signature=[REDACTED]&api_key=[REDACTED]&x-api-key=[REDACTED]"
            "&X-Amz-Signature=[REDACTED]&X-Goog-Credential=[REDACTED]"
            "&monkey=banana&token_count=42#fragment",
        )

    def test_recorder_recovers_malformed_final_line_and_continues_sequence(self):
        run_dir = self.project / "work" / "runs" / "run-2"
        recorder = memory.RunRecorder(run_dir, run_id="run-2", fsync=False)
        recorder.record("one")
        with (run_dir / "events.jsonl").open("ab") as handle:
            handle.write(b'{"seq":2,"broken"')
        recovered = memory.RunRecorder(run_dir, run_id="run-2", fsync=False)
        event = recovered.record("two")
        self.assertEqual(event["seq"], 2)
        self.assertEqual([item["event"] for item in memory.read_events(run_dir / "events.jsonl")], ["one", "two"])

    def test_recovery_completes_valid_final_line_without_newline(self):
        path = self.project / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"seq":1,"event":"ok"}', encoding="utf-8")
        result = memory.recover_jsonl(path)
        self.assertTrue(result["recovered"])
        self.assertEqual(result["truncated_bytes"], 0)
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_malformed_interior_line_is_not_silently_discarded(self):
        path = self.project / "events.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"seq":1}\nnot-json\n{"seq":2}\n')
        with self.assertRaisesRegex(ValueError, "line 2"):
            memory.recover_jsonl(path)

    def test_event_payload_cannot_override_sequence_or_schema(self):
        path = self.project / "run"
        recorder = memory.RunRecorder(path, run_id="trusted", fsync=False)
        event = recorder.record({"event": "x", "seq": 999, "run_id": "evil", "schema": "evil"})
        self.assertEqual(event["seq"], 1)
        self.assertEqual(event["run_id"], "trusted")
        self.assertEqual(event["schema"], memory.EVENT_SCHEMA)

    def test_two_recorder_instances_re_read_sequence_under_lock(self):
        run_dir = self.project / "run-shared"
        first = memory.RunRecorder(run_dir, run_id="shared", fsync=False)
        second = memory.RunRecorder(run_dir, run_id="shared", fsync=False)
        events = [first.record("first"), second.record("second"), first.record("third")]
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual(
            [event["seq"] for event in memory.read_events(run_dir / "events.jsonl")],
            [1, 2, 3],
        )

    def test_two_processes_do_not_duplicate_sequences(self):
        run_dir = self.project / "run-process-shared"
        script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import bidviz_memory as memory
recorder = memory.RunRecorder(Path(sys.argv[2]), run_id="shared", fsync=False)
for number in range(int(sys.argv[3])):
    recorder.record("worker", worker=sys.argv[4], number=number)
"""
        count = 30
        command = [sys.executable, "-c", script, str(BASE / "src"), str(run_dir), str(count)]
        processes = [
            subprocess.Popen(
                [*command, worker],
                cwd=BASE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            for worker in ("a", "b")
        ]
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        events = memory.read_events(run_dir / "events.jsonl")
        self.assertEqual(len(events), count * 2)
        self.assertEqual([event["seq"] for event in events], list(range(1, count * 2 + 1)))


class IndexSearchAndContextTests(MemoryTestCase):
    def test_index_rebuild_is_deterministic_after_deletion(self):
        self.save(self.record("a", claim="alpha crane procedure"))
        self.save(self.record("b", claim="beta concrete procedure"))
        first = memory.rebuild_index(self.root, self.index)
        first_rows = self._index_rows()
        first_mode = first["fts5"]
        self.index.unlink()
        second = memory.rebuild_index(self.root, self.index)
        self.assertEqual(second["fts5"], first_mode)
        self.assertEqual(self._index_rows(), first_rows)

    def _index_rows(self):
        connection = sqlite3.connect(self.index)
        try:
            return connection.execute(
                "SELECT id,path,effective_status,record_json FROM records ORDER BY id"
            ).fetchall()
        finally:
            connection.close()

    def test_forced_like_fallback_and_two_character_chinese_query(self):
        self.save(self.record("cn", title="真实页码", claim="页码必须来自 PDF 页序。"))
        info = memory.rebuild_index(self.root, self.index, use_fts=False)
        self.assertFalse(info["fts5"])
        result = memory.search_records(self.root, "页码", index_path=self.index)
        self.assertEqual([item["id"] for item in result], ["cn"])
        self.assertEqual(result[0]["search_mode"], "like")

    def test_missing_index_scan_filters_query(self):
        self.save(self.record("match", claim="needle in this record"))
        self.save(self.record("other", claim="unrelated haystack"))
        result = memory.search_records(
            self.root,
            "needle",
            index_path=self.index,
            rebuild_if_missing=False,
        )
        self.assertEqual([item["id"] for item in result], ["match"])
        self.assertEqual(result[0]["search_mode"], "scan")

    def test_corrupt_index_scan_filters_query(self):
        self.save(self.record("match", title="Needle title", claim="matching record"))
        self.save(self.record("other", claim="unrelated haystack"))
        self.index.parent.mkdir(parents=True)
        self.index.write_bytes(b"not a sqlite database")
        result = memory.search_records(
            self.root,
            "needle",
            index_path=self.index,
            rebuild_if_missing=False,
        )
        self.assertEqual([item["id"] for item in result], ["match"])
        self.assertEqual(result[0]["search_mode"], "scan")

    def test_long_chinese_search_uses_fts_or_like_fallback(self):
        self.save(self.record("cn", title="真实页码", claim="页图采用真实 PDF 页码。"))
        memory.rebuild_index(self.root, self.index)
        result = memory.search_records(self.root, "真实页码", index_path=self.index)
        self.assertEqual([item["id"] for item in result], ["cn"])
        self.assertIn(result[0]["search_mode"], {"fts5", "like"})

    def test_scope_isolates_project_engine_and_page(self):
        self.save(
            self.record(
                "exact",
                scope={"project": "alpha", "engine": "html", "page": 2},
                claim="alpha html page two",
            )
        )
        self.save(
            self.record(
                "wrong-project",
                scope={"project": "beta", "engine": "html", "page": 2},
                claim="alpha html page two",
            )
        )
        self.save(
            self.record(
                "wrong-engine",
                scope={"project": "alpha", "engine": "fullimage", "page": 2},
                claim="alpha html page two",
            )
        )
        self.save(
            self.record(
                "wrong-page",
                scope={"project": "alpha", "engine": "html", "page": 3},
                claim="alpha html page two",
            )
        )
        memory.rebuild_index(self.root, self.index)
        result = memory.search_records(
            self.root,
            "alpha",
            scope={"project": "alpha", "engine": "html", "page": 2},
            index_path=self.index,
        )
        self.assertEqual([item["id"] for item in result], ["exact"])

    def test_human_signed_policy_precedes_exact_machine_verified(self):
        policy = self.record(
            "signed-policy",
            kind="policy",
            status="candidate",
            claim="build must be approved",
            scope={"project": "demo"},
            evidence_items=(evidence("episode", "run-1", episode_id="run-1"),),
        )
        policy = memory.promoted_record(
            policy,
            "human_signed",
            explicit_authority=True,
            signed_by="chief-engineer",
            now=NOW,
        )
        self.save(policy)
        self.save(
            self.record(
                "exact-machine",
                status="machine_verified",
                claim="build approval exact engine",
                scope={"project": "demo", "engine": "html"},
            )
        )
        memory.rebuild_index(self.root, self.index)
        result = memory.search_records(
            self.root,
            "build",
            scope={"project": "demo", "engine": "html"},
            index_path=self.index,
        )
        self.assertEqual([item["id"] for item in result], ["signed-policy", "exact-machine"])

    def test_candidate_and_inactive_statuses_are_excluded_by_default(self):
        for status in ("candidate", "stale", "rejected", "superseded", "expired"):
            self.save(self.record(status, status=status, claim="needle"))
        self.save(self.record("active", status="corroborated", claim="needle"))
        memory.rebuild_index(self.root, self.index)
        default = memory.search_records(self.root, "needle", index_path=self.index)
        self.assertEqual([item["id"] for item in default], ["active"])
        all_results = memory.search_records(
            self.root, "needle", include_inactive=True, index_path=self.index
        )
        self.assertEqual({item["id"] for item in all_results}, {
            "candidate", "stale", "rejected", "superseded", "expired", "active"
        })

    def test_context_budget_and_hash_are_stable(self):
        for number in range(3):
            self.save(
                self.record(
                    f"item-{number}",
                    claim=(f"context claim {number} " + "x" * 70),
                    scope={"project": "demo"},
                )
            )
        memory.rebuild_index(self.root, self.index)
        first = memory.materialize_context(
            self.root,
            "context",
            scope={"project": "demo"},
            char_budget=180,
            index_path=self.index,
        )
        second = memory.materialize_context(
            self.root,
            "context",
            scope={"project": "demo"},
            char_budget=180,
            index_path=self.index,
        )
        self.assertLessEqual(first["characters"], 180)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(first["memory_ids"], second["memory_ids"])


class EffectiveStatusTests(MemoryTestCase):
    def test_dependency_change_makes_record_effectively_stale(self):
        dependency = self.project / "config.toml"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("value=1", encoding="utf-8")
        record = self.record(
            "dependent",
            depends_on=(
                {"path": "config.toml", "expected_hash": memory.sha256_file(dependency)},
            ),
        )
        self.save(record)
        memory.rebuild_index(self.root, self.index, now=NOW)
        self.assertEqual(memory.effective_status(record, base_dir=self.project, now=NOW), "machine_verified")
        dependency.write_text("value=2", encoding="utf-8")
        self.assertEqual(memory.effective_status(record, base_dir=self.project, now=NOW), "stale")
        self.assertEqual(
            memory.search_records(self.root, "dependent", index_path=self.index, now=NOW), []
        )

    def test_subject_dependency_hash(self):
        record = self.record(
            "subject",
            depends_on=(
                {"subject": "layout-context", "expected_hash": memory.sha256_text("v1")},
            ),
        )
        self.assertEqual(
            memory.effective_status(record, subjects={"layout-context": "v1"}, now=NOW),
            "machine_verified",
        )
        self.assertEqual(
            memory.effective_status(record, subjects={"layout-context": "v2"}, now=NOW),
            "stale",
        )

    def test_ttl_expiry(self):
        record = self.record(
            "ttl",
            valid_until=NOW + dt.timedelta(hours=1),
        )
        self.assertEqual(memory.effective_status(record, now=NOW), "machine_verified")
        self.assertEqual(
            memory.effective_status(record, now=NOW + dt.timedelta(hours=2)), "expired"
        )

    def test_validate_fix_persists_effective_stale(self):
        dependency = self.project / "source.txt"
        dependency.parent.mkdir(parents=True)
        dependency.write_text("old", encoding="utf-8")
        self.save(
            self.record(
                "fix-me",
                depends_on=(
                    {"path": "source.txt", "expected_hash": memory.sha256_file(dependency)},
                ),
            )
        )
        dependency.write_text("new", encoding="utf-8")
        result = memory.validate_memory(self.root, fix=True, now=NOW)
        self.assertEqual(result["fixed"], ["fix-me"])
        self.assertEqual(memory.load_record(memory.find_record_path(self.root, "fix-me"))["status"], "stale")


class ConflictAndLifecycleTests(MemoryTestCase):
    def test_conflict_detection_then_supersession(self):
        old = self.record("old", status="machine_verified", conflict_key="engine:primary")
        other = self.record("other", status="machine_verified", conflict_key="engine:primary")
        self.save(old)
        self.save(other)
        validation = memory.validate_memory(self.root)
        self.assertFalse(validation["ok"])
        self.assertEqual(validation["conflicts"][0]["ids"], ["old", "other"])

        replacement = self.record(
            "new",
            status="machine_verified",
            conflict_key="engine:primary",
        )
        memory.supersede_records(self.root, replacement, ["old", "other"], actor="tester", now=NOW)
        self.assertEqual(memory.load_record(memory.find_record_path(self.root, "old"))["status"], "superseded")
        self.assertEqual(memory.load_record(memory.find_record_path(self.root, "other"))["superseded_by"], "new")
        self.assertEqual(memory.validate_memory(self.root)["conflicts"], [])

    def test_supersede_re_signs_human_signed_replacement(self):
        self.save(self.record("legacy", status="candidate"))
        signed = memory.promoted_record(
            self.record("signed-new", kind="decision", status="machine_verified"),
            "human_signed", explicit_authority=True, signed_by="chief", now=NOW,
        )
        result = memory.supersede_records(
            self.root, signed, ["legacy"], actor="chief", now=NOW)
        self.assertEqual(memory.validate_record(result), [])
        saved = memory.load_record(memory.find_record_path(self.root, "signed-new"))
        self.assertEqual(memory.validate_record(saved), [])
        self.assertEqual(saved["supersedes"], ["legacy"])
        self.assertEqual(
            memory.load_record(memory.find_record_path(self.root, "legacy"))["status"],
            "superseded")

    def test_strict_save_rejects_unresolved_conflict(self):
        self.save(self.record("old", conflict_key="same"))
        with self.assertRaises(memory.RecordConflictError):
            memory.save_record(
                self.root,
                self.record("new", conflict_key="same"),
                strict_conflicts=True,
            )

    def test_invalidate_requires_reason_and_records_provenance(self):
        self.save(self.record("invalidate-me"))
        with self.assertRaises(ValueError):
            memory.invalidate_record(self.root, "invalidate-me", "")
        updated = memory.invalidate_record(
            self.root,
            "invalidate-me",
            "source changed",
            actor="operator",
            now=NOW,
        )
        self.assertEqual(updated["status"], "stale")
        self.assertEqual(updated["invalidation"]["previous_status"], "machine_verified")
        self.assertEqual(updated["invalidation"]["by"], "operator")


class PromotionTests(MemoryTestCase):
    def test_candidate_to_corroborated_needs_two_independent_episodes(self):
        record = self.record(
            "candidate",
            status="candidate",
            evidence_items=(
                evidence("episode", "run-1#a", episode_id="run-1"),
                evidence("episode", "run-1#b", episode_id="run-1"),
            ),
        )
        with self.assertRaisesRegex(memory.PromotionError, "2 independent episodes"):
            memory.promoted_record(record, "corroborated", now=NOW)
        record["evidence"].append(evidence("episode", "run-2#a", episode_id="run-2"))
        promoted = memory.promoted_record(record, "corroborated", actor="agent", now=NOW)
        self.assertEqual(promoted["status"], "corroborated")

    def test_machine_verified_needs_passing_test_or_audit(self):
        record = self.record(
            "corroborated",
            status="corroborated",
            evidence_items=(
                evidence("episode", "run-1", episode_id="run-1"),
                evidence("episode", "run-2", episode_id="run-2"),
            ),
        )
        with self.assertRaisesRegex(memory.PromotionError, "passing test or audit"):
            memory.promoted_record(record, "machine_verified", now=NOW)
        record["evidence"].append(evidence("audit", "audit/layout.json", "passed"))
        promoted = memory.promoted_record(record, "machine_verified", now=NOW)
        self.assertEqual(promoted["status"], "machine_verified")
        self.assertEqual(promoted["last_verified_at"], memory.isoformat(NOW))

    def test_machine_verified_is_blocked_by_counterevidence(self):
        record = self.record(
            "countered",
            status="corroborated",
            evidence_items=(
                evidence("test", "tests/test_x.py", "passed"),
                evidence("audit", "audit/latest.json", "failed"),
            ),
        )
        with self.assertRaisesRegex(memory.PromotionError, "counterevidence"):
            memory.promoted_record(record, "machine_verified", now=NOW)

    def test_human_signed_requires_explicit_authority(self):
        record = self.record("sign-me", status="machine_verified", kind="decision")
        with self.assertRaisesRegex(memory.PromotionError, "explicit_authority"):
            memory.promoted_record(record, "human_signed", signed_by="chief", now=NOW)
        with self.assertRaisesRegex(memory.PromotionError, "signed_by"):
            memory.promoted_record(record, "human_signed", explicit_authority=True, now=NOW)
        signed = memory.promoted_record(
            record,
            "human_signed",
            explicit_authority=True,
            signed_by="chief",
            now=NOW,
        )
        self.assertEqual(signed["authority"]["signed_by"], "chief")
        self.assertRegex(signed["authority"]["content_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(memory.validate_record(signed), [])


class ProposalTests(MemoryTestCase):
    def _write_run(self, run_id: str, event: str, **fields):
        recorder = memory.RunRecorder(
            self.project / "work" / "runs" / run_id,
            run_id=run_id,
            fsync=False,
        )
        recorder.record(event, **fields)
        recorder.close()

    def test_repeated_errors_propose_candidate_with_independent_evidence(self):
        self._write_run("run-a", "error", error_signature="HTTP 503 provider unavailable")
        self._write_run("run-b", "error", error_signature="HTTP 503 provider unavailable")
        proposals = memory.propose_from_run_events(
            self.root,
            self.project / "work" / "runs",
            project="demo",
            save=True,
            now=NOW,
        )
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["status"], "candidate")
        self.assertEqual({item["episode_id"] for item in proposal["evidence"]}, {"run-a", "run-b"})
        self.assertIsNotNone(memory.find_record_path(self.root, proposal["id"]))

    def test_single_repeated_pattern_event_does_not_propose(self):
        self._write_run("run-a", "error", error_signature="one-off")
        proposals = memory.propose_from_run_events(
            self.root, self.project / "work" / "runs", project="demo", now=NOW
        )
        self.assertEqual(proposals, [])

    def test_explicit_lesson_may_propose_once_but_never_auto_promotes(self):
        self._write_run(
            "run-a",
            "lesson",
            claim="Run coverage after template changes.",
            title="Coverage gate",
            kind="procedure",
            tags=["coverage"],
        )
        proposals = memory.propose_from_run_events(
            self.root, self.project / "work" / "runs", project="demo", now=NOW
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["status"], "candidate")
        self.assertEqual(proposals[0]["kind"], "procedure")

    def test_proposal_reader_recovers_truncated_run_tail(self):
        self._write_run("run-a", "lesson", claim="Keep source authoritative.")
        events_path = self.project / "work" / "runs" / "run-a" / "events.jsonl"
        with events_path.open("ab") as handle:
            handle.write(b'{"broken"')
        proposals = memory.propose_from_run_events(
            self.root, self.project / "work" / "runs", project="demo", now=NOW
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(len(memory.read_events(events_path)), 1)


class PackagingAndFeatureDetectionTests(MemoryTestCase):
    def test_module_is_declared_in_pyproject(self):
        import tomllib

        with (BASE / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)
        self.assertIn("bidviz_memory", project["tool"]["setuptools"]["py-modules"])

    def test_fts_detection_failure_has_like_fallback(self):
        self.save(self.record("fallback", claim="fallback search text"))
        with mock.patch.object(memory, "detect_fts5", return_value=False):
            info = memory.rebuild_index(self.root, self.index)
        self.assertFalse(info["fts5"])
        result = memory.search_records(self.root, "fallback", index_path=self.index)
        self.assertEqual([item["id"] for item in result], ["fallback"])

    def test_cli_friendly_aliases_are_plain_callables(self):
        for name in (
            "validate",
            "list_memory",
            "save",
            "invalidate",
            "supersede",
            "promote",
            "propose",
            "reindex",
            "search",
            "context",
            "doctor",
        ):
            self.assertTrue(callable(getattr(memory, name)))


if __name__ == "__main__":
    unittest.main()
