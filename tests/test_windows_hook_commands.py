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
WRAPPER_NAME = "codex-gardener-hook.cmd"

EXPECTED_WINDOWS_COMMANDS = {
    "PreToolUse": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} project_boundary",
    "SessionStart": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} gardener SessionStart",
    "UserPromptSubmit": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} gardener UserPromptSubmit",
    "PostToolUse": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} gardener PostToolUse",
    "Stop": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} gardener Stop",
    "SessionEnd": f"cmd.exe /d /c %PLUGIN_ROOT%\\scripts\\{WRAPPER_NAME} gardener SessionEnd",
}


def windows_commands() -> dict[str, str]:
    manifest = json.loads(HOOKS.read_text(encoding="utf-8"))
    return {
        event: entries[0]["hooks"][0]["commandWindows"]
        for event, entries in manifest["hooks"].items()
    }


class WindowsHookCommandTest(unittest.TestCase):
    def test_all_windows_commands_are_quote_free_wrapper_invocations(self) -> None:
        commands = windows_commands()
        self.assertEqual(commands, EXPECTED_WINDOWS_COMMANDS)
        for event, command in commands.items():
            with self.subTest(event=event):
                self.assertNotIn('"', command)

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_wrapper_decodes_raw_utf8_correction_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "scripts"
            shutil.copytree(PLUGIN / "scripts", scripts)
            workspace = root / "workspace"
            workspace.mkdir()
            transcript = root / "trusted-transcript.jsonl"
            transcript.write_text("trusted metadata only\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("CODEX_GARDENER_DATA", None)
            env.pop("PYTHONIOENCODING", None)
            env.pop("PYTHONUTF8", None)
            env.update(
                {
                    "PLUGIN_DATA": str(root / "plugin-data"),
                    "CODEX_HOME": str(root / "codex-home"),
                    "CODEX_GARDENER_EFFECTIVENESS_LOG": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            base = {
                "session_id": "utf8-correction-session",
                "turn_id": "utf8-correction-turn",
                "cwd": str(workspace),
                "transcript_path": str(transcript),
            }

            def invoke(event: str, payload: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
                return subprocess.run(
                    [
                        os.environ.get("COMSPEC", "cmd.exe"),
                        "/d",
                        "/c",
                        str(scripts / WRAPPER_NAME),
                        "gardener",
                        event,
                    ],
                    input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    capture_output=True,
                    check=False,
                    cwd=workspace,
                    env=env,
                )

            session_start = invoke("SessionStart", {**base, "source": "startup"})
            self.assertEqual(session_start.returncode, 0, session_start.stderr.decode("utf-8", errors="replace"))
            prompt = invoke("UserPromptSubmit", {**base, "prompt": "纠正：请使用另一个入口"})
            self.assertEqual(prompt.returncode, 0, prompt.stderr.decode("utf-8", errors="replace"))
            stop = invoke("Stop", {**base, "stop_hook_active": False})
            self.assertEqual(stop.returncode, 0, stop.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(json.loads(stop.stdout.decode("utf-8")), {"continue": True})
            pending = (root / "plugin-data" / "pending.jsonl").read_text(encoding="utf-8")
            self.assertIn("user_correction", pending)

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_wrapper_preserves_unicode_paths_for_project_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "scripts"
            shutil.copytree(PLUGIN / "scripts", scripts)
            primary = root / "主要项目"
            other = root / "其他项目"
            primary.mkdir()
            other.mkdir()
            subprocess.run(["git", "init", "-q", str(primary)], check=True)
            subprocess.run(["git", "init", "-q", str(other)], check=True)
            env = os.environ.copy()
            env.pop("CODEX_GARDENER_DATA", None)
            env.pop("PYTHONIOENCODING", None)
            env.pop("PYTHONUTF8", None)
            env.update(
                {
                    "PLUGIN_DATA": str(root / "plugin-data"),
                    "CODEX_HOME": str(root / "codex-home"),
                    "CODEX_GARDENER_EFFECTIVENESS_LOG": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            payload = {
                "session_id": "utf8-boundary-session",
                "cwd": str(primary),
                "tool_name": "Write",
                "tool_input": {"path": str(other / "规则.txt")},
            }
            result = subprocess.run(
                [
                    os.environ.get("COMSPEC", "cmd.exe"),
                    "/d",
                    "/c",
                    str(scripts / WRAPPER_NAME),
                    "project_boundary",
                ],
                input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                capture_output=True,
                check=False,
                cwd=primary,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            output = json.loads(result.stdout.decode("utf-8"))
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_outer_quoted_cmd_runner_executes_all_hooks_through_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin-root"
            self.assertNotIn(" ", str(plugin_root))
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

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_wrapper_forwards_standard_streams_and_exit_code_from_spaced_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "Wrapper Root With Spaces" / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(PLUGIN / "scripts" / WRAPPER_NAME, scripts / WRAPPER_NAME)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "python.cmd").write_text(
                "@echo off\n"
                "set /p INPUT=\n"
                "echo stdout:%INPUT%\n"
                "echo stderr-sentinel 1>&2\n"
                "exit /b 23\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PATH"] = str(fake_bin) + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", WRAPPER_NAME, "project_boundary"],
                input="payload-sentinel\n",
                text=True,
                capture_output=True,
                check=False,
                cwd=scripts,
                env=env,
            )
            self.assertEqual(result.returncode, 23)
            self.assertEqual(result.stdout.strip(), "stdout:payload-sentinel")
            self.assertEqual(result.stderr.strip(), "stderr-sentinel")

    @unittest.skipUnless(os.name == "nt", "requires cmd.exe")
    def test_wrapper_preserves_explicit_python_encoding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(PLUGIN / "scripts" / WRAPPER_NAME, scripts / WRAPPER_NAME)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "python.cmd").write_text(
                "@echo off\n"
                "echo %PYTHONUTF8%:%PYTHONIOENCODING%\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                    "PYTHONUTF8": "0",
                    "PYTHONIOENCODING": "gbk",
                }
            )
            result = subprocess.run(
                [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", WRAPPER_NAME, "project_boundary"],
                capture_output=True,
                text=True,
                check=False,
                cwd=scripts,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "0:gbk")


if __name__ == "__main__":
    unittest.main()
