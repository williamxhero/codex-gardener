# Durable knowledge and recovery

Codex Gardener keeps the plugin itself portable while keeping runtime learning private. Version control should contain the plugin manifest, Hooks, Skills, tests, and reviewed formal knowledge such as concise AGENTS.md guidance or documentation. These artifacts are restored by cloning the repository and reinstalling the plugin.

The following data is intentionally local and ignored: candidate and promoted JSONL stores, pending-review markers, effectiveness logs, short-lived session state, and derived SQLite retrieval indexes. They can contain repository-specific evidence, paths, or other private metadata and are not a backup format. Do not commit them or copy prompts, transcripts, tool input/output, secrets, or absolute paths into the repository.

To recover after reinstalling a machine, clone this repository, run python install.py (or python3 install.py), then review and trust the Hooks in /hooks and start a new task. Recreate runtime learning through normal reviewed capture and curation; only explicitly promoted, privacy-safe rules should be added to versioned project files. The installer rebuilds derived indexes from the local stores and never replaces unrelated local records.
