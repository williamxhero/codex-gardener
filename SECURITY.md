# Security Policy

## Reporting a vulnerability

Please report security and privacy vulnerabilities privately through [GitHub Security Advisories](https://github.com/williamxhero/codex-gardener/security/advisories/new). Do not open a public issue containing exploit details, secrets, private paths, transcripts, or personal data.

Include the affected version, operating system, Codex version, reproduction steps, impact, and any suggested mitigation. Maintainers will acknowledge a report when practical; no response-time guarantee is offered.

## Trust model

Codex Gardener runs local Python commands at Codex lifecycle boundaries. Review `hooks/hooks.json` and the three bundled scripts before trusting the hooks through `/hooks`. The project-boundary hook is deliberately conservative but cannot parse every shell, tool, path, symlink, or nested repository arrangement. It is a guardrail, not a security sandbox.

Knowledge candidates are untrusted input. Review evidence and scope before promotion, keep secrets out of summaries, inspect every diff, and preserve normal repository review and CI controls. Global promotion has wider impact and must remain confirmation-gated. Project fingerprints are diversity signals, not security identities or proof of project independence. Install only from a repository and Git ref you trust.

Deferred capture markers are also untrusted input. The Stop Hook accepts them only from the current repository and hashed session directory, rejects unknown fields or mismatched session/schema/fingerprint data, and derives all write destinations itself. Invalid markers fail open without being promoted or falsely counted as a no-candidate review.

Deferred audit completion markers are untrusted input too. Stop accepts them only from the current repository and hashed session directory, rejects unknown fields and mismatched schema/session/run-kind/completion data, derives the checkpoint destination from trusted `PLUGIN_DATA`, and applies a hashed completion-ID idempotency check. Invalid, missing, corrupt, or unwritable checkpoint data fails open: it cannot falsely complete an audit, mutate knowledge, or loop the current turn.

Deferred maintenance outcomes are untrusted input. Stop accepts them only from the maintenance repository and hashed maintenance-session directory, rejects unknown fields, validates the schema, outcome, opaque pending ID, candidate bounds, and fingerprint, and requires the ID to belong to the state-bounded batch. It derives the original task, repository/global store, and project fingerprint from trusted `PLUGIN_DATA`; the marker cannot supply those paths or identities. Pending selection uses expiring atomic claims, and final outcome handling serializes on the opaque ID before rechecking active state, so concurrent maintenance tasks cannot commit conflicting results. Structurally invalid pending objects are ignored rather than allowed to starve the bounded batch. Missing, invalid, corrupt, or mismatched markers fail open and leave the pending item unresolved. Repeated second Stop delivery is idempotent.

Scheduled audits are read-only by contract. Ordinary count/time deadlines never interrupt a task. The curator may inspect local effectiveness and knowledge artifacts, but must not promote, resolve, discard, edit, or delete them during a scheduled audit continuation. Maintenance continuations have the same artifact boundary and may only create deferred pending outcomes. Completion markers are operational acknowledgements, not proof that findings are correct or that the local event log is tamper-proof.

Ordinary Stop processing is short and never waits for a model. The Stop command has a 15-second timeout because a scheduled maintenance second Stop may validate and commit three independently locked outcomes. SessionEnd remains capped at three seconds and only performs the idempotent pending fallback plus state cleanup.

The exact scheduled prompt markers are routing signals, not authentication tokens. Use them only in the fixed Gardener maintenance and audit tasks. Near matches do nothing, but any task that deliberately includes an exact marker can request the corresponding bounded continuation.

Effectiveness logs are local operational records, not a security boundary or tamper-proof audit trail. Their schema rejects fields outside a small allowlist and hashes correlating identifiers, but local users and processes with filesystem access can read, change, or delete them. Logging and rotation failures fail open so they never interrupt a Codex hook or Gardener CLI operation. Set `CODEX_GARDENER_EFFECTIVENESS_LOG=0` to disable these events.

The v0.5.2 global metadata migration recognizes only the two exact historical cross-project delegation Skill targets, including their slash-equivalent Windows forms. It strictly parses the complete Gardener-owned JSONL file under a per-file lock before atomically replacing it; corrupt or unreadable data fails open without rewrite, and unrelated targets and standalone Skill files are not touched.

Do not keep both `codex-gardener@personal` and `codex-gardener@codex-gardener` enabled: Codex may run matching Hooks from both installations. Version `0.4.0` reports this duplicate state and the cloned installer fails with an explicit removal command rather than deleting another installation. The health report prints local data, log, and standalone Skill paths for diagnosis; avoid publishing that report without reviewing those paths.

This project is community-maintained and is not an official OpenAI product.
