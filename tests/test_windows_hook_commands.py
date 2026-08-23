from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-gardener"
HOOKS = PLUGIN / "hooks" / "hooks.json"

EXPECTED_WINDOWS_COMMANDS = {
    "PreToolUse": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m project_boundary",
    "SessionStart": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m gardener hook SessionStart",
    "UserPromptSubmit": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m gardener hook UserPromptSubmit",
    "PostToolUse": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m gardener hook PostToolUse",
    "Stop": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m gardener hook Stop",
    "SessionEnd": "set PYTHONPATH=%PLUGIN_ROOT%\\scripts&& python -m gardener hook SessionEnd",
}


def windows_commands() -> dict[str, str]:
    manifest = json.loads(HOOKS.read_text(encoding="utf-8"))
    return {
        event: entries[0]["hooks"][0]["commandWindows"]
        for event, entries in manifest["hooks"].items()
    }


class WindowsHookCommandTest(unittest.TestCase):
    def test_all_windows_commands_are_quote_free_module_invocations(self) -> None:
        commands = windows_commands()
        self.assertEqual(commands, EXPECTED_WINDOWS_COMMANDS)
        for event, command in commands.items():
            with self.subTest(event=event):
                self.assertNotIn('"', command)

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_outer_quoted_cmd_runner_executes_all_hooks_with_spaced_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "Plugin Root With Spaces"
            shutil.copytree(PLUGIN / "scripts", plugin_root / "scripts")
            workspace = root / "workspace"
            workspace.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "PLUGIN_ROOT": str(plugin_root),
                    "PLUGIN_DATA": str(root / "plugin data"),
                    "CODEX_HOME": str(root / "codex home"),
                    "CODEX_GARDENER_EFFECTIVENESS_LOG": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            base = {
                "session_id": "windows-smoke-session",
                "turn_id": "windows-smoke-turn",
                "cwd": str(workspace),
            }
            payloads = {
                "PreToolUse": {
                    **base,
                    "tool_name": "Read",
                    "tool_input": {"path": str(workspace / "file.txt")},
                },
                "SessionStart": {**base, "source": "startup"},
                "UserPromptSubmit": {**base, "prompt": "ordinary request"},
                "PostToolUse": {
                    **base,
                    "tool_name": "wait",
                    "tool_input": {},
                    "tool_response": {"status": "ok"},
                },
                "Stop": {**base, "stop_hook_active": False},
                "SessionEnd": base,
            }

            for event, command in windows_commands().items():
                with self.subTest(event=event):
                    runner = subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/C"])
                    result = subprocess.run(
                        f'{runner} "{command}"',
                        input=json.dumps(payloads[event]),
                        text=True,
                        capture_output=True,
                        check=False,
                        cwd=workspace,
                        env=env,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                    self.assertEqual(result.stderr, "")
                    if event == "Stop":
                        self.assertEqual(json.loads(result.stdout), {"continue": True})
                    else:
                        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
