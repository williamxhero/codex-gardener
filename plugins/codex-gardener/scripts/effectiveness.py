#!/usr/bin/env python3
"""Privacy-bounded local effectiveness events and deterministic summaries."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


EVENT_SCHEMA_VERSION = 1
MAX_LOG_BYTES = 1024 * 1024
MAX_BACKUPS = 4
RETENTION_DAYS = 90
OPT_OUT_ENV = "CODEX_GARDENER_EFFECTIVENESS_LOG"
PROCESS_WRITE_LOCK = threading.Lock()
LOCATOR_NAME = "codex-gardener-data-path"

EVENT_FIELDS: dict[str, set[str]] = {
    "session_start": {"session", "project"},
    "context_lookup": {
        "session",
        "project",
        "repository_available",
        "global_available",
        "repository_hits",
        "global_hits",
        "injected",
    },
    "review_requested": {"session", "project", "signals"},
    "capture_recorded": {
        "session",
        "project",
        "knowledge_scope",
        "recommended_target",
        "confidence_bucket",
    },
    "review_completed_no_candidate": {"session", "project"},
    "pending_queued": {"session", "project", "signals"},
    "pending_resolved": {"session", "project"},
    "resolution_recorded": {"project", "knowledge_scope", "status", "target"},
    "project_boundary_denied": {"session", "primary_project", "target_project", "tool_category"},
    "operation_error": {"operation", "category"},
}

ENUM_VALUES = {
    "knowledge_scope": {"repository", "global"},
    "recommended_target": {"agents", "skill", "test", "hook", "docs", "discard"},
    "confidence_bucket": {"low", "medium", "high"},
    "status": {"promoted", "discarded", "proposed"},
    "target": {"agents", "skill", "test", "hook", "docs", "discard", "other"},
    "tool_category": {"patch", "shell", "file", "git", "mcp", "other"},
    "operation": {"hook", "record", "resolve", "report", "boundary"},
    "category": {"input", "storage", "runtime"},
}
SIGNALS = {"workspace_changed", "user_correction", "repeated_failures", "repeated_tool_workflow"}
HASH_FIELDS = {"session", "project", "primary_project", "target_project"}
COUNT_FIELDS = {
    "repository_available",
    "global_available",
    "repository_hits",
    "global_hits",
    "injected",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _write_locator(locator: Path, root: Path) -> None:
    locator.parent.mkdir(parents=True, exist_ok=True)
    temp = locator.with_name(locator.name + ".tmp")
    temp.write_text(str(root.resolve()) + "\n", encoding="utf-8", newline="\n")
    os.replace(temp, locator)


def data_root_info(*, persist_plugin_data: bool = True) -> tuple[Path, str]:
    explicit = os.environ.get("CODEX_GARDENER_DATA")
    plugin_data = os.environ.get("PLUGIN_DATA")
    locator = codex_home() / LOCATOR_NAME
    if explicit:
        return Path(explicit), "CODEX_GARDENER_DATA"
    if plugin_data:
        root = Path(plugin_data)
        if persist_plugin_data:
            with contextlib.suppress(OSError):
                _write_locator(locator, root)
        return root, "PLUGIN_DATA"
    if locator.is_file():
        with contextlib.suppress(OSError):
            located = locator.read_text(encoding="utf-8").strip()
            if located:
                return Path(located), "locator"
    return codex_home() / "codex-gardener-data", "default"


def plugin_data_root() -> Path:
    return data_root_info()[0]


def log_dir(root: Path | None = None) -> Path:
    return (root or plugin_data_root()) / "effectiveness"


def log_path(root: Path | None = None) -> Path:
    return log_dir(root) / "events.jsonl"


def logging_enabled() -> bool:
    return os.environ.get(OPT_OUT_ENV, "1").strip().casefold() not in {"0", "false", "no", "off"}


def hash_identifier(value: Any, domain: str = "id") -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(f"codex-gardener:{domain}\0{text}".encode("utf-8", errors="replace")).hexdigest()[:24]


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 1.5) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                raise TimeoutError("effectiveness log is busy")
            time.sleep(0.01)
    try:
        yield
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            lock.unlink()


def _rotate(path: Path, incoming_bytes: int) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    for rotated in path.parent.glob(f"{path.stem}.*{path.suffix}"):
        with contextlib.suppress(OSError):
            if rotated.stat().st_mtime < cutoff:
                rotated.unlink()
    if not path.exists():
        return
    stale = path.stat().st_mtime < cutoff
    oversized = path.stat().st_size + incoming_bytes > MAX_LOG_BYTES
    if not (stale or oversized):
        return
    oldest = path.with_name(f"{path.stem}.{MAX_BACKUPS}{path.suffix}")
    with contextlib.suppress(FileNotFoundError):
        oldest.unlink()
    for index in range(MAX_BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.stem}.{index}{path.suffix}")
        destination = path.with_name(f"{path.stem}.{index + 1}{path.suffix}")
        if source.exists():
            os.replace(source, destination)
    os.replace(path, path.with_name(f"{path.stem}.1{path.suffix}"))
    for rotated in path.parent.glob(f"{path.stem}.*{path.suffix}"):
        with contextlib.suppress(OSError):
            if rotated.stat().st_mtime < cutoff:
                rotated.unlink()


def _safe_value(key: str, value: Any) -> Any | None:
    if key in HASH_FIELDS:
        return hash_identifier(value, key)
    if key in COUNT_FIELDS:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None
    if key == "signals":
        if not isinstance(value, (list, tuple, set)):
            return []
        return sorted({str(item) for item in value if str(item) in SIGNALS})
    if key in ENUM_VALUES:
        text = str(value or "").casefold()
        return text if text in ENUM_VALUES[key] else None
    return None


def build_event(event: str, **fields: Any) -> dict[str, Any] | None:
    allowed = EVENT_FIELDS.get(event)
    if allowed is None:
        return None
    record: dict[str, Any] = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event": event,
        "created_at": utc_now(),
    }
    for key in sorted(allowed):
        if key not in fields:
            continue
        value = _safe_value(key, fields[key])
        if value is not None:
            record[key] = value
    return record


def log_event(event: str, *, root: Path | None = None, **fields: Any) -> bool:
    """Append an allow-listed event. Any logging failure is intentionally ignored."""
    if not logging_enabled():
        return False
    try:
        record = build_event(event, **fields)
        if record is None:
            return False
        rendered = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        encoded = rendered.encode("utf-8")
        path = log_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with PROCESS_WRITE_LOCK:
            with file_lock(path):
                _rotate(path, len(encoded))
                with path.open("ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
        return True
    except (OSError, TimeoutError, TypeError, ValueError):
        return False


def event_paths(root: Path | None = None) -> list[Path]:
    path = log_path(root)
    rotated = [path.with_name(f"{path.stem}.{index}{path.suffix}") for index in range(MAX_BACKUPS, 0, -1)]
    return [candidate for candidate in [*rotated, path] if candidate.is_file()]


def _parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_events(*, root: Path | None = None, since: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    corrupt = 0
    for path in event_paths(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                corrupt += 1
                continue
            if not isinstance(record, dict) or record.get("event") not in EVENT_FIELDS:
                corrupt += 1
                continue
            created = _parse_timestamp(record.get("created_at"))
            if created is None:
                corrupt += 1
                continue
            if since is not None and created < since:
                continue
            records.append(record)
    return records, corrupt


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def summarize(*, since_days: int = 14, root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    if since_days < 0:
        raise ValueError("since-days must be non-negative")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    since = current - timedelta(days=since_days)
    resolved_root, root_source = (root, "argument") if root is not None else data_root_info()
    events, corrupt = read_events(root=resolved_root, since=since)
    all_events, all_corrupt = read_events(root=resolved_root)
    paths = event_paths(resolved_root)
    latest_event_at = max((str(item["created_at"]) for item in all_events), default=None)
    if not logging_enabled():
        observation_status = "logging_disabled"
    elif all_events:
        observation_status = "observed"
    elif paths and all_corrupt:
        observation_status = "unreadable"
    else:
        observation_status = "not_observed"
    by_type = Counter(str(item["event"]) for item in events)
    sessions = {str(item["session"]) for item in events if item.get("session")}
    projects = {
        str(item[key])
        for item in events
        for key in ("project", "primary_project", "target_project")
        if item.get(key)
    }
    captures = [item for item in events if item["event"] == "capture_recorded"]
    resolutions = [item for item in events if item["event"] == "resolution_recorded"]
    lookups = [item for item in events if item["event"] == "context_lookup"]
    lookups_with_hits = sum(1 for item in lookups if int(item.get("injected") or 0) > 0)
    boundary = [item for item in events if item["event"] == "project_boundary_denied"]
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "window": {
            "since_days": since_days,
            "since": since.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "through": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "health": {
            "observation_status": observation_status,
            "logging_enabled": logging_enabled(),
            "data_root": str(resolved_root.resolve()),
            "data_root_source": root_source,
            "log_path": str(log_path(resolved_root).resolve()),
            "log_exists": bool(paths),
            "latest_event_at": latest_event_at,
        },
        "events": {
            "valid": len(events),
            "corrupt_lines_ignored": corrupt,
            "by_type": dict(sorted(by_type.items())),
        },
        "coverage": {"sessions_observed": len(sessions), "projects_observed": len(projects)},
        "reviews": {
            "requested": by_type["review_requested"],
            "captures_recorded": len(captures),
            "candidates_per_requested_review": _rate(len(captures), by_type["review_requested"]),
            "completed_without_candidate": by_type["review_completed_no_candidate"],
            "pending_queued": by_type["pending_queued"],
            "pending_resolved": by_type["pending_resolved"],
        },
        "candidates": {
            "knowledge_scope": dict(sorted(Counter(str(item.get("knowledge_scope")) for item in captures).items())),
            "recommended_target": dict(sorted(Counter(str(item.get("recommended_target")) for item in captures).items())),
            "confidence_bucket": dict(sorted(Counter(str(item.get("confidence_bucket")) for item in captures).items())),
        },
        "resolutions": {
            "status": dict(sorted(Counter(str(item.get("status")) for item in resolutions).items())),
            "knowledge_scope": dict(sorted(Counter(str(item.get("knowledge_scope")) for item in resolutions).items())),
            "target": dict(sorted(Counter(str(item.get("target")) for item in resolutions).items())),
        },
        "context": {
            "lookups": len(lookups),
            "lookups_with_hits": lookups_with_hits,
            "lookup_hit_rate": _rate(lookups_with_hits, len(lookups)),
            "entries_available_repository": sum(int(item.get("repository_available") or 0) for item in lookups),
            "entries_available_global": sum(int(item.get("global_available") or 0) for item in lookups),
            "hits_repository": sum(int(item.get("repository_hits") or 0) for item in lookups),
            "hits_global": sum(int(item.get("global_hits") or 0) for item in lookups),
            "entries_injected": sum(int(item.get("injected") or 0) for item in lookups),
        },
        "boundary": {
            "denials": len(boundary),
            "tool_category": dict(sorted(Counter(str(item.get("tool_category")) for item in boundary).items())),
        },
        "errors": {
            "count": by_type["operation_error"],
            "by_operation": dict(
                sorted(Counter(str(item.get("operation")) for item in events if item["event"] == "operation_error").items())
            ),
        },
    }
