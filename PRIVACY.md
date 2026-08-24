# Privacy

## Retrieval index

Version 0.7.0 derives a local SQLite index only from the promoted `index.jsonl` record: documents, postings, term statistics, approved metadata, and per-scope aggregate usage counters (`last_used_at`, hit, eligible, and miss counts). The authoritative JSONL remains separate. Hot retrieval reads neither its body nor a hash: it compares only stored `size` and `mtime_ns`. Prompts, tool input/output, query terms, raw paths discovered from prompts, transcripts, secrets, embeddings, and network telemetry are never persisted by retrieval. A missing, stale, corrupt, or busy index fails open without JSON fallback.

Codex Gardener is a local Codex plugin. Its Python code makes no network requests and includes no network telemetry, remote analytics service, or credential collection. Its optional local effectiveness log lets users audit whether the plugin is useful. Codex itself may communicate with services according to your Codex configuration and OpenAI's applicable policies.

## Data processed

Lifecycle hooks receive Codex hook event JSON on standard input. The plugin derives small signals such as whether a workspace changed, a command failed repeatedly, or a correction was made. It does not intentionally persist raw prompts, raw tool output, file contents, secrets, credentials, or transcript contents.

The plugin may store:

- short-lived task state and a pending-review queue under `PLUGIN_DATA`, `CODEX_GARDENER_DATA`, or `$CODEX_HOME/codex-gardener-data/`; new pending records use an opaque stable ID and bounded signal metadata, while maintenance claims add only that ID, a hashed claim owner, and a timestamp;
- repository paths, task/session identifiers, signal names, timestamps, and—when Codex supplies it—a local transcript path, but not transcript contents;
- repository-scoped candidate summaries, resolutions, and indexes under `<repository>/.codex/learning/`;
- short-lived, ignored deferred candidate markers under `<repository>/.codex/learning/deferred-captures/` until the second Stop Hook consumes them;
- short-lived, ignored audit completion markers under `<repository>/.codex/learning/deferred-audits/` until Stop consumes them;
- short-lived, ignored maintenance outcome markers under `<maintenance-repository>/.codex/learning/deferred-maintenance/` until the second Stop consumes them;
- global candidate summaries, resolutions, and indexes under `$CODEX_HOME/codex-gardener-global-learning/`;
- append-only effectiveness events under `<plugin-data>/effectiveness/`;
- an automatic-audit checkpoint under `<plugin-data>/audit-checkpoint.json`.

Version `0.4.0` preserves and copies legacy user-level `$CODEX_HOME/learning/` JSONL into the v2 global store with migration provenance. It does not delete or rewrite the legacy source. Version `0.5.2` may rewrite only the `target_path` field for two exact historical cross-project delegation Skill targets in the Gardener-owned v2 global index and resolution files; it does not modify the legacy source or either standalone or bundled Skill file. The local effectiveness report may print resolved runtime, log, and standalone Skill paths for diagnosis, while `pending` and `maintenance-status` may print trusted pending metadata for local review. Inspect those reports before sharing them; raw paths are not stored in effectiveness events or deferred maintenance markers.

Effectiveness events have a strict field allowlist. They may contain counts, fixed categories, validated `real` or `smoke` run kinds, timestamps, and truncated SHA-256 hashes used to correlate sessions and projects. Audit events contain only a fixed reason, run kind, and hashed session/project identities. They never intentionally contain prompt text, tool input or output, transcript content or paths, file content, secrets, raw repository paths, or raw session/turn IDs. The logger does not record ordinary tool calls. Its active JSONL file rotates at 1 MiB, keeps at most four backups, and removes rotated files older than 90 days. Logging failures fail open and cannot block hooks or CLI operations.

Candidate records contain an explicit knowledge scope, topical scope, concise lesson and evidence summary, confidence, target recommendation, session ID, timestamp, and a one-way project fingerprint. The fingerprint is derived from a normalized Git remote identity when available, otherwise a normalized repository path. Only the truncated SHA-256 hash is stored in candidate records; it is intended to measure evidence diversity, not to anonymize a guessable repository identity.

Deferred markers contain the same concise candidate fields except repository path and project fingerprint. They never contain prompts, transcripts, tool input/output, or caller-supplied target paths. The Stop Hook derives the repository and project fingerprint from trusted session state, validates the marker, writes the formal candidate, and then removes the marker.

Deferred audit markers contain only schema/type, session ID, a random completion ID, validated run kind, and creation time; they contain no audit findings or paths. The checkpoint contains initialization/completion times plus hashed successful-audit session and completion identities, but no prompt, transcript, findings, repository path, or raw session ID. The exact unconditional and conditional scheduled-audit prompt markers are each reduced immediately to a boolean session signal; prompt text is not persisted.

Deferred maintenance markers contain the maintenance session ID, opaque pending ID, outcome type, creation time, and—only for a candidate—the same bounded lesson fields used by capture. Before writing, the CLI cross-checks the active pending record and rejects lesson text containing its source session, repository path, or transcript path; Stop repeats that validation before accepting a marker. Markers never contain those original identifiers, prompts, or tool output. The unsandboxed Stop Hook accepts only IDs in the requested bounded batch, maps them to trusted pending data, and derives repository/global destinations itself. The exact maintenance prompt marker is also reduced immediately to a boolean.

The JSONL filenames are listed in a `.gitignore` inside each learning store. They remain local unless you deliberately inspect, copy, sync, or commit them. Promoted repository knowledge may become repository files after review. Promoted global knowledge may become `$CODEX_HOME/AGENTS.md` or a Skill under `$CODEX_HOME/skills/`, but only after the curator proposes the exact change and receives explicit user confirmation.

## Control and deletion

Disable or untrust the hooks in Codex to stop lifecycle processing. Uninstalling the plugin does not delete local data. After reviewing the exact paths, remove any of the following yourself:

```text
<repository>/.codex/learning/
$CODEX_HOME/codex-gardener-global-learning/
$CODEX_HOME/learning/  (legacy source, when present)
$CODEX_HOME/codex-gardener-data/
```

Set `CODEX_GARDENER_EFFECTIVENESS_LOG=0` before starting Codex to disable effectiveness events while leaving the rest of Gardener available. If `PLUGIN_DATA` or `CODEX_GARDENER_DATA` is set, runtime data may be stored there instead. Avoid personal data, secrets, private URLs, and raw source text in candidate summaries. If you discover a privacy issue, follow [SECURITY.md](SECURITY.md).
