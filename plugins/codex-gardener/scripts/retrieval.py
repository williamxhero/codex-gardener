#!/usr/bin/env python3
"""Bounded, privacy-preserving promoted-knowledge retrieval.

The JSONL index is authoritative only while rebuilding or auditing.  Normal
retrieval deliberately consults only SQLite and the JSONL file's size/mtime,
so a hot prompt neither reads the JSONL nor scans all documents.
"""
from __future__ import annotations

import contextlib
import fnmatch
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
SCHEMA_VERSION = 2
K1, B = 1.2, .75
DEFAULT_MIN_SCORE, MAX_RESULTS, MAX_CONTEXT_TOKENS = .75, 3, 500
LOCK_TIMEOUT_SECONDS, MAX_PATHS, MAX_FIELD_ITEMS = .25, 32, 16
COLD_REPAIR_MAX_INDEX_BYTES = 128 * 1024
COLD_REPAIR_BUDGET_SECONDS = .75
REBUILD_LOCK_TIMEOUT_SECONDS = 5.0
USAGE_LOCK_TIMEOUT_SECONDS = .25
REBUILD_ORPHAN_AGE_SECONDS = 300
CONTEXT_HEADING = "Codex Gardener found relevant promoted knowledge:"
SLUG_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*$")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{6,128}$")
CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]+")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[A-Za-z0-9_.\-/\\:]*[A-Za-z0-9])?")
PATH_RE = re.compile(r"(?<!\w)(?:[A-Za-z]:[\\/])?[\w.@+-]+(?:[\\/][\w.@+ -]+)+")

class RetrievalError(RuntimeError):
    pass

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _code_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))

def store_for(repo: Path | str, knowledge_scope: str) -> Path:
    root = Path(repo).resolve()
    if knowledge_scope == "repository":
        return root / ".codex" / "learning"
    if knowledge_scope == "global":
        return _code_home() / "codex-gardener-global-learning"
    raise ValueError("knowledge_scope must be repository or global")

def database_path(store: Path) -> Path:
    return store / DATABASE_NAME

def normalize_text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold().strip()

def normalize_phrase(value: Any) -> str:
    return " ".join(normalize_text(value).split())

def _camel_parts(word: str) -> list[str]:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", word).split()

def tokenize(value: Any) -> list[str]:
    """Identifier-aware tokens and CJK uni/bigrams within each contiguous run."""
    original = unicodedata.normalize("NFKC", str(value or ""))
    tokens: list[str] = []
    for word in WORD_RE.findall(original):
        for piece in re.split(r"[\s./\\:_-]+", word):
            tokens.extend(part.casefold() for part in _camel_parts(piece) if part)
    for run in CJK_RUN_RE.findall(original):
        tokens.extend(run)
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens

def normalized_summary(value: Any) -> str:
    return normalize_phrase(value)

def estimate_tokens(value: Any) -> int:
    text = str(value or "")
    cjk = sum(1 for char in text if CJK_RUN_RE.fullmatch(char))
    other = sum(1 for char in text if not char.isspace() and not CJK_RUN_RE.fullmatch(char))
    return cjk + math.ceil(other / 4)

def render_line(entry: dict[str, Any]) -> str:
    title = str(entry.get("title") or entry.get("scope") or "Promoted knowledge")
    scope = str(entry.get("knowledge_scope") or "repository")
    return f"- [{scope}] {title}: {entry.get('summary', '')} (source: {entry.get('target_path', '')})"

def _stat_signature(index: Path) -> tuple[int, int]:
    try:
        stat = index.stat()
        return stat.st_size, stat.st_mtime_ns
    except OSError as exc:
        raise RetrievalError("authoritative index is unavailable") from exc

def _connect(path: Path, *, write: bool = False, readonly: bool = False) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        if readonly:
            connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
        else:
            connection = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SECONDS, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 250")
        if write:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            with contextlib.suppress(sqlite3.Error):
                connection.close()
        raise RetrievalError("retrieval database is unavailable") from exc


@contextlib.contextmanager
def file_lock(path: Path, timeout: float) -> Iterable[None]:
    """Serialize rebuild/usage writers without blocking hot retrieval reads."""
    deadline = time.monotonic() + timeout
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            if time.monotonic() >= deadline:
                raise TimeoutError("retrieval index writer is busy")
            time.sleep(.01)
    try:
        yield
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _rebuild_lock_path(store: Path) -> Path:
    return store / ".retrieval-rebuild.lock"


def _cleanup_orphan_temps(store: Path) -> None:
    """Only remove old abandoned rebuild files; a fresh concurrent writer is untouched."""
    cutoff = time.time() - REBUILD_ORPHAN_AGE_SECONDS
    for temporary in store.glob(".retrieval-rebuild-*.sqlite3*"):
        with contextlib.suppress(OSError):
            if temporary.stat().st_mtime < cutoff:
                temporary.unlink()

def _slug_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FIELD_ITEMS:
        raise ValueError(f"{name} must contain at most 16 values")
    result: list[str] = []
    for item in value:
        raw = str(item)
        text = normalize_text(item)
        if len(text) > 80 or raw != text or not SLUG_RE.fullmatch(text):
            raise ValueError(f"{name} contains an invalid lowercase slug")
        result.append(text)
    return sorted(set(result))

def _phrases(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FIELD_ITEMS:
        raise ValueError("negative_keywords must contain at most 16 values")
    result = [normalize_phrase(item) for item in value]
    if any(not item or len(item) > 80 for item in result): raise ValueError("negative_keywords contains an invalid value")
    return sorted(set(result))

def _path_globs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_FIELD_ITEMS:
        raise ValueError("path_globs must contain at most 16 values")
    result: list[str] = []
    for item in value:
        path = str(item).replace("\\", "/").strip()
        parts = path.split("/")
        if not path or len(path) > 200 or path.startswith("/") or re.match(r"^[A-Za-z]:/", path) or ".." in parts:
            raise ValueError("path_globs must be normalized relative paths")
        result.append(path)
    return sorted(set(result))

def validate_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = {key: _slug_list(record.get(key, []), key) for key in ("task_types", "languages", "tools", "platforms")}
    metadata["negative_keywords"] = _phrases(record.get("negative_keywords", []))
    metadata["path_globs"] = _path_globs(record.get("path_globs", []))
    try:
        minimum = float(record.get("min_score", DEFAULT_MIN_SCORE))
    except (ValueError, TypeError) as exc:
        raise ValueError("min_score must be numeric") from exc
    if not 0 <= minimum <= 20:
        raise ValueError("min_score must be between 0 and 20")
    supersedes = record.get("supersedes", [])
    if supersedes is None:
        supersedes = []
    if not isinstance(supersedes, list) or len(supersedes) > MAX_FIELD_ITEMS:
        raise ValueError("supersedes must be a bounded list")
    if any(not isinstance(item, str) or not FINGERPRINT_RE.fullmatch(item) for item in supersedes):
        raise ValueError("supersedes must contain fingerprints")
    metadata.update(min_score=minimum, supersedes=sorted(set(supersedes)))
    return metadata

def _records(store: Path, knowledge_scope: str) -> list[dict[str, Any]]:
    try:
        lines = (store / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RetrievalError("authoritative index is unavailable") from exc
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetrievalError("authoritative index is corrupt") from exc
        if not isinstance(entry, dict) or not str(entry.get("fingerprint") or "").strip() or not str(entry.get("summary") or "").strip():
            raise RetrievalError("authoritative index has an invalid promoted entry")
        entry = dict(entry)
        entry.update(validate_metadata(entry))
        entry["knowledge_scope"] = knowledge_scope
        entry["estimated_tokens"] = estimate_tokens(render_line(entry))
        result.append(entry)
    return result


def _audit_records(store: Path, knowledge_scope: str) -> tuple[list[dict[str, Any]], int]:
    """Read every JSONL line independently; malformed content is never returned."""
    try:
        lines = (store / INDEX_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    records: list[dict[str, Any]] = []
    invalid = 0
    for line in lines:
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("record is not an object")
            if not str(entry.get("fingerprint") or "").strip() or not str(entry.get("summary") or "").strip():
                raise ValueError("missing required fields")
            entry = dict(entry)
            entry.update(validate_metadata(entry))
            entry["knowledge_scope"] = knowledge_scope
        except (json.JSONDecodeError, TypeError, ValueError):
            invalid += 1
            continue
        records.append(entry)
    return records, invalid

def _schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE documents (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL, summary_norm TEXT NOT NULL, doc_len INTEGER NOT NULL, promoted_at TEXT NOT NULL DEFAULT '');
    CREATE TABLE postings (term TEXT NOT NULL, fingerprint TEXT NOT NULL, tf REAL NOT NULL, PRIMARY KEY(term,fingerprint));
    CREATE INDEX postings_fingerprint ON postings(fingerprint);
    CREATE TABLE term_stats (term TEXT PRIMARY KEY, df INTEGER NOT NULL);
    CREATE TABLE usage (fingerprint TEXT PRIMARY KEY, last_used_at TEXT, hit_count INTEGER NOT NULL DEFAULT 0, eligible_count INTEGER NOT NULL DEFAULT 0, miss_count_since_hit INTEGER NOT NULL DEFAULT 0);
    """)

def _weighted_terms(entry: dict[str, Any]) -> Counter[str]:
    terms = Counter(tokenize(entry.get("summary")))
    for keyword in entry.get("keywords", []) if isinstance(entry.get("keywords"), list) else []:
        terms.update({token: 3 for token in tokenize(keyword)})
    return terms

def _old_usage(path: Path) -> dict[str, tuple[Any, ...]]:
    if not path.is_file():
        return {}
    try:
        connection = _connect(path, readonly=True)
        try:
            return {
                str(row[0]): tuple(row[1:])
                for row in connection.execute(
                    "SELECT fingerprint,last_used_at,hit_count,eligible_count,miss_count_since_hit FROM usage"
                )
            }
        finally:
            connection.close()
    except (RetrievalError, sqlite3.Error):
        return {}

def _clean_sidecars(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            path.with_name(path.name + suffix).unlink()


def _require_budget(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise RetrievalError("retrieval index rebuild exceeded its budget")


def sync_scope(
    repo: Path | str,
    knowledge_scope: str = "repository",
    *,
    deadline: float | None = None,
    lock_timeout: float = REBUILD_LOCK_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Rebuild one derived scope transactionally; retain its previous healthy DB on failure."""
    store = store_for(repo, knowledge_scope)
    try:
        store.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RetrievalError("retrieval index rebuild failed") from exc
    try:
        with file_lock(_rebuild_lock_path(store), lock_timeout):
            _cleanup_orphan_temps(store)
            _require_budget(deadline)
            records = _records(store, knowledge_scope)
            size, mtime_ns = _stat_signature(store / INDEX_NAME)
            target = database_path(store)
            previous_usage = _old_usage(target)
            _require_budget(deadline)
            fd, raw = tempfile.mkstemp(prefix=".retrieval-rebuild-", suffix=".sqlite3", dir=store)
            os.close(fd)
            temporary = Path(raw)
            try:
                connection = _connect(temporary, write=True)
                try:
                    _schema(connection)
                    connection.execute("BEGIN IMMEDIATE")
                    for entry in records:
                        _require_budget(deadline)
                        terms = _weighted_terms(entry)
                        fingerprint = str(entry["fingerprint"])
                        connection.execute(
                            "INSERT INTO documents VALUES(?,?,?,?,?)",
                            (
                                fingerprint,
                                json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                                normalized_summary(entry["summary"]),
                                max(1, sum(terms.values())),
                                str(entry.get("promoted_at") or ""),
                            ),
                        )
                        connection.executemany(
                            "INSERT INTO postings VALUES(?,?,?)",
                            [(term, fingerprint, float(tf)) for term, tf in terms.items()],
                        )
                        connection.execute(
                            "INSERT INTO usage VALUES(?,?,?,?,?)",
                            (fingerprint, *previous_usage.get(fingerprint, (None, 0, 0, 0))),
                        )
                    _require_budget(deadline)
                    connection.execute("INSERT INTO term_stats SELECT term,COUNT(*) FROM postings GROUP BY term")
                    values = {
                        "schema": SCHEMA_VERSION,
                        "document_count": len(records),
                        "average_length": sum(max(1, sum(_weighted_terms(entry).values())) for entry in records)
                        / max(1, len(records)),
                        "index_size": size,
                        "index_mtime_ns": mtime_ns,
                        "last_successful_rebuild": _now(),
                    }
                    connection.executemany(
                        "INSERT INTO meta VALUES(?,?)",
                        [(key, str(value)) for key, value in values.items()],
                    )
                    connection.execute("COMMIT")
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
                _require_budget(deadline)
                check = _connect(temporary, readonly=True)
                try:
                    schema = check.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                    count = check.execute("SELECT COUNT(*) FROM documents").fetchone()
                    if schema is None or count is None or int(schema[0]) != SCHEMA_VERSION or count[0] != len(records):
                        raise RetrievalError("temporary index validation failed")
                finally:
                    check.close()
                _require_budget(deadline)
                os.replace(temporary, target)
            finally:
                _clean_sidecars(temporary)
    except (OSError, TimeoutError, sqlite3.Error, ValueError, RetrievalError) as exc:
        raise RetrievalError("retrieval index rebuild failed") from exc
    return {
        "schema": SCHEMA_VERSION,
        "document_count": len(records),
        "last_successful_rebuild": _now(),
        "in_sync": True,
        "needs_rebuild": False,
    }


def cold_repair_scope(
    repo: Path | str,
    knowledge_scope: str = "repository",
    *,
    deadline: float | None = None,
) -> bool:
    """Bounded SessionStart repair. Large or slow stores wait for index-rebuild."""
    store = store_for(repo, knowledge_scope)
    index = store / INDEX_NAME
    try:
        if not index.is_file() or index.stat().st_size > COLD_REPAIR_MAX_INDEX_BYTES:
            return False
    except OSError:
        return False
    try:
        sync_scope(
            repo,
            knowledge_scope,
            deadline=deadline if deadline is not None else time.monotonic() + COLD_REPAIR_BUDGET_SECONDS,
            lock_timeout=min(COLD_REPAIR_BUDGET_SECONDS, REBUILD_LOCK_TIMEOUT_SECONDS),
        )
    except RetrievalError:
        return False
    return True


def _read_index(store: Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = database_path(store)
    if not path.is_file():
        raise RetrievalError("retrieval index is missing")
    connection = _connect(path, write=not readonly, readonly=readonly)
    try:
        meta = dict(
            connection.execute("SELECT key,value FROM meta WHERE key IN ('schema','index_size','index_mtime_ns')")
        )
        size, mtime_ns = _stat_signature(store / INDEX_NAME)
        if (
            meta.get("schema") != str(SCHEMA_VERSION)
            or meta.get("index_size") != str(size)
            or meta.get("index_mtime_ns") != str(mtime_ns)
        ):
            raise RetrievalError("retrieval index is stale")
        return connection
    except (OSError, sqlite3.Error, ValueError, RetrievalError) as exc:
        with contextlib.suppress(sqlite3.Error):
            connection.close()
        if isinstance(exc, RetrievalError):
            raise
        raise RetrievalError("retrieval database is unavailable") from exc

def _context_values(context: dict[str, Any] | None, name: str) -> set[str]:
    values = (context or {}).get(name, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return set()
    return {normalize_text(value) for value in values if normalize_text(value)}

TASK_ALIASES = {"debug": ("debug", "bug", "fix", "调试", "修复"), "deploy": ("deploy", "deployment", "部署"), "refactor": ("refactor", "重构"), "research": ("research", "研究"), "test": ("test", "测试"), "review": ("review", "审查", "评审"), "docs": ("docs", "document", "文档"), "build": ("build", "构建", "编译"), "data": ("data", "数据"), "ops": ("ops", "operation", "运维"), "unity": ("unity",), "quant": ("quant", "量化")}
MARKERS = {
    "python": ("pyproject.toml", "requirements.txt"),
    "javascript": ("package.json",),
    "typescript": ("tsconfig.json",),
    "rust": ("cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle"),
    "csharp": (".sln", ".csproj"),
}
EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".cs": "csharp",
}


def _root_has_marker(names: set[str], marker: str) -> bool:
    return any(name.endswith(marker) for name in names) if marker.startswith(".") else marker in names

def derive_task_context(prompt: str, repo: Path | None = None, *, tool_names: Iterable[str] = (), tool_parameters: Iterable[Any] = ()) -> dict[str, list[str]]:
    root = repo.resolve() if repo else None
    paths: set[str] = set()

    def add_paths(value: Any) -> None:
        for raw in PATH_RE.findall(str(value or "")):
            if not root:
                continue
            try:
                candidate = Path(raw)
                resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
                relative = resolved.relative_to(root)
                paths.add(relative.as_posix())
            except (OSError, ValueError):
                continue

    add_paths(prompt)
    for parameter in list(tool_parameters)[:16]:
        add_paths(parameter)
    marker_names: set[str] = set()
    if root:
        with contextlib.suppress(OSError):
            marker_names = {child.name.casefold() for child in list(root.iterdir())[:64]}
    words = set(tokenize(prompt))
    prompt_norm = normalize_phrase(prompt)
    task_types = {
        kind
        for kind, aliases in TASK_ALIASES.items()
        if any(alias in words or _phrase_matches(alias, prompt_norm) for alias in aliases)
    }
    languages = {lang for suffix, lang in EXTENSIONS.items() if any(path.casefold().endswith(suffix) for path in paths)}
    languages.update(
        lang
        for lang, markers in MARKERS.items()
        if any(_root_has_marker(marker_names, marker) for marker in markers)
    )
    languages.update(lang for lang in MARKERS if lang in words)
    tools = {normalize_text(name) for name in tool_names if normalize_text(name)}
    tools.update(
        tool
        for tool in ("pytest", "unittest", "npm", "pnpm", "yarn", "cargo", "go", "dotnet", "git", "python")
        if tool in words or _phrase_matches(tool, prompt_norm)
    )
    if {"assets", "projectsettings"}.issubset(marker_names):
        task_types.add("unity")
        languages.add("csharp")
        tools.add("unity")
    platform = "windows" if os.name == "nt" else "linux" if os.name == "posix" else os.name
    platforms = {platform}
    platforms.update(item for item in ("windows", "linux", "macos", "android", "ios", "web") if item in words)
    return {
        "task_types": sorted(task_types)[:16],
        "paths": sorted(paths)[:MAX_PATHS],
        "languages": sorted(languages)[:16],
        "tools": sorted(tools)[:16],
        "platforms": sorted(platforms)[:16],
    }


def _phrase_matches(phrase: Any, text: Any) -> bool:
    normalized_phrase = normalize_phrase(phrase)
    normalized_text = normalize_phrase(text)
    if not normalized_phrase or not normalized_text:
        return False
    if CJK_RUN_RE.search(normalized_phrase):
        return normalized_phrase in normalized_text
    pattern = rf"(?<![a-z0-9_]){re.escape(normalized_phrase)}(?![a-z0-9_])"
    return re.search(pattern, normalized_text) is not None

def _gate(entry: dict[str, Any], prompt_phrase: str, context: dict[str, Any] | None) -> str | None:
    if any(_phrase_matches(keyword, prompt_phrase) for keyword in entry.get("negative_keywords", [])):
        return "negative"
    patterns = entry.get("path_globs", [])
    paths = _context_values(context, "paths")
    if patterns and (not paths or not any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in patterns)):
        return "path"
    return None

def _metadata_boost(entry: dict[str, Any], context: dict[str, Any] | None) -> float:
    boosts = {"task_types":1., "languages":.5, "tools":.75, "platforms":.5}
    result = sum(value for key, value in boosts.items() if set(entry.get(key, [])).intersection(_context_values(context, key)))
    if entry.get("path_globs") and any(fnmatch.fnmatchcase(path, pattern) for path in _context_values(context, "paths") for pattern in entry["path_globs"]): result += 1.
    return result

def _scope_candidates(repo: Path, scope: str, prompt: str, context: dict[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    started = time.monotonic()
    metrics = {
        "eligible": 0,
        "scored": 0,
        "below_threshold": 0,
        "filtered_negative": 0,
        "filtered_path": 0,
        "estimated_tokens": 0,
        "retrieval_ms": 0,
        "index_rebuilt": 0,
        "retrieval_degraded": False,
    }
    terms = Counter(tokenize(prompt))
    prompt_phrase = normalize_phrase(prompt)
    if not terms:
        return [], metrics, []
    try:
        store = store_for(repo, scope)
        if not (store / INDEX_NAME).is_file():
            return [], metrics, []
        connection = _read_index(store)
        try:
            placeholders = ",".join("?" for _ in terms)
            rows = connection.execute(
                "SELECT p.fingerprint,p.term,p.tf,d.payload,d.doc_len "
                "FROM postings p JOIN documents d ON d.fingerprint=p.fingerprint "
                f"WHERE p.term IN ({placeholders})",
                tuple(terms),
            ).fetchall()
            if not rows:
                return [], metrics, []
            stats = dict(
                connection.execute(f"SELECT term,df FROM term_stats WHERE term IN ({placeholders})", tuple(terms))
            )
            info = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('document_count','average_length')"))
            count = int(info["document_count"])
            average = float(info["average_length"])
            candidates: dict[str, tuple[dict[str, Any], int, dict[str, float]]] = {}
            for fingerprint, term, tf, payload, doc_len in rows:
                entry, length, tfs = candidates.setdefault(
                    str(fingerprint), (json.loads(payload), int(doc_len), {})
                )
                tfs[str(term)] = float(tf)
            accepted: list[dict[str, Any]] = []
            eligible: list[str] = []
            for fingerprint, (entry, length, tfs) in candidates.items():
                gate = _gate(entry, prompt_phrase, context)
                if gate:
                    metrics[f"filtered_{gate}"] += 1
                    continue
                metrics["eligible"] += 1
                eligible.append(fingerprint)
                score = 0.0
                for term, qtf in terms.items():
                    if term in tfs:
                        df = int(stats[term])
                        tf = tfs[term]
                        idf = math.log(1 + (count - df + .5) / (df + .5))
                        score += qtf * idf * (tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / average)))
                score += 2.0 * sum(
                    1 for keyword in entry.get("keywords", []) if _phrase_matches(keyword, prompt_phrase)
                )
                score += _metadata_boost(entry, context)
                if score < float(entry.get("min_score", DEFAULT_MIN_SCORE)):
                    metrics["below_threshold"] += 1
                    continue
                entry["knowledge_scope"] = scope
                entry["score"] = score
                entry["estimated_tokens"] = estimate_tokens(render_line(entry))
                accepted.append(entry)
                metrics["scored"] += 1
            return accepted, metrics, eligible
        finally: connection.close()
    except (RetrievalError, sqlite3.Error, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        metrics["retrieval_degraded"] = True
        return [], metrics, []
    finally:
        metrics["retrieval_ms"] = int((time.monotonic() - started) * 1000)

def _update_usage(repo: Path, scope: str, eligible: list[str], selected: set[str]) -> None:
    if not eligible:
        return
    store = store_for(repo, scope)
    try:
        with file_lock(_rebuild_lock_path(store), USAGE_LOCK_TIMEOUT_SECONDS):
            connection = _read_index(store)
            try:
                connection.execute("BEGIN IMMEDIATE")
                now = _now()
                for fingerprint in eligible:
                    if fingerprint in selected:
                        connection.execute(
                            "UPDATE usage SET eligible_count=eligible_count+1,hit_count=hit_count+1,"
                            "last_used_at=?,miss_count_since_hit=0 WHERE fingerprint=?",
                            (now, fingerprint),
                        )
                    else:
                        connection.execute(
                            "UPDATE usage SET eligible_count=eligible_count+1,miss_count_since_hit=miss_count_since_hit+1 "
                            "WHERE fingerprint=?",
                            (fingerprint,),
                        )
                connection.execute("COMMIT")
            finally:
                connection.close()
    except (RetrievalError, TimeoutError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        # A rebuild owns the writer lock. This bounded bookkeeping update may be dropped;
        # retrieval remains available and no partially rebuilt database is ever installed.
        return

def retrieve(repo: Path | str, prompt: str, task_context: dict[str, Any] | None = None, limits: dict[str, int] | None = None, *, error_logger: Callable[[str], None] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Public retrieval seam. It resolves both stores and owns ranking, dedupe and budget."""
    root = Path(repo).resolve()
    limits = limits or {}
    max_results = min(MAX_RESULTS, int(limits.get("max_results", MAX_RESULTS)))
    budget = min(MAX_CONTEXT_TOKENS, int(limits.get("max_tokens", MAX_CONTEXT_TOKENS)))
    total = {
        "eligible": 0,
        "scored": 0,
        "below_threshold": 0,
        "filtered_negative": 0,
        "filtered_path": 0,
        "estimated_tokens": 0,
        "retrieval_ms": 0,
        "index_rebuilt": 0,
        "retrieval_degraded": False,
    }
    per_scope: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}
    scopes = ("repository", "global")
    for scope in scopes:
        entries, metrics, eligible = _scope_candidates(root, scope, prompt, task_context)
        per_scope[scope] = (entries, eligible)
        for key in total:
            total[key] = (
                bool(total[key] or metrics[key])
                if key == "retrieval_degraded"
                else total[key] + int(metrics[key])
            )
    if total["retrieval_degraded"] and error_logger is not None:
        with contextlib.suppress(Exception):
            error_logger("retrieval_degraded")
    by_fingerprint: dict[str, dict[str, Any]] = {}
    for scope in ("global", "repository"):
        for entry in per_scope.get(scope, ([], []))[0]: by_fingerprint[str(entry["fingerprint"])] = entry
    by_summary: dict[str, dict[str, Any]] = {}
    for entry in by_fingerprint.values():
        key = normalized_summary(entry["summary"])
        current = by_summary.get(key)
        if (
            current is None
            or (entry["knowledge_scope"] == "repository" and current["knowledge_scope"] != "repository")
            or (
                entry["knowledge_scope"] == current["knowledge_scope"]
                and str(entry.get("promoted_at") or "") > str(current.get("promoted_at") or "")
            )
        ):
            by_summary[key] = entry
    def descending_text(value: Any) -> tuple[int, ...]:
        return tuple(-ord(character) for character in str(value or ""))
    ranked = sorted(by_summary.values(), key=lambda item: (-float(item["score"]), 0 if item["knowledge_scope"] == "repository" else 1, descending_text(item.get("promoted_at")), str(item["fingerprint"])))
    selected: list[dict[str, Any]] = []
    context_tokens = 0
    heading_tokens = estimate_tokens(CONTEXT_HEADING)
    for entry in ranked:
        cost = estimate_tokens(render_line(entry))
        entry["estimated_tokens"] = cost
        candidate_total = (heading_tokens if not selected else context_tokens) + cost
        if len(selected) < max_results and candidate_total <= budget:
            selected.append(entry)
            context_tokens = candidate_total
    total["estimated_tokens"] = context_tokens
    for scope, (_, eligible) in per_scope.items():
        selected_fingerprints = {
            str(item["fingerprint"]) for item in selected if item["knowledge_scope"] == scope
        }
        _update_usage(root, scope, eligible, selected_fingerprints)
    total["injected"] = len(selected)
    total["repository_hits"] = sum(item["knowledge_scope"] == "repository" for item in selected)
    total["global_hits"] = sum(item["knowledge_scope"] == "global" for item in selected)
    return selected, total

def index_status(repo: Path | str, knowledge_scope: str = "repository") -> dict[str, Any]:
    store = store_for(repo, knowledge_scope)
    result = {
        "schema": SCHEMA_VERSION,
        "document_count": 0,
        "in_sync": False,
        "needs_rebuild": True,
        "last_successful_rebuild": None,
    }
    try:
        connection = _read_index(store, readonly=True)
        try:
            meta = dict(connection.execute("SELECT key,value FROM meta"))
            result.update(
                schema=int(meta["schema"]),
                document_count=int(meta["document_count"]),
                in_sync=True,
                needs_rebuild=False,
                last_successful_rebuild=meta.get("last_successful_rebuild"),
            )
        finally:
            connection.close()
    except (RetrievalError, sqlite3.Error, OSError, ValueError, KeyError):
        pass
    return result

def _parse_time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

def _target_is_orphaned(repo: Path, scope: str, target: str) -> bool:
    if not target:
        return False
    if scope == "repository":
        candidate = (repo / target).resolve()
        try:
            candidate.relative_to(repo)
        except ValueError:
            return True
        return not candidate.exists()
    target = target.replace("$CODEX_HOME", str(_code_home()))
    if target.startswith("~") or os.path.isabs(target):
        return not Path(os.path.expanduser(target)).exists()
    return not (_code_home() / target).exists()

def audit_scope(repo: Path | str, knowledge_scope: str = "repository", now: datetime | None = None) -> dict[str, Any]:
    """Read-only audit; opens SQLite in mode=ro and never changes sidecars."""
    root = Path(repo).resolve()
    store = store_for(repo, knowledge_scope)
    current = now or datetime.now(timezone.utc)
    issues: list[dict[str, str]] = []
    records, invalid_records = _audit_records(store, knowledge_scope)
    issues.extend({"kind": "invalid-metadata"} for _ in range(invalid_records))
    active = {str(record.get("fingerprint")) for record in records}
    summaries: dict[str, list[str]] = {}
    usage: dict[str, tuple[Any, ...]] = {}
    try:
        connection = _read_index(store, readonly=True)
        try: usage = {str(row[0]): tuple(row[1:]) for row in connection.execute("SELECT fingerprint,last_used_at,miss_count_since_hit FROM usage")}
        finally:
            connection.close()
    except (RetrievalError, sqlite3.Error, OSError, ValueError):
        pass
    for record in records:
        fingerprint = str(record.get("fingerprint") or "unknown")
        summaries.setdefault(normalized_summary(record.get("summary")), []).append(fingerprint)
        if _target_is_orphaned(root, knowledge_scope, str(record.get("target_path") or "")):
            issues.append({"kind": "orphaned-target", "fingerprint": fingerprint})
        last_used, misses = usage.get(fingerprint, (None, 0))
        age = max(
            (moment for moment in (_parse_time(record.get("promoted_at")), _parse_time(last_used)) if moment),
            default=None,
        )
        try:
            stale = age is not None and current - age >= timedelta(days=90) and int(misses) >= 50
        except (TypeError, ValueError):
            stale = False
        if stale:
            issues.append({"kind": "stale-review", "fingerprint": fingerprint})
        if any(item in active for item in record.get("supersedes", [])):
            issues.extend(
                {"kind": "superseded-review", "fingerprint": old}
                for old in record["supersedes"]
                if old in active
            )
    for members in summaries.values():
        if len(members) > 1:
            issues.extend({"kind": "exact-duplicate", "fingerprint": fingerprint} for fingerprint in members)
    counts = Counter(item["kind"] for item in issues)
    kinds = ("stale-review", "exact-duplicate", "superseded-review", "orphaned-target", "invalid-metadata")
    return {"counts": {key: counts[key] for key in kinds}, "issues": issues}
