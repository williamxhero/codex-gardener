from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "install.py"
SPEC = importlib.util.spec_from_file_location("install", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class FakeRunner:
    def __init__(self, marketplaces=None, installed=None, failure_command=None):
        self.marketplaces = marketplaces or []
        self.installed = installed or []
        self.failure_command = failure_command
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self.failure_command and self.failure_command in command:
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="simulated failure")
        if command[-3:] == ["marketplace", "list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"marketplaces": self.marketplaces}),
                stderr="",
            )
        if command[-3:] == ["plugin", "list", "--json"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"installed": self.installed, "available": []}),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict("os.environ", {"CODEX_HOME": str(Path(self.temp.name) / "codex-home")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_main(self, runner: FakeRunner, argv=None, which=lambda _: "codex"):
        output = io.StringIO()
        error = io.StringIO()
        code = installer.main(argv or [], which=which, runner=runner, output=output, error=error)
        return code, output.getvalue(), error.getvalue()

    def test_missing_codex_fails_clearly(self) -> None:
        code, _, error = self.run_main(FakeRunner(), which=lambda _: None)
        self.assertEqual(code, 1)
        self.assertIn("not found on PATH", error)

    def test_dry_run_lists_state_but_does_not_mutate(self) -> None:
        runner = FakeRunner()
        code, output, error = self.run_main(runner, ["--dry-run"])
        self.assertEqual(code, 0, error)
        self.assertEqual(
            runner.commands,
            [
                ["codex", "plugin", "marketplace", "list", "--json"],
                ["codex", "plugin", "list", "--json"],
            ],
        )
        self.assertIn("marketplace add", output)
        self.assertIn("plugin add codex-gardener@codex-gardener", output)
        self.assertIn("no marketplace or plugin changes", output)

    def test_installs_marketplace_and_plugin(self) -> None:
        runner = FakeRunner()
        code, output, error = self.run_main(runner)
        self.assertEqual(code, 0, error)
        self.assertEqual(len(runner.commands), 4)
        self.assertEqual(runner.commands[2][1:4], ["plugin", "marketplace", "add"])
        self.assertEqual(runner.commands[3][1:], ["plugin", "add", "codex-gardener@codex-gardener"])
        self.assertIn("Rebuilt local global retrieval index (0 entries)", output)
        self.assertTrue((Path(os.environ["CODEX_HOME"]) / "codex-gardener-global-learning" / "retrieval.sqlite3").is_file())
        self.assertIn("run /hooks", output)
        self.assertIn("new task", output)

    def test_existing_061_plugin_upgrades_in_an_isolated_codex_home(self) -> None:
        runner = FakeRunner(
            marketplaces=[{"name": "codex-gardener", "root": str(installer.REPO_ROOT)}],
            installed=[
                {
                    "pluginId": "codex-gardener@codex-gardener",
                    "version": "0.6.1",
                    "installed": True,
                    "enabled": True,
                }
            ],
        )
        code, output, error = self.run_main(runner)
        self.assertEqual(code, 0, error)
        self.assertEqual(runner.commands[-1][1:], ["plugin", "add", "codex-gardener@codex-gardener"])
        index = Path(os.environ["CODEX_HOME"]) / "codex-gardener-global-learning" / "retrieval.sqlite3"
        self.assertTrue(index.is_file())
        self.assertIn("Rebuilt local global retrieval index (0 entries)", output)

    def test_existing_matching_marketplace_skips_marketplace_add(self) -> None:
        runner = FakeRunner([{"name": "codex-gardener", "root": str(installer.REPO_ROOT)}])
        code, output, error = self.run_main(runner)
        self.assertEqual(code, 0, error)
        self.assertEqual(len(runner.commands), 3)
        self.assertEqual(runner.commands[-1][1:], ["plugin", "add", "codex-gardener@codex-gardener"])
        self.assertIn("already points to this checkout", output)

    def test_conflicting_marketplace_fails_without_mutation(self) -> None:
        runner = FakeRunner([{"name": "codex-gardener", "root": str(installer.REPO_ROOT.parent)}])
        code, _, error = self.run_main(runner)
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.commands), 2)
        self.assertIn("different location", error)
        self.assertIn("marketplace remove codex-gardener", error)

    def test_command_failure_includes_diagnostic(self) -> None:
        runner = FakeRunner(failure_command="codex-gardener@codex-gardener")
        code, _, error = self.run_main(runner)
        self.assertEqual(code, 1)
        self.assertIn("simulated failure", error)

    def test_legacy_personal_install_fails_with_explicit_removal_command(self) -> None:
        runner = FakeRunner(
            installed=[
                {
                    "pluginId": "codex-gardener@personal",
                    "name": "codex-gardener",
                    "installed": True,
                    "enabled": True,
                }
            ]
        )
        code, _, error = self.run_main(runner)
        self.assertEqual(code, 1)
        self.assertIn("codex plugin remove codex-gardener@personal", error)
        self.assertFalse(any(command[1:3] == ["plugin", "add"] for command in runner.commands))

    def test_reports_standalone_delegation_skill_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            codex_home = Path(temp) / "codex-home"
            standalone = codex_home / "skills" / "cross-project-delegation" / "SKILL.md"
            standalone.parent.mkdir(parents=True)
            standalone.write_text("legacy guidance\n", encoding="utf-8")
            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                code, output, error = self.run_main(FakeRunner(), ["--dry-run"])
            self.assertEqual(code, 0, error)
            self.assertIn("standalone Skill detected", output)
            self.assertIn("$codex-gardener:cross-project-delegation", output)
            self.assertEqual(standalone.read_text(encoding="utf-8"), "legacy guidance\n")


if __name__ == "__main__":
    unittest.main()
