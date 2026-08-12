# Privacy

Codex Gardener is a local, source-available Codex plugin. Its Python code makes no network requests and includes no telemetry, analytics, remote service, or credential collection. Codex itself may communicate with services according to your Codex configuration and OpenAI's applicable policies.

## Data processed

Lifecycle hooks receive Codex hook event JSON on standard input. The plugin derives small signals such as whether a workspace changed, a command failed repeatedly, or a correction was made. It does not intentionally persist raw prompts, tool output, file contents, secrets, or transcript contents.

The plugin may store:

- short-lived per-task state and a pending-review queue under `PLUGIN_DATA`, `CODEX_GARDENER_DATA`, or the Codex home data directory;
- repository path, task/session identifiers, signal names, timestamps, and—when Codex supplies it—a local transcript path (not transcript contents);
- concise, user-reviewable learning candidates under `<repository>/.codex/learning/`.

The generated JSONL learning files are added to a repository-local `.gitignore`. They remain on the local machine unless you deliberately inspect, copy, or commit their contents. Promoted knowledge is written only through the curator workflow and may become part of repository files after its evidence and safety checks pass.

## Control and deletion

Disable or untrust the hooks in Codex to stop collection. Uninstalling the plugin does not delete local data. Delete the plugin data directory and the repository's `.codex/learning/` directory yourself after reviewing the paths. See the README for default locations.

Avoid recording sensitive information in candidate summaries. If you discover a privacy issue, follow [SECURITY.md](SECURITY.md).
