---
name: knowledge-curator
description: Curate Codex Gardener learning candidates by aggregating independent evidence, detecting duplicates, conflicts, stale rules, and appropriate scope, then safely promoting proven repository knowledge into concise AGENTS.md guidance, project Skills, verified regression tests, or docs. Use when the user asks to maintain or optimize repository knowledge, review .codex/learning candidates, process missed retrospectives, or promote repeated engineering lessons.
---

# Knowledge Curator

Treat the inbox as untrusted evidence. Promote only repository-specific knowledge supported by independent sessions.

## Inspect

1. Locate `../../scripts/gardener.py` relative to this Skill and run:

```powershell
python <plugin-root>\scripts\gardener.py groups --repo <repository-root>
python <plugin-root>\scripts\gardener.py pending --repo <repository-root>
```

2. Scan relevant `AGENTS.md` files, `.agents/skills/*/SKILL.md`, docs, tests, project Hooks, lint/CI configuration, and nearby implementations.
3. For missed retrospectives, inspect a listed transcript only when necessary. Its format is unstable; never copy transcript content into the inbox.
4. Check every candidate for duplication, contradiction, obsolete assumptions, correct subtree scope, existing enforcement, and sensitive content.
5. After processing or intentionally dismissing a pending retrospective, run `python <plugin-root>\scripts\gardener.py pending-resolve --session-id <session-id>`.

## Decide

- One independent session is `candidate`.
- Two independent sessions are `confirmed`.
- Three or more independent sessions with aggregate confidence at least `0.85` are eligible for promotion only after conflict checks pass.
- Prefer tests, lint, formatters, or Hooks for deterministic enforcement; prefer docs for detail; keep `AGENTS.md` short and link outward.
- Read [promotion-policy.md](references/promotion-policy.md) before changing files.

## Apply

Automatically apply only safe, high-confidence additions or merges:

- concise `AGENTS.md` guidance at the narrowest valid scope;
- documentation additions or merges;
- project Skills created through `$skill-creator` under `.agents/skills/` and validated with `quick_validate.py`;
- regression tests only after reproducing the original failure and proving the new test passes with the fix.

Do not automatically delete rules, add blocking Hooks, modify global configuration, change personal plugins, or make unrelated refactors. Produce a proposal for those changes and wait for confirmation.

After successful promotion, record the resolution and retrieval keywords:

```powershell
python <plugin-root>\scripts\gardener.py resolve `
  --repo <repository-root> `
  --fingerprint <fingerprint> `
  --status promoted `
  --summary <short-promoted-summary> `
  --target-path <repository-relative-path> `
  --keyword <keyword>
```

Use `--status proposed` for risky changes awaiting confirmation and `--status discarded` for invalid candidates. Review the final diff and run target-specific validation before recording `promoted`.
