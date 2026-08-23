# Privacy

Codex Gardener is a local Codex plugin. Its Python code makes no network requests and includes no network telemetry, remote analytics service, or credential collection. Its optional local effectiveness log lets users audit whether the plugin is useful. Codex itself may communicate with services according to your Codex configuration and OpenAI's applicable policies.

## Data processed

Lifecycle hooks receive Codex hook event JSON on standard input. The plugin derives small signals such as whether a workspace changed, a command failed repeatedly, or a correction was made. It does not intentionally persist raw prompts, raw tool output, file contents, secrets, credentials, or transcript contents.

The plugin may store:

- short-lived task state and a pending-review queue under `PLUGIN_DATA`, `CODEX_GARDENER_DATA`, or `$CODEX_HOME/codex-gardener-data/`;
- repository paths, task/session identifiers, signal names, timestamps, and—when Codex supplies it—a local transcript path, but not transcript contents;
- repository-scoped candidate summaries, resolutions, and indexes under `<repository>/.codex/learning/`;
- global candidate summaries, resolutions, and indexes under `$CODEX_HOME/codex-gardener-global-learning/`.
- append-only effectiveness events under `<plugin-data>/effectiveness/`.

Version `0.4.0` preserves and copies legacy user-level `$CODEX_HOME/learning/` JSONL into the v2 global store with migration provenance. It does not delete or rewrite the legacy source. The local effectiveness report may print resolved runtime, log, and standalone Skill paths for diagnosis, but those raw paths are not stored in effectiveness events.

Effectiveness events have a strict field allowlist. They may contain counts, fixed categories, timestamps, and truncated SHA-256 hashes used to correlate sessions and projects. They never intentionally contain prompt text, tool input or output, transcript content or paths, file content, secrets, raw repository paths, or raw session/turn IDs. The logger does not record ordinary tool calls. Its active JSONL file rotates at 1 MiB, keeps at most four backups, and removes rotated files older than 90 days. Logging failures fail open and cannot block hooks or CLI operations.

Candidate records contain an explicit knowledge scope, topical scope, concise lesson and evidence summary, confidence, target recommendation, session ID, timestamp, and a one-way project fingerprint. The fingerprint is derived from a normalized Git remote identity when available, otherwise a normalized repository path. Only the truncated SHA-256 hash is stored in candidate records; it is intended to measure evidence diversity, not to anonymize a guessable repository identity.

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
