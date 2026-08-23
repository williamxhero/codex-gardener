#!/usr/bin/env python3
"""Install a cloned Codex Gardener marketplace with the Codex CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence, TextIO


MARKETPLACE_NAME = "codex-gardener"
PLUGIN_SELECTOR = "codex-gardener@codex-gardener"
LEGACY_PLUGIN_SELECTOR = "codex-gardener@personal"
REPO_ROOT = Path(__file__).resolve().parent
Runner = Callable[..., subprocess.CompletedProcess[str]]


class InstallError(RuntimeError):
    """A user-actionable installation failure."""


def display_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(command))
    return shlex.join(command)


def run_command(command: Sequence[str], runner: Runner) -> subprocess.CompletedProcess[str]:
    result = runner(
        list(command),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise InstallError(f"Command failed ({result.returncode}): {display_command(command)}\n{detail}")
    return result


def read_marketplaces(codex: str, runner: Runner) -> list[dict[str, Any]]:
    command = [codex, "plugin", "marketplace", "list", "--json"]
    result = run_command(command, runner)
    try:
        payload = json.loads(result.stdout)
        marketplaces = payload["marketplaces"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstallError("Codex returned invalid marketplace JSON; update the Codex CLI and retry.") from exc
    if not isinstance(marketplaces, list):
        raise InstallError("Codex returned an unexpected marketplace list.")
    return [item for item in marketplaces if isinstance(item, dict)]


def read_installed_plugins(codex: str, runner: Runner) -> list[dict[str, Any]]:
    command = [codex, "plugin", "list", "--json"]
    result = run_command(command, runner)
    try:
        payload = json.loads(result.stdout)
        installed = payload["installed"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InstallError("Codex returned invalid plugin JSON; update the Codex CLI and retry.") from exc
    if not isinstance(installed, list):
        raise InstallError("Codex returned an unexpected installed-plugin list.")
    return [item for item in installed if isinstance(item, dict)]


def same_path(left: str | Path, right: str | Path) -> bool:
    left_path = Path(left).resolve()
    right_path = Path(right).resolve()
    try:
        return os.path.samefile(left_path, right_path)
    except OSError:
        left_text = os.path.normcase(str(left_path))
        right_text = os.path.normcase(str(right_path))
        return left_text == right_text


def validate_checkout(repo_root: Path) -> None:
    marketplace = repo_root / ".agents" / "plugins" / "marketplace.json"
    plugin = repo_root / "plugins" / "codex-gardener" / ".codex-plugin" / "plugin.json"
    missing = [str(path) for path in (marketplace, plugin) if not path.is_file()]
    if missing:
        raise InstallError("This does not look like a complete Codex Gardener checkout. Missing: " + ", ".join(missing))


def standalone_delegation_skill() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / "cross-project-delegation" / "SKILL.md"


def install(
    repo_root: Path,
    codex: str,
    *,
    dry_run: bool,
    runner: Runner = subprocess.run,
    output: TextIO = sys.stdout,
) -> None:
    repo_root = repo_root.resolve()
    validate_checkout(repo_root)
    marketplaces = read_marketplaces(codex, runner)
    installed = read_installed_plugins(codex, runner)
    standalone_skill = standalone_delegation_skill()
    legacy = next(
        (
            item
            for item in installed
            if item.get("pluginId") == LEGACY_PLUGIN_SELECTOR
            and item.get("installed", True)
            and item.get("enabled", True)
        ),
        None,
    )
    if legacy is not None:
        standalone_note = (
            f" A standalone Skill also exists at {standalone_skill}; compare it with the bundled namespaced Skill "
            "and remove or rename it only after preserving any unique valid guidance."
            if standalone_skill.is_file()
            else ""
        )
        raise InstallError(
            f"Legacy {LEGACY_PLUGIN_SELECTOR} is still enabled and would duplicate hooks and Skills. "
            f"Remove it explicitly with 'codex plugin remove {LEGACY_PLUGIN_SELECTOR}', then run this installer again. "
            "This installer will not delete another installation automatically."
            + standalone_note
        )
    existing = next((item for item in marketplaces if item.get("name") == MARKETPLACE_NAME), None)

    add_marketplace = [codex, "plugin", "marketplace", "add", str(repo_root)]
    add_plugin = [codex, "plugin", "add", PLUGIN_SELECTOR]

    if existing is not None:
        existing_root = existing.get("root")
        if not isinstance(existing_root, str) or not same_path(existing_root, repo_root):
            raise InstallError(
                f"Marketplace '{MARKETPLACE_NAME}' is already configured from a different location: "
                f"{existing_root or '<unknown>'}. Remove it with 'codex plugin marketplace remove "
                f"{MARKETPLACE_NAME}' before installing this clone."
            )
        print(f"Marketplace '{MARKETPLACE_NAME}' already points to this checkout.", file=output)
    elif dry_run:
        print(f"Would run: {display_command(add_marketplace)}", file=output)
    else:
        print(f"Adding marketplace from {repo_root}...", file=output)
        run_command(add_marketplace, runner)

    if dry_run:
        print(f"Would run: {display_command(add_plugin)}", file=output)
        print("Dry run complete; no marketplace or plugin changes were made.", file=output)
    else:
        print(f"Installing {PLUGIN_SELECTOR}...", file=output)
        run_command(add_plugin, runner)
        print("Codex Gardener is installed.", file=output)

    if standalone_skill.is_file():
        print(
            f"Warning: standalone Skill detected at {standalone_skill}. It may compete with "
            "$codex-gardener:cross-project-delegation; compare them and remove or rename the standalone copy "
            "only after preserving any unique valid guidance.",
            file=output,
        )
    print("Next: open Codex, run /hooks, and review and trust the hook commands before enabling them.", file=output)
    print("Then start a new task so Codex discovers the bundled skills and hooks.", file=output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install Codex Gardener from this cloned repository.")
    parser.add_argument("--dry-run", action="store_true", help="Show mutating commands without running them.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner = subprocess.run,
    output: TextIO = sys.stdout,
    error: TextIO = sys.stderr,
) -> int:
    args = build_parser().parse_args(argv)
    codex = which("codex")
    if not codex:
        print("error: Codex CLI was not found on PATH. Install or update Codex, then retry.", file=error)
        return 1
    try:
        install(REPO_ROOT, codex, dry_run=args.dry_run, runner=runner, output=output)
    except InstallError as exc:
        print(f"error: {exc}", file=error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
