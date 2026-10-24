#!/usr/bin/env python3
"""Full-image store lifecycle tests."""
from __future__ import annotations

import copy
import multiprocessing
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_fullimage_store as store  # noqa: E402


def _ensure_page_in_process(root: str, sid: str) -> None:
    store.ensure_page(root, sid, 1, f"source-{sid}", "template", "prompt")


def _crash_during_publish(root: str) -> None:
    original = store.save_pages_doc
    def crash(_root, _doc):
        os._exit(73)
    store.save_pages_doc = crash
    try:
        store.publish_candidate(root, "S001", 1, 2)
    finally:
        store.save_pages_doc = original


class FullimageStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ensure(self, sid="S001", page=1):
        return store.ensure_page(self.root, sid, page, "source-1", "template-1", "prompt-1")

    def candidate(self, version=None, content=b"image", sid="S001", page=1, **metadata):
        if version is None:
            version = store.next_candidate_version(self.root, sid, page)
        path = store.candidate_path(self.root, sid, page, version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        page_rec = store.get_page(self.root, sid, page)
        for name in ("source_hash", "template_hash", "prompt_hash"):
            metadata.setdefault(name, page_rec.get(name))
        return store.register_candidate(
            self.root, {"sid": sid, "page": page, "version": version, **metadata})

    def mark_review(self, sid="S001", page=1, version=1):
        doc = store.load_pages_doc(self.root)
        candidate = next(c for c in doc["pages"][f"{sid}:p{page}"]["candidates"]
                         if c["version"] == version)
        candidate["status"] = "review"
        store.save_pages_doc(self.root, doc)

    def test_override_stale_and_reset(self):
        resolved = store.get_prompt_override(self.root, "S001", 1, "auto", "source-1")
        self.assertEqual(resolved["resolved_prompt"], "auto")
        self.assertFalse(resolved["custom"])

        store.set_prompt_override(self.root, "S001", 1, "custom", "source-1")
        resolved = store.get_prompt_override(self.root, "S001", 1, "auto-2", "source-1")
        self.assertEqual(resolved["resolved_prompt"], "custom")
        self.assertTrue(resolved["custom"])
        self.assertFalse(resolved["stale"])
        self.assertTrue(store.get_prompt_override(
            self.root, "S001", 1, "auto-2", "source-2")["stale"])

        reset = store.reset_prompt_override(self.root, "S001", 1, "auto-2", "source-2")
        self.assertEqual(reset["resolved_prompt"], "auto-2")
        self.assertFalse(reset["custom"])

    def test_prompt_override_marks_page_stale_and_blocks_old_candidate(self):
        self.ensure()
        self.candidate(1, b"old")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.set_prompt_override(self.root, "S001", 1, "custom", "source-1")
        page = store.get_page(self.root, "S001", 1)
        self.assertTrue(page["stale"])
        self.assertEqual(page["prompt_hash"], store.sha256_text("custom"))
        with self.assertRaisesRegex(PermissionError, "prompt_hash"):
            store.publish_candidate(self.root, "S001", 1, 1)

    def test_local_edit_candidate_publishes_but_keeps_baseline_staleness(self):
        self.ensure()
        self.candidate(1, b"base")                      # 整页候选：三哈希一致
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)

        # 局部改字候选：prompt 是编辑指令，不等于整页提示词。
        edit_prompt = "保持图片其他部分完全不变……"
        self.candidate(2, b"base-edited", kind="local-edit",
                       prompt_hash=store.sha256_text(edit_prompt))
        self.mark_review(version=2)
        store.record_decision(self.root, "S001", 1, 2, "approve")
        result = store.publish_candidate(self.root, "S001", 1, 2)
        page = store.get_page(self.root, "S001", 1)
        self.assertEqual(result["current"]["version"], 2)
        self.assertFalse(page["stale"])                # 页面本就新鲜，改字不清也不破

        # 原文变化后，基于旧原文底图的改字必须被拒（source_hash 不匹配）。
        store.ensure_page(self.root, "S001", 1, "source-2", "template-1", "prompt-1")
        self.assertTrue(store.get_page(self.root, "S001", 1)["stale"])
        self.candidate(3, b"edited-against-old-source", kind="local-edit",
                       source_hash="source-1", template_hash="template-1",
                       prompt_hash=store.sha256_text(edit_prompt))
        self.mark_review(version=3)
        store.record_decision(self.root, "S001", 1, 3, "approve")
        with self.assertRaisesRegex(PermissionError, "source_hash"):
            store.publish_candidate(self.root, "S001", 1, 3)  # 旧原文上的改字仍被拒

    def test_candidate_versions_and_lookup(self):
        self.ensure()
        self.assertEqual(store.next_candidate_version(self.root, "S001", 1), 1)
        first = self.candidate(model="test-model")
        second = self.candidate()
        self.assertEqual((first["version"], second["version"]), (1, 2))
        self.assertEqual(store.candidate_path(self.root, "S001", 1, 2).name, "v0002.png")
        self.assertEqual(store.get_candidate(self.root, "S001", 1, 1)["model"], "test-model")
        self.assertEqual([c["version"] for c in store.list_candidates(
            self.root, "S001", 1)], [1, 2])

    def test_unapproved_cannot_publish_then_approved_publishes(self):
        self.ensure()
        self.candidate(content=b"approved-image")
        with self.assertRaises(PermissionError):
            store.publish_candidate(self.root, "S001", 1, 1)

        # 自动门禁通过后才进入 review，方可人工批准
        self.mark_review()
        store.record_decision(self.root, "S001", 1, 1, "approve", "reviewer", "ok")
        result = store.publish_candidate(self.root, "S001", 1, 1)
        self.assertEqual(result["path"].read_bytes(), b"approved-image")
        self.assertEqual(result["current"]["version"], 1)
        self.assertIsNone(result["previous"])

    def test_reject_does_not_change_published(self):
        self.ensure()
        self.candidate(1, b"first")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        published = store.publish_candidate(self.root, "S001", 1, 1)["path"]

        self.candidate(2, b"second")
        store.record_decision(self.root, "S001", 1, 2, "reject", comment="bad")
        self.assertEqual(published.read_bytes(), b"first")
        current = store.load_pages_doc(self.root)["pages"]["S001:p1"]["published"]["current"]
        self.assertEqual(current["version"], 1)

    def test_waive_blocked_candidate_can_publish(self):
        self.ensure()
        self.candidate(content=b"waived")
        store.record_decision(self.root, "S001", 1, 1, "reject")
        store.record_decision(self.root, "S001", 1, 1, "waive", "lead", "accepted risk")
        result = store.publish_candidate(self.root, "S001", 1, 1)
        self.assertTrue(result["current"]["waived"])
        self.assertEqual(result["path"].read_bytes(), b"waived")

    def test_publication_is_independent_and_rollback_keeps_one_image(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two"), (3, b"three")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            result = store.publish_candidate(self.root, "S001", 1, version)
            current = result["current"]
            self.assertNotIn("candidate_path", current)
            self.assertEqual(current["published_path"],
                             "work/fullimage/published/S001_p1.png")
            self.assertEqual(current["image_sha256"], store.sha256_file(result["path"]))
        previous_path = store.previous_published_path(self.root, "S001", 1)
        self.assertEqual(previous_path.read_bytes(), b"two")
        self.assertEqual(result["previous"]["version"], 2)
        self.assertEqual(result["previous"]["published_path"],
                         "work/fullimage/published/previous/S001_p1.png")

        for version in (1, 2, 3):
            store.candidate_path(self.root, "S001", 1, version).unlink()
        self.assertEqual(result["path"].read_bytes(), b"three")
        restored = store.rollback_published(self.root, "S001", 1)
        self.assertEqual(restored["path"].read_bytes(), b"two")
        self.assertEqual(previous_path.read_bytes(), b"three")
        self.assertEqual(restored["current"]["version"], 2)
        self.assertEqual(restored["previous"]["version"], 3)
        store.rollback_published(self.root, "S001", 1)
        self.assertEqual(result["path"].read_bytes(), b"three")

    def test_migrate_legacy_publication_then_compact_safely(self):
        self.ensure()
        self.candidate(1, b"published")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        self.candidate(2, b"unresolved", kind="local-edit")

        doc = store.load_pages_doc(self.root)
        page = doc["pages"]["S001:p1"]
        page.pop("version_high_watermark", None)
        current = page["published"]["current"]
        current["candidate_path"] = page["candidates"][0]["path"]
        for field in ("published_path", "image_sha256", "source_hash",
                      "template_hash", "prompt_hash"):
            current.pop(field, None)
        store.published_path(self.root, "S001", 1).unlink()
        store.save_pages_doc(self.root, doc)

        migration = store.migrate_publication_store(self.root)
        self.assertEqual(migration["pages"], 1)
        page = store.get_page(self.root, "S001", 1)
        current = page["published"]["current"]
        self.assertEqual((self.root / current["published_path"]).read_bytes(), b"published")
        self.assertEqual(current["source_hash"], "source-1")
        self.assertEqual(current["image_sha256"],
                         store.sha256_file(self.root / current["published_path"]))
        self.assertNotIn("candidate_path", current)
        self.assertEqual(page["version_high_watermark"], 2)

        result = store.compact_candidates(self.root, retain=[("S001", 1, 2)])
        self.assertEqual(result["pruned"], 1)
        self.assertEqual(result["retained"], 1)
        self.assertTrue(store.candidate_path(self.root, "S001", 1, 2).is_file())
        self.assertEqual([candidate["version"] for candidate in
                          store.list_candidates(self.root, "S001", 1)], [2])
        pruned = store.get_candidate(self.root, "S001", 1, 1)
        self.assertTrue(pruned["artifact_pruned"])
        self.assertFalse(pruned["artifact_exists"])
        self.assertIsNone(store.get_candidate(
            self.root, "S001", 1, 1, include_pruned=False))
        history = store.list_candidates(self.root, "S001", 1, include_pruned=True)
        self.assertEqual([candidate["version"] for candidate in history], [1, 2])
        self.assertTrue(history[0]["artifact_pruned"])
        self.assertTrue(history[1]["artifact_exists"])

    def test_compact_publication_history_keeps_current(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            store.publish_candidate(self.root, "S001", 1, version)
        current = store.published_path(self.root, "S001", 1)
        previous = store.previous_published_path(self.root, "S001", 1)
        self.assertTrue(previous.is_file())

        result = store.compact_publication_history(self.root)
        self.assertEqual(result["cleared"], 1)
        self.assertFalse(previous.exists())
        self.assertEqual(current.read_bytes(), b"two")
        page = store.get_page(self.root, "S001", 1)
        self.assertIsNone(page["published"]["previous"])
        self.assertEqual(page["published"]["current"]["version"], 2)
        self.assertEqual(store.next_candidate_version(self.root, "S001", 1), 3)

    def test_compaction_validates_retain_set_before_pruning(self):
        self.ensure()
        self.candidate(1, b"one")
        with self.assertRaises(KeyError):
            store.compact_candidates(self.root, retain=[("S001", 1, 99)])
        self.assertTrue(store.candidate_path(self.root, "S001", 1, 1).is_file())
        self.assertFalse(store.get_candidate(self.root, "S001", 1, 1)["artifact_pruned"])

    def test_version_high_watermark_survives_candidate_metadata_pruning(self):
        self.ensure()
        self.candidate(7, b"seven")
        doc = store.load_pages_doc(self.root)
        page = doc["pages"]["S001:p1"]
        page["candidates"].clear()
        store.candidate_path(self.root, "S001", 1, 7).unlink()
        store.save_pages_doc(self.root, doc)
        self.assertEqual(store.next_candidate_version(self.root, "S001", 1), 8)
        candidate = self.candidate(content=b"eight")
        self.assertEqual(candidate["version"], 8)

    def test_published_page_with_newer_candidate_stays_buildable(self):
        self.ensure()
        self.candidate(1, b"published")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        self.candidate(2, b"review")
        self.mark_review(version=2)

        summary = store.gate_summary(self.root, [("S001", 1)])
        self.assertEqual(summary["published"], 1)
        self.assertEqual(summary["pending"], 0)
        self.assertEqual(summary["update_review"], 1)
        self.assertEqual(summary["update_review_pages"], ["S001:p1"])
        self.assertTrue(summary["can_build_final"])

    def test_summary_blocks_missing_pending_blocked_and_stale(self):
        expected = [("S001", 1), ("S002", 1), ("S003", 1), ("S004", 1)]
        self.ensure("S001")
        self.candidate(sid="S001")
        self.mark_review("S001", 1, 1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)

        self.ensure("S002")
        self.candidate(sid="S002")
        self.ensure("S003")
        self.candidate(sid="S003")
        store.record_decision(self.root, "S003", 1, 1, "reject")
        store.ensure_page(self.root, "S001", 1, "source-2", "template-1", "prompt-2")

        summary = store.gate_summary(self.root, expected)
        self.assertEqual(summary["stale"], 1)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["blocked"], 1)
        self.assertEqual(summary["missing"], 1)
        self.assertFalse(summary["can_build_final"])
        self.assertFalse(store.can_build_final(self.root, expected))

    def test_publish_rejects_stale_page_and_candidate_hashes(self):
        self.ensure()
        self.candidate(1, b"old")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.ensure_page(self.root, "S001", 1, "source-2", "template-1", "prompt-2")

        with self.assertRaisesRegex(PermissionError, "stale candidate hashes"):
            store.publish_candidate(self.root, "S001", 1, 1)
        page = store.get_page(self.root, "S001", 1)
        self.assertTrue(page["stale"])
        self.assertEqual(page["status"], "stale")
        self.assertFalse(store.published_path(self.root, "S001", 1).exists())

        self.candidate(2, b"fresh")
        self.mark_review(version=2)
        store.record_decision(self.root, "S001", 1, 2, "approve")
        store.publish_candidate(self.root, "S001", 1, 2)
        self.assertFalse(store.get_page(self.root, "S001", 1)["stale"])

    def test_ensure_page_hashes_validates_and_refreshes(self):
        with self.assertRaisesRegex(ValueError, "missing page hashes"):
            store.ensure_page_hashes(self.root, "S001", 1, {"source_hash": "source"})
        rec = store.ensure_page_hashes(self.root, "S001", 1, {
            "source_hash": "source", "template_hash": "template", "prompt_hash": "prompt"})
        self.assertEqual(rec["prompt_hash"], "prompt")

    def test_whole_ledger_updates_survive_threads_and_processes(self):
        thread_sids = [f"T{i:03d}" for i in range(12)]
        barrier = threading.Barrier(len(thread_sids))

        def ensure_thread(sid):
            barrier.wait()
            _ensure_page_in_process(str(self.root), sid)

        threads = [threading.Thread(target=ensure_thread, args=(sid,))
                   for sid in thread_sids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())

        process_sids = [f"P{i:03d}" for i in range(8)]
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=_ensure_page_in_process,
                                 args=(str(self.root), sid)) for sid in process_sids]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        pages = store.load_pages_doc(self.root)["pages"]
        self.assertTrue({f"{sid}:p1" for sid in thread_sids + process_sids}.issubset(pages))

    def test_concurrent_registration_preserves_pages_and_versions(self):
        self.ensure("S001")
        self.ensure("S002")
        for sid in ("S001", "S002"):
            for version in range(1, 7):
                path = store.candidate_path(self.root, sid, 1, version)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{sid}-{version}".encode())
        barrier = threading.Barrier(12)
        errors = []

        def register(sid, version):
            try:
                barrier.wait()
                store.register_candidate(self.root, {
                    "sid": sid, "page": 1, "version": version,
                    "source_hash": "source-1", "template_hash": "template-1",
                    "prompt_hash": "prompt-1",
                })
            except Exception as exc:  # captured for an assertion in the test thread
                errors.append(exc)

        threads = [threading.Thread(target=register, args=(sid, version))
                   for sid in ("S001", "S002") for version in range(1, 7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(store.list_candidates(self.root, "S001", 1)), 6)
        self.assertEqual(len(store.list_candidates(self.root, "S002", 1)), 6)

    def test_publish_save_failure_restores_artifacts_and_metadata(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two"), (3, b"three")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            if version < 3:
                store.publish_candidate(self.root, "S001", 1, version)
        before_doc = copy.deepcopy(store.load_pages_doc(self.root))
        current = store.published_path(self.root, "S001", 1)
        previous = store.previous_published_path(self.root, "S001", 1)
        before_bytes = (current.read_bytes(), previous.read_bytes())
        with patch.object(store, "save_pages_doc", side_effect=OSError("injected save")):
            with self.assertRaisesRegex(OSError, "injected save"):
                store.publish_candidate(self.root, "S001", 1, 3)
        self.assertEqual((current.read_bytes(), previous.read_bytes()), before_bytes)
        self.assertEqual(store.load_pages_doc(self.root), before_doc)

    def test_process_death_during_publish_recovers_on_next_read(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            if version == 1:
                store.publish_candidate(self.root, "S001", 1, version)
        ctx = multiprocessing.get_context("spawn")
        process = ctx.Process(target=_crash_during_publish, args=(str(self.root),))
        process.start(); process.join(15)
        self.assertEqual(process.exitcode, 73)
        self.assertTrue((self.root / store._TXN).is_file())
        page = store.get_page(self.root, "S001", 1)
        self.assertEqual(page["published"]["current"]["version"], 1)
        self.assertEqual(store.published_path(self.root, "S001", 1).read_bytes(), b"one")
        self.assertFalse((self.root / store._TXN).exists())

    def test_recover_interrupted_publish_from_durable_journal(self):
        self.ensure()
        self.candidate(1, b"one")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        current = store.published_path(self.root, "S001", 1)
        ledger_before = store.sha256_file(self.root / "work/fullimage-pages.json")
        backup = store._stage_copy(current, current, "backup")
        staged = store._stage_copy(store.candidate_path(self.root, "S001", 1, 1), current, "publish")
        store._write_artifact_journal(self.root.resolve(), "publish", [
            (current, staged, backup, True),
        ])
        current.write_bytes(b"crash-new-bytes")
        journal = store.load_json(self.root / store._TXN)
        self.assertEqual(journal["ledger_before_sha256"], ledger_before)
        store.load_pages_doc(self.root)
        self.assertEqual(current.read_bytes(), b"one")
        self.assertFalse((self.root / store._TXN).exists())

    def test_gate_verifies_published_image_hash(self):
        self.ensure()
        self.candidate(1, b"one")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        store.published_path(self.root, "S001", 1).write_bytes(b"corrupt")
        summary = store.gate_summary(self.root, [("S001", 1)], verify_artifacts=True)
        self.assertEqual(summary["stale"], 1)
        self.assertFalse(summary["can_build_final"])

    def test_gate_rejects_missing_digest_and_can_build_final_verifies(self):
        self.ensure()
        self.candidate(1, b"one")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        doc = store.load_pages_doc(self.root)
        doc["pages"]["S001:p1"]["published"]["current"].pop("image_sha256")
        store.save_pages_doc(self.root, doc)
        summary = store.gate_summary(self.root, [("S001", 1)], verify_artifacts=True)
        self.assertEqual(summary["stale"], 1)
        self.assertFalse(store.can_build_final(self.root, [("S001", 1)]))

    def test_publish_copy_failure_does_not_change_state(self):
        self.ensure()
        self.candidate(1, b"one")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        before_doc = copy.deepcopy(store.load_pages_doc(self.root))
        with patch.object(store.shutil, "copy2", side_effect=OSError("injected copy")):
            with self.assertRaisesRegex(OSError, "injected copy"):
                store.publish_candidate(self.root, "S001", 1, 1)
        self.assertFalse(store.published_path(self.root, "S001", 1).exists())
        self.assertEqual(store.load_pages_doc(self.root), before_doc)

    def test_rollback_save_failure_restores_artifacts_and_metadata(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            store.publish_candidate(self.root, "S001", 1, version)
        before_doc = copy.deepcopy(store.load_pages_doc(self.root))
        current = store.published_path(self.root, "S001", 1)
        previous = store.previous_published_path(self.root, "S001", 1)
        before_bytes = (current.read_bytes(), previous.read_bytes())
        with patch.object(store, "save_pages_doc", side_effect=OSError("injected save")):
            with self.assertRaisesRegex(OSError, "injected save"):
                store.rollback_published(self.root, "S001", 1)
        self.assertEqual((current.read_bytes(), previous.read_bytes()), before_bytes)
        self.assertEqual(store.load_pages_doc(self.root), before_doc)

    def test_compact_candidates_preserves_unresolved_and_requires_force(self):
        self.ensure()
        self.candidate(1, b"pending")
        self.candidate(2, b"published")
        self.mark_review(version=2)
        store.record_decision(self.root, "S001", 1, 2, "approve")
        store.publish_candidate(self.root, "S001", 1, 2)
        result = store.compact_candidates(self.root)
        self.assertEqual(result["retained"], 1)
        self.assertTrue(store.candidate_path(self.root, "S001", 1, 1).exists())
        self.assertFalse(store.candidate_path(self.root, "S001", 1, 2).exists())
        with self.assertRaisesRegex(ValueError, "omits unresolved"):
            store.compact_candidates(self.root, retain=[])
        store.compact_candidates(self.root, retain=[], force=True)
        self.assertFalse(store.candidate_path(self.root, "S001", 1, 1).exists())

    def test_compaction_save_failures_leave_artifacts_and_metadata(self):
        self.ensure()
        self.candidate(1, b"one")
        self.mark_review(version=1)
        store.record_decision(self.root, "S001", 1, 1, "approve")
        store.publish_candidate(self.root, "S001", 1, 1)
        before_doc = copy.deepcopy(store.load_pages_doc(self.root))
        candidate = store.candidate_path(self.root, "S001", 1, 1)
        with patch.object(store, "save_pages_doc", side_effect=OSError("injected save")):
            with self.assertRaisesRegex(OSError, "injected save"):
                store.compact_candidates(self.root)
        self.assertTrue(candidate.exists())
        self.assertEqual(store.load_pages_doc(self.root), before_doc)

        self.candidate(2, b"two")
        self.mark_review(version=2)
        store.record_decision(self.root, "S001", 1, 2, "approve")
        store.publish_candidate(self.root, "S001", 1, 2)
        previous = store.previous_published_path(self.root, "S001", 1)
        before_doc = copy.deepcopy(store.load_pages_doc(self.root))
        before_bytes = previous.read_bytes()
        with patch.object(store, "save_pages_doc", side_effect=OSError("injected save")):
            with self.assertRaisesRegex(OSError, "injected save"):
                store.compact_publication_history(self.root)
        self.assertEqual(previous.read_bytes(), before_bytes)
        self.assertEqual(store.load_pages_doc(self.root), before_doc)

    def test_job_lifecycle(self):
        job = store.create_job(self.root, {"sid": "S001", "page": 1})
        self.assertEqual(job["status"], "generating")
        self.assertEqual(len(job["job_id"]), 36)
        updated = store.update_job(
            self.root, job["job_id"], {"status": "completed", "candidate_version": 1})
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(store.get_job(self.root, job["job_id"])["candidate_version"], 1)


if __name__ == "__main__":
    unittest.main()
