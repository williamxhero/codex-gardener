#!/usr/bin/env python3
"""Portable, dependency-free validation for the Codex Gardener marketplace."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-gardener"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class ValidationError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Invalid JSON at {path.relative_to(ROOT)}: {exc}") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    require(lines and lines[0] == "---", f"{path.relative_to(ROOT)} must begin with YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValidationError(f"{path.relative_to(ROOT)} has unterminated YAML frontmatter") from exc
    values: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        require(bool(separator), f"Malformed frontmatter in {path.relative_to(ROOT)}: {line}")
        values[key.strip()] = value.strip().strip("\"'")
    require(set(values) == {"name", "description"}, f"{path.relative_to(ROOT)} frontmatter must contain only name and description")
    return values


def validate_marketplace() -> None:
    path = ROOT / ".agents" / "plugins" / "marketplace.json"
    data = load_json(path)
    require(data.get("name") == "codex-gardener", "Marketplace name must be codex-gardener")
    require(data.get("interface", {}).get("displayName") == "Codex Gardener", "Marketplace display name is invalid")
    plugins = data.get("plugins")
    require(isinstance(plugins, list) and len(plugins) == 1, "Marketplace must contain exactly one plugin")
    entry = plugins[0]
    require(entry.get("name") == "codex-gardener", "Marketplace plugin name is invalid")
    require(entry.get("source") == {"source": "local", "path": "./plugins/codex-gardener"}, "Marketplace source is invalid")
    require(entry.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "Marketplace policy is invalid")
    require(entry.get("category") == "Developer Tools", "Marketplace category is invalid")


def validate_manifest() -> None:
    path = PLUGIN / ".codex-plugin" / "plugin.json"
    data = load_json(path)
    require(data.get("name") == "codex-gardener", "Plugin name is invalid")
    require(bool(VERSION_RE.fullmatch(str(data.get("version", "")))), "Plugin version must be strict x.y.z semver")
    require(data.get("version") == "0.2.0", "Plugin version must be 0.2.0")
    require(isinstance(data.get("description"), str) and len(data["description"]) >= 20, "Plugin description is too short")
    require("right scope" in data["description"], "Plugin description must explain scope-aware promotion")
    require(data.get("author", {}).get("name") == "williamxhero", "Plugin author is invalid")
    require(data.get("license") == "MIT", "Plugin license must be MIT")
    require(data.get("repository") == "https://github.com/williamxhero/codex-gardener", "Plugin repository URL is invalid")
    require(data.get("skills") == "./skills/", "Plugin skills path is invalid")
    require("hooks" not in data, "Hooks are discovered from hooks/hooks.json and must not be declared in plugin.json")
    interface = data.get("interface", {})
    for key in ("displayName", "shortDescription", "longDescription", "developerName", "category", "capabilities", "defaultPrompt"):
        require(interface.get(key), f"Plugin interface.{key} is required")
    prompts = interface["defaultPrompt"]
    require(isinstance(prompts, list) and 1 <= len(prompts) <= 3, "Plugin defaultPrompt must contain one to three prompts")
    require(all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts), "Plugin prompts must be strings no longer than 128 characters")
    require("Repository and global learning" in interface["capabilities"], "Plugin must advertise repository and global learning")


def validate_hooks() -> None:
    path = PLUGIN / "hooks" / "hooks.json"
    data = load_json(path)
    hooks = data.get("hooks", {})
    expected = {"PreToolUse", "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"}
    require(set(hooks) == expected, "Hook lifecycle events are incomplete or unexpected")
    rendered = path.read_text(encoding="utf-8")
    for script in ("gardener.py", "project_boundary.py"):
        require(script in rendered and (PLUGIN / "scripts" / script).is_file(), f"Hook script is missing: {script}")


def validate_skills() -> None:
    skills = PLUGIN / "skills"
    directories = sorted(path for path in skills.iterdir() if path.is_dir())
    require(len(directories) == 3, "The plugin must bundle exactly three skills")
    for directory in directories:
        metadata = frontmatter(directory / "SKILL.md")
        name = metadata["name"]
        require(name == directory.name, f"Skill name does not match its directory: {directory.name}")
        require(bool(NAME_RE.fullmatch(name)) and len(name) <= 64, f"Invalid skill name: {name}")
        require(len(metadata["description"]) >= 40, f"Skill description is too short: {name}")
        openai_yaml = directory / "agents" / "openai.yaml"
        require(openai_yaml.is_file(), f"Missing agents/openai.yaml for {name}")
        yaml_text = openai_yaml.read_text(encoding="utf-8")
        require(f"${name}" in yaml_text, f"Default prompt must mention ${name}")


def validate_python() -> None:
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        compile(source, str(path), "exec")


def validate_hygiene() -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        require(path.name != "__pycache__", f"Generated cache directory must not be packaged: {path.relative_to(ROOT)}")
        require(path.suffix not in {".pyc", ".pyo"}, f"Generated bytecode must not be packaged: {path.relative_to(ROOT)}")
    for name in ("README.md", "LICENSE", "PRIVACY.md", "SECURITY.md", "CONTRIBUTING.md"):
        require((ROOT / name).is_file(), f"Missing public repository file: {name}")
    gardener_source = (PLUGIN / "scripts" / "gardener.py").read_text(encoding="utf-8")
    require("codex-gardener-global-learning" in gardener_source, "Global learning store path is missing")
    require("knowledge_scope" in gardener_source, "Knowledge scope schema is missing")


def main() -> int:
    try:
        validate_marketplace()
        validate_manifest()
        validate_hooks()
        validate_skills()
        validate_python()
        validate_hygiene()
    except (OSError, ValidationError) as exc:
        print(f"validation failed: {exc}", file=sys.stderr)
        return 1
    print("Validated marketplace, plugin manifest, hooks, 3 skills, Python sources, and repository hygiene.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
