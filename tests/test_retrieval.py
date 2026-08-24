from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "plugins" / "codex-gardener" / "scripts" / "retrieval.py"
SPEC = importlib.util.spec_from_file_location("retrieval", SCRIPT)
assert SPEC and SPEC.loader
retrieval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(retrieval)


class RetrievalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "learning"
        self.store.mkdir()

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
        result = retrieval.sync_scope(self.store)
        self.assertEqual(result["document_count"], 2)
        found, metrics = retrieval.retrieve(
            self.store,
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
        retrieval.sync_scope(self.store)
        (self.store / "index.jsonl").write_text(
            json.dumps({"fingerprint": "bbbbbb", "summary": "Different rule", "keywords": ["different"], "target_path": "x"})
            + "\n",
            encoding="utf-8",
        )
        found, metrics = retrieval.retrieve(self.store, "different")
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
        retrieval.sync_scope(self.store)
        before = (self.store / "index.jsonl").read_bytes()
        audit = retrieval.audit_scope(self.store)
        self.assertGreaterEqual(audit["counts"]["exact-duplicate"], 2)
        self.assertGreaterEqual(audit["counts"]["orphaned-target"], 2)
        self.assertEqual(before, (self.store / "index.jsonl").read_bytes())

    def test_corruption_and_privacy_fail_open_without_query_persistence(self) -> None:
        secret_prompt = "do-not-persist-this-unique-prompt"
        self.write_index(
            {"fingerprint": "aaaaaa", "summary": "Read parser docs.", "keywords": ["parser"], "target_path": "docs.md"}
        )
        retrieval.sync_scope(self.store)
        retrieval.retrieve(self.store, secret_prompt)
        self.assertNotIn(secret_prompt.encode("utf-8"), (self.store / "retrieval.sqlite3").read_bytes())
        (self.store / "retrieval.sqlite3").write_bytes(b"not a sqlite database")
        found, metrics = retrieval.retrieve(self.store, "parser")
        self.assertEqual(found, [])
        self.assertTrue(metrics["retrieval_degraded"])

    def test_aging_uses_misses_without_auto_retiring(self) -> None:
        self.write_index(
            {
                "fingerprint": "aaaaaa", "summary": "Old parser rule.", "keywords": ["parser"], "target_path": "missing.md",
                "promoted_at": "2020-01-01T00:00:00Z", "min_score": 20,
            }
        )
        retrieval.sync_scope(self.store)
        for _ in range(50):
            retrieval.retrieve(self.store, "parser")
        audit = retrieval.audit_scope(self.store)
        self.assertEqual(audit["counts"]["stale-review"], 1)
        self.assertIn("aaaaaa", (self.store / "index.jsonl").read_text(encoding="utf-8"))

    def test_retrieval_benchmarks_1k_10k_and_50k_entries(self) -> None:
        """A regression guard against accidental linear JSON fallback at scale."""
        for total in (1_000, 10_000, 50_000):
            with self.subTest(entries=total):
                store = Path(self.temp.name) / f"scale-{total}"
                store.mkdir()
                with (store / "index.jsonl").open("w", encoding="utf-8") as handle:
                    for index in range(total):
                        handle.write(json.dumps({
                            "fingerprint": f"{index:064x}", "summary": f"Rule {index} {'parser ' if index == total - 1 else ''}contract.",
                            "keywords": ["parser" if index == total - 1 else "other"], "target_path": "AGENTS.md",
                        }) + "\n")
                retrieval.sync_scope(store)
                started = time.monotonic()
                found, metrics = retrieval.retrieve(store, "parser")
                elapsed = time.monotonic() - started
                self.assertEqual(found[0]["fingerprint"], f"{total - 1:064x}")
                self.assertEqual(metrics["injected"], 1)
                limit = 3.0 if total == 50_000 else 0.25
                self.assertLess(elapsed, limit, f"{total} entries took {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()
