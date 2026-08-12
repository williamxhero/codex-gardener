from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "plugins" / "codex-gardener" / "scripts" / "project_boundary.py"
SPEC = importlib.util.spec_from_file_location("project_boundary", SCRIPT)
assert SPEC and SPEC.loader
project_boundary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(project_boundary)


class ProjectBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.primary = root / "primary"
        self.other = root / "other"
        self.primary.mkdir()
        self.other.mkdir()
        subprocess.run(["git", "init", "-q", str(self.primary)], check=True)
        subprocess.run(["git", "init", "-q", str(self.other)], check=True)

    def payload(self, tool_name: str, tool_input: object) -> dict:
        return {"cwd": str(self.primary), "tool_name": tool_name, "tool_input": tool_input}

    def test_allows_same_repository_write(self) -> None:
        result = project_boundary.denial(
            self.payload("apply_patch", {"patch": "*** Begin Patch\n*** Update File: local.txt\n*** End Patch"})
        )
        self.assertIsNone(result)

    def test_denies_apply_patch_into_another_repository(self) -> None:
        patch = "*** Begin Patch\n*** Add File: ../other/new.txt\n+value\n*** End Patch"
        result = project_boundary.denial(self.payload("apply_patch", {"patch": patch}))
        self.assertIsNotNone(result)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("$cross-project-delegation", output["permissionDecisionReason"])

    def test_denies_shell_write_with_explicit_other_repo_path(self) -> None:
        command = f'Set-Content -LiteralPath "{self.other / "file.txt"}" -Value changed'
        result = project_boundary.denial(self.payload("Bash", {"command": command}))
        self.assertIsNotNone(result)

    def test_allows_read_from_another_repository(self) -> None:
        result = project_boundary.denial(self.payload("Read", {"path": str(self.other / "file.txt")}))
        self.assertIsNone(result)

    def test_non_git_primary_degrades_open(self) -> None:
        outside = Path(self.temp.name) / "not-a-repo"
        outside.mkdir()
        payload = {"cwd": str(outside), "tool_name": "Write", "tool_input": {"path": str(self.other / "x")}}
        self.assertIsNone(project_boundary.denial(payload))


if __name__ == "__main__":
    unittest.main()
