from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-gardener"


class PackagedPolicyTest(unittest.TestCase):
    def test_manifest_version_is_0_5_2(self) -> None:
        manifest = json.loads(
            (PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "0.5.2")

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


if __name__ == "__main__":
    unittest.main()
