#!/usr/bin/env python3
"""Derived, privacy-bounded SQLite retrieval for promoted Gardener knowledge.

``index.jsonl`` is authoritative.  This module stores only normalized promoted
metadata and aggregate counters in SQLite; prompts, tool output, and query terms
are deliberately never written to disk.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


INDEX_NAME = "index.jsonl"
DATABASE_NAME = "retrieval.sqlite3"
SCHEMA_VERSION = 1
K1 = 1.2
B = 0.75
DEFAULT_MIN_SCORE = 0.75
MAX_RESULTS = 3
MAX_CONTEXT_TOKENS = 500
LOCK_TIMEOUT_SECONDS = 0.25
MAX_PATHS = 32
MAX_FIELD_ITEMS = 16
MAX_FIELD_TEXT = 160
WORD_RE = re.compile(r"[\w.-]+", re.UNICODE)
CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
PATH_RE = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/])?[\w.@+-]+(?:[\\/][\w.@+ -]+)+")


class RetrievalError(RuntimeError):
    """The derived index is unavailable; callers must fail open."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()


def tokenize(value: Any) -> list[str]:
    """NFKC/casefold identifier tokens plus CJK unigrams and adjacent bigrams."""
    text = normalize_text(value)
    tokens: list[str] = []
    for word in WORD_RE.findall(text):
        for part in re.split(r"[_./\\:-]+", word):
            if part:
                tokens.append(part)
    chars = CJK_RE.findall(text)
    tokens.extend(chars)
    tokens.extend("".join(chars[index : index + 2]) for index in range(max(0, len(chars) - 1)))
    return tokens


def normalized_summary(value: Any) -> str:
    return " ".join(tokenize(value))


def estimate_tokens(value: Any) -> int:
    text = str(value or "")
    return max(1, math.ceil(len(text) / 4))


def source_signature(index_path: Path) -> str:
    try:
        data = index_path.read_bytes()
    except OSError as exc:
        raise RetrievalError("authoritative index is unavailable") from exc
    return hashlib.sha256(data).hexdigest()


def database_path(store: Path) -> Path:
    return store / DATABASE_NAME


def _connect(path: Path, *, write: bool = False) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 250")
        if write:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection
    except sqlite3.Error as exc:
        raise RetrievalError("retrieval database is unavailable") from exc


def _list(value: Any, name: str, *, patterns: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FIELD_ITEMS:
        raise ValueError(f"{name} must contain at most {MAX_FIELD_ITEMS} values")
    output: list[str] = []
    for item in value:
        text = normalize_text(item)
        if not text or len(text) > MAX_FIELD_TEXT:
            raise ValueError(f"{name} contains an invalid value")
        if patterns and (Path(str(item)).is_absolute() or ".." in Path(str(item)).parts):
            raise ValueError("path_globs must be relative")
        output.append(text)
    return sorted(set(output))


def validate_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Validate accepted promoted metadata and compute bounded derived fields."""
    metadata = {
        "task_types": _list(record.get("task_types", []), "task_types"),
        "path_globs": _list(record.get("path_globs", []), "path_globs", patterns=True),
        "languages": _list(record.get("languages", []), "languages"),
        "tools": _list(record.get("tools", []), "tools"),
        "platforms": _list(record.get("platforms", []), "platforms"),
        "negative_keywords": _list(record.get("negative_keywords", []), "negative_keywords"),
    }
    minimum = record.get("min_score", DEFAULT_MIN_SCORE)
    try:
        minimum = float(minimum)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_score must be numeric") from exc
    if not 0.0 <= minimum <= 20.0:
        raise ValueError("min_score must be between 0 and 20")
    supersedes = record.get("supersedes")
    if supersedes is not None:
        supersedes = str(supersedes).strip()
        if not re.fullmatch(r"[0-9a-f]{6,128}", supersedes):
            raise ValueError("supersedes must be a fingerprint")
    metadata["min_score"] = minimum
    metadata["supersedes"] = supersedes
    metadata["estimated_tokens"] = estimate_tokens(record.get("summary"))
    return metadata


def _records(store: Path) -> list[dict[str, Any]]:
    path = store / INDEX_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetrievalError("authoritative index is unavailable") from exc
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalError("authoritative index is corrupt") from exc
        if not isinstance(value, dict):
            raise RetrievalError("authoritative index is corrupt")
        fingerprint = str(value.get("fingerprint") or "")
        summary = str(value.get("summary") or "").strip()
        if not fingerprint or not summary:
            raise RetrievalError("authoritative index has an invalid promoted entry")
        entry = dict(value)
        entry.update(validate_metadata(entry))
        result.append(entry)
    return result


def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE entries (
          fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL, summary_norm TEXT NOT NULL,
          doc_len INTEGER NOT NULL, last_used_at TEXT, hit_count INTEGER NOT NULL DEFAULT 0,
          eligible_count INTEGER NOT NULL DEFAULT 0, miss_count_since_hit INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE terms (fingerprint TEXT NOT NULL, term TEXT NOT NULL, tf REAL NOT NULL,
          PRIMARY KEY (fingerprint, term));
        CREATE INDEX terms_term ON terms(term);
        """
    )


def _weighted_terms(entry: dict[str, Any]) -> Counter[str]:
    terms: Counter[str] = Counter()
    for token, count in Counter(tokenize(entry.get("summary"))).items():
        terms[token] += count
    for keyword in entry.get("keywords", []) if isinstance(entry.get("keywords"), list) else []:
        for token in tokenize(keyword):
            terms[token] += 3
    return terms


def sync_scope(store: Path | str) -> dict[str, Any]:
    """Atomically rebuild the complete derived index from authoritative JSONL."""
    store = Path(store)
    records = _records(store)
    signature = source_signature(store / INDEX_NAME)
    store.mkdir(parents=True, exist_ok=True)
    target = database_path(store)
    fd, temporary_name = tempfile.mkstemp(prefix=".retrieval.", suffix=".sqlite3", dir=store)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        connection = _connect(temporary, write=True)
        try:
            _schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            for entry in records:
                payload = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                terms = _weighted_terms(entry)
                connection.execute(
                    "INSERT INTO entries(fingerprint,payload,summary_norm,doc_len) VALUES(?,?,?,?)",
                    (str(entry["fingerprint"]), payload, normalized_summary(entry["summary"]), max(1, sum(terms.values()))),
                )
                connection.executemany(
                    "INSERT INTO terms(fingerprint,term,tf) VALUES(?,?,?)",
                    [(str(entry["fingerprint"]), term, float(tf)) for term, tf in terms.items()],
                )
            connection.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            connection.execute("INSERT INTO meta(key,value) VALUES('source_signature',?)", (signature,))
            connection.execute("INSERT INTO meta(key,value) VALUES('rebuilt_at',?)", (_now(),))
            connection.execute("COMMIT")
        finally:
            connection.close()
        os.replace(temporary, target)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise RetrievalError("retrieval index rebuild failed") from exc
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return {"scope_path": str(store), "entries": len(records), "source_signature": signature, "rebuilt_at": _now()}


def _read_index(store: Path) -> sqlite3.Connection:
    path = database_path(store)
    if not path.is_file():
        raise RetrievalError("retrieval index is missing")
    connection = _connect(path)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key='source_signature'").fetchone()
        version = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or not version or version[0] != str(SCHEMA_VERSION) or row[0] != source_signature(store / INDEX_NAME):
            raise RetrievalError("retrieval index is stale")
    except Exception:
        connection.close()
        raise
    return connection


def _context_values(context: dict[str, Any] | None, name: str) -> set[str]:
    value = (context or {}).get(name, [])
    if isinstance(value, str):
        value = [value]
    return {normalize_text(item) for item in value if normalize_text(item)} if isinstance(value, list) else set()


def derive_task_context(prompt: str, repo: Path | None = None, *, tool_names: Iterable[str] = ()) -> dict[str, list[str]]:
    """Derive only bounded transient context. Nothing returned here is persisted."""
    paths: list[str] = []
    if repo is not None:
        root = repo.resolve()
        for raw in PATH_RE.findall(prompt):
            candidate = Path(raw)
            try:
                resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                paths.append(str(resolved.relative_to(root)).replace("\\", "/"))
            except (OSError, ValueError):
                continue
        # Root marker inspection is deliberately bounded and has no recursion.
        with contextlib.suppress(OSError):
            for child in sorted(root.iterdir(), key=lambda item: item.name.casefold())[:16]:
                if child.name in {"AGENTS.md", "package.json", "pyproject.toml", "Cargo.toml", "go.mod"}:
                    paths.append(child.name)
    words = set(tokenize(prompt))
    task_types = [kind for kind in ("bugfix", "feature", "refactor", "test", "docs", "deploy") if kind in words]
    languages = [language for language in ("python", "javascript", "typescript", "rust", "go", "java", "csharp") if language in words]
    platforms = ["windows" if os.name == "nt" else "linux" if os.name == "posix" else os.name]
    return {
        "task_types": task_types,
        "paths": sorted(set(paths))[:MAX_PATHS],
        "languages": languages,
        "tools": sorted({normalize_text(item) for item in tool_names if normalize_text(item)})[:MAX_FIELD_ITEMS],
        "platforms": platforms,
    }


def _eligible(entry: dict[str, Any], terms: set[str], context: dict[str, Any] | None) -> bool:
    if terms & set(entry.get("negative_keywords", [])):
        return False
    patterns = entry.get("path_globs", [])
    if patterns:
        paths = _context_values(context, "paths")
        if not paths or not any(fnmatch.fnmatch(path, pattern) for path in paths for pattern in patterns):
            return False
    return True


def _metadata_boost(entry: dict[str, Any], context: dict[str, Any] | None) -> float:
    boosts = {"task_types": 1.0, "languages": 0.5, "tools": 0.75, "platforms": 0.5}
    return sum(boosts[key] for key in boosts if set(entry.get(key, [])).intersection(_context_values(context, key))) + (
        1.0 if entry.get("path_globs") and _context_values(context, "paths") else 0.0
    )


def retrieve(store: Path | str, prompt: str, *, task_context: dict[str, Any] | None = None,
             error_logger: Callable[[str], None] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve up to three entries. Degraded sources fail open without JSON fallback."""
    started = time.monotonic()
    metrics: dict[str, Any] = {"eligible": 0, "scored": 0, "filtered": 0, "threshold": 0, "token": 0, "injected": 0,
                               "latency_ms": 0, "rebuild": 0, "degraded": False}
    terms = tokenize(prompt)
    phrase = normalize_text(prompt)
    if not terms:
        return [], metrics
    try:
        connection = _read_index(Path(store))
        try:
            rows = connection.execute("SELECT fingerprint,payload,summary_norm,doc_len FROM entries ORDER BY fingerprint").fetchall()
            count = len(rows)
            average = max(1.0, sum(int(row[3]) for row in rows) / max(1, count))
            scored: list[tuple[float, dict[str, Any]]] = []
            query = Counter(terms)
            placeholders = ",".join("?" for _ in query)
            term_rows = connection.execute(
                f"SELECT fingerprint,term,tf FROM terms WHERE term IN ({placeholders})", tuple(query)
            ).fetchall()
            frequencies = Counter(str(term) for _, term, _ in term_rows)
            term_values = {(str(fingerprint), str(term)): float(tf) for fingerprint, term, tf in term_rows}
            term_set = set(terms)
            eligible_fingerprints: list[str] = []
            for fingerprint, payload, summary_norm, doc_len in rows:
                entry = json.loads(payload)
                if not _eligible(entry, term_set, task_context):
                    metrics["filtered"] += 1
                    continue
                metrics["eligible"] += 1
                eligible_fingerprints.append(str(fingerprint))
                score = 0.0
                for term, qtf in query.items():
                    df = frequencies[term]
                    tf = term_values.get((str(fingerprint), term))
                    if tf is not None:
                        idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                        score += qtf * idf * ((tf * (K1 + 1)) / (tf + K1 * (1 - B + B * int(doc_len) / average)))
                if phrase and phrase in summary_norm:
                    score += 2.0
                # Keywords are an explicit curated field boost, not merely a
                # copy of summary text. This preserves useful one-keyword
                # retrieval under the conservative default threshold.
                score += 3.0 * sum(
                    1
                    for keyword in entry.get("keywords", []) if normalize_text(keyword) in term_set
                )
                score += _metadata_boost(entry, task_context)
                if score < float(entry.get("min_score", DEFAULT_MIN_SCORE)):
                    metrics["threshold"] += 1
                    continue
                metrics["scored"] += 1
                entry["score"] = round(score, 6)
                scored.append((score, entry))
            scored.sort(key=lambda item: (-item[0], str(item[1].get("promoted_at") or ""), str(item[1].get("fingerprint"))))
            selected: list[dict[str, Any]] = []
            tokens = 0
            for _, entry in scored:
                cost = int(entry.get("estimated_tokens") or estimate_tokens(entry.get("summary")))
                if len(selected) >= MAX_RESULTS or tokens + cost > MAX_CONTEXT_TOKENS:
                    metrics["token"] += 1
                    continue
                selected.append(entry)
                tokens += cost
            selected_ids = {str(item["fingerprint"]) for item in selected}
            # Aggregate counters are permitted derived effectiveness data; no query data is stored.
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            hits = [(now, fingerprint) for fingerprint in eligible_fingerprints if fingerprint in selected_ids]
            misses = [(fingerprint,) for fingerprint in eligible_fingerprints if fingerprint not in selected_ids]
            connection.executemany(
                "UPDATE entries SET eligible_count=eligible_count+1,hit_count=hit_count+1,last_used_at=?,miss_count_since_hit=0 WHERE fingerprint=?", hits
            )
            connection.executemany(
                "UPDATE entries SET eligible_count=eligible_count+1,miss_count_since_hit=miss_count_since_hit+1 WHERE fingerprint=?", misses
            )
            connection.execute("COMMIT")
            metrics["injected"] = len(selected)
            return selected, metrics
        finally:
            connection.close()
    except (RetrievalError, sqlite3.Error, json.JSONDecodeError, OSError, ValueError) as exc:
        metrics["degraded"] = True
        if error_logger and "missing" not in str(exc):
            error_logger("retrieval")
        return [], metrics
    finally:
        metrics["latency_ms"] = int((time.monotonic() - started) * 1000)


def index_status(store: Path | str) -> dict[str, Any]:
    store = Path(store)
    result: dict[str, Any] = {"scope_path": str(store), "available": False, "stale": True, "entries": 0, "degraded": False}
    try:
        connection = _read_index(store)
        try:
            result.update({"available": True, "stale": False, "entries": connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0],
                           "source_signature": connection.execute("SELECT value FROM meta WHERE key='source_signature'").fetchone()[0],
                           "rebuilt_at": connection.execute("SELECT value FROM meta WHERE key='rebuilt_at'").fetchone()[0]})
        finally:
            connection.close()
    except RetrievalError:
        result["degraded"] = True
    return result


def audit_scope(store: Path | str, *, now: datetime | None = None) -> dict[str, Any]:
    """Read-only retrieval health audit; it never changes JSONL or SQLite."""
    store = Path(store)
    current = now or datetime.now(timezone.utc)
    issues: list[dict[str, Any]] = []
    # Validate the authoritative source separately so invalid future metadata is
    # reported even when it prevents a derived rebuild.
    with contextlib.suppress(OSError):
        for line in (store / INDEX_NAME).read_text(encoding="utf-8").splitlines():
            raw: Any = None
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("not an object")
                validate_metadata(raw)
            except (ValueError, TypeError, json.JSONDecodeError):
                issues.append(
                    {"kind": "invalid_metadata", "fingerprint": str(raw.get("fingerprint") or "unknown") if isinstance(raw, dict) else "unknown"}
                )
    try:
        connection = _read_index(store)
        try:
            rows = connection.execute("SELECT fingerprint,payload,summary_norm,last_used_at,miss_count_since_hit FROM entries ORDER BY fingerprint").fetchall()
        finally:
            connection.close()
    except RetrievalError as exc:
        counts = Counter(issue["kind"] for issue in issues)
        return {"scope_path": str(store), "available": False, "counts": {"stale": 0, "duplicates": 0, "superseded": 0,
                "orphaned_targets": 0, "invalid_metadata": counts["invalid_metadata"], "degraded": 1},
                "issues": [*issues, {"kind": "degraded", "detail": str(exc)}]}
    summaries: dict[str, list[str]] = {}
    for fingerprint, payload, summary_norm, last_used, misses in rows:
        entry = json.loads(payload)
        summaries.setdefault(summary_norm, []).append(fingerprint)
        target = str(entry.get("target_path") or "")
        if target and not target.startswith("$") and not (store.parent.parent / target).exists():
            issues.append({"kind": "orphaned_target", "fingerprint": fingerprint})
        promoted = entry.get("promoted_at")
        promoted_at = _parse_time(promoted)
        if promoted_at and current - promoted_at >= timedelta(days=90) and int(misses) >= 50:
            issues.append({"kind": "stale", "fingerprint": fingerprint})
        if entry.get("supersedes"):
            issues.append({"kind": "superseded", "fingerprint": str(entry["supersedes"])})
    for group in summaries.values():
        if len(group) > 1:
            issues.extend({"kind": "duplicate", "fingerprint": fingerprint} for fingerprint in group)
    counts = Counter(issue["kind"] for issue in issues)
    return {"scope_path": str(store), "available": True, "counts": {"stale": counts["stale"], "duplicates": counts["duplicate"],
            "superseded": counts["superseded"], "orphaned_targets": counts["orphaned_target"], "invalid_metadata": counts["invalid_metadata"]}, "issues": issues}


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def cleanup_session(store: Path | str) -> None:
    """Best-effort removal of SQLite WAL sidecars at SessionEnd; main DB is retained."""
    store = Path(store)
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            (database_path(store).with_name(database_path(store).name + suffix)).unlink()
