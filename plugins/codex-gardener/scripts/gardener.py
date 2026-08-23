#!/usr/bin/env python3
"""Deterministic storage and lifecycle hooks for codex-gardener."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import effectiveness


SCHEMA_VERSION = 2
PLUGIN_ID = "codex-gardener@codex-gardener"
LEGACY_MIGRATION_PROVENANCE = "legacy-user-learning-v1"
BUNDLED_DELEGATION_SKILL_TARGET = "plugins/codex-gardener/skills/cross-project-delegation/SKILL.md"
LEGACY_DELEGATION_SKILL_TARGETS = {
    ".codex/skills/cross-project-delegation/skill.md",
    "~/.codex/skills/cross-project-delegation/skill.md",
}
TARGETS = {"agents", "skill", "test", "hook", "docs", "discard"}
KNOWLEDGE_SCOPES = {"repository", "global"}
SAFE_RESOLUTION_STATUSES = {"promoted", "discarded", "proposed"}
DEFERRED_CAPTURE_DIR = "deferred-captures"
DEFERRED_AUDIT_DIR = "deferred-audits"
AUDIT_CHECKPOINT_SCHEMA_VERSION = 1
AUDIT_THRESHOLD_ENV = "CODEX_GARDENER_AUDIT_THRESHOLD"
AUDIT_MAX_DAYS_ENV = "CODEX_GARDENER_AUDIT_MAX_DAYS"
DEFAULT_AUDIT_THRESHOLD = 10
DEFAULT_AUDIT_MAX_DAYS = 7
SCHEDULED_AUDIT_MARKER = "[codex-gardener:scheduled-audit]"
AUDIT_REASONS = {"review_threshold", "elapsed_time", "forced", "scheduled"}
CORRECTION_RE = re.compile(
    r"(?:不对|错了|纠正|我说的是|你忘了|没有按|不是.{0,20}而是|"
    r"\bwrong\b|\bincorrect\b|that's not|that is not|i said|you forgot|not what i asked)",
    re.IGNORECASE,
)
TEST_RE = re.compile(
    r"(?:\bpytest\b|\bunittest\b|\bnpm\s+(?:run\s+)?test\b|\bpnpm\s+(?:run\s+)?test\b|"
    r"\byarn\s+test\b|\bdotnet\s+test\b|\bcargo\s+test\b|\bgo\s+test\b|"
    r"\bgradle\w*\s+test\b|\bmvn\w*\s+test\b)",
    re.IGNORECASE,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("hook input must be a JSON object")
    return parsed


def emit_json(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def plugin_data_root() -> Path:
    root = effectiveness.plugin_data_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def positive_int_env(name: str, default: int, maximum: int = 1_000_000) -> int:
    try:
        value = int(os.environ.get(name, str(default)).strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return value if 0 < value <= maximum else default


def parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def audit_checkpoint_path() -> Path:
    return plugin_data_root() / "audit-checkpoint.json"


def valid_audit_checkpoint(value: Any) -> bool:
    allowed = {
        "schema_version",
        "initialized_at",
        "last_successful_audit_at",
        "last_successful_audit_session",
        "last_successful_audit_completion",
    }
    if (
        not isinstance(value, dict)
        or set(value) != allowed
        or value.get("schema_version") != AUDIT_CHECKPOINT_SCHEMA_VERSION
    ):
        return False
    if parse_utc(value.get("initialized_at")) is None:
        return False
    completed = value.get("last_successful_audit_at")
    session_hash = value.get("last_successful_audit_session")
    completion_hash = value.get("last_successful_audit_completion")
    if completed is None:
        return session_hash is None and completion_hash is None
    return (
        parse_utc(completed) is not None
        and bool(re.fullmatch(r"[0-9a-f]{24}", str(session_hash or "")))
        and bool(re.fullmatch(r"[0-9a-f]{24}", str(completion_hash or "")))
    )


def load_audit_checkpoint(*, initialize: bool, now: datetime) -> tuple[dict[str, Any] | None, str]:
    path = audit_checkpoint_path()
    existing = load_json_file(path, None)
    if valid_audit_checkpoint(existing):
        return existing, "existing"
    checkpoint = {
        "schema_version": AUDIT_CHECKPOINT_SCHEMA_VERSION,
        "initialized_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "last_successful_audit_at": None,
        "last_successful_audit_session": None,
        "last_successful_audit_completion": None,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            current = load_json_file(path, None)
            if valid_audit_checkpoint(current):
                return current, "existing"
            if path.exists():
                return None, "corrupt"
            if not initialize:
                return None, "missing"
            atomic_write_json(path, checkpoint)
    except (OSError, TimeoutError):
        return None, "unavailable"
    return checkpoint, "initialized"


def audit_status(*, initialize: bool = False, now: datetime | None = None) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    threshold = positive_int_env(AUDIT_THRESHOLD_ENV, DEFAULT_AUDIT_THRESHOLD)
    max_days = positive_int_env(AUDIT_MAX_DAYS_ENV, DEFAULT_AUDIT_MAX_DAYS, 36_500)
    checkpoint, checkpoint_status = load_audit_checkpoint(initialize=initialize, now=current)
    result: dict[str, Any] = {
        "schema_version": AUDIT_CHECKPOINT_SCHEMA_VERSION,
        "available": checkpoint is not None,
        "checkpoint_status": checkpoint_status,
        "run_kind": effectiveness.current_run_kind(),
        "review_threshold": threshold,
        "max_days": max_days,
        "qualifying_reviews": 0,
        "deadline_at": None,
        "due": False,
        "reason": "checkpoint_unavailable" if checkpoint is None else "not_due",
        "checkpoint": None,
    }
    if checkpoint is None:
        return result
    baseline_text = checkpoint.get("last_successful_audit_at") or checkpoint["initialized_at"]
    baseline = parse_utc(baseline_text)
    if baseline is None:
        return result
    requests: dict[str, datetime] = {}
    terminals: dict[str, datetime] = {}
    events, _ = effectiveness.read_events(root=plugin_data_root())
    for event in events:
        if event.get("run_kind") != "real":
            continue
        created = parse_utc(event.get("created_at"))
        session = str(event.get("session") or "")
        if created is None or created < baseline or not session:
            continue
        if event.get("event") == "review_requested":
            prior = requests.get(session)
            requests[session] = min(prior, created) if prior else created
        elif event.get("event") in {"capture_recorded", "review_completed_no_candidate"}:
            prior = terminals.get(session)
            terminals[session] = max(prior, created) if prior else created
    count = sum(
        1
        for session, requested_at in requests.items()
        if session in terminals and terminals[session] >= requested_at
    )
    deadline = baseline + timedelta(days=max_days)
    due = count >= threshold or current >= deadline
    reason = "review_threshold" if count >= threshold else "elapsed_time" if current >= deadline else "not_due"
    result.update(
        {
            "qualifying_reviews": count,
            "deadline_at": deadline.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "due": due,
            "reason": reason,
            "checkpoint": checkpoint,
        }
    )
    return result


def safe_name(value: str) -> str:
    return sha256_text(value)[:24]


def session_identity(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def project_identity(repo: Path | None) -> str | None:
    if repo is None:
        return None
    with contextlib.suppress(OSError):
        return os.path.normcase(str(repo.resolve()))
    return str(repo)


def log_effectiveness(event: str, **fields: Any) -> None:
    effectiveness.log_event(event, **fields)


def state_path(session_id: str) -> Path:
    path = plugin_data_root() / "state" / f"{safe_name(session_id)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 2.0) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {lock_path}")
            time.sleep(0.025)
    try:
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def run_git(cwd: Path, args: list[str], timeout: float = 1.5) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def repo_root(cwd: Path) -> Path | None:
    raw = run_git(cwd, ["rev-parse", "--show-toplevel"])
    if not raw:
        return None
    return Path(raw.decode("utf-8", errors="replace").strip()).resolve()


def git_snapshot(cwd: Path) -> str | None:
    root = repo_root(cwd)
    if root is None:
        return None
    status = run_git(root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    diff = run_git(root, ["diff", "--no-ext-diff", "--binary", "HEAD"], timeout=2.0)
    if status is None:
        return None
    digest = hashlib.sha256()
    digest.update(str(root).casefold().encode("utf-8"))
    digest.update(b"\0")
    digest.update(status)
    digest.update(b"\0")
    if diff is not None:
        digest.update(diff)
    return digest.hexdigest()


def new_state(payload: dict[str, Any]) -> dict[str, Any]:
    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    root = repo_root(cwd)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": str(payload.get("session_id") or "unknown"),
        "cwd": str(cwd),
        "repo_root": str(root) if root else None,
        "turn_id": payload.get("turn_id"),
        "baseline_git": git_snapshot(cwd),
        "correction_signal": False,
        "edit_signal": False,
        "failure_count": 0,
        "repeated_tool_signal": False,
        "test_signal": False,
        "tool_counts": {},
        "review_requested": False,
        "capture_completed": False,
        "audit_requested": False,
        "audit_completed": False,
        "audit_reason": None,
        "continuation_kind": None,
        "force_audit": False,
        "updated_at": utc_now(),
    }


def load_state(payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    session_id = str(payload.get("session_id") or "unknown")
    path = state_path(session_id)
    state = load_json_file(path, None)
    if not isinstance(state, dict):
        state = new_state(payload)
    return path, state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_write_json(path, state)


def signal_names(state: dict[str, Any], cwd: Path | None = None) -> list[str]:
    names: list[str] = []
    if cwd is not None:
        current = git_snapshot(cwd)
        baseline = state.get("baseline_git")
        if current is not None and baseline is not None and current != baseline:
            state["edit_signal"] = True
    if state.get("edit_signal"):
        names.append("workspace changed")
    if state.get("correction_signal"):
        names.append("user correction")
    if int(state.get("failure_count") or 0) >= 2:
        names.append("repeated failures")
    if state.get("repeated_tool_signal"):
        names.append("repeated tool workflow")
    return names


def safe_signal_categories(state: dict[str, Any]) -> list[str]:
    categories: list[str] = []
    if state.get("edit_signal"):
        categories.append("workspace_changed")
    if state.get("correction_signal"):
        categories.append("user_correction")
    if int(state.get("failure_count") or 0) >= 2:
        categories.append("repeated_failures")
    if state.get("repeated_tool_signal"):
        categories.append("repeated_tool_workflow")
    return categories


def confidence_bucket(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


def target_category(target_path: str) -> str:
    lowered = target_path.replace("\\", "/").casefold()
    if "agents.md" in lowered:
        return "agents"
    if "/skills/" in f"/{lowered.strip('/')}" or lowered.endswith("skill.md"):
        return "skill"
    if "test" in lowered:
        return "test"
    if "hook" in lowered:
        return "hook"
    if lowered.endswith((".md", ".rst", ".txt")) or "/docs/" in f"/{lowered.strip('/')}":
        return "docs"
    return "other"


def tool_failed(response: Any) -> bool:
    if isinstance(response, dict):
        if response.get("isError") is True or response.get("is_error") is True:
            return True
        for key in ("exit_code", "exitCode", "returncode"):
            if key in response:
                with contextlib.suppress(TypeError, ValueError):
                    return int(response[key]) != 0
        if str(response.get("status", "")).casefold() in {"error", "failed", "failure"}:
            return True
    rendered = json.dumps(response, ensure_ascii=False) if not isinstance(response, str) else response
    match = re.search(r"exit\s+code\s*[:=]\s*(-?\d+)", rendered, re.IGNORECASE)
    return bool(match and int(match.group(1)) != 0)


def tool_command(tool_name: str, tool_input: Any) -> str:
    if tool_name.casefold() not in {"bash", "shell", "shell_command", "apply_patch"}:
        return ""
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or "")
    return ""


def tool_mutates(tool_name: str, command: str) -> bool:
    if tool_name.casefold() in {"apply_patch", "edit", "write"}:
        return True
    return bool(
        command
        and re.search(
            r"(?:\bapply_patch\b|\bgit\s+(?:add|commit|mv|rm)\b|\b(?:rm|mv|cp|mkdir|touch)\b|"
            r"\bsed\s+-i\b|\b(?:new-item|set-content|add-content|remove-item|move-item|copy-item)\b)",
            command,
            re.IGNORECASE,
        )
    )


def normalize_knowledge_scope(value: Any) -> str:
    scope = str(value or "repository").casefold()
    if scope not in KNOWLEDGE_SCOPES:
        raise ValueError(f"knowledge scope must be one of: {', '.join(sorted(KNOWLEDGE_SCOPES))}")
    return scope


def stored_knowledge_scope(value: Any) -> str:
    scope = str(value or "repository").casefold()
    return scope if scope in KNOWLEDGE_SCOPES else "repository"


def learning_dir(repo: Path, knowledge_scope: str = "repository") -> Path:
    if normalize_knowledge_scope(knowledge_scope) == "global":
        return codex_home() / "codex-gardener-global-learning"
    return repo / ".codex" / "learning"


def normalized_global_target_path(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    normalized = value.replace("\\", "/").casefold()
    if normalized in LEGACY_DELEGATION_SKILL_TARGETS:
        return BUNDLED_DELEGATION_SKILL_TARGET
    return value


def _normalized_global_target_record(filename: str, raw: dict[str, Any]) -> dict[str, Any]:
    record = dict(raw)
    if filename in {"index.jsonl", "resolutions.jsonl"} and "target_path" in record:
        record["target_path"] = normalized_global_target_path(record["target_path"])
    return record


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            return None
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        records.append(record)
    return records


def _atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)


def migrate_global_delegation_targets(target: Path) -> dict[str, int]:
    changed: dict[str, int] = {}
    for filename in ("index.jsonl", "resolutions.jsonl"):
        path = target / filename
        if not path.is_file():
            continue
        try:
            with file_lock(path):
                current = _read_jsonl_strict(path)
                if current is None:
                    changed[filename] = 0
                    continue
                migrated = [_normalized_global_target_record(filename, record) for record in current]
                updates = sum(
                    1
                    for before, after in zip(current, migrated)
                    if before.get("target_path") != after.get("target_path")
                )
                if updates:
                    _atomic_write_jsonl(path, migrated)
                changed[filename] = updates
        except (OSError, TimeoutError):
            changed[filename] = 0
    return changed


def _migration_key(filename: str, record: dict[str, Any]) -> tuple[str, ...]:
    if filename == "inbox.jsonl":
        return (str(record.get("fingerprint") or ""), str(record.get("session_id") or "unknown"))
    if filename == "index.jsonl":
        return (str(record.get("fingerprint") or ""),)
    return (
        str(record.get("fingerprint") or ""),
        str(record.get("status") or ""),
        str(record.get("created_at") or ""),
    )


def _migrated_legacy_record(filename: str, raw: dict[str, Any]) -> dict[str, Any]:
    record = _normalized_global_target_record(filename, raw)
    record["schema_version"] = SCHEMA_VERSION
    record["knowledge_scope"] = "global"
    record["migration_provenance"] = LEGACY_MIGRATION_PROVENANCE
    if filename == "inbox.jsonl":
        record.setdefault("id", "legacy-" + sha256_text(json_dump(raw))[:24])
        record.setdefault("session_id", "legacy-unknown")
    return record


def migrate_legacy_global_learning() -> dict[str, int]:
    legacy = codex_home() / "learning"
    target = codex_home() / "codex-gardener-global-learning"
    result: dict[str, int] = {}
    if legacy.resolve() == target.resolve():
        return result
    if not legacy.is_dir():
        migrate_global_delegation_targets(target)
        return result
    target.mkdir(parents=True, exist_ok=True)
    ignore = target / ".gitignore"
    with contextlib.suppress(OSError):
        existing_ignore = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
        missing_ignore = [
            name
            for name in ("inbox.jsonl", "index.jsonl", "resolutions.jsonl")
            if name not in existing_ignore
        ]
        if missing_ignore:
            ignore.write_text(
                "\n".join([*existing_ignore, *missing_ignore]).rstrip() + "\n",
                encoding="utf-8",
                newline="\n",
            )
    for filename in ("inbox.jsonl", "index.jsonl", "resolutions.jsonl"):
        source_records = read_jsonl(legacy / filename)
        if not source_records:
            continue
        path = target / filename
        try:
            with file_lock(path):
                current = _read_jsonl_strict(path) if path.is_file() else []
                if current is None:
                    result[filename] = 0
                    continue
                known = {_migration_key(filename, item) for item in current}
                additions: list[dict[str, Any]] = []
                for raw in source_records:
                    migrated = _migrated_legacy_record(filename, raw)
                    key = _migration_key(filename, migrated)
                    if not key[0] or key in known:
                        continue
                    known.add(key)
                    additions.append(migrated)
                if additions:
                    _atomic_write_jsonl(path, [*current, *additions])
                result[filename] = len(additions)
        except (OSError, TimeoutError):
            result[filename] = 0
    migrate_global_delegation_targets(target)
    return result


def read_learning_records(repo: Path, knowledge_scope: str, filename: str) -> list[dict[str, Any]]:
    knowledge_scope = normalize_knowledge_scope(knowledge_scope)
    current = read_jsonl(learning_dir(repo, knowledge_scope) / filename)
    if knowledge_scope != "global":
        return current
    current = [_normalized_global_target_record(filename, record) for record in current]
    known = {_migration_key(filename, item) for item in current}
    for raw in read_jsonl(codex_home() / "learning" / filename):
        migrated = _migrated_legacy_record(filename, raw)
        key = _migration_key(filename, migrated)
        if not key[0] or key in known:
            continue
        known.add(key)
        current.append(migrated)
    return current


def ensure_learning_dir(repo: Path, knowledge_scope: str = "repository") -> Path:
    if normalize_knowledge_scope(knowledge_scope) == "global":
        migrate_legacy_global_learning()
    root = learning_dir(repo, knowledge_scope)
    root.mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    required = ["inbox.jsonl", "index.jsonl", "resolutions.jsonl"]
    existing = []
    if ignore.is_file():
        existing = ignore.read_text(encoding="utf-8").splitlines()
    missing = [entry for entry in required if entry not in existing]
    if missing:
        content = existing + missing
        ignore.write_text("\n".join(content).rstrip() + "\n", encoding="utf-8", newline="\n")
    return root


def project_fingerprint(repo: Path) -> str:
    root = repo_root(repo) or repo.resolve()
    remote = run_git(root, ["config", "--get", "remote.origin.url"])
    if remote:
        identity = "remote\0" + remote.decode("utf-8", errors="replace").strip().casefold()
    else:
        identity = "path\0" + os.path.normcase(str(root.resolve()))
    return sha256_text(identity)[:24]


def normalize_lesson(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def candidate_fingerprint(scope: str, lesson: str, target: str) -> str:
    return sha256_text(f"{scope.strip().casefold()}\0{normalize_lesson(lesson)}\0{target}")[:24]


def normalized_candidate_fields(args: argparse.Namespace) -> dict[str, Any]:
    knowledge_scope = normalize_knowledge_scope(getattr(args, "knowledge_scope", "repository"))
    session_id = str(args.session_id).strip()
    scope = str(args.scope).strip()
    lesson = str(args.lesson).strip()
    evidence = str(args.evidence).strip()
    target = str(args.target).casefold()
    if target not in TARGETS:
        raise ValueError(f"target must be one of: {', '.join(sorted(TARGETS))}")
    confidence = float(args.confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    for name, value, limit in (
        ("session-id", session_id, 256),
        ("scope", scope, 160),
        ("lesson", lesson, 2000),
        ("evidence", evidence, 2000),
    ):
        if not value:
            raise ValueError(f"{name} must not be empty")
        if len(value) > limit:
            raise ValueError(f"{name} must not exceed {limit} characters")
    return {
        "session_id": session_id,
        "knowledge_scope": knowledge_scope,
        "scope": scope,
        "lesson": lesson,
        "evidence": evidence,
        "target": target,
        "confidence": confidence,
    }


def deferred_capture_dir(repo: Path, session_id: str) -> Path:
    return repo.resolve() / ".codex" / "learning" / DEFERRED_CAPTURE_DIR / safe_name(session_id)


def deferred_audit_dir(repo: Path, session_id: str) -> Path:
    return repo.resolve() / ".codex" / "learning" / DEFERRED_AUDIT_DIR / safe_name(session_id)


def ensure_deferred_audit_dir(repo: Path, session_id: str) -> Path:
    learning = repo.resolve() / ".codex" / "learning"
    directory = deferred_audit_dir(repo, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    ignore = learning / ".gitignore"
    existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    ignored = f"{DEFERRED_AUDIT_DIR}/"
    if ignored not in existing:
        ignore.write_text("\n".join([*existing, ignored]).rstrip() + "\n", encoding="utf-8", newline="\n")
    return directory


def defer_audit_completion(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    session_id = str(args.session_id).strip()
    if not session_id or len(session_id) > 256:
        raise ValueError("session-id must contain 1 to 256 characters")
    run_kind = effectiveness.current_run_kind()
    marker = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "deferred_audit_complete",
        "session_id": session_id,
        "run_kind": run_kind,
        "completion_id": str(uuid.uuid4()),
        "created_at": utc_now(),
    }
    path = ensure_deferred_audit_dir(repo, session_id) / "audit-complete.json"
    atomic_write_json(path, marker)
    return {"deferred": True, "run_kind": run_kind}


def complete_audit_checkpoint(
    repo: Path,
    session_id: str,
    audit_reason: str,
    run_kind: str,
    completion_id: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, bool]:
    if audit_reason not in AUDIT_REASONS or run_kind not in {"real", "smoke"}:
        return False, False
    try:
        uuid.UUID(completion_id)
    except (AttributeError, TypeError, ValueError):
        return False, False
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if run_kind == "real":
        path = audit_checkpoint_path()
        session_hash = effectiveness.hash_identifier(session_id, "session")
        completion_hash = effectiveness.hash_identifier(completion_id, "audit-completion")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(path):
                checkpoint = load_json_file(path, None)
                if checkpoint is None and not path.exists():
                    checkpoint = {
                        "schema_version": AUDIT_CHECKPOINT_SCHEMA_VERSION,
                        "initialized_at": current.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        "last_successful_audit_at": None,
                        "last_successful_audit_session": None,
                        "last_successful_audit_completion": None,
                    }
                if not valid_audit_checkpoint(checkpoint):
                    return False, False
                if checkpoint.get("last_successful_audit_completion") == completion_hash:
                    return True, False
                checkpoint["last_successful_audit_at"] = current.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                )
                checkpoint["last_successful_audit_session"] = session_hash
                checkpoint["last_successful_audit_completion"] = completion_hash
                atomic_write_json(path, checkpoint)
        except (OSError, TimeoutError):
            return False, False
    log_effectiveness(
        "audit_completed",
        session=session_identity(session_id),
        project=project_identity(repo),
        audit_reason=audit_reason,
        run_kind=run_kind,
    )
    return True, True


def consume_deferred_audits(repo: Path, session_id: str, audit_reason: str) -> tuple[int, int]:
    processed = 0
    invalid = 0
    directory = deferred_audit_dir(repo, session_id)
    for marker_path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        marker = load_json_file(marker_path, None)
        allowed = {"schema_version", "record_type", "session_id", "run_kind", "completion_id", "created_at"}
        if (
            not isinstance(marker, dict)
            or set(marker) - allowed
            or marker.get("schema_version") != SCHEMA_VERSION
            or marker.get("record_type") != "deferred_audit_complete"
            or str(marker.get("session_id") or "") != session_id
            or marker.get("run_kind") != effectiveness.current_run_kind()
            or parse_utc(marker.get("created_at")) is None
        ):
            invalid += 1
            continue
        completed, _ = complete_audit_checkpoint(
            repo,
            session_id,
            audit_reason if audit_reason in AUDIT_REASONS else "scheduled",
            str(marker["run_kind"]),
            str(marker.get("completion_id") or ""),
        )
        if not completed:
            invalid += 1
            continue
        with contextlib.suppress(FileNotFoundError):
            marker_path.unlink()
        processed += 1
    with contextlib.suppress(OSError):
        directory.rmdir()
    return processed, invalid


def ensure_deferred_capture_dir(repo: Path, session_id: str) -> Path:
    learning = repo.resolve() / ".codex" / "learning"
    directory = deferred_capture_dir(repo, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    ignore = learning / ".gitignore"
    existing = ignore.read_text(encoding="utf-8").splitlines() if ignore.is_file() else []
    ignored = f"{DEFERRED_CAPTURE_DIR}/"
    if ignored not in existing:
        ignore.write_text("\n".join([*existing, ignored]).rstrip() + "\n", encoding="utf-8", newline="\n")
    return directory


def defer_candidate(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    fields = normalized_candidate_fields(args)
    fingerprint = candidate_fingerprint(fields["scope"], fields["lesson"], fields["target"])
    marker = {
        "schema_version": SCHEMA_VERSION,
        "record_type": "deferred_capture",
        "fingerprint": fingerprint,
        "session_id": fields["session_id"],
        "knowledge_scope": fields["knowledge_scope"],
        "scope": fields["scope"],
        "lesson": fields["lesson"],
        "evidence_summary": fields["evidence"],
        "recommended_target": fields["target"],
        "confidence": fields["confidence"],
        "created_at": utc_now(),
    }
    path = ensure_deferred_capture_dir(repo, fields["session_id"]) / f"{fingerprint}.json"
    atomic_write_json(path, marker)
    return {
        "deferred": True,
        "fingerprint": fingerprint,
        "knowledge_scope": fields["knowledge_scope"],
        "recommended_target": fields["target"],
    }


def append_candidate_once(path: Path, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        for existing in read_jsonl(path):
            if (
                existing.get("fingerprint") == record["fingerprint"]
                and str(existing.get("session_id") or "") == record["session_id"]
                and stored_knowledge_scope(existing.get("knowledge_scope")) == record["knowledge_scope"]
            ):
                return existing, False
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record, True


def record_candidate(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    fields = normalized_candidate_fields(args)
    fingerprint = candidate_fingerprint(fields["scope"], fields["lesson"], fields["target"])
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "fingerprint": fingerprint,
        "session_id": fields["session_id"],
        "knowledge_scope": fields["knowledge_scope"],
        "project_fingerprint": project_fingerprint(repo),
        "scope": fields["scope"],
        "lesson": fields["lesson"],
        "evidence_summary": fields["evidence"],
        "recommended_target": fields["target"],
        "confidence": fields["confidence"],
        "created_at": utc_now(),
    }
    stored, created = append_candidate_once(
        ensure_learning_dir(repo, fields["knowledge_scope"]) / "inbox.jsonl",
        record,
    )
    mark_review_complete(fields["session_id"])
    if created:
        log_effectiveness(
            "capture_recorded",
            session=session_identity(fields["session_id"]),
            project=project_identity(repo),
            knowledge_scope=fields["knowledge_scope"],
            recommended_target=fields["target"],
            confidence_bucket=confidence_bucket(fields["confidence"]),
        )
    return stored


def candidate_args_from_marker(repo: Path, expected_session_id: str, marker: dict[str, Any]) -> argparse.Namespace:
    allowed = {
        "schema_version",
        "record_type",
        "fingerprint",
        "session_id",
        "knowledge_scope",
        "scope",
        "lesson",
        "evidence_summary",
        "recommended_target",
        "confidence",
        "created_at",
    }
    if set(marker) - allowed:
        raise ValueError("deferred capture contains unsupported fields")
    if marker.get("schema_version") != SCHEMA_VERSION or marker.get("record_type") != "deferred_capture":
        raise ValueError("invalid deferred capture schema")
    if str(marker.get("session_id") or "") != expected_session_id:
        raise ValueError("deferred capture session does not match Hook state")
    args = argparse.Namespace(
        repo=str(repo),
        session_id=expected_session_id,
        knowledge_scope=marker.get("knowledge_scope"),
        scope=marker.get("scope"),
        lesson=marker.get("lesson"),
        evidence=marker.get("evidence_summary"),
        target=marker.get("recommended_target"),
        confidence=marker.get("confidence"),
    )
    fields = normalized_candidate_fields(args)
    fingerprint = candidate_fingerprint(fields["scope"], fields["lesson"], fields["target"])
    if marker.get("fingerprint") != fingerprint:
        raise ValueError("deferred capture fingerprint is invalid")
    return args


def consume_deferred_candidates(repo: Path, session_id: str) -> tuple[int, int]:
    processed = 0
    invalid = 0
    directory = deferred_capture_dir(repo, session_id)
    for marker_path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        marker = load_json_file(marker_path, None)
        if not isinstance(marker, dict):
            invalid += 1
            continue
        try:
            args = candidate_args_from_marker(repo, session_id, marker)
            record_candidate(args)
        except (OSError, TimeoutError, TypeError, ValueError):
            invalid += 1
            continue
        with contextlib.suppress(FileNotFoundError):
            marker_path.unlink()
        processed += 1
    with contextlib.suppress(OSError):
        directory.rmdir()
    return processed, invalid


def aggregate_candidates(repo: Path, knowledge_scope: str = "repository") -> list[dict[str, Any]]:
    knowledge_scope = normalize_knowledge_scope(knowledge_scope)
    records = [
        record
        for record in read_learning_records(repo, knowledge_scope, "inbox.jsonl")
        if stored_knowledge_scope(record.get("knowledge_scope")) == knowledge_scope
    ]
    resolutions = [
        resolution
        for resolution in read_learning_records(repo, knowledge_scope, "resolutions.jsonl")
        if stored_knowledge_scope(resolution.get("knowledge_scope")) == knowledge_scope
    ]
    latest_resolution = {
        str(item.get("fingerprint")): item
        for item in resolutions
        if item.get("fingerprint") and str(item.get("status") or "") in SAFE_RESOLUTION_STATUSES
    }
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        fingerprint = record.get("fingerprint")
        if fingerprint:
            grouped[str(fingerprint)].append(record)
    result: list[dict[str, Any]] = []
    for fingerprint, items in grouped.items():
        by_session: dict[str, dict[str, Any]] = {}
        for item in items:
            session = str(item.get("session_id") or "unknown")
            current = by_session.get(session)
            if current is None or float(item.get("confidence") or 0) > float(current.get("confidence") or 0):
                by_session[session] = item
        unique = list(by_session.values())
        confidence = sum(float(item.get("confidence") or 0) for item in unique) / len(unique)
        projects = sorted(
            {
                str(item.get("project_fingerprint"))
                for item in unique
                if item.get("project_fingerprint")
            }
        )
        evidence_status = "candidate"
        if (
            len(unique) >= 3
            and confidence >= 0.85
            and (knowledge_scope == "repository" or len(projects) >= 2)
        ):
            evidence_status = "promotable"
        elif knowledge_scope == "global" and len(unique) >= 3 and confidence >= 0.85:
            evidence_status = "proposed"
        elif len(unique) >= 2:
            evidence_status = "confirmed"
        exemplar = max(unique, key=lambda item: str(item.get("created_at") or ""))
        resolution = latest_resolution.get(fingerprint)
        resolution_status = str(resolution.get("status") or "") if resolution else ""
        status = resolution_status if resolution_status in SAFE_RESOLUTION_STATUSES else evidence_status
        result.append(
            {
                "fingerprint": fingerprint,
                "status": status,
                "evidence_status": evidence_status,
                "occurrences": len(unique),
                "confidence": round(confidence, 4),
                "knowledge_scope": knowledge_scope,
                "scope": exemplar.get("scope"),
                "lesson": exemplar.get("lesson"),
                "recommended_target": exemplar.get("recommended_target"),
                "evidence": [item.get("evidence_summary") for item in unique if item.get("evidence_summary")],
                "session_ids": sorted(by_session),
                "project_fingerprints": projects,
                "project_count": len(projects),
                "migration_provenance": exemplar.get("migration_provenance"),
                "resolution": resolution,
            }
        )
    return sorted(result, key=lambda item: (-item["occurrences"], item["fingerprint"]))


def update_index(repo: Path, resolution: dict[str, Any], knowledge_scope: str = "repository") -> None:
    knowledge_scope = normalize_knowledge_scope(knowledge_scope)
    root = ensure_learning_dir(repo, knowledge_scope)
    path = root / "index.jsonl"
    entries = read_jsonl(path)
    entries = [item for item in entries if item.get("fingerprint") != resolution["fingerprint"]]
    entries.append(
        {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": resolution["fingerprint"],
            "knowledge_scope": knowledge_scope,
            "summary": resolution["summary"],
            "keywords": resolution["keywords"],
            "target_path": resolution["target_path"],
            "promoted_at": resolution["created_at"],
        }
    )
    with file_lock(path):
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(
            "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in entries),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temp, path)


def resolve_candidate(args: argparse.Namespace) -> dict[str, Any]:
    if args.status not in SAFE_RESOLUTION_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(SAFE_RESOLUTION_STATUSES))}")
    repo = Path(args.repo).resolve()
    knowledge_scope = normalize_knowledge_scope(getattr(args, "knowledge_scope", "repository"))
    resolution = {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": args.fingerprint,
        "knowledge_scope": knowledge_scope,
        "status": args.status,
        "summary": (args.summary or "").strip(),
        "keywords": sorted(set(args.keyword or [])),
        "target_path": (args.target_path or "").strip(),
        "created_at": utc_now(),
    }
    append_jsonl(ensure_learning_dir(repo, knowledge_scope) / "resolutions.jsonl", resolution)
    if args.status == "promoted" and resolution["summary"] and resolution["target_path"]:
        update_index(repo, resolution, knowledge_scope)
    log_effectiveness(
        "resolution_recorded",
        project=project_identity(repo),
        knowledge_scope=knowledge_scope,
        status=args.status,
        target=target_category(resolution["target_path"]),
    )
    return resolution


def promoted_context_result(repo: Path, prompt: str) -> tuple[str | None, dict[str, int]]:
    combined: dict[str, dict[str, Any]] = {}
    available = {"repository": 0, "global": 0}
    for knowledge_scope in ("global", "repository"):
        for raw_entry in read_learning_records(repo, knowledge_scope, "index.jsonl"):
            entry = dict(raw_entry)
            entry["knowledge_scope"] = stored_knowledge_scope(entry.get("knowledge_scope"))
            fingerprint = str(entry.get("fingerprint") or "")
            if fingerprint:
                available[knowledge_scope] += 1
                combined[fingerprint] = entry
    entries = list(combined.values())
    lowered = prompt.casefold()
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in entries:
        keywords = [str(item).casefold() for item in entry.get("keywords", []) if str(item).strip()]
        score = sum(1 for keyword in keywords if keyword in lowered)
        if score:
            scored.append((score, entry))
    if not scored:
        return None, {
            "repository_available": available["repository"],
            "global_available": available["global"],
            "repository_hits": 0,
            "global_hits": 0,
            "injected": 0,
        }
    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("promoted_at") or "")), reverse=False)
    selected = scored[:3]
    lines = ["Codex Gardener found relevant promoted knowledge:"]
    hits = {"repository": 0, "global": 0}
    for _, entry in selected:
        hits[str(entry["knowledge_scope"])] += 1
        lines.append(
            f"- [{entry.get('knowledge_scope')}] {entry.get('summary')} "
            f"(source: {entry.get('target_path')})"
        )
    return "\n".join(lines), {
        "repository_available": available["repository"],
        "global_available": available["global"],
        "repository_hits": hits["repository"],
        "global_hits": hits["global"],
        "injected": len(selected),
    }


def promoted_context(repo: Path, prompt: str) -> str | None:
    context, _ = promoted_context_result(repo, prompt)
    return context


def pending_path() -> Path:
    return plugin_data_root() / "pending.jsonl"


def pending_records(repo: Path | None = None) -> list[dict[str, Any]]:
    records = read_jsonl(pending_path())
    resolved = {
        str(record.get("session_id"))
        for record in records
        if record.get("record_type") == "resolved" and record.get("session_id")
    }
    records = [
        record
        for record in records
        if record.get("record_type", "pending") == "pending"
        and str(record.get("session_id")) not in resolved
    ]
    if repo is None:
        return records
    wanted = str(repo.resolve()).casefold()
    return [record for record in records if str(record.get("repo_root") or record.get("cwd") or "").casefold() == wanted]


def resolve_pending(session_id: str) -> None:
    matching = next(
        (
            record
            for record in reversed(read_jsonl(pending_path()))
            if record.get("record_type", "pending") == "pending"
            and str(record.get("session_id")) == session_id
        ),
        {},
    )
    append_jsonl(
        pending_path(),
        {
            "schema_version": SCHEMA_VERSION,
            "record_type": "resolved",
            "session_id": session_id,
            "created_at": utc_now(),
        },
    )
    raw_project = matching.get("repo_root") or matching.get("cwd")
    project = project_identity(Path(str(raw_project))) if raw_project else None
    log_effectiveness(
        "pending_resolved",
        session=session_identity(session_id),
        project=project,
    )


def mark_review_complete(session_id: str) -> None:
    path = state_path(session_id)
    state = load_json_file(path, None)
    if isinstance(state, dict):
        state["capture_completed"] = True
        save_state(path, state)


def complete_review_without_candidate(session_id: str) -> None:
    path = state_path(session_id)
    state = load_json_file(path, None)
    mark_review_complete(session_id)
    raw_project = (state.get("repo_root") or state.get("cwd")) if isinstance(state, dict) else None
    project = project_identity(Path(str(raw_project))) if raw_project else None
    log_effectiveness(
        "review_completed_no_candidate",
        session=session_identity(session_id),
        project=project,
    )


def handle_session_start(payload: dict[str, Any]) -> None:
    migrate_legacy_global_learning()
    audit_status(initialize=True)
    path, state = load_state(payload)
    state = new_state(payload)
    save_state(path, state)
    root_value = state.get("repo_root")
    log_effectiveness(
        "session_start",
        session=session_identity(state.get("session_id")),
        project=project_identity(Path(str(root_value))) if root_value else None,
    )
    if root_value:
        count = len(pending_records(Path(root_value)))
        if count:
            emit_json(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": (
                            f"Codex Gardener has {count} unreviewed prior session(s) for this repository. "
                            "Use $codex-gardener:knowledge-curator when retrospective cleanup is appropriate."
                        ),
                    }
                }
            )


def handle_user_prompt(payload: dict[str, Any]) -> None:
    path, state = load_state(payload)
    prompt = str(payload.get("prompt") or "")
    turn_id = payload.get("turn_id")
    if turn_id != state.get("turn_id"):
        cwd = Path(str(state.get("cwd") or payload.get("cwd") or os.getcwd()))
        state.update(
            {
                "turn_id": turn_id,
                "baseline_git": git_snapshot(cwd),
                "correction_signal": bool(CORRECTION_RE.search(prompt)),
                "edit_signal": False,
                "failure_count": 0,
                "repeated_tool_signal": False,
                "test_signal": False,
                "tool_counts": {},
                "review_requested": False,
                "capture_completed": False,
                "audit_requested": False,
                "audit_completed": False,
                "audit_reason": None,
                "continuation_kind": None,
                "force_audit": SCHEDULED_AUDIT_MARKER in prompt,
            }
        )
    else:
        if CORRECTION_RE.search(prompt):
            state["correction_signal"] = True
        if SCHEDULED_AUDIT_MARKER in prompt:
            state["force_audit"] = True
    save_state(path, state)
    lookup_root = Path(str(state.get("repo_root") or state.get("cwd") or payload.get("cwd") or os.getcwd()))
    context, metrics = promoted_context_result(lookup_root, prompt)
    log_effectiveness(
        "context_lookup",
        session=session_identity(state.get("session_id")),
        project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
        **metrics,
    )
    if context:
        emit_json(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
        )


def handle_post_tool(payload: dict[str, Any]) -> None:
    path, state = load_state(payload)
    name = str(payload.get("tool_name") or "unknown")
    tool_input = payload.get("tool_input")
    fingerprint = sha256_text(name.casefold() + "\0" + json_dump(tool_input))[:24]
    counts = state.setdefault("tool_counts", {})
    counts[fingerprint] = int(counts.get(fingerprint) or 0) + 1
    if name.casefold() not in {"wait", "write_stdin", "functions.wait"} and counts[fingerprint] >= 2:
        state["repeated_tool_signal"] = True
    if tool_failed(payload.get("tool_response")):
        state["failure_count"] = int(state.get("failure_count") or 0) + 1
    command = tool_command(name, tool_input)
    if command and TEST_RE.search(command):
        state["test_signal"] = True
    if tool_mutates(name, command):
        state["edit_signal"] = True
    save_state(path, state)


def handle_stop(payload: dict[str, Any]) -> None:
    path, state = load_state(payload)
    session_id = str(payload.get("session_id") or state.get("session_id") or "unknown")
    repo = Path(str(state.get("repo_root") or state.get("cwd") or payload.get("cwd") or os.getcwd()))
    continuation_kind = str(state.get("continuation_kind") or "")
    audit_processed, audit_invalid = consume_deferred_audits(
        repo,
        session_id,
        str(state.get("audit_reason") or "scheduled"),
    )
    if audit_invalid:
        log_effectiveness("operation_error", operation="audit", category="input")
    if audit_processed:
        state["audit_completed"] = True
        state["continuation_kind"] = None
        save_state(path, state)
    if payload.get("stop_hook_active"):
        if continuation_kind == "audit" or (state.get("audit_requested") and not state.get("review_requested")):
            emit_json({"continue": True})
            return
        processed, invalid = consume_deferred_candidates(repo, session_id)
        if invalid:
            log_effectiveness("operation_error", operation="record", category="input")
        if invalid and not processed and not state.get("capture_completed"):
            emit_json({"continue": True})
            return
        completed_before_stop = bool(state.get("capture_completed"))
        state["capture_completed"] = True
        save_state(path, state)
        if not completed_before_stop and not processed:
            log_effectiveness(
                "review_completed_no_candidate",
                session=session_identity(state.get("session_id")),
                project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
            )
        emit_json({"continue": True})
        return
    cwd = Path(str(state.get("cwd") or payload.get("cwd") or os.getcwd()))
    signals = signal_names(state, cwd)
    if signals and not state.get("review_requested"):
        state["review_requested"] = True
        state["continuation_kind"] = "capture"
        save_state(path, state)
        session_id = str(payload.get("session_id") or state.get("session_id") or "unknown")
        repository = str(state.get("repo_root") or state.get("cwd") or payload.get("cwd") or "")
        log_effectiveness(
            "review_requested",
            session=session_identity(session_id),
            project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
            signals=safe_signal_categories(state),
        )
        reason = (
            "Use $codex-gardener:gardener-capture now to review this completed task for reusable knowledge at the right scope. "
            f"Session ID: {session_id}. Repository: {repository}. Signals: {', '.join(signals)}. "
            "Record only generalizable lessons with concise evidence; do not copy prompts, tool output, secrets, or credentials. "
            "Do not modify AGENTS.md, skills, tests, hooks, or docs during capture. "
            "If nothing is reusable, report that outcome without running a completion command."
        )
        emit_json({"decision": "block", "reason": reason})
        return
    if audit_processed:
        save_state(path, state)
        emit_json({"continue": True})
        return
    status = audit_status(initialize=True)
    should_audit = bool(state.get("force_audit")) or (
        effectiveness.current_run_kind() == "real" and bool(status.get("due"))
    )
    if state.get("review_requested") or state.get("audit_requested") or not should_audit:
        save_state(path, state)
        emit_json({"continue": True})
        return
    audit_reason = "forced" if state.get("force_audit") else str(status.get("reason") or "elapsed_time")
    state["audit_requested"] = True
    state["audit_reason"] = audit_reason
    state["continuation_kind"] = "audit"
    save_state(path, state)
    session_id = str(payload.get("session_id") or state.get("session_id") or "unknown")
    repository = str(state.get("repo_root") or state.get("cwd") or payload.get("cwd") or "")
    log_effectiveness(
        "audit_requested",
        session=session_identity(session_id),
        project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
        audit_reason=audit_reason,
    )
    reason = (
        "Use $codex-gardener:knowledge-curator now in audit-only, read-only mode. "
        f"Session ID: {session_id}. Repository: {repository}. Audit reason: {audit_reason}. "
        "Inspect effectiveness, pending reviews, repository/global candidate scopes, conflicts, and staleness. "
        "Do not promote, resolve, edit, or delete any knowledge artifact. When the audit is complete, write only the "
        "sandbox-safe deferred audit completion marker described by the Skill."
    )
    emit_json({"decision": "block", "reason": reason})


def handle_session_end(payload: dict[str, Any]) -> None:
    path, state = load_state(payload)
    cwd = Path(str(state.get("cwd") or payload.get("cwd") or os.getcwd()))
    signals = signal_names(state, cwd)
    audit_completed_without_capture_request = state.get("audit_completed") and not state.get("review_requested")
    if signals and not state.get("capture_completed") and not audit_completed_without_capture_request:
        session_id = str(payload.get("session_id") or state.get("session_id") or "unknown")
        append_jsonl(
            pending_path(),
            {
                "schema_version": SCHEMA_VERSION,
                "record_type": "pending",
                "session_id": session_id,
                "cwd": str(cwd),
                "repo_root": state.get("repo_root"),
                "transcript_path": payload.get("transcript_path"),
                "signals": signals,
                "created_at": utc_now(),
            },
        )
        log_effectiveness(
            "pending_queued",
            session=session_identity(session_id),
            project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
            signals=safe_signal_categories(state),
        )
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def handle_hook(event: str) -> int:
    payload = read_stdin_json()
    handlers = {
        "SessionStart": handle_session_start,
        "UserPromptSubmit": handle_user_prompt,
        "PostToolUse": handle_post_tool,
        "Stop": handle_stop,
        "SessionEnd": handle_session_end,
    }
    handler = handlers.get(event)
    if handler is None:
        raise ValueError(f"unsupported hook event: {event}")
    handler(payload)
    return 0


def effectiveness_report(since_days: int = 14, repo: Path | None = None) -> dict[str, Any]:
    report = effectiveness.summarize(since_days=since_days)
    report["health"].update(plugin_health())
    report["health"]["audit"] = audit_status(initialize=True)
    report["reviews"]["current_pending"] = len(pending_records(repo))
    if repo is not None:
        group_status: dict[str, dict[str, int]] = {}
        group_evidence_status: dict[str, dict[str, int]] = {}
        for knowledge_scope in ("repository", "global"):
            counts: dict[str, int] = defaultdict(int)
            evidence_counts: dict[str, int] = defaultdict(int)
            for group in aggregate_candidates(repo, knowledge_scope):
                counts[str(group.get("status") or "unknown")] += 1
                evidence_counts[str(group.get("evidence_status") or "unknown")] += 1
            group_status[knowledge_scope] = dict(sorted(counts.items()))
            group_evidence_status[knowledge_scope] = dict(sorted(evidence_counts.items()))
        report["candidate_group_status"] = group_status
        report["candidate_group_evidence_status"] = group_evidence_status
    return report


def _plugin_version() -> str | None:
    manifest = load_json_file(SCRIPT_DIR.parent / ".codex-plugin" / "plugin.json", {})
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return str(version) if version else None


def enabled_gardener_plugin_ids(config_path: Path | None = None) -> list[str]:
    path = config_path or (codex_home() / "config.toml")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    current: str | None = None
    enabled: dict[str, bool] = {}
    section = re.compile(
        r'^\s*\[plugins\.(["\']?)(codex-gardener@[^"\']+)\1\]\s*(?:#.*)?$',
        re.IGNORECASE,
    )
    for line in lines:
        match = section.match(line)
        if match:
            current = match.group(2)
            enabled.setdefault(current, False)
            continue
        if line.lstrip().startswith("["):
            current = None
            continue
        if current and re.match(r"^\s*enabled\s*=\s*true\s*(?:#.*)?$", line, re.IGNORECASE):
            enabled[current] = True
    return sorted(plugin_id for plugin_id, is_enabled in enabled.items() if is_enabled)


def plugin_health() -> dict[str, Any]:
    enabled = enabled_gardener_plugin_ids()
    duplicates = [plugin_id for plugin_id in enabled if plugin_id != PLUGIN_ID]
    config = codex_home() / "config.toml"
    standalone_skill = codex_home() / "skills" / "cross-project-delegation" / "SKILL.md"
    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": _plugin_version(),
        "enabled_plugin_ids": enabled,
        "duplicate_enabled_plugin_ids": duplicates,
        "plugin_config_observed": config.is_file(),
        "legacy_learning_source_exists": (codex_home() / "learning").is_dir(),
        "standalone_cross_project_skill_exists": standalone_skill.is_file(),
        "standalone_cross_project_skill_path": str(standalone_skill.resolve()) if standalone_skill.is_file() else None,
    }


def format_effectiveness_report(report: dict[str, Any]) -> str:
    window = report["window"]
    coverage = report["coverage"]
    reviews = report["reviews"]
    context = report["context"]
    boundary = report["boundary"]
    lines = [
        f"Codex Gardener effectiveness ({window['since']} through {window['through']})",
        f"Events: {report['events']['valid']} valid, {report['events']['corrupt_lines_ignored']} corrupt ignored",
        (
            "Health: "
            f"{report['health']['observation_status']}; log={report['health']['log_path']}; "
            f"latest={report['health']['latest_event_at'] or 'never'}"
        ),
        f"Coverage: {coverage['sessions_observed']} sessions, {coverage['projects_observed']} projects",
        (
            "Reviews: "
            f"{reviews['requested']} requested, {reviews['captures_recorded']} captured, "
            f"{reviews['candidates_per_requested_review']:.2f} candidates/request, "
            f"{reviews['completed_without_candidate']} completed without candidate, "
            f"{reviews['current_pending']} currently pending"
        ),
        (
            "Context: "
            f"{context['lookups_with_hits']}/{context['lookups']} lookups hit "
            f"({context['lookup_hit_rate']:.2%}); {context['hits_repository']} repository and "
            f"{context['hits_global']} global entries injected"
        ),
        f"Boundary denials: {boundary['denials']}",
        (
            "Knowledge audit: "
            f"due={report['health']['audit']['due']} "
            f"reason={report['health']['audit']['reason']} "
            f"reviews={report['health']['audit']['qualifying_reviews']}/"
            f"{report['health']['audit']['review_threshold']} "
            f"deadline={report['health']['audit']['deadline_at'] or 'unavailable'}"
        ),
    ]
    if "candidate_group_status" in report:
        lines.append("Current candidate groups: " + json.dumps(report["candidate_group_status"], sort_keys=True))
        lines.append(
            "Candidate evidence maturity: "
            + json.dumps(report["candidate_group_evidence_status"], sort_keys=True)
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Gardener hook and candidate CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook")
    hook.add_argument("event", choices=["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"])

    def add_candidate_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repo", required=True)
        command.add_argument("--session-id", required=True)
        command.add_argument("--knowledge-scope", choices=sorted(KNOWLEDGE_SCOPES), default="repository")
        command.add_argument("--scope", required=True)
        command.add_argument("--lesson", required=True)
        command.add_argument("--evidence", required=True)
        command.add_argument("--target", required=True, choices=sorted(TARGETS))
        command.add_argument("--confidence", required=True, type=float)

    record = sub.add_parser("record")
    add_candidate_arguments(record)

    deferred = sub.add_parser("defer-record")
    add_candidate_arguments(deferred)

    deferred_audit = sub.add_parser("defer-audit-complete")
    deferred_audit.add_argument("--repo", required=True)
    deferred_audit.add_argument("--session-id", required=True)

    groups = sub.add_parser("groups")
    groups.add_argument("--repo", required=True)
    groups.add_argument("--knowledge-scope", choices=sorted(KNOWLEDGE_SCOPES), default="repository")

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--fingerprint", required=True)
    resolve.add_argument("--knowledge-scope", choices=sorted(KNOWLEDGE_SCOPES), default="repository")
    resolve.add_argument("--status", required=True, choices=sorted(SAFE_RESOLUTION_STATUSES))
    resolve.add_argument("--summary")
    resolve.add_argument("--target-path")
    resolve.add_argument("--keyword", action="append")

    review = sub.add_parser("review-complete")
    review.add_argument("--session-id", required=True)

    pending = sub.add_parser("pending")
    pending.add_argument("--repo")

    pending_resolve = sub.add_parser("pending-resolve")
    pending_resolve.add_argument("--session-id", required=True)

    report = sub.add_parser("effectiveness")
    report.add_argument("--since-days", type=int, default=14)
    report.add_argument("--repo")
    report.add_argument("--json", action="store_true", dest="as_json")

    audit = sub.add_parser("audit-status")
    audit.add_argument("--repo")
    audit.add_argument(
        "--initialize",
        action="store_true",
        help="Explicitly request first-use checkpoint initialization (also the default for this command).",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "hook":
            return handle_hook(args.event)
        if args.command == "record":
            emit_json(record_candidate(args))
        elif args.command == "defer-record":
            emit_json(defer_candidate(args))
        elif args.command == "defer-audit-complete":
            emit_json(defer_audit_completion(args))
        elif args.command == "groups":
            emit_json(
                {
                    "knowledge_scope": args.knowledge_scope,
                    "groups": aggregate_candidates(Path(args.repo).resolve(), args.knowledge_scope),
                }
            )
        elif args.command == "resolve":
            emit_json(resolve_candidate(args))
        elif args.command == "review-complete":
            complete_review_without_candidate(args.session_id)
            emit_json({"completed": True, "session_id": args.session_id})
        elif args.command == "pending":
            repo = Path(args.repo).resolve() if args.repo else None
            emit_json({"pending": pending_records(repo)})
        elif args.command == "pending-resolve":
            resolve_pending(args.session_id)
            emit_json({"resolved": True, "session_id": args.session_id})
        elif args.command == "effectiveness":
            repo = Path(args.repo).resolve() if args.repo else None
            report = effectiveness_report(args.since_days, repo)
            if args.as_json:
                emit_json(report)
            else:
                sys.stdout.write(format_effectiveness_report(report) + "\n")
        elif args.command == "audit-status":
            emit_json(audit_status(initialize=True))
        return 0
    except (OSError, ValueError, TimeoutError) as exc:
        sys.stderr.write(f"codex-gardener: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
