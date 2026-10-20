#!/usr/bin/env python3
"""Full-image store lifecycle tests."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_fullimage_store as store  # noqa: E402


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

    def test_rollback_published(self):
        self.ensure()
        for version, content in ((1, b"one"), (2, b"two")):
            self.candidate(version, content)
            self.mark_review(version=version)
            store.record_decision(self.root, "S001", 1, version, "approve")
            store.publish_candidate(self.root, "S001", 1, version)
        restored = store.rollback_published(self.root, "S001", 1)
        self.assertEqual(restored["path"].read_bytes(), b"one")
        self.assertEqual(restored["current"]["version"], 1)
        self.assertEqual(restored["previous"]["version"], 2)

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
