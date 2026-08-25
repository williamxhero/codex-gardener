from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "plugins" / "codex-gardener" / "scripts" / "retrieval.py"
SPEC = importlib.util.spec_from_file_location("retrieval", SCRIPT)
assert SPEC and SPEC.loader
retrieval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retrieval)


class RetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {"CODEX_HOME": str(Path(self.temp.name) / "codex-home")},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = Path(self.temp.name) / "repository"
        self.repo.mkdir()
        self.store = self.repo / ".codex" / "learning"
        self.store.mkdir(parents=True)

    def write_index(self, *records: dict) -> None:
        (self.store / "index.jsonl").write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8"
        )

    def test_sync_and_retrieve_uses_weighted_tokens_and_hard_filters(self) -> None:
        self.write_index(
            {
                "fingerprint": "aaaaaa",
                "knowledge_scope": "repository",
                "summary": "Run parser contract tests before parser changes.",
                "keywords": ["parser", "contract"],
                "target_path": "tests/test_parser.py",
                "task_types": ["bugfix"],
                "path_globs": ["src/**"],
                "languages": ["python"],
                "min_score": 0.75,
                "promoted_at": "2026-01-01T00:00:00Z",
            },
            {
                "fingerprint": "bbbbbb",
                "knowledge_scope": "repository",
                "summary": "Never expose secrets in examples.",
                "keywords": ["secret"],
                "target_path": "README.md",
                "negative_keywords": ["parser"],
                "promoted_at": "2026-01-02T00:00:00Z",
            },
        )
        result = retrieval.sync_scope(self.repo)
        self.assertEqual(result["document_count"], 2)
        found, metrics = retrieval.retrieve(
            self.repo,
            "Fix the parser contract failure",
            task_context={"task_types": ["bugfix"], "paths": ["src/parser.py"], "languages": ["python"]},
        )
        self.assertEqual([item["fingerprint"] for item in found], ["aaaaaa"])
        # The negative entry is unrelated and is not reached from parser postings.
        self.assertEqual(metrics["filtered_negative"], 0)
        self.assertEqual(metrics["scored"], 1)

    def test_stale_database_fails_open_without_json_fallback(self) -> None:
        self.write_index(
            {
                "fingerprint": "aaaaaa",
                "knowledge_scope": "repository",
                "summary": "Parser contract rule.",
                "keywords": ["parser"],
                "target_path": "AGENTS.md",
            }
        )
        retrieval.sync_scope(self.repo)
        (self.store / "index.jsonl").write_text(
            json.dumps({"fingerprint": "bbbbbb", "summary": "Different rule", "keywords": ["different"], "target_path": "x"})
            + "\n",
            encoding="utf-8",
        )
        found, metrics = retrieval.retrieve(self.repo, "different")
        self.assertEqual(found, [])
        self.assertTrue(metrics["retrieval_degraded"])

    def test_audit_is_read_only_and_reports_stale_duplicate_and_orphan(self) -> None:
        self.write_index(
            {
                "fingerprint": "aaaaaa",
                "summary": "Run the parser test.",
                "keywords": ["parser"],
                "target_path": "missing.md",
                "promoted_at": "2020-01-01T00:00:00Z", "min_score": 20,
            },
            {
                "fingerprint": "bbbbbb",
                "summary": "run the parser test.",
                "keywords": ["parser"],
                "target_path": "also-missing.md",
                "promoted_at": "2020-01-01T00:00:00Z", "min_score": 20,
            },
        )
        retrieval.sync_scope(self.repo)
        before = (self.store / "index.jsonl").read_bytes()
        database_before = (self.store / "retrieval.sqlite3").read_bytes()
        audit = retrieval.audit_scope(self.repo)
        self.assertGreaterEqual(audit["counts"]["exact-duplicate"], 2)
        self.assertGreaterEqual(audit["counts"]["orphaned-target"], 2)
        self.assertEqual(before, (self.store / "index.jsonl").read_bytes())
        self.assertEqual(database_before, (self.store / "retrieval.sqlite3").read_bytes())

    def test_corruption_and_privacy_fail_open_without_query_persistence(self) -> None:
        secret_prompt = "do-not-persist-this-unique-prompt"
        self.write_index(
            {"fingerprint": "aaaaaa", "summary": "Read parser docs.", "keywords": ["parser"], "target_path": "docs.md"}
        )
        retrieval.sync_scope(self.repo)
        retrieval.retrieve(self.repo, secret_prompt)
        self.assertNotIn(secret_prompt.encode("utf-8"), (self.store / "retrieval.sqlite3").read_bytes())
        (self.store / "retrieval.sqlite3").write_bytes(b"not a sqlite database")
        found, metrics = retrieval.retrieve(self.repo, "parser")
        self.assertEqual(found, [])
        self.assertTrue(metrics["retrieval_degraded"])

    def test_aging_uses_misses_without_auto_retiring(self) -> None:
        self.write_index(
            {
                "fingerprint": "aaaaaa", "summary": "Old parser rule.", "keywords": ["parser"], "target_path": "missing.md",
                "promoted_at": "2020-01-01T00:00:00Z", "min_score": 20,
            }
        )
        retrieval.sync_scope(self.repo)
        for _ in range(50):
            retrieval.retrieve(self.repo, "parser")
        audit = retrieval.audit_scope(self.repo)
        self.assertEqual(audit["counts"]["stale-review"], 1)
        self.assertIn("aaaaaa", (self.store / "index.jsonl").read_text(encoding="utf-8"))

    def test_retrieval_benchmarks_1k_10k_and_50k_entries(self) -> None:
        """A regression guard against accidental linear JSON fallback at scale."""
        for total in (1_000, 10_000, 50_000):
            with self.subTest(entries=total):
                repo = Path(self.temp.name) / f"scale-{total}"
                store = repo / ".codex" / "learning"
                store.mkdir(parents=True)
                with (store / "index.jsonl").open("w", encoding="utf-8") as handle:
                    for index in range(total):
                        handle.write(json.dumps({
                            "fingerprint": f"{index:064x}", "summary": f"Rule {index} {'parser ' if index == total - 1 else ''}contract.",
                            "keywords": ["parser" if index == total - 1 else "other"], "target_path": "AGENTS.md",
                        }) + "\n")
                retrieval.sync_scope(repo)
                started = time.monotonic()
                found, metrics = retrieval.retrieve(repo, "parser")
                elapsed = time.monotonic() - started
                self.assertEqual(found[0]["fingerprint"], f"{total - 1:064x}")
                self.assertEqual(metrics["injected"], 1)
                limit = 3.0 if total == 50_000 else 0.25
                self.assertLess(elapsed, limit, f"{total} entries took {elapsed:.3f}s")

    def test_public_repository_scope_never_treats_repo_index_as_the_store(self) -> None:
        (self.repo / "index.jsonl").write_text('{"legacy":"ignored"}\n', encoding="utf-8")
        self.write_index({"fingerprint": "aaaaaa", "summary": "Repository rule.", "target_path": "AGENTS.md"})
        self.assertEqual(retrieval.store_for(self.repo, "repository"), self.store)
        retrieval.sync_scope(self.repo)
        self.assertTrue((self.store / "retrieval.sqlite3").is_file())
        self.assertFalse((self.repo / "retrieval.sqlite3").exists())

    def test_audit_tolerates_each_bad_authoritative_line_without_content_leaks(self) -> None:
        valid = {"fingerprint": "aaaaaa", "summary": "Keep valid rule.", "target_path": "missing.md"}
        invalid_metadata = {"fingerprint": "bbbbbb", "summary": "secret summary", "target_path": "secret/path", "languages": ["Bad"]}
        (self.store / "index.jsonl").write_text(
            json.dumps(valid) + "\nnot-json-secret\n" + json.dumps(invalid_metadata) + "\n",
            encoding="utf-8",
        )
        before = (self.store / "index.jsonl").read_bytes()
        audit = retrieval.audit_scope(self.repo)
        self.assertEqual(audit["counts"]["invalid-metadata"], 2)
        self.assertEqual(audit["counts"]["orphaned-target"], 1)
        rendered = json.dumps(audit)
        self.assertNotIn("secret summary", rendered)
        self.assertNotIn("secret/path", rendered)
        self.assertNotIn("not-json-secret", rendered)
        self.assertEqual(before, (self.store / "index.jsonl").read_bytes())

    def test_latin_phrases_are_boundary_aware_and_cjk_phrases_remain_normalized(self) -> None:
        self.write_index(
            {"fingerprint": "aaaaaa", "summary": "Google guidance.", "keywords": ["google"], "negative_keywords": ["go"], "target_path": "AGENTS.md"},
            {"fingerprint": "bbbbbb", "summary": "中文 指导", "keywords": ["中文"], "negative_keywords": ["中文 指导"], "target_path": "AGENTS.md"},
        )
        retrieval.sync_scope(self.repo)
        found, metrics = retrieval.retrieve(self.repo, "google search 中文　指导")
        self.assertEqual([entry["fingerprint"] for entry in found], ["aaaaaa"])
        self.assertEqual(metrics["filtered_negative"], 1)

    def test_bounded_root_markers_detect_solution_projects_and_unity_without_recursive_scan(self) -> None:
        (self.repo / "Project.sln").write_text("", encoding="utf-8")
        (self.repo / "Game.csproj").write_text("", encoding="utf-8")
        (self.repo / "Assets").mkdir()
        (self.repo / "ProjectSettings").mkdir()
        context = retrieval.derive_task_context("update gameplay", self.repo)
        self.assertIn("csharp", context["languages"])
        self.assertIn("unity", context["task_types"])
        self.assertIn("unity", context["tools"])

    def test_corrupt_and_locked_sqlite_degrade_or_report_status_without_sqlite_errors(self) -> None:
        self.write_index({"fingerprint": "aaaaaa", "summary": "Parser rule.", "keywords": ["parser"], "target_path": "AGENTS.md"})
        retrieval.sync_scope(self.repo)
        database = self.store / "retrieval.sqlite3"
        database.write_bytes(b"not sqlite")
        self.assertTrue(retrieval.index_status(self.repo)["needs_rebuild"])
        self.assertEqual(retrieval.audit_scope(self.repo)["counts"]["invalid-metadata"], 0)
        found, metrics = retrieval.retrieve(self.repo, "parser")
        self.assertEqual(found, [])
        self.assertTrue(metrics["retrieval_degraded"])

        retrieval.sync_scope(self.repo)
        with patch.object(retrieval, "_read_index", side_effect=sqlite3.OperationalError("database is locked")):
            found, metrics = retrieval.retrieve(self.repo, "parser")
        self.assertEqual(found, [])
        self.assertTrue(metrics["retrieval_degraded"])

    def test_degraded_lookup_logs_once_anonymously(self) -> None:
        self.write_index({"fingerprint": "aaaaaa", "summary": "Parser rule.", "keywords": ["parser"], "target_path": "AGENTS.md"})
        retrieval.sync_scope(self.repo)
        (self.store / "retrieval.sqlite3").write_bytes(b"not sqlite")
        messages: list[str] = []
        retrieval.retrieve(self.repo, "private parser prompt", error_logger=messages.append)
        self.assertEqual(messages, ["retrieval_degraded"])

    def test_context_budget_includes_heading_and_skips_oversized_entries(self) -> None:
        huge_summary = "parser " * 3_000
        self.write_index(
            {"fingerprint": "aaaaaa", "summary": huge_summary, "keywords": ["parser"], "target_path": "AGENTS.md"},
            {"fingerprint": "bbbbbb", "summary": "Use parser contract tests.", "keywords": ["parser"], "target_path": "AGENTS.md"},
        )
        retrieval.sync_scope(self.repo)
        found, metrics = retrieval.retrieve(self.repo, "parser")
        rendered = "\n".join([retrieval.CONTEXT_HEADING, *(retrieval.render_line(entry) for entry in found)])
        self.assertEqual([entry["fingerprint"] for entry in found], ["bbbbbb"])
        self.assertEqual(metrics["estimated_tokens"], retrieval.estimate_tokens(rendered))
        self.assertLessEqual(metrics["estimated_tokens"], retrieval.MAX_CONTEXT_TOKENS)

    def test_concurrent_rebuilds_preserve_a_valid_database_and_usage_updates_degrade_safely(self) -> None:
        self.write_index({"fingerprint": "aaaaaa", "summary": "Parser rule.", "keywords": ["parser"], "target_path": "AGENTS.md"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: retrieval.sync_scope(self.repo), range(2)))
        self.assertEqual([result["document_count"] for result in results], [1, 1])
        found, metrics = retrieval.retrieve(self.repo, "parser")
        self.assertEqual([entry["fingerprint"] for entry in found], ["aaaaaa"])
        self.assertFalse(metrics["retrieval_degraded"])


if __name__ == "__main__":
    unittest.main()
