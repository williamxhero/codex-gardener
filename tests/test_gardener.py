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

    def test_clean_git_repository_is_not_a_signal(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        self.run_hook("UserPromptSubmit", self.payload(prompt="Inspect the repository"))
        result = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(result, {"continue": True})

    def test_edit_triggers_once_and_active_stop_does_not_loop(self) -> None:
        self.init_git()
        self.run_hook("SessionStart", self.payload(source="startup"))
        (self.root / "tracked.txt").write_text("after\n", encoding="utf-8")
        first = self.run_hook("Stop", self.payload(stop_hook_active=False))
        self.assertEqual(first["decision"], "block")
        self.assertIn("$codex-gardener:gardener-capture", first["reason"])
        second = self.run_hook("Stop", self.payload(stop_hook_active=True))
        self.assertEqual(second, {"continue": True})
        report = gardener.effectiveness_report(14, self.root)
        self.assertEqual(report["reviews"]["completed_without_candidate"], 1)

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
        self.assertEqual(result["decision"], "block")
        self.assertIn("user correction", result["reason"])
        self.assertIn("repeated failures", result["reason"])

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
        self.assertEqual(first["decision"], "block")
        self.run_hook("Stop", self.payload(turn_id="turn-1", stop_hook_active=True))
        second_prompt = self.payload(turn_id="turn-2", prompt="wrong, use the documented command")
        self.run_hook("UserPromptSubmit", second_prompt)
        second = self.run_hook("Stop", self.payload(turn_id="turn-2", stop_hook_active=False))
        self.assertEqual(second["decision"], "block")

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
        ignore = (self.root / ".codex" / "learning" / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("inbox.jsonl", ignore)
        self.assertIn("index.jsonl", ignore)

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

        first = gardener.aggregate_candidates(self.root, "global")
        self.assertFalse((self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl").exists())
        self.run_hook("SessionStart", self.payload(source="startup"))
        second = gardener.aggregate_candidates(self.other, "global")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]["occurrences"], 1)
        self.assertEqual(first[0]["knowledge_scope"], "global")
        self.assertIn("legacy-user-learning-v1", json.dumps(first))
        self.assertIn("[global]", gardener.promoted_context(self.other, "cross-project") or "")

        migrated = gardener.read_jsonl(self.codex_home / "codex-gardener-global-learning" / "inbox.jsonl")
        self.assertEqual(len(migrated), 1)
        self.assertEqual(migrated[0]["knowledge_scope"], "global")
        self.assertEqual(migrated[0]["migration_provenance"], "legacy-user-learning-v1")
        self.assertEqual(gardener.read_jsonl(legacy / "inbox.jsonl"), [candidate])

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
        self.assertEqual(stop["decision"], "block")
        gardener.record_candidate(self.candidate_args())

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
        self.assertEqual(report["health"]["plugin_version"], "0.4.0")
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
