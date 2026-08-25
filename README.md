# Codex Gardener

Codex Gardener turns lessons from completed Codex tasks into small, verified knowledge at the right scope. Project-specific facts stay with one repository; genuinely portable principles and workflows can become available across all projects.

Capture and promotion are separate. The plugin records concise candidates first, accumulates independent evidence, then helps a curator challenge scope, conflicts, sensitivity, and target before anything becomes lasting guidance.

It also includes an opt-in cross-project write guard. When a task attempts to write into another Git repository, the guard points Codex to a bundled delegation workflow so the target project remains the implementation context. The same workflow requires a unique Git worktree and branch for each concurrent writer that may touch one repository or overlapping plugin files.

> Codex Gardener is an independent community project. It is not an official OpenAI product.

## Requirements

- Codex CLI with `codex plugin` marketplace support
- Python 3.10 or newer on `PATH` (`python` on Windows, `python3` on Linux/macOS)
- Git, for repository detection and Git marketplace installation

The runtime and installer use only the Python standard library.

## Install from Git

If `codex plugin list --json` shows `codex-gardener@personal` enabled, remove that legacy installation first with `codex plugin remove codex-gardener@personal`; otherwise both copies can contribute matching Hooks and Skills.

```bash
codex plugin marketplace add williamxhero/codex-gardener
codex plugin add codex-gardener@codex-gardener
```

Start a new Codex task after installation. Run `/hooks`, inspect every command in `hooks/hooks.json`, and explicitly trust the hooks only if you are comfortable with their local behavior. Hooks remain opt-in.

Version `0.4.3` uses a quote-free outer Windows command that invokes `scripts/codex-gardener-hook.cmd`; quoted Python setup stays inside the wrapper so Codex CLI `0.147.0` can launch Hooks reliably. The wrapper also defaults Python to UTF-8 mode so raw UTF-8 Hook JSON—including Chinese prompts and Unicode paths—is decoded correctly on GBK Windows systems. Explicit user `PYTHONUTF8` or `PYTHONIOENCODING` settings are preserved. The outer command cannot quote `%PLUGIN_ROOT%`, so Windows plugin roots containing spaces remain unsupported by this workaround. Codex's normal marketplace cache path does not contain spaces.

## Install from a clone

```bash
git clone https://github.com/williamxhero/codex-gardener.git
cd codex-gardener
python install.py --dry-run
python install.py
```

Use `python3` when `python` is unavailable. The installer locates `codex`, validates the checkout, safely reuses a matching local marketplace, installs `codex-gardener@codex-gardener`, and prints hook-trust and new-task guidance. It stops if the marketplace name already points elsewhere or the legacy `codex-gardener@personal` is still enabled. It never removes another installation silently; follow its explicit removal command and rerun it.

## Architecture

| Component | Role |
| --- | --- |
| `gardener.py` lifecycle hooks | Track bounded task signals, retrieve promoted repository and global context, silently queue reviews, and reserve continuations for fixed maintenance tasks. |
| `$codex-gardener:gardener-capture` | Manually classify a lesson and defer concise evidence for the unsandboxed Stop Hook without editing promoted artifacts. |
| `$codex-gardener:knowledge-curator` | Process bounded scheduled maintenance, run read-only quality audits, or explicitly curate both stores and promote only sufficiently supported knowledge. |
| `effectiveness.py` | Append privacy-bounded local events and produce deterministic effectiveness reports without network telemetry. |
| `project_boundary.py` | Conservatively deny detected writes from one Git repository into another. |
| `$codex-gardener:cross-project-delegation` | Move authorized implementation into its owning repository and isolate concurrent writers in unique Git worktrees. |

### Two knowledge scopes

- `repository` is the default. Use it for language, framework, architecture, layout, team, deployment, data-contract, and other project-dependent knowledge.
- `global` is for lessons that stay correct across unrelated repositories without relying on project conventions. Uncertainty defaults to `repository`.

Repository candidates, resolutions, and retrieval indexes live under:

```text
<repository>/.codex/learning/
```

Global candidates, resolutions, and retrieval indexes live under:

```text
$CODEX_HOME/codex-gardener-global-learning/
```

Both stores contain a `.gitignore` for their JSONL data. Missing `CODEX_HOME` defaults to `~/.codex`. Runtime state and pending-review records use the official `PLUGIN_DATA` supplied to Hooks. Gardener writes its resolved location atomically to `$CODEX_HOME/codex-gardener-data-path` so a later CLI audit can find the same data; `CODEX_GARDENER_DATA` remains an explicit diagnostic/test override and `$CODEX_HOME/codex-gardener-data/` is the last-resort fallback.

Version `0.4.4` makes capture safe under normal `workspace-write` execution. The continued model writes validated `defer-record` markers only under ignored `<repository>/.codex/learning/deferred-captures/`; it never needs model-shell access to plugin data or the global store. The second Stop Hook validates session, schema, allowed fields, scope, target, confidence, and fingerprint, then writes formal repository or global candidates and effectiveness events outside the model sandbox. Repeated Stop delivery is idempotent by fingerprint and session. A no-candidate review runs no model command and is recorded directly by the second Stop Hook. The original `record` and `review-complete` CLI commands remain available for backward-compatible direct use outside restricted model execution.

Version `0.5.0` added a read-only knowledge-quality audit. It becomes due after either 10 unique completed real reviews or 7 elapsed days since the checkpoint was initialized or last successfully audited, whichever happens first. A qualifying review must contain a `review_requested` event and a terminal `capture_recorded` or `review_completed_no_candidate` event for the same session. Duplicate sessions, incomplete reviews, smoke runs, and pre-0.5 unlabelled events do not count.

The due state remains visible in `audit-status`. Starting with `0.6.0`, count/time deadlines never request a continuation in an ordinary task. A fixed recurring task with the exact `[codex-gardener:scheduled-audit-check]` marker can request one `$codex-gardener:knowledge-curator` audit-only continuation when due. It writes only an ignored `defer-audit-complete` marker; the unsandboxed second Stop validates it, updates the checkpoint under `PLUGIN_DATA`, and records an `audit_completed` event. A missing or invalid marker never advances the checkpoint.

Version `0.5.1` prevents tools used inside a successfully completed audit continuation from being queued as an unfinished capture at SessionEnd. Smoke audits remain observable but intentionally do not reset the real audit checkpoint or count toward its review threshold.

Version `0.5.2` separates a group's evidence maturity (`evidence_status`) from its effective top-level `status`; the latest `proposed`, `promoted`, or `discarded` resolution now wins without hiding the underlying candidate/confirmed/promotable evidence level. Effectiveness JSON keeps effective counts in `candidate_group_status` and adds parallel evidence counts in `candidate_group_evidence_status`. This version also replaces only the exact legacy standalone cross-project Skill targets in Gardener-owned global index and resolution metadata with the bundled plugin path. Read-only reports normalize those targets in memory, while SessionStart performs the locked, atomic, idempotent persisted migration without changing standalone Skill files or the legacy source store.

Version `0.6.0` makes ordinary task completion silent. Stop never emits `decision: block` for ordinary capture or a count/time audit. When bounded signals exist, it atomically queues one pending review under `PLUGIN_DATA` and returns `{"continue": true}`. SessionEnd never duplicates that record; if a session ends before Stop was delivered, it may create the same idempotent fallback record before removing short-lived state. The queue stores an opaque stable pending ID plus bounded metadata; it never stores prompt or tool output. A fixed task containing the exact `[codex-gardener:scheduled-maintenance]` marker may request one curator continuation only when pending work exists. It atomically claims at most three items so concurrent maintenance tasks cannot review the same pending work; abandoned claims expire after one hour. Maintenance writes ignored `defer-pending-outcome` markers under its own repository. Those markers contain no original repository, transcript, or source-session path. The unsandboxed second Stop maps each opaque ID to trusted plugin data, writes repository or global candidates through existing locked stores, records no-candidate outcomes, and resolves pending work idempotently. Maintenance cannot promote or resolve candidate status, run an audit, or edit formal knowledge artifacts. Stop has a 15-second timeout so a full three-item locked batch can finish without changing the three-second SessionEnd limit. The v0.4.4 deferred-capture path remains supported for explicit manual capture and legacy in-flight continuations.

Version `0.6.1` closes conditional audit scheduling without bringing continuations back to ordinary tasks. A fixed task containing `[codex-gardener:scheduled-audit-check]` requests an audit-only continuation only when `audit-status` is due after 10 qualifying real reviews or 7 elapsed days. When it is not due, Stop returns `{"continue": true}`. The unconditional `[codex-gardener:scheduled-audit]` marker remains available. In a combined maintenance and audit-check task, a due audit runs before pending maintenance so a sustained backlog cannot starve the audit; after a successful audit checkpoint, the next scheduled run resumes maintenance.

Version `0.7.0` keeps `index.jsonl` authoritative and adds a derived, standard-library SQLite retrieval index. It uses NFKC/casefold identifier and CJK tokenization, BM25 (`k1=1.2`, `b=.75`), bounded metadata boosts, and hard scope/negative/path filters. Retrieval is capped at three complete entries and 500 estimated tokens; repository guidance wins global guidance for the same normalized summary. Prompts, tool output, query terms, embeddings, and network access are never persisted or used. Corrupt, locked, or stale derived indexes fail open, emit only an anonymous retrieval error metric, and never fall back to unbounded JSON scanning. SessionStart may repair only a small index within a fixed budget; explicit `index-rebuild` handles larger or slow recovery. The local learning `.gitignore` covers SQLite derivatives and all deferred marker directories.

The same release makes pending maintenance evidence-aware. A new review is queued only when trusted hook metadata names a regular readable transcript at queue time; null, missing, and unreadable metadata create only an anonymous `missing` or `unreadable` effectiveness event. Each new pending item keeps that verified private path and a bounded safe capsule (signals, counters/booleans, normalized task context, and at most 32 already-permitted repository-relative paths), never transcript text, prompts, or tool input/output. Maintenance prioritizes currently readable evidence and reports aggregate backlog health without rendering capsules. Old pending records remain compatible. Use `pending-prune-unreviewable --confirm` only as an explicit local cleanup: it terminalizes active records whose evidence has since vanished, writes no candidate or formal knowledge, and is idempotent.

## Local effectiveness audit

Version `0.3.0` adds a modest append-only JSONL audit under the existing plugin data root:

```text
<plugin-data>/effectiveness/events.jsonl
```

It records only allow-listed derived events: session and project hashes, promoted-context lookup and hit counts split by repository/global scope, safe retrospective signal categories, capture scope/target/confidence bucket, no-candidate completion, audit requests/completions and fixed reasons, pending queue changes, resolution distributions, and cross-project denial categories. Relevant review and audit events include a validated `real` or `smoke` run kind. It does not record prompts, tool input/output, transcripts, file contents, secrets, raw paths, or raw session/turn IDs. Ordinary tool calls are not logged.

The active log rotates at 1 MiB, keeps at most four bounded backups, and discards rotated files older than 90 days. Logging uses only the Python standard library, is concurrency-safe, and always fails open. Disable it before starting Codex with:

```bash
CODEX_GARDENER_EFFECTIVENESS_LOG=0
```

On PowerShell use `$env:CODEX_GARDENER_EFFECTIVENESS_LOG = "0"`. Generate a human-readable 14-day report or deterministic JSON with:

```bash
python <plugin-root>/scripts/gardener.py effectiveness
python <plugin-root>/scripts/gardener.py effectiveness --since-days 14 --json
python <plugin-root>/scripts/gardener.py effectiveness --since-days 14 --repo /path/to/repo --json
python <plugin-root>/scripts/gardener.py audit-status --repo /path/to/repo
python <plugin-root>/scripts/gardener.py audit-status --repo /path/to/repo --initialize
python <plugin-root>/scripts/gardener.py maintenance-status
python <plugin-root>/scripts/gardener.py index-status --repo /path/to/repo
python <plugin-root>/scripts/gardener.py index-rebuild --repo /path/to/repo
python <plugin-root>/scripts/gardener.py index-audit --repo /path/to/repo
```

`audit-status` initializes a missing v0.5 checkpoint so its time deadline begins; `--initialize` is accepted for callers that want to make that first-use intent explicit. Without `--repo`, the effectiveness report remains useful across all observed projects. Supplying a repository additionally reports its current pending count and repository/global candidate-group status counts. JSON includes a `health` block with the plugin ID/version, enabled Gardener plugin IDs, duplicate legacy IDs, standalone cross-project Skill detection, data-root source, resolved local data/log paths, log existence, latest event time, audit checkpoint metadata, and an explicit `observed`, `not_observed`, `unreadable`, or `logging_disabled` status. It also reports `audit_requested` and `audit_completed` totals and distributions. A missing log is therefore never presented as a healthy all-zero window. These paths are printed only in the local report and are never written into effectiveness events.

The schedule defaults can be overridden before Codex starts:

```bash
CODEX_GARDENER_AUDIT_THRESHOLD=10
CODEX_GARDENER_AUDIT_MAX_DAYS=7
CODEX_GARDENER_RUN_KIND=real
```

Thresholds must be positive integers; invalid values safely fall back to 10 reviews and 7 days. `CODEX_GARDENER_RUN_KIND` accepts only `real` or `smoke` and otherwise falls back to `real`. Use `smoke` for manual Hook or end-to-end tests so they never advance the real-review counter or real audit checkpoint.

For a fixed recurring audit-check automation, include the exact marker `[codex-gardener:scheduled-audit-check]` in its prompt and tell the first response not to audit directly. Stop requests the audit-only curator continuation only when the 10-review or 7-day checkpoint is due; otherwise it stays silent. Use `[codex-gardener:scheduled-audit]` only when that fixed task should force an audit regardless of the checkpoint. Near matches do not trigger either marker. If an initial response already completed a requested audit and left a valid marker, Stop consumes that marker before considering another request.

For a fixed daily or otherwise dedicated maintenance task, include the exact marker `[codex-gardener:scheduled-maintenance]` and tell the first response not to review anything directly. Stop continues only when pending work exists, supplies at most three opaque IDs, and lets the curator create one deferred candidate or no-candidate outcome per ID. `maintenance-status` reports the bounded batch and current audit status; pass each supplied ID with `--pending-id` when another maintenance task may be running. The same fixed prompt may also contain `[codex-gardener:scheduled-audit-check]`: a due audit takes priority, while a not-due audit check leaves pending maintenance free to run. After the due audit is checkpointed, the next scheduled run resumes maintenance. Do not use scheduled markers in ordinary prompts.

## Learning and promotion model

Each new candidate records an explicit `knowledge_scope`, its existing topical `scope`, a concise lesson and evidence summary, confidence, session ID, target recommendation, and a one-way project fingerprint. The fingerprint is derived from Git remote identity when available, otherwise the resolved repository path; the raw identity is not stored in the candidate.

Repository eligibility:

1. One independent session is a candidate.
2. Two independent sessions make it confirmed.
3. Three or more sessions with aggregate confidence of at least `0.85` make it eligible for repository promotion after conflict checks.

Global evidence keeps the same ladder: one session is a candidate and two are confirmed. Three or more sessions below aggregate confidence `0.85` remain confirmed. Confidence-qualified evidence from only one project is proposed rather than eligible. Global eligibility requires at least three independent sessions, aggregate confidence of at least `0.85`, and evidence from at least two distinct project fingerprints. A fingerprint is a diversity signal, not proof that projects are unrelated. The curator must inspect representative project conventions and choose the narrowest valid scope.

Global changes have higher blast radius. Even eligible global guidance requires explicit user confirmation before writing `$CODEX_HOME/AGENTS.md` or `$CODEX_HOME/skills/`. Concise global principles belong in global `AGENTS.md`; portable multi-step workflows usually belong in a validated global Skill. Global hooks, configuration, and plugin changes remain proposal-only.

Promoted global keyword matches are available in every project context, including non-Git directories. Repository entries are retrieved only inside their owning repository. When the same fingerprint exists in both indexes, the repository entry wins so combined context does not duplicate the lesson.

## Usage

Normal use is passive and silent after hook trust. Ordinary tasks never receive a Gardener-generated Stop continuation; useful signals are queued for the fixed maintenance task. Invoke capture manually only when you want an immediate explicit review:

```text
Use $codex-gardener:gardener-capture to review this task and record reusable knowledge at the right scope.
Use $codex-gardener:knowledge-curator to curate repository and global lessons at the narrowest valid scope.
Use $codex-gardener:cross-project-delegation to coordinate this change in the project that owns it.
```

For concurrent repository maintenance, the Skill first inspects existing worktrees and branches, pins a base commit, assigns one unique worktree and branch per writer, and forbids shared-checkout writes. One coordinator then merges or cherry-picks branches serially in a dedicated, clean integration worktree and branch—not the shared or main checkout—resolves conflicts there, reruns validation after every merge, and runs the full acceptance suite after final integration. If isolation cannot be established, concurrent writes must stop or be serialized. Worktrees are removed only after their branches and exact resolved paths are verified.

Examples:

- “This repository's generated API clients must never be edited” is repository-scoped.
- “Inspect the final diff before declaring an implementation complete” may be global if independent projects support it.
- “Always run pytest” is not global merely because several Python repositories use it.

The CLI keeps backward-compatible defaults:

```bash
# Existing behavior: repository scope
python gardener.py groups --repo /path/to/repo

# Cross-project candidate groups
python gardener.py groups --repo /path/to/repo --knowledge-scope global
```

### Promoted retrieval metadata

`resolve --status promoted` accepts bounded optional `--task-type`, `--path-glob`, `--language`, `--tool`, `--platform`, `--negative-keyword`, `--min-score`, and repeatable `--supersedes` metadata. Scope, normalized negative phrases, and path globs are hard filters; task type, language, tool, platform, and matching paths additionally boost a matching result. `estimated_tokens` is always calculated from the final rendered injection line. To remove an active entry from retrieval without altering historical candidate/resolution evidence, use `resolve --status retired --reason stale|duplicate|superseded`; retirement is always explicit and audits never retire entries automatically.

`index-audit` is read-only. It reports stale entries (90 days and at least 50 misses), normalized-summary duplicates, explicit supersession, orphaned target paths, and invalid metadata. It does not modify JSONL, SQLite, or promoted artifacts.

Records created before `0.2.0` inside a repository have no `knowledge_scope`; they continue to load as `repository` and remain in that repository store. Version `0.4.0` also reads legacy user-level `$CODEX_HOME/learning/{inbox,index,resolutions}.jsonl` as global knowledge and copies it into the v2 global store at SessionStart or before a global write. Read-only reports can combine the legacy source in memory without changing it. Copies are marked `migration_provenance: legacy-user-learning-v1`, deduplicated by fingerprint/session or resolution identity, and the source files are preserved. Version `0.5.2` normalizes the two historical cross-project delegation Skill targets to `plugins/codex-gardener/skills/cross-project-delegation/SKILL.md`; all unrelated targets remain unchanged. Migrated evidence without project fingerprints cannot by itself satisfy the cross-project global promotion threshold.

## Update and uninstall

For a Git marketplace installation:

```bash
codex plugin marketplace upgrade codex-gardener
codex plugin add codex-gardener@codex-gardener
```

For a cloned installation, run `git pull` and then `python install.py` again. Start a new task after updating. If an old personal installation is enabled, remove it explicitly first:

```bash
codex plugin remove codex-gardener@personal
```

An old standalone `$CODEX_HOME/skills/cross-project-delegation/` may also contain pre-plugin guidance and compete with the bundled Skill. Compare it with `$codex-gardener:cross-project-delegation`; after preserving any unique valid guidance, explicitly remove or rename the standalone copy. The installer and curator report this reconciliation path but never modify a global Skill automatically.

To uninstall:

```bash
codex plugin remove codex-gardener@codex-gardener
codex plugin marketplace remove codex-gardener
```

Uninstalling does not delete runtime, repository learning, or global learning data. Review and remove those directories separately if desired.

## Privacy and security

The plugin makes no network requests and includes no network telemetry. Hook payloads are processed locally. It persists derived signals and concise candidates, not transcript contents or raw prompts/tool output. Repository paths may appear in runtime pending-review state. Effectiveness logs use allow-listed counts, categories, and hashed identities only. Global candidates store concise summaries, session IDs, timestamps, and hashed project fingerprints, so do not put secrets or personal data in summaries.

Hooks execute local commands with your Codex permissions. Review them before trust, install only from a ref you trust, and preserve normal code review and CI controls. Global promotion is reviewable and confirmation-gated. The cross-project detector is a useful guardrail, not a complete shell parser or security boundary. See [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md).

## Limitations

- Signal and scope classification are heuristic and may miss lessons or choose a scope that needs correction.
- Project fingerprints establish distinct stored identities, not organizational or semantic independence.
- Promotion thresholds establish repeated evidence, not truth; the curator must still inspect conflicts, sensitivity, target, and final changes.
- Global retrieval is keyword-based and intentionally bounded; it can miss synonyms and returns at most three combined entries.
- The write guard cannot understand every shell construct, symlink, worktree, nested repository, or custom mutating tool.
- Effectiveness metrics show observed behavior, not whether a promoted lesson was objectively correct; low-volume windows can be misleading.
- Transcript formats are unstable. A new pending item requires a supplied path that is a regular readable file at queue time; its private metadata never copies transcript contents, and deferred maintenance markers and learning inboxes never contain that path.

## Development

```bash
python scripts/validate_repository.py
python -m unittest discover -s tests -v
```

CI runs both commands on Windows and Ubuntu. The repository validator checks marketplace and plugin metadata, lifecycle hooks, all three bundled Skills, Python syntax, and packaging hygiene. Contributors with the Codex system Skill tooling can additionally run `quick_validate.py` for each Skill and `validate_plugin.py` for the distributable plugin. See [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under the [MIT License](LICENSE).
