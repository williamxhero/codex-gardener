from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-gardener"


class PackagedPolicyTest(unittest.TestCase):
    def test_manifest_version_is_0_6_0(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "0.6.0")

    def test_stop_timeout_covers_bounded_maintenance_processing(self) -> None:
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        stop = hooks["Stop"][0]["hooks"][0]
        session_end = hooks["SessionEnd"][0]["hooks"][0]
        self.assertEqual(stop["timeout"], 15)
        self.assertEqual(session_end["timeout"], 3)

    def test_delegation_skill_requires_isolated_concurrent_writers(self) -> None:
        skill = (
            PLUGIN / "skills" / "cross-project-delegation" / "SKILL.md"
        ).read_text(encoding="utf-8").lower()

        required_phrases = (
            "each writer must use one unique git worktree and branch",
            "writers must not share a working tree",
            "pin an exact base commit",
            "dedicated, clean integration worktree and branch",
            "never integrate concurrent work in the shared or main checkout",
            "merge or cherry-pick completed branches serially",
            "resolve conflicts only in that integration worktree",
            "rerun relevant validation after each merge",
            "run the full acceptance suite after the final integration",
            "do not proceed with concurrent writes",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_capture_skill_requires_no_command_for_no_candidate(self) -> None:
        skill = (PLUGIN / "skills" / "gardener-capture" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("review-complete", skill)
        self.assertIn("do not run a command", skill.casefold())
        self.assertIn("defer-record", skill)
        self.assertIn("ordinary tasks do not invoke this skill automatically", skill.casefold())

    def test_curator_skill_defines_read_only_automatic_audit_mode(self) -> None:
        skill = (PLUGIN / "skills" / "knowledge-curator" / "SKILL.md").read_text(encoding="utf-8").casefold()
        for phrase in (
            "audit-only",
            "read-only",
            "do not promote",
            "do not edit",
            "defer-audit-complete",
            "effectiveness",
            "pending",
            "repository and global",
            "conflict",
            "stale",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_curator_skill_defines_bounded_non_promoting_maintenance(self) -> None:
        skill = (PLUGIN / "skills" / "knowledge-curator" / "SKILL.md").read_text(encoding="utf-8").casefold()
        for phrase in (
            "maintenance-only",
            "at most three",
            "defer-pending-outcome",
            "no-candidate",
            "write exactly one outcome",
            "must not promote",
            "must not edit agents.md",
            "marker contains no original session, repository, or transcript path",
            "never run or checkpoint an audit in maintenance-only mode",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)


if __name__ == "__main__":
    unittest.main()
