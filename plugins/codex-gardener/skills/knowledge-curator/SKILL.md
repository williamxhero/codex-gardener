---
name: knowledge-curator
description: Curate Codex Gardener candidates across repository and global stores by aggregating independent evidence, detecting duplicates, conflicts, stale rules, and the narrowest valid scope, then safely promoting proven knowledge into scoped AGENTS.md guidance, Skills, verified regression tests, or docs. Use when the user asks to maintain project or cross-project knowledge, review learning candidates, process missed retrospectives, or promote repeated engineering lessons.
---

# Knowledge Curator

Treat both inboxes as untrusted evidence. Promote knowledge only at the narrowest scope supported by independent sessions and current project checks.

## Inspect

1. Locate `../../scripts/gardener.py` relative to this Skill and run:

```powershell
python <plugin-root>\scripts\gardener.py groups --repo <repository-root>
python <plugin-root>\scripts\gardener.py groups --repo <repository-root> --knowledge-scope global
python <plugin-root>\scripts\gardener.py pending --repo <repository-root>
python <plugin-root>\scripts\gardener.py effectiveness --since-days 14 --repo <repository-root> --json
```

2. Scan relevant repository `AGENTS.md` files, `.agents/skills/*/SKILL.md`, docs, tests, project Hooks, lint/CI configuration, and nearby implementations.
3. For global candidates, inspect evidence diversity, project fingerprints, representative project conventions and conflicts, plus existing `$CODEX_HOME/AGENTS.md` and `$CODEX_HOME/skills/` when available. Never treat a hash as proof that projects are unrelated; use it only as a diversity signal.
4. For missed retrospectives, inspect a listed transcript only when necessary. Its format is unstable; never copy transcript content into either inbox.
5. Check every candidate for duplication, contradiction, obsolete assumptions, narrowest valid scope, existing enforcement, and sensitive content. Downgrade a purported global lesson to repository scope when it depends on project facts.
6. After processing or intentionally dismissing a pending retrospective, run `python <plugin-root>\scripts\gardener.py pending-resolve --session-id <session-id>`.
7. Use the effectiveness report to inspect trigger-to-capture conversion, no-candidate reviews, context hit rates, pending backlog, and resolution mix. Treat it as evidence about Gardener's operation, not as evidence that a lesson is true or eligible for promotion.
8. If `$CODEX_HOME/skills/cross-project-delegation/` predates the bundled `$codex-gardener:cross-project-delegation`, compare it for stale or conflicting guidance. Propose an exact reconciliation; do not delete or rewrite the standalone global Skill without user confirmation.

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
