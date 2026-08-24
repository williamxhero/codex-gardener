# Retrieval reference

`index.jsonl` is the authoritative promoted retrieval artifact. `retrieval.sqlite3` is a replaceable local derivative. Never hand-edit SQLite and never place prompt, transcript, tool output, query term, or secret data in either artifact.

Use `index-status` to observe derived-index health, `index-rebuild` to rebuild it from JSONL, and read-only `index-audit` to review stale entries (90 days plus 50 misses), normalized-summary duplicates, superseded entries, orphaned targets, and invalid metadata. A bad or locked derived index fails open; repair it explicitly rather than inventing a JSON fallback.

At promotion, supply only narrow approved metadata: lowercase task types/languages/tools/platforms, relative path globs, normalized negative phrases, a bounded minimum score, and repeatable superseded fingerprints. Scope, negative, and path metadata filter; other matching metadata boosts. `estimated_tokens` is computed from the final rendered injection line and is never curator input. Audits only report. To disable active retrieval, record an explicit `retired` resolution with `stale`, `duplicate`, or `superseded` reason; never auto-retire.
