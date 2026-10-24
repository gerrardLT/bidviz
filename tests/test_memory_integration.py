from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

import bidviz_memory as memory  # noqa: E402


class MemoryCliIntegrationTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "src.bidviz_cli", *args],
            cwd=BASE,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )

    def test_validate_and_doctor_current_project(self):
        validated = self.run_cli("memory", "validate", "anke-35kv")
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertTrue(json.loads(validated.stdout)["ok"])

        doctor = self.run_cli("memory", "doctor", "anke-35kv")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertTrue(payload["ok"])
        self.assertTrue(all(item["fts5"] for item in payload["indexes"]))

    def test_context_is_scoped_and_excludes_candidate(self):
        result = self.run_cli(
            "memory", "context", "anke-35kv", "local-edit",
            "--engine", "fullimage", "--operation", "local-edit",
            "--section", "S030", "--page", "1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("proc.fullimage.local-edit.mask-only", payload["memory_ids"])
        self.assertNotIn(
            "candidate.anke.fullimage-quoted-prompt-rebaseline",
            payload["memory_ids"],
        )
        self.assertEqual(len(payload["context_hash"]), 64)


class FullimageCliGateTests(unittest.TestCase):
    def test_module_pdf_path_is_disabled_before_generation(self):
        result = subprocess.run(
            [sys.executable, "-m", "src.bidviz_fullimage", "projects/anke-35kv",
             "--all", "--pdf"],
            cwd=BASE, text=True, encoding="utf-8",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("模块直调 --pdf 已停用", result.stderr + result.stdout)


class MemoryKernelIntegrationTests(unittest.TestCase):
    def test_project_policy_does_not_match_another_project(self):
        records = memory.search_records(
            BASE / "projects" / "anke-35kv" / "memory",
            "45–50",
            scope={"project": "other-project", "operation": "compact"},
            refresh_index=True,
        )
        self.assertEqual(records, [])

    def test_index_is_disposable(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / "memory.sqlite3"
            first = memory.rebuild_index(BASE / "memory", index)
            first_records = memory.search_records(
                BASE / "memory", "局部改字", index_path=index,
                rebuild_if_missing=False,
            )
            index.unlink()
            second = memory.rebuild_index(BASE / "memory", index)
            second_records = memory.search_records(
                BASE / "memory", "局部改字", index_path=index,
                rebuild_if_missing=False,
            )
        self.assertEqual(first["records"], second["records"])
        self.assertEqual(
            [item["id"] for item in first_records],
            [item["id"] for item in second_records],
        )


if __name__ == "__main__":
    unittest.main()
