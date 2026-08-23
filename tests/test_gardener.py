from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "plugins" / "codex-gardener" / "scripts" / "gardener.py"
SPEC = importlib.util.spec_from_file_location("gardener", SCRIPT)
assert SPEC and SPEC.loader
gardener = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gardener)


class GardenerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.other = Path(self.temp.name) / "other-repo"
        self.other.mkdir()
        self.data = Path(self.temp.name) / "plugin-data"
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.env = patch.dict(
            os.environ,
            {
                "CODEX_GARDENER_DATA": str(self.data),
                "CODEX_HOME": str(self.codex_home),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def payload(self, **extra):
        value = {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": str(self.root),
            "transcript_path": None,
        }
        value.update(extra)
        return value

    def run_hook(self, event: str, payload: dict) -> dict:
        output = io.StringIO()
        with patch.object(gardener, "read_stdin_json", return_value=payload), redirect_stdout(output):
            self.assertEqual(gardener.handle_hook(event), 0)
        return json.loads(output.getvalue()) if output.getvalue() else {}

    def run_cli(self, *arguments: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=self.root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def log_completed_review(self, session_id: str) -> None:
        common = {"session": session_id, "project": str(self.root), "run_kind": "real"}
        gardener.log_effectiveness("review_requested", **common, signals=["workspace_changed"])
        gardener.log_effectiveness("review_completed_no_candidate", **common)

    def queue_pending(self, session_id: str, repo: Path | None = None) -> dict:
        target = repo or self.root
        payload = self.payload(
            session_id=session_id,
            turn_id=f"turn-{session_id}",
            cwd=str(target),
        )
        self.run_hook("SessionStart", payload)
        self.run_hook("UserPromptSubmit", payload | {"prompt": "纠正：这个任务需要复盘。"})
        self.assertEqual(self.run_hook("Stop", payload | {"stop_hook_active": False}), {"continue": True})
        return next(record for record in gardener.pending_records() if record["session_id"] == session_id)

    def start_maintenance(self, *, session_id: str = "maintenance-session") -> tuple[dict, dict]:
        payload = self.payload(
            session_id=session_id,
            turn_id=f"turn-{session_id}",
            cwd=str(self.other),
            prompt="[codex-gardener:scheduled-maintenance]",
        )
        self.run_hook("SessionStart", payload)
        self.run_hook("UserPromptSubmit", payload)
        requested = self.run_hook("Stop", payload | {"stop_hook_active": False})
        return payload, requested

    def init_git(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "gardener@example.test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Gardener Test"], check=True)
        (self.root / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "baseline"], check=True)

    def candidate_args(self, *, repo: Path | None = None, **extra) -> argparse.Namespace:
        values = {
            "repo": str(repo or self.root),
            "session_id": "session-1",
            "knowledge_scope": "repository",
            "scope": "tests",
            "lesson": "Run the contract test before changing the parser.",
            "evidence": "Observed in a completed task.",
            "target": "test",
            "confidence": 0.9,
        }
        values.update(extra)
        return argparse.Namespace(**values)

    def resolution_args(self, **extra) -> argparse.Namespace:
        values = {
            "repo": str(self.root),
            "fingerprint": "abc123",
            "knowledge_scope": "repository",
            "status": "promoted",
            "summary": "Run parser contract tests before parser changes.",
            "target_path": "AGENTS.md",
            "keyword": ["parser", "contract"],
        }
        values.update(extra)
        return argparse.Namespace(**values)

    def test_stop_without_signal_continues(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})

    def test_audit_status_becomes_due_on_tenth_completed_real_review(self) -> None:
        initialized = self.run_cli("audit-status", "--repo", str(self.root), "--initialize")
        self.assertFalse(initialized["due"])
        for index in range(9):
            self.log_completed_review(f"review-{index}")
        at_nine = self.run_cli("audit-status", "--repo", str(self.root))
        self.assertEqual(at_nine["qualifying_reviews"], 9)
        self.assertFalse(at_nine["due"])
        self.log_completed_review("review-9")
        at_ten = self.run_cli("audit-status", "--repo", str(self.root))
        self.assertEqual(at_ten["qualifying_reviews"], 10)
        self.assertTrue(at_ten["due"])
        self.assertEqual(at_ten["reason"], "review_threshold")

    def test_first_audit_status_initializes_the_time_deadline(self) -> None:
        status = self.run_cli("audit-status", "--repo", str(self.root))
        self.assertTrue(status["available"])
        self.assertEqual(status["checkpoint_status"], "initialized")
        self.assertIsNotNone(status["checkpoint"]["initialized_at"])
        self.assertIsNotNone(status["deadline_at"])

    def test_audit_status_excludes_smoke_legacy_duplicate_and_incomplete_reviews(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(8):
            self.log_completed_review(f"real-{index}")
        self.log_completed_review("real-0")
        for session in ("smoke-1", "smoke-2"):
            common = {"session": session, "project": str(self.root), "run_kind": "smoke"}
            gardener.log_effectiveness("review_requested", **common, signals=["workspace_changed"])
            gardener.log_effectiveness("capture_recorded", **common, knowledge_scope="repository", recommended_target="docs", confidence_bucket="high")
        legacy_request = gardener.effectiveness.build_event(
            "review_requested", session="legacy", project=str(self.root), signals=["workspace_changed"]
        )
        legacy_terminal = gardener.effectiveness.build_event(
            "review_completed_no_candidate", session="legacy", project=str(self.root)
        )
        assert legacy_request and legacy_terminal
        legacy_request.pop("run_kind")
        legacy_terminal.pop("run_kind")
        gardener.append_jsonl(gardener.effectiveness.log_path(self.data), legacy_request)
        gardener.append_jsonl(gardener.effectiveness.log_path(self.data), legacy_terminal)
        gardener.log_effectiveness(
            "review_requested", session="incomplete", project=str(self.root), signals=["workspace_changed"]
        )
        gardener.log_effectiveness("review_completed_no_candidate", session="orphan", project=str(self.root))
        gardener.log_effectiveness("review_completed_no_candidate", session="reversed", project=str(self.root))
        gardener.log_effectiveness(
            "review_requested", session="reversed", project=str(self.root), signals=["workspace_changed"]
        )

        status = self.run_cli("audit-status")
        self.assertEqual(status["qualifying_reviews"], 8)
        self.assertFalse(status["due"])

    def test_elapsed_schedule_does_not_interrupt_smoke_runs(self) -> None:
        status = self.run_cli("audit-status", "--initialize")
        checkpoint = status["checkpoint"]
        checkpoint["initialized_at"] = (
            datetime.now(timezone.utc) - timedelta(days=8)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        gardener.atomic_write_json(gardener.audit_checkpoint_path(), checkpoint)
        with patch.dict(os.environ, {gardener.effectiveness.RUN_KIND_ENV: "smoke"}):
            self.run_hook("SessionStart", self.payload(source="startup"))
            result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})

    def test_audit_status_becomes_due_after_seven_days_without_reviews(self) -> None:
        status = self.run_cli("audit-status", "--initialize")
        checkpoint = status["checkpoint"]
        checkpoint["initialized_at"] = (
            datetime.now(timezone.utc) - timedelta(days=8)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        gardener.atomic_write_json(gardener.audit_checkpoint_path(), checkpoint)

        due = self.run_cli("audit-status")
        self.assertEqual(due["qualifying_reviews"], 0)
        self.assertTrue(due["due"])
        self.assertEqual(due["reason"], "elapsed_time")

    def test_invalid_audit_configuration_falls_back_to_safe_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                gardener.AUDIT_THRESHOLD_ENV: "not-an-integer",
                gardener.AUDIT_MAX_DAYS_ENV: "0",
            },
        ):
            status = self.run_cli("audit-status")
        self.assertEqual(status["review_threshold"], 10)
        self.assertEqual(status["max_days"], 7)

    def test_exact_scheduled_marker_forces_audit_but_near_match_does_not(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="Run [codex-gardener:scheduled-audit] when this response stops."),
        )
        forced = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(forced["decision"], "block")
        self.assertIn("$codex-gardener:knowledge-curator", forced["reason"])
        self.assertIn("read-only", forced["reason"].casefold())
        state = gardener.load_json_file(gardener.state_path("session-1"), {})
        self.assertTrue(state["force_audit"])
        self.assertNotIn("scheduled-audit", json.dumps(state))

        near_payload = self.payload(session_id="session-near", prompt="[codex-gardener:scheduled-audits]")
        self.run_hook("SessionStart", near_payload)
        self.run_hook("UserPromptSubmit", near_payload)
        self.assertEqual(self.run_hook("Stop", near_payload | {"stop_hook_active": False}), {"continue": True})

    def test_exact_maintenance_marker_continues_only_with_a_bounded_pending_batch(self) -> None:
        for index in range(4):
            payload = self.payload(session_id=f"source-{index}", turn_id=f"source-turn-{index}")
            self.run_hook("SessionStart", payload)
            self.run_hook(
                "UserPromptSubmit",
                payload | {"prompt": "纠正：保留这个可复用经验。"},
            )
            self.assertEqual(self.run_hook("Stop", payload | {"stop_hook_active": False}), {"continue": True})
        pending = gardener.pending_records()
        self.assertEqual(len(pending), 4)

        maintenance = self.payload(
            session_id="maintenance-session",
            turn_id="maintenance-turn",
            cwd=str(self.other),
            prompt="[codex-gardener:scheduled-maintenance]",
        )
        self.run_hook("SessionStart", maintenance)
        self.run_hook("UserPromptSubmit", maintenance)
        requested = self.run_hook("Stop", maintenance | {"stop_hook_active": False})

        self.assertEqual(requested["decision"], "block")
        self.assertIn("$codex-gardener:knowledge-curator", requested["reason"])
        self.assertIn("maintenance-only", requested["reason"])
        state = gardener.load_json_file(gardener.state_path("maintenance-session"), {})
        self.assertEqual(state["maintenance_pending_ids"], [item["pending_id"] for item in pending[:3]])
        self.assertNotIn(pending[3]["pending_id"], requested["reason"])

        empty_payload = self.payload(
            session_id="near-maintenance",
            turn_id="near-turn",
            cwd=str(self.other),
            prompt="[codex-gardener:scheduled-maintenances]",
        )
        self.run_hook("SessionStart", empty_payload)
        self.run_hook("UserPromptSubmit", empty_payload)
        self.assertEqual(
            self.run_hook("Stop", empty_payload | {"stop_hook_active": False}),
            {"continue": True},
        )

    def test_maintenance_marker_without_pending_never_continues(self) -> None:
        maintenance = self.payload(
            session_id="maintenance-empty",
            turn_id="maintenance-empty-turn",
            cwd=str(self.other),
            prompt="[codex-gardener:scheduled-maintenance]",
        )
        self.run_hook("SessionStart", maintenance)
        self.run_hook("UserPromptSubmit", maintenance)
        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": False}),
            {"continue": True},
        )

    def test_concurrent_maintenance_claims_are_disjoint_and_stale_claims_expire(self) -> None:
        for index in range(6):
            self.queue_pending(f"claim-source-{index}", self.root)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(gardener.claim_pending_records, "maintenance-a")
            second_future = pool.submit(gardener.claim_pending_records, "maintenance-b")
            first = first_future.result()
            second = second_future.result()

        first_ids = {item["pending_id"] for item in first}
        second_ids = {item["pending_id"] for item in second}
        self.assertEqual(len(first_ids), 3)
        self.assertEqual(len(second_ids), 3)
        self.assertFalse(first_ids & second_ids)

        records = gardener.read_jsonl(gardener.pending_path())
        for record in records:
            if record.get("record_type") == "claim":
                record["created_at"] = "2026-01-01T00:00:00Z"
        gardener._atomic_write_jsonl(gardener.pending_path(), records)
        reclaimed = gardener.claim_pending_records("maintenance-c")
        self.assertEqual(len(reclaimed), 3)

    def test_structurally_corrupt_pending_objects_do_not_starve_maintenance(self) -> None:
        path = gardener.pending_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n{}\n{}\n", encoding="utf-8")
        pending = self.queue_pending("valid-after-corruption", self.root)

        _, requested = self.start_maintenance(session_id="maintenance-after-corruption")

        self.assertEqual(requested["decision"], "block")
        self.assertIn(pending["pending_id"], requested["reason"])

    def test_maintenance_status_returns_a_bounded_batch_and_audit_status(self) -> None:
        for index in range(4):
            self.queue_pending(f"maintenance-status-{index}", self.root)

        status = self.run_cli("maintenance-status")

        self.assertEqual(status["pending_count"], 4)
        self.assertEqual(len(status["batch"]), 3)
        self.assertEqual(
            [item["pending_id"] for item in status["batch"]],
            [item["pending_id"] for item in gardener.pending_records()[:3]],
        )
        self.assertIn("due", status["audit"])
        selected = status["batch"][1]["pending_id"]
        filtered = self.run_cli("maintenance-status", "--pending-id", selected)
        self.assertEqual([item["pending_id"] for item in filtered["batch"]], [selected])

    def test_due_audit_remains_status_only_during_nonempty_maintenance(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"maintenance-audit-{index}")
        pending = self.queue_pending("source-audit-maintenance", self.root)
        maintenance, requested = self.start_maintenance(session_id="maintenance-with-audit")
        self.assertEqual(requested["decision"], "block")
        self.assertNotIn("read-only audit", requested["reason"].casefold())
        state = gardener.load_json_file(gardener.state_path("maintenance-with-audit"), {})
        self.assertFalse(state["audit_requested"])

        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-with-audit",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "no-candidate",
        )

        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertTrue(self.run_cli("audit-status")["due"])
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 0)

    def test_maintenance_deferred_repository_candidate_uses_trusted_pending_mapping_once(self) -> None:
        pending = self.queue_pending("source-repository", self.root)
        maintenance, requested = self.start_maintenance()
        self.assertEqual(requested["decision"], "block")

        deferred = self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-session",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "candidate",
            "--knowledge-scope",
            "repository",
            "--scope",
            "testing",
            "--lesson",
            "Run the focused regression before the full suite.",
            "--evidence",
            "The focused check isolated the failure before broad validation.",
            "--target",
            "test",
            "--confidence",
            "0.9",
        )
        self.assertEqual(
            deferred,
            {"deferred": True, "outcome": "candidate", "pending_id": pending["pending_id"]},
        )
        marker_text = next(
            (self.other / ".codex" / "learning" / "deferred-maintenance").rglob("*.json")
        ).read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), marker_text)
        self.assertNotIn("source-repository", marker_text)

        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        records = gardener.read_learning_records(self.root, "repository", "inbox.jsonl")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_id"], "source-repository")
        self.assertEqual(gardener.pending_records(), [])
        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertEqual(len(gardener.read_learning_records(self.root, "repository", "inbox.jsonl")), 1)

    def test_maintenance_deferred_global_candidate_is_written_only_by_second_stop(self) -> None:
        pending = self.queue_pending("source-global", self.root)
        maintenance, _ = self.start_maintenance(session_id="maintenance-global")
        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-global",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "candidate",
            "--knowledge-scope",
            "global",
            "--scope",
            "workflow",
            "--lesson",
            "Use isolated Git worktrees for concurrent writers.",
            "--evidence",
            "The rule applied across unrelated repositories.",
            "--target",
            "skill",
            "--confidence",
            "0.95",
        )
        self.assertEqual(gardener.read_learning_records(self.root, "global", "inbox.jsonl"), [])

        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        global_records = gardener.read_learning_records(self.root, "global", "inbox.jsonl")
        self.assertEqual(len(global_records), 1)
        self.assertEqual(global_records[0]["session_id"], "source-global")
        self.assertFalse((self.root / ".codex" / "learning" / "inbox.jsonl").exists())

    def test_concurrent_second_stops_commit_only_one_pending_outcome(self) -> None:
        pending = self.queue_pending("source-concurrent-outcome", self.root)
        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-candidate",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "candidate",
            "--knowledge-scope",
            "repository",
            "--scope",
            "concurrency",
            "--lesson",
            "Serialize pending outcomes by opaque identifier.",
            "--evidence",
            "Concurrent maintenance must commit one terminal result.",
            "--target",
            "test",
            "--confidence",
            "0.9",
        )
        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-empty",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "no-candidate",
        )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda session: gardener.consume_deferred_pending_outcomes(
                        self.other, session, [pending["pending_id"]]
                    ),
                    ("maintenance-candidate", "maintenance-empty"),
                )
            )

        self.assertEqual(sum(processed for processed, _ in results), 1)
        self.assertEqual(gardener.pending_records(), [])
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(
            report["reviews"]["captures_recorded"]
            + report["reviews"]["completed_without_candidate"],
            1,
        )
        self.assertLessEqual(
            len(gardener.read_learning_records(self.root, "repository", "inbox.jsonl")),
            1,
        )

    def test_maintenance_no_candidate_resolves_pending_and_logs_terminal_once(self) -> None:
        pending = self.queue_pending("source-empty", self.root)
        maintenance, _ = self.start_maintenance(session_id="maintenance-empty-outcome")
        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-empty-outcome",
            "--pending-id",
            pending["pending_id"],
            "--outcome",
            "no-candidate",
        )

        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertEqual(gardener.pending_records(), [])
        self.assertEqual(gardener.effectiveness_report(14, self.root)["reviews"]["completed_without_candidate"], 1)
        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertEqual(gardener.effectiveness_report(14, self.root)["reviews"]["completed_without_candidate"], 1)

    def test_invalid_or_mismatched_maintenance_marker_fails_open_and_stays_pending(self) -> None:
        pending = self.queue_pending("source-invalid", self.root)
        maintenance, _ = self.start_maintenance(session_id="maintenance-invalid")
        deferred_dir = (
            self.other
            / ".codex"
            / "learning"
            / "deferred-maintenance"
            / gardener.safe_name("maintenance-invalid")
        )
        deferred_dir.mkdir(parents=True, exist_ok=True)
        gardener.atomic_write_json(
            deferred_dir / "invalid.json",
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "record_type": "deferred_pending_outcome",
                "maintenance_session_id": "maintenance-invalid",
                "pending_id": "f" * 32,
                "outcome": "no-candidate",
                "created_at": gardener.utc_now(),
            },
        )
        gardener.atomic_write_json(
            deferred_dir / f"{pending['pending_id']}.json",
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "record_type": "deferred_pending_outcome",
                "maintenance_session_id": "maintenance-invalid",
                "pending_id": pending["pending_id"],
                "outcome": "candidate",
                "created_at": gardener.utc_now(),
                "fingerprint": gardener.candidate_fingerprint(
                    "privacy", "Keep maintenance handoffs path-free.", "docs"
                ),
                "knowledge_scope": "repository",
                "scope": "privacy",
                "lesson": "Keep maintenance handoffs path-free.",
                "evidence_summary": f"Copied from {self.root}",
                "recommended_target": "docs",
                "confidence": 0.9,
            },
        )

        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertEqual([item["pending_id"] for item in gardener.pending_records()], [pending["pending_id"]])
        self.assertFalse(any(deferred_dir.glob("*.json")))

    def test_defer_pending_outcome_rejects_source_identifiers_before_writing(self) -> None:
        pending = self.queue_pending("source-private-session", self.root)
        self.start_maintenance(session_id="maintenance-private")
        for evidence in (
            f"Copied from {self.root}",
            "Copied from C:\\private\\source-repo",
            "Observed in source-private-session",
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "defer-pending-outcome",
                    "--repo",
                    str(self.other),
                    "--session-id",
                    "maintenance-private",
                    "--pending-id",
                    pending["pending_id"],
                    "--outcome",
                    "candidate",
                    "--knowledge-scope",
                    "repository",
                    "--scope",
                    "privacy",
                    "--lesson",
                    "Keep maintenance handoffs private.",
                    "--evidence",
                    evidence,
                    "--target",
                    "docs",
                    "--confidence",
                    "0.9",
                ],
                cwd=self.root,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("must not contain source identifiers or paths", result.stderr)
        self.assertFalse(any(gardener.deferred_maintenance_dir(self.other, "maintenance-private").glob("*.json")))

    def test_legacy_pending_record_gets_a_stable_id_and_can_be_completed_by_maintenance(self) -> None:
        unresolved_root = self.root / ".." / self.root.name
        legacy = {
            "schema_version": gardener.SCHEMA_VERSION,
            "record_type": "pending",
            "session_id": "legacy-source",
            "cwd": str(unresolved_root),
            "repo_root": str(unresolved_root),
            "transcript_path": "C:/legacy/rollout.jsonl",
            "signals": ["user correction"],
            "created_at": "2026-08-01T00:00:00Z",
        }
        gardener.append_jsonl(gardener.pending_path(), legacy)
        first = gardener.pending_records()[0]
        self.assertRegex(first["pending_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(gardener.pending_records()[0]["pending_id"], first["pending_id"])
        legacy_state = gardener.new_state(
            self.payload(session_id="legacy-source", cwd=str(self.root))
        )
        legacy_state["correction_signal"] = True
        queued, created = gardener.queue_pending_review(
            legacy_state,
            self.payload(session_id="legacy-source", cwd=str(self.root)),
        )
        self.assertFalse(created)
        self.assertEqual(queued["pending_id"], first["pending_id"])

        maintenance, requested = self.start_maintenance(session_id="maintenance-legacy")
        self.assertEqual(requested["decision"], "block")
        self.run_cli(
            "defer-pending-outcome",
            "--repo",
            str(self.other),
            "--session-id",
            "maintenance-legacy",
            "--pending-id",
            first["pending_id"],
            "--outcome",
            "no-candidate",
        )
        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )
        self.assertEqual(gardener.pending_records(), [])

    def test_pending_queue_is_concurrent_idempotent_and_corrupt_lines_fail_open(self) -> None:
        gardener.pending_path().parent.mkdir(parents=True, exist_ok=True)
        gardener.pending_path().write_text("not-json\n", encoding="utf-8")
        state = gardener.new_state(self.payload())
        state["edit_signal"] = True

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: gardener.queue_pending_review(state, self.payload()), range(16)))

        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(len(gardener.pending_records()), 1)
        lines = gardener.pending_path().read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "not-json")
        self.assertEqual(len(lines), 2)

    def test_deferred_audit_completion_checkpoints_once_and_resets_schedule(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        for index in range(10):
            self.log_completed_review(f"before-audit-{index}")
        due_before = self.run_cli("audit-status")
        self.assertTrue(due_before["due"])
        self.assertEqual(due_before["qualifying_reviews"], 10)
        self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
        requested = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertIn("$codex-gardener:knowledge-curator", requested["reason"])

        deferred = self.run_cli(
            "defer-audit-complete",
            "--repo",
            str(self.root),
            "--session-id",
            "session-1",
        )
        self.assertTrue(deferred["deferred"])
        self.assertIsNone(self.run_cli("audit-status")["checkpoint"]["last_successful_audit_at"])

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        after = self.run_cli("audit-status")
        self.assertFalse(after["due"])
        self.assertEqual(after["qualifying_reviews"], 0)
        self.assertIsNotNone(after["checkpoint"]["last_successful_audit_at"])
        self.assertNotEqual(after["checkpoint"]["last_successful_audit_session"], "session-1")
        checkpoint_text = gardener.audit_checkpoint_path().read_text(encoding="utf-8")
        self.assertNotIn("session-1", checkpoint_text)
        self.assertNotIn(str(self.root), checkpoint_text)
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["completed"], 1)

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["completed"], 1)

    def test_capture_has_priority_over_forced_audit(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="纠正：先复盘任务。[codex-gardener:scheduled-audit]"),
        )
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})
        self.assertEqual(len(gardener.pending_records()), 1)
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 0)

    def test_forced_audit_completion_is_checkpointed_once(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
        requested = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertIn("Audit reason: forced", requested["reason"])
        self.run_cli(
            "defer-audit-complete",
            "--repo",
            str(self.root),
            "--session-id",
            "session-1",
        )
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["audits"]["requested"], 1)
        self.assertEqual(report["audits"]["completed"], 1)
        self.assertEqual(report["audits"]["reasons"], {"forced": 2})

    def test_completed_smoke_audit_does_not_queue_its_own_tool_signals(self) -> None:
        with patch.dict(os.environ, {gardener.effectiveness.RUN_KIND_ENV: "smoke"}):
            self.run_hook("SessionStart", self.payload(source="startup"))
            self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
            requested = self.run_hook("Stop", self.payload(stop_hook_active=False))
            self.assertIn("$codex-gardener:knowledge-curator", requested["reason"])

            for tool_use_id in ("audit-tool-1", "audit-tool-2"):
                self.run_hook(
                    "PostToolUse",
                    self.payload(
                        tool_name="Bash",
                        tool_use_id=tool_use_id,
                        tool_input={"command": "python gardener.py audit-status --json"},
                        tool_response={"exit_code": 1},
                    ),
                )
            self.run_cli(
                "defer-audit-complete",
                "--repo",
                str(self.root),
                "--session-id",
                "session-1",
            )
            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
            self.run_hook("SessionEnd", self.payload(reason="other"))

        status = self.run_cli("audit-status")
        self.assertIsNone(status["checkpoint"]["last_successful_audit_at"])
        self.assertIsNone(status["checkpoint"]["last_successful_audit_session"])
        self.assertIsNone(status["checkpoint"]["last_successful_audit_completion"])
        self.assertEqual(gardener.pending_records(), [])
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["audits"]["requested"], 1)
        self.assertEqual(report["audits"]["completed"], 1)
        self.assertEqual(report["reviews"]["pending_queued"], 0)

    def test_completed_real_audit_persists_checkpoint_once_without_pending(self) -> None:
        with patch.dict(os.environ, {gardener.effectiveness.RUN_KIND_ENV: "real"}):
            self.run_hook("SessionStart", self.payload(source="startup"))
            self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
            requested = self.run_hook("Stop", self.payload(stop_hook_active=False))
            self.assertIn("$codex-gardener:knowledge-curator", requested["reason"])

            for tool_use_id in ("audit-tool-1", "audit-tool-2"):
                self.run_hook(
                    "PostToolUse",
                    self.payload(
                        tool_name="Bash",
                        tool_use_id=tool_use_id,
                        tool_input={"command": "python gardener.py audit-status --json"},
                        tool_response={"exit_code": 1},
                    ),
                )
            self.run_cli(
                "defer-audit-complete",
                "--repo",
                str(self.root),
                "--session-id",
                "session-1",
            )
            before = self.run_cli("audit-status")["checkpoint"]
            self.assertIsNone(before["last_successful_audit_at"])

            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
            checkpoint_text = gardener.audit_checkpoint_path().read_text(encoding="utf-8")
            after = self.run_cli("audit-status")["checkpoint"]
            self.assertIsNotNone(after["last_successful_audit_at"])
            self.assertIsNotNone(after["last_successful_audit_session"])
            self.assertIsNotNone(after["last_successful_audit_completion"])

            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
            self.assertEqual(
                gardener.audit_checkpoint_path().read_text(encoding="utf-8"),
                checkpoint_text,
            )
            self.run_hook("SessionEnd", self.payload(reason="other"))

        self.assertEqual(gardener.pending_records(), [])
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["audits"]["requested"], 1)
        self.assertEqual(report["audits"]["completed"], 1)
        self.assertEqual(report["reviews"]["pending_queued"], 0)

    def test_completed_audit_does_not_hide_an_unfinished_capture(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_cli(
            "defer-audit-complete",
            "--repo",
            str(self.root),
            "--session-id",
            "session-1",
        )
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="纠正：先完成 capture。[codex-gardener:scheduled-audit]"),
        )
        requested = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(requested, {"continue": True})
        self.run_hook("SessionEnd", self.payload(reason="other"))

        pending = gardener.pending_records()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["session_id"], "session-1")
        self.assertIn("user_correction", pending[0]["signals"])

    def test_audit_request_is_one_shot_and_missing_marker_retries_next_session(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.log_completed_review("threshold-review")
        with patch.dict(os.environ, {gardener.AUDIT_THRESHOLD_ENV: "1"}):
            self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
            first = self.run_hook("Stop", self.payload(stop_hook_active=False))
            self.assertIn("$codex-gardener:knowledge-curator", first["reason"])
            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})
            self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
            self.run_hook("SessionEnd", self.payload(reason="other"))

            next_payload = self.payload(session_id="session-2", turn_id="turn-2")
            self.run_hook("SessionStart", next_payload)
            self.run_hook("UserPromptSubmit", next_payload | {"prompt": "[codex-gardener:scheduled-audit]"})
            retried = self.run_hook("Stop", next_payload | {"stop_hook_active": False})
        self.assertIn("$codex-gardener:knowledge-curator", retried["reason"])
        audits = gardener.effectiveness_report(14, self.root)["audits"]
        self.assertEqual(audits["requested"], 2)
        self.assertEqual(audits["completed"], 0)

    def test_mismatched_audit_marker_fails_open_without_checkpoint(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False))["decision"], "block")
        directory = gardener.deferred_audit_dir(self.root, "session-1")
        directory.mkdir(parents=True)
        marker = directory / "invalid.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": gardener.SCHEMA_VERSION,
                    "record_type": "deferred_audit_complete",
                    "session_id": "different-session",
                    "run_kind": "real",
                    "created_at": gardener.utc_now(),
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertTrue(marker.exists())
        status = self.run_cli("audit-status")
        self.assertIsNone(status["checkpoint"]["last_successful_audit_at"])
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["audits"]["completed"], 0)
        self.assertEqual(report["errors"]["count"], 1)

    def test_scheduled_initial_turn_marker_is_consumed_before_duplicate_request(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_cli(
            "defer-audit-complete",
            "--repo",
            str(self.root),
            "--session-id",
            "session-1",
        )
        self.run_hook("UserPromptSubmit", self.payload(prompt="[codex-gardener:scheduled-audit]"))
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["audits"]["requested"], 0)
        self.assertEqual(report["audits"]["completed"], 1)

    def test_effectiveness_health_exposes_audit_checkpoint_metadata(self) -> None:
        report = self.run_cli("effectiveness", "--repo", str(self.root), "--json")
        audit = report["health"]["audit"]
        self.assertTrue(audit["available"])
        self.assertEqual(audit["review_threshold"], 10)
        self.assertEqual(audit["max_days"], 7)
        self.assertEqual(audit["qualifying_reviews"], 0)
        self.assertFalse(audit["due"])
        self.assertEqual(audit["reason"], "not_due")
        self.assertIsNotNone(audit["deadline_at"])
        self.assertIsNotNone(audit["checkpoint"]["initialized_at"])

    def test_defer_audit_complete_cli_writes_only_inside_repository(self) -> None:
        env = os.environ.copy()
        env.pop("CODEX_GARDENER_DATA", None)
        env.pop("PLUGIN_DATA", None)
        isolated_home = Path(self.temp.name) / "isolated-codex-home"
        env.update({"CODEX_HOME": str(isolated_home), "PYTHONDONTWRITEBYTECODE": "1"})
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "defer-audit-complete",
                "--repo",
                str(self.root),
                "--session-id",
                "sandbox-audit",
            ],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["deferred"])
        self.assertEqual(len(list(gardener.deferred_audit_dir(self.root, "sandbox-audit").glob("*.json"))), 1)
        self.assertIn("deferred-audits/", (self.root / ".codex" / "learning" / ".gitignore").read_text())
        self.assertFalse((isolated_home / "codex-gardener-data").exists())

    def test_corrupt_checkpoint_and_concurrent_initialization_fail_open(self) -> None:
        def initialize(_: int) -> dict:
            return self.run_cli("audit-status")

        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(initialize, range(12)))
        self.assertTrue(all(status["available"] for status in statuses), statuses)
        checkpoint = gardener.audit_checkpoint_path()
        checkpoint.write_text("not-json", encoding="utf-8")
        corrupt = self.run_cli("audit-status")
        self.assertFalse(corrupt["available"])
        self.assertFalse(corrupt["due"])
        self.assertEqual(corrupt["reason"], "checkpoint_unavailable")
        self.assertEqual(self.run_hook("SessionStart", self.payload(source="startup")), {})
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})

    def test_clean_git_repository_is_not_a_signal(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="Inspect the repository"))
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})

    def test_ordinary_stop_queues_review_without_a_continuation(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")

        first = self.run_hook("Stop", self.payload(stop_hook_active=False))
        second = self.run_hook("Stop", self.payload(stop_hook_active=False))

        self.assertEqual(first, {"continue": True})
        self.assertEqual(second, {"continue": True})
        pending = gardener.pending_records()
        self.assertEqual(len(pending), 1)
        self.assertRegex(pending[0]["pending_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(pending[0]["session_id"], "session-1")
        self.assertEqual(pending[0]["signals"], ["workspace_changed"])

    def test_session_end_does_not_duplicate_an_immediately_queued_review(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "PostToolUse",
            self.payload(
                tool_name="apply_patch",
                tool_input={"patch": "private tool payload"},
                tool_response={"status": "ok"},
            ),
        )
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})

        self.run_hook(
            "SessionEnd",
            self.payload(reason="other", transcript_path="C:/private/rollout.jsonl"),
        )

        pending = gardener.pending_records()
        self.assertEqual(len(pending), 1)
        rendered = json.dumps(pending)
        self.assertNotIn("private tool payload", rendered)
        self.assertEqual(gardener.effectiveness_report(14, self.root)["reviews"]["pending_queued"], 1)

    def test_pending_lock_failure_does_not_fail_stop_or_session_end(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "PostToolUse",
            self.payload(tool_name="apply_patch", tool_input={}, tool_response={"status": "ok"}),
        )
        with patch.object(gardener, "queue_pending_review", side_effect=TimeoutError("busy")):
            self.assertEqual(
                self.run_hook("Stop", self.payload(stop_hook_active=False)),
                {"continue": True},
            )
            self.assertEqual(self.run_hook("SessionEnd", self.payload(reason="other")), {})
        self.assertFalse(gardener.state_path("session-1").exists())

    def test_due_audit_never_interrupts_an_ordinary_task(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"ordinary-due-{index}")
        self.run_hook("SessionStart", self.payload(source="startup"))

        result = self.run_hook("Stop", self.payload(stop_hook_active=False))

        self.assertEqual(result, {"continue": True})
        self.assertTrue(self.run_cli("audit-status")["due"])
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 0)

    def test_conditional_audit_marker_stays_silent_when_not_due(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="Check only when due. [codex-gardener:scheduled-audit-check]"),
        )

        result = self.run_hook("Stop", self.payload(stop_hook_active=False))

        self.assertEqual(result, {"continue": True})
        state = gardener.load_json_file(gardener.state_path("session-1"), {})
        self.assertTrue(state["check_audit"])
        self.assertNotIn("scheduled-audit-check", json.dumps(state))
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 0)

    def test_conditional_audit_marker_requests_due_audit_exactly_once(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"conditional-due-{index}")
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="[codex-gardener:scheduled-audit-check]"),
        )

        first = self.run_hook("Stop", self.payload(stop_hook_active=False))
        second = self.run_hook("Stop", self.payload(stop_hook_active=False))
        active = self.run_hook("Stop", self.payload(stop_hook_active=True))

        self.assertEqual(first["decision"], "block")
        self.assertIn("Audit reason: review_threshold", first["reason"])
        self.assertEqual(second, {"continue": True})
        self.assertEqual(active, {"continue": True})
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 1)

    def test_conditional_audit_marker_near_match_does_not_request_due_audit(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"conditional-near-{index}")
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "UserPromptSubmit",
            self.payload(prompt="[codex-gardener:scheduled-audit-checks]"),
        )

        result = self.run_hook("Stop", self.payload(stop_hook_active=False))

        self.assertEqual(result, {"continue": True})
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 0)

    def test_combined_maintenance_and_audit_check_prioritizes_pending_work(self) -> None:
        pending = self.queue_pending("source-session")
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"maintenance-priority-{index}")
        maintenance = self.payload(
            session_id="maintenance-session",
            turn_id="maintenance-turn",
            cwd=str(self.other),
            prompt=(
                "[codex-gardener:scheduled-maintenance] "
                "[codex-gardener:scheduled-audit-check]"
            ),
        )
        self.run_hook("SessionStart", maintenance)
        self.run_hook("UserPromptSubmit", maintenance)

        requested = self.run_hook("Stop", maintenance | {"stop_hook_active": False})

        self.assertEqual(requested["decision"], "block")
        self.assertIn("maintenance-only mode", requested["reason"])
        self.assertIn(pending["pending_id"], requested["reason"])
        self.assertNotIn("audit-only", requested["reason"])
        self.assertEqual(gardener.effectiveness_report(14, self.other)["audits"]["requested"], 0)
        self.assertEqual(
            self.run_hook("Stop", maintenance | {"stop_hook_active": True}),
            {"continue": True},
        )

    def test_combined_maintenance_and_audit_check_audits_when_due_and_queue_empty(self) -> None:
        self.run_cli("audit-status", "--initialize")
        for index in range(10):
            self.log_completed_review(f"empty-maintenance-{index}")
        payload = self.payload(
            prompt=(
                "[codex-gardener:scheduled-maintenance] "
                "[codex-gardener:scheduled-audit-check]"
            )
        )
        self.run_hook("SessionStart", payload)
        self.run_hook("UserPromptSubmit", payload)

        requested = self.run_hook("Stop", payload | {"stop_hook_active": False})

        self.assertEqual(requested["decision"], "block")
        self.assertIn("audit-only", requested["reason"])
        self.assertIn("Audit reason: review_threshold", requested["reason"])
        self.assertEqual(gardener.effectiveness_report(14, self.root)["audits"]["requested"], 1)

    def test_edit_triggers_once_and_active_stop_does_not_loop(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")
        first = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(first, {"continue": True})
        second = self.run_hook("Stop", self.payload(stop_hook_active=True))
        self.assertEqual(second, {"continue": True})
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["completed_without_candidate"], 0)
        self.assertEqual(report["reviews"]["current_pending"], 1)

    def test_correction_and_repeated_failures_trigger(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="不对，我说的是另一个入口"))
        for index in range(2):
            self.run_hook(
                "PostToolUse",
                self.payload(
                    tool_name="Bash",
                    tool_use_id=f"tool-{index}",
                    tool_input={"command": "pytest tests/test_x.py"},
                    tool_response={"exit_code": 1},
                ),
            )
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})
        self.assertEqual(
            gardener.pending_records()[0]["signals"],
            ["user_correction", "repeated_failures", "repeated_tool_workflow"],
        )

    def test_non_git_and_corrupt_state_degrade_safely(self) -> None:
        path = gardener.state_path("session-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})

    def test_session_end_is_fast_and_does_not_copy_transcript(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook(
            "PostToolUse",
            self.payload(
                tool_name="apply_patch",
                tool_input={"command": "secret transcript body"},
                tool_response={"status": "ok"},
            ),
        )
        started = time.monotonic()
        self.run_hook("SessionEnd", self.payload(reason="other", transcript_path="C:/private/rollout.jsonl"))
        self.assertLess(time.monotonic() - started, 3)
        pending = gardener.pending_records()
        self.assertEqual(len(pending), 1)
        rendered = json.dumps(pending)
        self.assertNotIn("secret transcript body", rendered)
        self.assertIn("C:/private/rollout.jsonl", rendered)
        gardener.resolve_pending("session-1")
        self.assertEqual(gardener.pending_records(), [])

    def test_new_user_turn_resets_review_state(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        first_prompt = self.payload(turn_id="turn-1", prompt="不对，我说的是项目入口")
        self.run_hook("UserPromptSubmit", first_prompt)
        first = self.run_hook("Stop", self.payload(turn_id="turn-1", stop_hook_active=False))
        self.assertEqual(first, {"continue": True})
        second_prompt = self.payload(turn_id="turn-2", prompt="wrong, use the documented command")
        self.run_hook("UserPromptSubmit", second_prompt)
        second = self.run_hook("Stop", self.payload(turn_id="turn-2", stop_hook_active=False))
        self.assertEqual(second, {"continue": True})
        self.assertEqual(len(gardener.pending_records()), 1)

    def test_candidates_dedupe_sessions_and_promote_at_three(self) -> None:
        for session in ("s1", "s1", "s2", "s3"):
            args = self.candidate_args(
                session_id=session,
                evidence=f"Observed in {session}",
            )
            gardener.record_candidate(args)
        groups = gardener.aggregate_candidates(self.root)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["occurrences"], 3)
        self.assertEqual(groups[0]["status"], "promotable")
        self.assertEqual(groups[0]["evidence_status"], "promotable")
        ignore = (self.root / ".codex" / "learning" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("inbox.jsonl", ignore)
        self.assertIn("index.jsonl", ignore)

    def test_active_stop_commits_deferred_repository_candidate_once(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="纠正：使用项目约定的入口"))
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})

        with patch.object(gardener, "state_path", side_effect=AssertionError("defer must stay in workspace")):
            deferred = gardener.defer_candidate(self.candidate_args())
        self.assertTrue(deferred["deferred"])
        marker_directory = gardener.deferred_capture_dir(self.root, "session-1")
        self.assertEqual(len(list(marker_directory.glob("*.json"))), 1)
        self.assertIn("deferred-captures/", (self.root / ".codex" / "learning" / ".gitignore").read_text())
        self.assertFalse((self.root / ".codex" / "learning" / "inbox.jsonl").exists())

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertFalse(marker_directory.exists())
        records = gardener.read_jsonl(self.root / ".codex" / "learning" / "inbox.jsonl")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["knowledge_scope"], "repository")
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["captures_recorded"], 1)
        self.assertEqual(report["reviews"]["completed_without_candidate"], 0)

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertEqual(len(gardener.read_jsonl(self.root / ".codex" / "learning" / "inbox.jsonl")), 1)
        self.assertEqual(gardener.effectiveness_report(14, self.root)["reviews"]["captures_recorded"], 1)

    def test_normal_stop_consumes_manual_or_legacy_deferred_capture_without_blocking(self) -> None:
        self.run_hook("SessionStart", self.payload(source="startup"))
        gardener.defer_candidate(self.candidate_args())

        result = self.run_hook("Stop", self.payload(stop_hook_active=False))

        self.assertEqual(result, {"continue": True})
        records = gardener.read_learning_records(self.root, "repository", "inbox.jsonl")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["session_id"], "session-1")
        self.assertEqual(gardener.pending_records(), [])

    def test_active_stop_commits_deferred_global_candidate_outside_model_process(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="纠正：这是跨项目原则"))
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})
        args = self.candidate_args(
            knowledge_scope="global",
            lesson="Keep portable capture handoffs independent of repository conventions.",
            target="skill",
        )
        with patch.object(gardener, "state_path", side_effect=AssertionError("defer must stay in workspace")), patch.object(
            gardener, "learning_dir", wraps=gardener.learning_dir
        ) as learning_dir:
            gardener.defer_candidate(args)
        self.assertTrue(all(call.args[1] != "global" for call in learning_dir.call_args_list if len(call.args) > 1))
        self.assertFalse((self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl").exists())

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        records = gardener.read_jsonl(self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["knowledge_scope"], "global")
        self.assertEqual(gardener.effectiveness_report(14, self.root)["reviews"]["captures_recorded"], 1)

    def test_defer_record_cli_needs_only_repository_write_access(self) -> None:
        env = os.environ.copy()
        env.pop("CODEX_GARDENER_DATA", None)
        env.pop("PLUGIN_DATA", None)
        env.update(
            {
                "CODEX_HOME": str(self.codex_home),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "defer-record",
                "--repo",
                str(self.root),
                "--session-id",
                "sandbox-session",
                "--knowledge-scope",
                "global",
                "--scope",
                "capture",
                "--lesson",
                "Defer formal candidate writes to the active Stop Hook.",
                "--evidence",
                "The model process has repository-only write access.",
                "--target",
                "hook",
                "--confidence",
                "0.9",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
            cwd=self.root,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["deferred"])
        self.assertEqual(len(list(gardener.deferred_capture_dir(self.root, "sandbox-session").glob("*.json"))), 1)
        self.assertFalse((self.codex_home / "codex-gardener-global-learning").exists())
        self.assertFalse((self.codex_home / "codex-gardener-data").exists())

    def test_active_stop_ignores_mismatched_deferred_marker_fail_open(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="纠正：检查错误 marker"))
        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=False)), {"continue": True})
        directory = gardener.deferred_capture_dir(self.root, "session-1")
        directory.mkdir(parents=True)
        marker = directory / "invalid.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": gardener.SCHEMA_VERSION,
                    "record_type": "deferred_capture",
                    "session_id": "different-session",
                    "knowledge_scope": "repository",
                    "scope": "tests",
                    "lesson": "This marker must not be accepted.",
                    "evidence_summary": "Mismatched session.",
                    "recommended_target": "test",
                    "confidence": 0.9,
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(self.run_hook("Stop", self.payload(stop_hook_active=True)), {"continue": True})
        self.assertTrue(marker.exists())
        state = gardener.load_json_file(gardener.state_path("session-1"), {})
        self.assertFalse(state["capture_completed"])
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["captures_recorded"], 0)
        self.assertEqual(report["reviews"]["completed_without_candidate"], 0)
        self.assertEqual(report["errors"]["count"], 1)

    def test_concurrent_candidate_appends_remain_valid_jsonl(self) -> None:
        def write(index: int) -> None:
            gardener.record_candidate(
                self.candidate_args(
                    session_id=f"parallel-{index}",
                    scope="parallel",
                    lesson=f"Independent lesson {index}",
                    evidence=f"Evidence {index}",
                    target="docs",
                    confidence=0.8,
                )
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        records = gardener.read_jsonl(self.root / ".codex" / "learning" / "inbox.jsonl")
        self.assertEqual(len(records), 24)
        self.assertEqual(len({record["id"] for record in records}), 24)

    def test_resolution_builds_retrieval_index(self) -> None:
        args = self.resolution_args()
        gardener.resolve_candidate(args)
        context = gardener.promoted_context(self.root, "Please update the parser contract")
        self.assertIsNotNone(context)
        self.assertIn("AGENTS.md", context)

    def test_groups_exposes_evidence_maturity_and_latest_resolution_status(self) -> None:
        candidate = gardener.record_candidate(self.candidate_args())

        for status in ("proposed", "promoted", "discarded"):
            gardener.resolve_candidate(
                self.resolution_args(
                    fingerprint=candidate["fingerprint"],
                    status=status,
                )
            )
            groups = self.run_cli("groups", "--repo", str(self.root))["groups"]
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["status"], status)
            self.assertEqual(groups[0]["evidence_status"], "candidate")
            self.assertEqual(groups[0]["resolution"]["status"], status)

    def test_groups_ignores_later_resolution_with_invalid_status(self) -> None:
        candidate = gardener.record_candidate(self.candidate_args())
        valid = gardener.resolve_candidate(
            self.resolution_args(
                fingerprint=candidate["fingerprint"],
                status="promoted",
            )
        )
        gardener.append_jsonl(
            gardener.ensure_learning_dir(self.root) / "resolutions.jsonl",
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": candidate["fingerprint"],
                "knowledge_scope": "repository",
                "status": "invalid-status",
                "created_at": gardener.utc_now(),
            },
        )

        group = gardener.aggregate_candidates(self.root)[0]

        self.assertEqual(group["status"], "promoted")
        self.assertEqual(group["evidence_status"], "candidate")
        self.assertEqual(group["resolution"], valid)

    def test_legacy_candidate_defaults_to_repository_scope(self) -> None:
        legacy = {
            "schema_version": 1,
            "id": "legacy",
            "fingerprint": "legacy-fingerprint",
            "session_id": "legacy-session",
            "scope": "parser",
            "lesson": "Legacy lessons remain local.",
            "evidence_summary": "Stored before scope tiers existed.",
            "recommended_target": "docs",
            "confidence": 0.8,
            "created_at": "2026-01-01T00:00:00Z",
        }
        gardener.append_jsonl(gardener.ensure_learning_dir(self.root) / "inbox.jsonl", legacy)
        groups = gardener.aggregate_candidates(self.root)
        self.assertEqual(groups[0]["knowledge_scope"], "repository")
        self.assertEqual(groups[0]["fingerprint"], "legacy-fingerprint")
        self.assertEqual(gardener.aggregate_candidates(self.root, "global"), [])

        gardener.append_jsonl(
            gardener.ensure_learning_dir(self.root) / "index.jsonl",
            {
                "schema_version": 1,
                "fingerprint": "legacy-index",
                "summary": "Legacy promoted context stays local.",
                "keywords": ["legacy context"],
                "target_path": "AGENTS.md",
                "promoted_at": "2026-01-01T00:00:00Z",
            },
        )
        self.assertIn("[repository]", gardener.promoted_context(self.root, "legacy context") or "")
        self.assertIsNone(gardener.promoted_context(self.other, "legacy context"))

    def test_user_level_v1_learning_migrates_to_global_store_idempotently(self) -> None:
        stable_target = "plugins/codex-gardener/skills/cross-project-delegation/SKILL.md"
        legacy = self.codex_home / "learning"
        legacy.mkdir(parents=True)
        candidate = {
            "schema_version": 1,
            "id": "legacy-global",
            "fingerprint": "legacy-global-fingerprint",
            "session_id": "legacy-global-session",
            "scope": "coordination",
            "lesson": "Delegate cross-project writes to the owning project.",
            "evidence_summary": "Confirmed before knowledge scopes existed.",
            "recommended_target": "skill",
            "confidence": 0.9,
            "created_at": "2026-08-12T00:00:00Z",
        }
        index = {
            "schema_version": 1,
            "fingerprint": "legacy-global-fingerprint",
            "summary": "Delegate cross-project writes to the owning project.",
            "keywords": ["cross-project"],
            "target_path": "~/.codex/skills/cross-project-delegation/SKILL.md",
            "promoted_at": "2026-08-12T00:00:01Z",
        }
        resolution = {
            "schema_version": 1,
            "fingerprint": "legacy-global-fingerprint",
            "status": "promoted",
            "summary": index["summary"],
            "keywords": index["keywords"],
            "target_path": index["target_path"],
            "created_at": "2026-08-12T00:00:01Z",
        }
        gardener.append_jsonl(legacy / "inbox.jsonl", candidate)
        gardener.append_jsonl(legacy / "index.jsonl", index)
        gardener.append_jsonl(legacy / "resolutions.jsonl", resolution)
        legacy_index_before = (legacy / "index.jsonl").read_bytes()
        legacy_resolution_before = (legacy / "resolutions.jsonl").read_bytes()

        first = gardener.aggregate_candidates(self.root, "global")
        self.assertFalse((self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl").exists())
        self.run_hook("SessionStart", self.payload(source="startup"))
        second = gardener.aggregate_candidates(self.other, "global")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["occurrences"], 1)
        self.assertEqual(first[0]["knowledge_scope"], "global")
        self.assertEqual(first[0]["status"], "promoted")
        self.assertEqual(first[0]["evidence_status"], "candidate")
        self.assertEqual(first[0]["resolution"]["target_path"], stable_target)
        self.assertIn("legacy-user-learning-v1", json.dumps(first))
        self.assertIn("[global]", gardener.promoted_context(self.other, "cross-project") or "")

        migrated = gardener.read_jsonl(self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl")
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["knowledge_scope"], "global")
        self.assertEqual(migrated[0]["migration_provenance"], "legacy-user-learning-v1")
        self.assertEqual(gardener.read_jsonl(legacy / "inbox.jsonl"), [candidate])
        migrated_store = self.codex_home / "codex-gardener-global-learning"
        self.assertEqual(gardener.read_jsonl(migrated_store / "index.jsonl")[0]["target_path"], stable_target)
        self.assertEqual(gardener.read_jsonl(migrated_store / "resolutions.jsonl")[0]["target_path"], stable_target)
        self.assertEqual((legacy / "index.jsonl").read_bytes(), legacy_index_before)
        self.assertEqual((legacy / "resolutions.jsonl").read_bytes(), legacy_resolution_before)

    def test_session_start_migrates_only_exact_global_delegation_targets(self) -> None:
        stable_target = "plugins/codex-gardener/skills/cross-project-delegation/SKILL.md"
        store = self.codex_home / "codex-gardener-global-learning"
        store.mkdir(parents=True)
        standalone = self.codex_home / "skills" / "cross-project-delegation" / "SKILL.md"
        standalone.parent.mkdir(parents=True)
        standalone.write_text("standalone sentinel\n", encoding="utf-8")
        candidate = {
            "schema_version": gardener.SCHEMA_VERSION,
            "id": "candidate-stale-target",
            "fingerprint": "stale-target",
            "session_id": "legacy-session",
            "knowledge_scope": "global",
            "scope": "coordination",
            "lesson": "Use the bundled delegation workflow.",
            "evidence_summary": "Observed before the Skill was bundled.",
            "recommended_target": "skill",
            "confidence": 0.9,
            "created_at": "2026-08-12T00:00:00Z",
        }
        gardener.append_jsonl(store / "inbox.jsonl", candidate)
        resolutions = [
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": "stale-target",
                "knowledge_scope": "global",
                "status": "promoted",
                "summary": "Use the bundled delegation workflow.",
                "keywords": ["delegation"],
                "target_path": r"~\.codex\skills\cross-project-delegation\SKILL.md",
                "created_at": "2026-08-12T00:00:01Z",
                "sentinel": "preserve-resolution",
            },
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": "unrelated-target",
                "knowledge_scope": "global",
                "status": "promoted",
                "summary": "Leave unrelated Skills alone.",
                "keywords": ["unrelated"],
                "target_path": "~/.codex/skills/unrelated/SKILL.md",
                "created_at": "2026-08-12T00:00:02Z",
                "sentinel": "preserve-unrelated",
            },
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": "already-stable",
                "knowledge_scope": "global",
                "status": "promoted",
                "summary": "Already migrated.",
                "keywords": ["stable"],
                "target_path": stable_target,
                "created_at": "2026-08-12T00:00:03Z",
                "sentinel": "preserve-stable",
            },
        ]
        indexes = [
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": "stale-target",
                "knowledge_scope": "global",
                "summary": "Use the bundled delegation workflow.",
                "keywords": ["delegation"],
                "target_path": ".codex/skills/cross-project-delegation/SKILL.md",
                "promoted_at": "2026-08-12T00:00:01Z",
                "sentinel": "preserve-index",
            },
            {
                "schema_version": gardener.SCHEMA_VERSION,
                "fingerprint": "unrelated-target",
                "knowledge_scope": "global",
                "summary": "Leave unrelated Skills alone.",
                "keywords": ["unrelated"],
                "target_path": ".codex/skills/unrelated/SKILL.md",
                "promoted_at": "2026-08-12T00:00:02Z",
                "sentinel": "preserve-unrelated-index",
            },
        ]
        for record in resolutions:
            gardener.append_jsonl(store / "resolutions.jsonl", record)
        for record in indexes:
            gardener.append_jsonl(store / "index.jsonl", record)
        resolution_before = (store / "resolutions.jsonl").read_bytes()
        index_before = (store / "index.jsonl").read_bytes()

        groups = self.run_cli(
            "groups",
            "--repo",
            str(self.root),
            "--knowledge-scope",
            "global",
        )["groups"]
        self.run_cli("effectiveness", "--repo", str(self.root), "--json")
        self.assertEqual(groups[0]["resolution"]["target_path"], stable_target)
        self.assertEqual((store / "resolutions.jsonl").read_bytes(), resolution_before)
        self.assertEqual((store / "index.jsonl").read_bytes(), index_before)

        self.run_hook("SessionStart", self.payload(source="startup"))
        migrated_resolutions = gardener.read_jsonl(store / "resolutions.jsonl")
        migrated_indexes = gardener.read_jsonl(store / "index.jsonl")
        self.assertEqual(
            [record["fingerprint"] for record in migrated_resolutions],
            ["stale-target", "unrelated-target", "already-stable"],
        )
        self.assertEqual(
            [record["target_path"] for record in migrated_resolutions],
            [stable_target, "~/.codex/skills/unrelated/SKILL.md", stable_target],
        )
        self.assertEqual(migrated_resolutions[0]["sentinel"], "preserve-resolution")
        self.assertEqual(
            [record["target_path"] for record in migrated_indexes],
            [stable_target, ".codex/skills/unrelated/SKILL.md"],
        )
        self.assertEqual(migrated_indexes[0]["sentinel"], "preserve-index")
        resolution_after = (store / "resolutions.jsonl").read_bytes()
        index_after = (store / "index.jsonl").read_bytes()

        next_session = self.payload(session_id="session-2", turn_id="turn-2", source="startup")
        self.run_hook("SessionStart", next_session)
        self.assertEqual((store / "resolutions.jsonl").read_bytes(), resolution_after)
        self.assertEqual((store / "index.jsonl").read_bytes(), index_after)
        self.assertEqual(standalone.read_text(encoding="utf-8"), "standalone sentinel\n")

    def test_global_target_migration_fails_open_on_corrupt_jsonl(self) -> None:
        store = self.codex_home / "codex-gardener-global-learning"
        store.mkdir(parents=True)
        path = store / "resolutions.jsonl"
        path.write_text(
            json.dumps(
                {
                    "fingerprint": "preserve-me",
                    "target_path": "~/.codex/skills/cross-project-delegation/SKILL.md",
                }
            )
            + "\nnot-json\n",
            encoding="utf-8",
            newline="\n",
        )
        before = path.read_bytes()

        self.run_hook("SessionStart", self.payload(source="startup"))

        self.assertEqual(path.read_bytes(), before)

    def test_records_repository_and_global_candidates_in_separate_stores(self) -> None:
        repository = gardener.record_candidate(self.candidate_args(session_id="repo-session"))
        global_record = gardener.record_candidate(
            self.candidate_args(
                session_id="global-session",
                knowledge_scope="global",
                lesson="Review diffs before declaring work complete.",
                target="agents",
            )
        )
        repository_records = gardener.read_jsonl(gardener.learning_dir(self.root) / "inbox.jsonl")
        global_records = gardener.read_jsonl(gardener.learning_dir(self.root, "global") / "inbox.jsonl")
        self.assertEqual(repository_records, [repository])
        self.assertEqual(global_records, [global_record])
        self.assertEqual(global_record["knowledge_scope"], "global")
        self.assertEqual(len(global_record["project_fingerprint"]), 24)
        self.assertNotIn(str(self.root), json.dumps(global_record))
        self.assertTrue(str(gardener.learning_dir(self.root, "global")).startswith(str(self.codex_home)))
        global_ignore = (gardener.learning_dir(self.root, "global") / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("inbox.jsonl", global_ignore)

    def test_global_promotion_requires_sessions_from_two_projects(self) -> None:
        for session in ("one", "two"):
            gardener.record_candidate(
                self.candidate_args(
                    session_id=session,
                    knowledge_scope="global",
                    scope="completion",
                    lesson="Review the final diff before completion.",
                    target="agents",
                )
            )
        confirmed = gardener.aggregate_candidates(self.root, "global")[0]
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["occurrences"], 2)

        gardener.record_candidate(
            self.candidate_args(
                session_id="three",
                knowledge_scope="global",
                scope="completion",
                lesson="Review the final diff before completion.",
                target="agents",
            )
        )
        same_project = gardener.aggregate_candidates(self.root, "global")[0]
        self.assertEqual(same_project["status"], "proposed")
        self.assertEqual(same_project["project_count"], 1)

        gardener.record_candidate(
            self.candidate_args(
                repo=self.other,
                session_id="four",
                knowledge_scope="global",
                scope="completion",
                lesson="Review the final diff before completion.",
                target="agents",
            )
        )
        cross_project = gardener.aggregate_candidates(self.other, "global")[0]
        self.assertEqual(cross_project["status"], "promotable")
        self.assertEqual(cross_project["occurrences"], 4)
        self.assertEqual(cross_project["project_count"], 2)

    def test_global_evidence_below_confidence_remains_confirmed(self) -> None:
        for session in ("one", "two", "three"):
            gardener.record_candidate(
                self.candidate_args(
                    session_id=session,
                    knowledge_scope="global",
                    scope="completion",
                    lesson="Review the final diff before completion.",
                    target="agents",
                    confidence=0.7,
                )
            )
        group = gardener.aggregate_candidates(self.root, "global")[0]
        self.assertEqual(group["status"], "confirmed")
        self.assertEqual(group["evidence_status"], "confirmed")
        self.assertEqual(group["occurrences"], 3)
        self.assertEqual(group["confidence"], 0.7)

    def test_global_retrieval_crosses_repositories_but_repository_retrieval_does_not(self) -> None:
        gardener.resolve_candidate(
            self.resolution_args(
                knowledge_scope="global",
                fingerprint="global-rule",
                summary="Always inspect the final diff.",
                target_path="~/.codex/AGENTS.md",
                keyword=["final diff"],
            )
        )
        gardener.resolve_candidate(
            self.resolution_args(
                fingerprint="local-rule",
                summary="Run this repository's parser contract test.",
                keyword=["parser contract"],
            )
        )
        global_context = gardener.promoted_context(self.other, "Inspect the final diff")
        local_context = gardener.promoted_context(self.other, "Run the parser contract")
        self.assertIn("[global]", global_context or "")
        self.assertIn("Always inspect the final diff.", global_context or "")
        self.assertIsNone(local_context)

        subprocess.run(["git", "init", "-q", str(self.other)], check=True)
        hook_payload = self.payload(
            session_id="other-session",
            turn_id="other-turn",
            cwd=str(self.other),
            prompt="Inspect the final diff",
        )
        self.run_hook("SessionStart", hook_payload)
        hook_context = self.run_hook("UserPromptSubmit", hook_payload)
        additional = hook_context["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[global]", additional)
        self.assertIn("Always inspect the final diff.", additional)

    def test_hook_retrieves_global_context_outside_git(self) -> None:
        gardener.resolve_candidate(
            self.resolution_args(
                knowledge_scope="global",
                fingerprint="global-non-git",
                summary="Keep portable guidance available outside Git.",
                target_path="~/.codex/AGENTS.md",
                keyword=["portable guidance"],
            )
        )
        outside = Path(self.temp.name) / "plain-directory"
        outside.mkdir()
        hook_payload = self.payload(
            session_id="plain-session",
            turn_id="plain-turn",
            cwd=str(outside),
            prompt="Use portable guidance",
        )
        self.run_hook("SessionStart", hook_payload)
        context = self.run_hook("UserPromptSubmit", hook_payload)
        additional = context["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[global]", additional)
        self.assertIn("outside Git", additional)

    def test_combined_retrieval_deduplicates_and_prefers_repository_entry(self) -> None:
        gardener.resolve_candidate(
            self.resolution_args(
                knowledge_scope="global",
                fingerprint="shared",
                summary="Global review guidance.",
                target_path="~/.codex/AGENTS.md",
                keyword=["review"],
            )
        )
        gardener.resolve_candidate(
            self.resolution_args(
                fingerprint="shared",
                summary="Repository-specific review guidance.",
                keyword=["review"],
            )
        )
        context = gardener.promoted_context(self.root, "Please review this") or ""
        self.assertEqual(context.count("shared"), 0)
        self.assertEqual(context.count("guidance"), 1)
        self.assertIn("Repository-specific", context)
        self.assertNotIn("Global review", context)

    def test_cli_defaults_repository_and_supports_global_scope(self) -> None:
        env = os.environ.copy()
        repository_command = [
            sys.executable,
            str(SCRIPT),
            "record",
            "--repo",
            str(self.root),
            "--session-id",
            "cli-repository",
            "--scope",
            "cli",
            "--lesson",
            "Keep legacy CLI storage local.",
            "--evidence",
            "CLI default exercised.",
            "--target",
            "docs",
            "--confidence",
            "0.8",
        ]
        repository = subprocess.run(repository_command, capture_output=True, text=True, check=True, env=env)
        self.assertEqual(json.loads(repository.stdout)["knowledge_scope"], "repository")

        global_command = repository_command.copy()
        global_command[global_command.index("--session-id") + 1] = "cli-global"
        global_command[global_command.index("--lesson") + 1] = "Keep portable CLI storage global."
        global_command[3:3] = ["--knowledge-scope", "global"]
        global_result = subprocess.run(global_command, capture_output=True, text=True, check=True, env=env)
        self.assertEqual(json.loads(global_result.stdout)["knowledge_scope"], "global")

        groups = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "groups",
                "--repo",
                str(self.root),
                "--knowledge-scope",
                "global",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        payload = json.loads(groups.stdout)
        self.assertEqual(payload["knowledge_scope"], "global")
        self.assertEqual(len(payload["groups"]), 1)
        fingerprint = payload["groups"][0]["fingerprint"]
        resolved = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "resolve",
                "--repo",
                str(self.root),
                "--fingerprint",
                fingerprint,
                "--knowledge-scope",
                "global",
                "--status",
                "promoted",
                "--summary",
                "Keep portable CLI storage global.",
                "--target-path",
                "~/.codex/AGENTS.md",
                "--keyword",
                "portable",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        self.assertEqual(json.loads(resolved.stdout)["knowledge_scope"], "global")
        self.assertIn("Keep portable CLI storage global.", gardener.promoted_context(self.other, "portable") or "")

    def test_effectiveness_report_combines_events_pending_and_group_status(self) -> None:
        self.init_git()
        gardener.resolve_candidate(self.resolution_args())
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="Update the parser contract"))
        (self.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
        stop = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(stop, {"continue": True})
        gardener.record_candidate(self.candidate_args())
        gardener.resolve_pending("session-1")

        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["coverage"]["sessions_observed"], 1)
        self.assertEqual(report["reviews"]["requested"], 1)
        self.assertEqual(report["reviews"]["captures_recorded"], 1)
        self.assertEqual(report["context"]["lookups"], 1)
        self.assertEqual(report["context"]["hits_repository"], 1)
        self.assertEqual(report["reviews"]["current_pending"], 0)
        self.assertEqual(report["candidate_group_status"]["repository"], {"candidate": 1})

        rendered = gardener.effectiveness.log_path(self.data).read_text(encoding="utf-8")
        self.assertNotIn("Update the parser contract", rendered)
        self.assertNotIn("session-1", rendered)
        self.assertNotIn(str(self.root), rendered)

    def test_effectiveness_counts_effective_resolution_statuses(self) -> None:
        promoted = gardener.record_candidate(self.candidate_args(session_id="promoted-session"))
        discarded = gardener.record_candidate(
            self.candidate_args(
                session_id="discarded-session",
                lesson="Do not retain obsolete parser workarounds.",
            )
        )
        gardener.resolve_candidate(
            self.resolution_args(fingerprint=promoted["fingerprint"], status="promoted")
        )
        gardener.resolve_candidate(
            self.resolution_args(fingerprint=discarded["fingerprint"], status="discarded")
        )

        report = self.run_cli("effectiveness", "--repo", str(self.root), "--json")
        self.assertEqual(
            report["candidate_group_status"]["repository"],
            {"discarded": 1, "promoted": 1},
        )
        self.assertEqual(
            report["candidate_group_evidence_status"]["repository"],
            {"candidate": 2},
        )

    def test_effectiveness_health_reports_duplicate_enabled_gardener_plugins(self) -> None:
        self.codex_home.mkdir(parents=True, exist_ok=True)
        standalone = self.codex_home / "skills" / "cross-project-delegation" / "SKILL.md"
        standalone.parent.mkdir(parents=True, exist_ok=True)
        standalone.write_text("legacy guidance\n", encoding="utf-8")
        (self.codex_home / "config.toml").write_text(
            '[plugins."codex-gardener@codex-gardener"]\n'
            'enabled = true\n\n'
            '[plugins."codex-gardener@personal"]\n'
            'enabled = true\n',
            encoding="utf-8",
        )
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(
            report["health"]["enabled_plugin_ids"],
            ["codex-gardener@codex-gardener", "codex-gardener@personal"],
        )
        self.assertEqual(report["health"]["duplicate_enabled_plugin_ids"], ["codex-gardener@personal"])
        self.assertEqual(report["health"]["plugin_id"], "codex-gardener@codex-gardener")
        self.assertEqual(report["health"]["plugin_version"], "0.6.1")
        self.assertTrue(report["health"]["standalone_cross_project_skill_exists"])
        self.assertEqual(Path(report["health"]["standalone_cross_project_skill_path"]), standalone.resolve())

    def test_fresh_hook_uses_plugin_data_and_writes_session_and_context_events(self) -> None:
        official_data = Path(self.temp.name) / "official-plugin-data"
        hook_home = Path(self.temp.name) / "fresh-codex-home"
        env = os.environ.copy()
        env.pop("CODEX_GARDENER_DATA", None)
        env.update({"PLUGIN_DATA": str(official_data), "CODEX_HOME": str(hook_home)})
        payload = json.dumps(self.payload(session_id="fresh-session", turn_id="fresh-turn"))
        for event in ("SessionStart", "UserPromptSubmit"):
            subprocess.run(
                [sys.executable, str(SCRIPT), "hook", event],
                input=payload,
                text=True,
                capture_output=True,
                check=True,
                env=env,
            )
        events, corrupt = gardener.effectiveness.read_events(root=official_data)
        self.assertEqual(corrupt, 0)
        self.assertEqual([event["event"] for event in events], ["session_start", "context_lookup"])
        checkpoint = json.loads((official_data / "audit-checkpoint.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(checkpoint["initialized_at"])
        self.assertIsNone(checkpoint["last_successful_audit_at"])
        locator = hook_home / "codex-gardener-data-path"
        self.assertEqual(Path(locator.read_text(encoding="utf-8").strip()), official_data.resolve())

    def test_effectiveness_tracks_no_candidate_and_current_pending(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        gardener.complete_review_without_candidate("session-1")
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["completed_without_candidate"], 1)

        second = self.payload(session_id="session-2", turn_id="turn-2")
        self.run_hook("SessionStart", second)
        self.run_hook(
            "PostToolUse",
            self.payload(
                session_id="session-2",
                turn_id="turn-2",
                tool_name="apply_patch",
                tool_input={"patch": "private contents"},
                tool_response={"status": "ok"},
            ),
        )
        self.run_hook("Stop", second | {"stop_hook_active": False})
        self.run_hook("SessionEnd", second)
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["pending_queued"], 1)
        self.assertEqual(report["reviews"]["current_pending"], 1)
        gardener.resolve_pending("session-2")
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["pending_resolved"], 1)
        self.assertEqual(report["reviews"]["current_pending"], 0)


if __name__ == "__main__":
    unittest.main()
