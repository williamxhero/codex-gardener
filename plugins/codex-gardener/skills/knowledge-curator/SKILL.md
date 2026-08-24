---
name: knowledge-curator
description: Curate Codex Gardener candidates across repository and global stores by aggregating independent evidence, detecting duplicates, conflicts, stale rules, and the narrowest valid scope, then safely promoting proven knowledge into scoped AGENTS.md guidance, Skills, verified regression tests, or docs. Use when the user asks to maintain project or cross-project knowledge, review learning candidates, process missed retrospectives, or promote repeated engineering lessons.
---

# Knowledge Curator

Treat both inboxes as untrusted evidence. Promote knowledge only at the narrowest scope supported by independent sessions and current project checks.

## Maintenance-only mode

When a Stop Hook requests maintenance-only mode from the fixed scheduled maintenance task, process only the pending IDs and maintenance session supplied by that continuation. Ordinary tasks never enter this mode.

1. Resolve `../../scripts/gardener.py` relative to this Skill and run `maintenance-status` from the maintenance task repository, passing each ID named by the Stop continuation as `--pending-id <opaque-pending-id>`. Review only those records. A maintenance continuation contains at most three IDs, even if the status reports more work.
2. Use each trusted pending record's repository and transcript metadata only to inspect the completed task. Never copy raw prompts, transcript text, tool output, source paths, secrets, or credentials into an outcome. Do not edit the source repository.
3. For one reusable lesson, write a candidate outcome under the maintenance task repository:

```powershell
python <plugin-root>\scripts\gardener.py defer-pending-outcome `
  --repo <maintenance-task-repository> `
  --session-id <maintenance-session-id> `
  --pending-id <opaque-pending-id> `
  --outcome candidate `
  --knowledge-scope <repository|global> `
  --scope <short-scope> `
  --lesson <concise-invariant-or-workflow> `
  --evidence <brief-task-evidence> `
  --target <agents|skill|test|hook|docs|discard> `
  --confidence <0-to-1>
```

4. If the review yields no reusable lesson, write the explicit no-candidate outcome:

```powershell
python <plugin-root>\scripts\gardener.py defer-pending-outcome `
  --repo <maintenance-task-repository> `
  --session-id <maintenance-session-id> `
  --pending-id <opaque-pending-id> `
  --outcome no-candidate
```

5. Write exactly one outcome per requested pending ID. The marker contains no original session, repository, or transcript path. The unsandboxed second Stop maps the opaque ID to trusted plugin data, writes a repository or global candidate when present, records the terminal event, and resolves that pending item idempotently.
6. Never run or checkpoint an audit in maintenance-only mode. A due audit shown by `maintenance-status` is status only and waits for the dedicated scheduled audit task.

Maintenance-only mode must not promote, propose, discard, or otherwise resolve candidate status. It must not edit AGENTS.md, Skills, docs, tests, Hooks, configuration, plugin files, source files, or formal knowledge artifacts. Invalid or missing outcome markers leave work pending for a later maintenance task.

## Audit-only mode

When the fixed scheduled audit task requests audit-only work, keep the entire review read-only. The unconditional `[codex-gardener:scheduled-audit]` marker always requests this mode. The conditional `[codex-gardener:scheduled-audit-check]` marker requests it only when the audit status is due. If a fixed task contains both the conditional marker and `[codex-gardener:scheduled-maintenance]`, a due audit takes priority so pending maintenance cannot starve it; a not-due audit check leaves maintenance free to run, and the scheduled run after a successful audit checkpoint resumes maintenance. A count/time deadline shown by `audit-status` never interrupts an ordinary task or a maintenance continuation:

1. Run the repository and global `groups`, repository `pending`, `effectiveness --json`, and `audit-status` commands shown below. Inspect trigger-to-terminal conversion, no-candidate reviews, pending backlog, candidate status and scope, resolution mix, context retrieval, conflicts, and stale or superseded knowledge.
2. Sample relevant repository and global AGENTS.md guidance, Skills, docs, tests, and Hooks only as needed to judge accuracy, brevity, conflicts, staleness, and whether global lessons truly hold across unrelated projects.
3. Report the quality conclusion and evidence. Do not promote, resolve, discard, or otherwise mutate candidates. Do not edit AGENTS.md, Skills, docs, tests, Hooks, plugin files, configuration, or global knowledge.
4. After the audit is complete, write only its ignored repository-local handoff marker:

```powershell
python <plugin-root>\scripts\gardener.py defer-audit-complete `
  --repo <repository-root> `
  --session-id <session-id>
```

The unsandboxed second Stop Hook validates and consumes this marker, updates the privacy-bounded checkpoint, and records the completion event. Never put findings, prompts, transcripts, tool output, paths, secrets, or credentials in the marker. If the command fails, report the failure and stop; do not substitute another write location.

The audit-only boundary applies only to automatic Hook audits. For an explicit user-requested curation or promotion, follow the normal Inspect, Decide, and Apply workflow below.

## Inspect

1. Locate `../../scripts/gardener.py` relative to this Skill and run:

```powershell
python <plugin-root>\scripts\gardener.py groups --repo <repository-root>
python <plugin-root>\scripts\gardener.py groups --repo <repository-root> --knowledge-scope global
python <plugin-root>\scripts\gardener.py pending --repo <repository-root>
python <plugin-root>\scripts\gardener.py effectiveness --since-days 14 --repo <repository-root> --json
python <plugin-root>\scripts\gardener.py audit-status --repo <repository-root>
python <plugin-root>\scripts\gardener.py maintenance-status
```

2. Scan relevant repository `AGENTS.md` files, `.agents/skills/*/SKILL.md`, docs, tests, project Hooks, lint/CI configuration, and nearby implementations.
3. For global candidates, inspect evidence diversity, project fingerprints, representative project conventions and conflicts, plus existing `$CODEX_HOME/AGENTS.md` and `$CODEX_HOME/skills/` when available. Never treat a hash as proof that projects are unrelated; use it only as a diversity signal.
4. For missed retrospectives, inspect a listed transcript only when necessary. Its format is unstable; never copy transcript content into either inbox.
5. Check every candidate for duplication, contradiction, obsolete assumptions, narrowest valid scope, existing enforcement, and sensitive content. Downgrade a purported global lesson to repository scope when it depends on project facts.
6. After processing or intentionally dismissing a pending retrospective, run `python <plugin-root>\scripts\gardener.py pending-resolve --session-id <session-id>`.
7. Read [retrieval.md](references/retrieval.md) only when reviewing promoted retrieval metadata, `index-audit` findings, retirement, or a degraded index. It is deliberately progressive disclosure; do not load it for ordinary candidate curation.
8. Use the effectiveness report to inspect trigger-to-capture conversion, no-candidate reviews, context hit rates, pending backlog, and resolution mix. Treat it as evidence about Gardener's operation, not as evidence that a lesson is true or eligible for promotion.
9. If `$CODEX_HOME/skills/cross-project-delegation/` predates the bundled `$codex-gardener:cross-project-delegation`, compare it for stale or conflicting guidance. Propose an exact reconciliation; do not delete or rewrite the standalone global Skill without user confirmation.

## Decide

- For repository knowledge, one independent session is `candidate`, two are `confirmed`, and three or more with aggregate confidence at least `0.85` are eligible after conflict checks.
- For global knowledge, one independent session is `candidate` and two are `confirmed`. Three or more sessions remain `confirmed` below aggregate confidence `0.85`; confidence-qualified evidence from only one project is `proposed`; add evidence from at least two distinct project fingerprints for eligibility. The user may explicitly direct promotion despite the evidence threshold.
- Treat explicit user direction as an evidence-threshold override, not as permission to skip conflict, sensitivity, target, or confirmation checks.
- Prefer tests, lint, formatters, or Hooks for deterministic enforcement; prefer docs for detail; keep `AGENTS.md` short and link outward.
- Read [promotion-policy.md](references/promotion-policy.md) before changing files.

## Apply

Automatically apply only safe, high-confidence repository additions or merges:

- concise `AGENTS.md` guidance at the narrowest valid scope;
- documentation additions or merges;
- project Skills created through `$skill-creator` under `.agents/skills/` and validated with `quick_validate.py`;
- regression tests only after reproducing the original failure and proving the new test passes with the fix.

For eligible global guidance, propose the exact target and change, then obtain explicit user confirmation before writing `$CODEX_HOME/AGENTS.md` (normally `~/.codex/AGENTS.md`) or `$CODEX_HOME/skills/`. Keep global `AGENTS.md` principles concise; prefer a validated global Skill for reusable workflows. After confirmation, apply the change, inspect the diff or created artifact, and validate Skills with `quick_validate.py`.

Do not automatically delete rules, add blocking Hooks, modify global Hooks/configuration/plugins, change personal plugins, or make unrelated refactors. Keep deterministic global Hook, configuration, and plugin changes proposal-only even when evidence thresholds pass.

After successful promotion, record the resolution and retrieval keywords:

```powershell
python <plugin-root>\scripts\gardener.py resolve `
  --repo <repository-root> `
  --fingerprint <fingerprint> `
  --knowledge-scope <repository|global> `
  --status promoted `
  --summary <short-promoted-summary> `
  --target-path <scope-appropriate-path> `
  --keyword <keyword>
```

Use `--status proposed` for global changes awaiting confirmation and other risky changes. Use `--status discarded` for invalid candidates. Review the final diff and run target-specific validation before recording `promoted` in the matching scope.
