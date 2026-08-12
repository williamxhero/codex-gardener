from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import subprocess
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
        self.data = Path(self.temp.name) / "plugin-data"
        self.env = patch.dict(os.environ, {"CODEX_GARDENER_DATA": str(self.data)}, clear=False)
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
        self.assertIn("$gardener-capture", first["reason"])
        second = self.run_hook("Stop", self.payload(stop_hook_active=True))
        self.assertEqual(second, {"continue": True})

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
            args = argparse.Namespace(
                repo=str(self.root),
                session_id=session,
                scope="tests",
                lesson="Run the contract test before changing the parser.",
                evidence=f"Observed in {session}",
                target="test",
                confidence=0.9,
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
                argparse.Namespace(
                    repo=str(self.root),
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
        args = argparse.Namespace(
            repo=str(self.root),
            fingerprint="abc123",
            status="promoted",
            summary="Run parser contract tests before parser changes.",
            target_path="AGENTS.md",
            keyword=["parser", "contract"],
        )
        gardener.resolve_candidate(args)
        context = gardener.promoted_context(self.root, "Please update the parser contract")
        self.assertIsNotNone(context)
        self.assertIn("AGENTS.md", context)


if __name__ == "__main__":
    unittest.main()
