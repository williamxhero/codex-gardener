#!/usr/bin/env python3
"""Conservatively block writes from one Git repository into another."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import effectiveness


MUTATING_NAME_RE = re.compile(
    r"(?:^|[_:.\-])(add|append|copy|create|delete|edit|move|patch|remove|rename|update|upload|write)(?:$|[_:.\-])",
    re.IGNORECASE,
)
MUTATING_COMMAND_RE = re.compile(
    r"(?:\bapply_patch\b|\bgit\s+(?:add|commit|mv|rm)\b|\b(?:rm|mv|cp|mkdir|touch)\b|"
    r"\bsed\s+-i\b|\b(?:new-item|set-content|add-content|remove-item|move-item|copy-item)\b)",
    re.IGNORECASE,
)
PATH_KEYS = {
    "dest",
    "destination",
    "file",
    "file_path",
    "filename",
    "folder",
    "output",
    "output_path",
    "path",
    "target",
    "target_path",
    "workdir",
}
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Delete|Update) File:\s*(.+?)\s*$", re.MULTILINE)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to:\s*(.+?)\s*$", re.MULTILINE)
WINDOWS_ABSOLUTE_RE = re.compile(r"(?<![\w])([A-Za-z]:[\\/][^\s\"'`|;&<>]+)")
POSIX_ABSOLUTE_RE = re.compile(r"(?<![\w])(/[^\s\"'`|;&<>]+)")


def git_root(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=1.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    with contextlib.suppress(OSError, UnicodeDecodeError):
        return Path(result.stdout.decode().strip()).resolve()
    return None


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def command_from(tool_input: Any) -> str:
    if isinstance(tool_input, str):
        return tool_input
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "script", "patch"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
    return ""


def is_mutating(tool_name: str, tool_input: Any) -> bool:
    lowered = tool_name.casefold()
    if lowered in {"apply_patch", "edit", "write"} or MUTATING_NAME_RE.search(tool_name):
        return True
    command = command_from(tool_input)
    return bool(command and MUTATING_COMMAND_RE.search(command))


def keyed_paths(value: Any, key: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from keyed_paths(child, str(child_key).casefold())
    elif isinstance(value, list):
        for child in value:
            yield from keyed_paths(child, key)
    elif key in PATH_KEYS and isinstance(value, str) and value.strip():
        yield value.strip()


def candidate_paths(tool_name: str, tool_input: Any) -> list[str]:
    values = list(keyed_paths(tool_input))
    command = command_from(tool_input)
    if tool_name.casefold() == "apply_patch" or "*** Begin Patch" in command:
        values.extend(PATCH_PATH_RE.findall(command))
        values.extend(PATCH_MOVE_RE.findall(command))
    if command and MUTATING_COMMAND_RE.search(command):
        values.extend(WINDOWS_ABSOLUTE_RE.findall(command))
        values.extend(POSIX_ABSOLUTE_RE.findall(command))
    return list(dict.fromkeys(value.strip().strip('"\'') for value in values if value.strip()))


def resolve_target(raw: str, base: Path) -> Path | None:
    if "\x00" in raw or raw.startswith(("http://", "https://", "codex://")):
        return None
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = base / path
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def tool_category(tool_name: str, tool_input: Any) -> str:
    lowered = tool_name.casefold()
    command = command_from(tool_input).casefold()
    if lowered == "apply_patch" or "*** begin patch" in command:
        return "patch"
    if lowered in {"bash", "shell", "shell_command"}:
        return "git" if re.search(r"\bgit\s+(?:add|commit|mv|rm)\b", command) else "shell"
    if lowered in {"edit", "write"}:
        return "file"
    if lowered.startswith(("mcp", "mcp__")):
        return "mcp"
    return "other"


def denial(payload: dict[str, Any]) -> dict[str, Any] | None:
    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    primary = git_root(cwd)
    if primary is None:
        return None
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input")
    if not is_mutating(tool_name, tool_input):
        return None
    base = cwd
    if isinstance(tool_input, dict) and isinstance(tool_input.get("workdir"), str):
        workdir = resolve_target(tool_input["workdir"], cwd)
        if workdir is not None:
            base = workdir
    for raw in candidate_paths(tool_name, tool_input):
        target = resolve_target(raw, base)
        if target is None:
            continue
        owner = git_root(target)
        if owner is None or same_path(owner, primary):
            continue
        effectiveness.log_event(
            "project_boundary_denied",
            session=payload.get("session_id"),
            primary_project=str(primary),
            target_project=str(owner),
            tool_category=tool_category(tool_name, tool_input),
        )
        reason = (
            f"Cross-project write blocked: the target belongs to {owner}, while this task's primary "
            f"repository is {primary}. Use $cross-project-delegation and perform the change in an "
            "agent or task whose working project is the target repository."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        result = denial(payload)
        if result:
            json.dump(result, sys.stdout, ensure_ascii=False)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # Ambiguous or malformed input must not break ordinary tool use.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
