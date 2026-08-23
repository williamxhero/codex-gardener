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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import effectiveness


SCHEMA_VERSION = 2
PLUGIN_ID = "codex-gardener@codex-gardener"
LEGACY_MIGRATION_PROVENANCE = "legacy-user-learning-v1"
TARGETS = {"agents", "skill", "test", "hook", "docs", "discard"}
KNOWLEDGE_SCOPES = {"repository", "global"}
SAFE_RESOLUTION_STATUSES = {"promoted", "discarded", "proposed"}
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
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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
    record = dict(raw)
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
    if not legacy.is_dir() or legacy.resolve() == target.resolve():
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
                current = read_jsonl(path)
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
                    temp = path.with_name(path.name + ".tmp")
                    temp.write_text(
                        "".join(
                            json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                            for item in [*current, *additions]
                        ),
                        encoding="utf-8",
                        newline="\n",
                    )
                    os.replace(temp, path)
                result[filename] = len(additions)
        except (OSError, TimeoutError):
            result[filename] = 0
    return result


def read_learning_records(repo: Path, knowledge_scope: str, filename: str) -> list[dict[str, Any]]:
    knowledge_scope = normalize_knowledge_scope(knowledge_scope)
    current = read_jsonl(learning_dir(repo, knowledge_scope) / filename)
    if knowledge_scope != "global":
        return current
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


def record_candidate(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    knowledge_scope = normalize_knowledge_scope(getattr(args, "knowledge_scope", "repository"))
    target = args.target.casefold()
    if target not in TARGETS:
        raise ValueError(f"target must be one of: {', '.join(sorted(TARGETS))}")
    confidence = float(args.confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    fingerprint = candidate_fingerprint(args.scope, args.lesson, target)
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "fingerprint": fingerprint,
        "session_id": args.session_id,
        "knowledge_scope": knowledge_scope,
        "project_fingerprint": project_fingerprint(repo),
        "scope": args.scope.strip(),
        "lesson": args.lesson.strip(),
        "evidence_summary": args.evidence.strip(),
        "recommended_target": target,
        "confidence": confidence,
        "created_at": utc_now(),
    }
    append_jsonl(ensure_learning_dir(repo, knowledge_scope) / "inbox.jsonl", record)
    mark_review_complete(args.session_id)
    log_effectiveness(
        "capture_recorded",
        session=session_identity(args.session_id),
        project=project_identity(repo),
        knowledge_scope=knowledge_scope,
        recommended_target=target,
        confidence_bucket=confidence_bucket(confidence),
    )
    return record


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
    latest_resolution = {str(item.get("fingerprint")): item for item in resolutions if item.get("fingerprint")}
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
        status = "candidate"
        if (
            len(unique) >= 3
            and confidence >= 0.85
            and (knowledge_scope == "repository" or len(projects) >= 2)
        ):
            status = "promotable"
        elif knowledge_scope == "global" and len(unique) >= 3 and confidence >= 0.85:
            status = "proposed"
        elif len(unique) >= 2:
            status = "confirmed"
        exemplar = max(unique, key=lambda item: str(item.get("created_at") or ""))
        result.append(
            {
                "fingerprint": fingerprint,
                "status": status,
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
                "resolution": latest_resolution.get(fingerprint),
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
            }
        )
    elif CORRECTION_RE.search(prompt):
        state["correction_signal"] = True
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
    if payload.get("stop_hook_active"):
        completed_before_stop = bool(state.get("capture_completed"))
        state["capture_completed"] = True
        save_state(path, state)
        if not completed_before_stop:
            log_effectiveness(
                "review_completed_no_candidate",
                session=session_identity(state.get("session_id")),
                project=project_identity(Path(str(state["repo_root"]))) if state.get("repo_root") else None,
            )
        emit_json({"continue": True})
        return
    cwd = Path(str(state.get("cwd") or payload.get("cwd") or os.getcwd()))
    signals = signal_names(state, cwd)
    if not signals or state.get("review_requested"):
        save_state(path, state)
        emit_json({"continue": True})
        return
    state["review_requested"] = True
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
        "If nothing is reusable, mark the review complete without recording a candidate."
    )
    emit_json({"decision": "block", "reason": reason})


def handle_session_end(payload: dict[str, Any]) -> None:
    path, state = load_state(payload)
    cwd = Path(str(state.get("cwd") or payload.get("cwd") or os.getcwd()))
    signals = signal_names(state, cwd)
    if signals and not state.get("capture_completed"):
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
    report["reviews"]["current_pending"] = len(pending_records(repo))
    if repo is not None:
        group_status: dict[str, dict[str, int]] = {}
        for knowledge_scope in ("repository", "global"):
            counts: dict[str, int] = defaultdict(int)
            for group in aggregate_candidates(repo, knowledge_scope):
                counts[str(group.get("status") or "unknown")] += 1
            group_status[knowledge_scope] = dict(sorted(counts.items()))
        report["candidate_group_status"] = group_status
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
    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": _plugin_version(),
        "enabled_plugin_ids": enabled,
        "duplicate_enabled_plugin_ids": duplicates,
        "plugin_config_observed": config.is_file(),
        "legacy_learning_source_exists": (codex_home() / "learning").is_dir(),
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
    ]
    if "candidate_group_status" in report:
        lines.append("Current candidate groups: " + json.dumps(report["candidate_group_status"], sort_keys=True))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Gardener hook and candidate CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    hook = sub.add_parser("hook")
    hook.add_argument("event", choices=["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd"])

    record = sub.add_parser("record")
    record.add_argument("--repo", required=True)
    record.add_argument("--session-id", required=True)
    record.add_argument("--knowledge-scope", choices=sorted(KNOWLEDGE_SCOPES), default="repository")
    record.add_argument("--scope", required=True)
    record.add_argument("--lesson", required=True)
    record.add_argument("--evidence", required=True)
    record.add_argument("--target", required=True, choices=sorted(TARGETS))
    record.add_argument("--confidence", required=True, type=float)

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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "hook":
            return handle_hook(args.event)
        if args.command == "record":
            emit_json(record_candidate(args))
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
        return 0
    except (OSError, ValueError, TimeoutError) as exc:
        sys.stderr.write(f"codex-gardener: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
