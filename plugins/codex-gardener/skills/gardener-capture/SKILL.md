---
name: gardener-capture
description: Review a completed Codex task for reusable repository knowledge and record concise learning candidates without changing promoted project artifacts. Use when a Codex Gardener Stop hook requests a retrospective, when the user asks to capture lessons from the current task, or when corrections, repeated failures, undocumented conventions, repeated workflows, or machine-checkable rules should be preserved for later curation.
---

# Gardener Capture

Capture only knowledge that is likely to help a future independent task in the same repository.

## Workflow

1. Review the current conversation, work performed, failures, user corrections, repository conventions, diff, and verification results.
2. Reject one-off requirements, generic advice, facts already enforced or documented, and anything supported only by speculation.
3. Classify each reusable lesson as `agents`, `skill`, `test`, `hook`, `docs`, or `discard`. Read [classification.md](references/classification.md) when the target is uncertain.
4. Record each distinct lesson with the bundled plugin CLI. Resolve `scripts/gardener.py` relative to this Skill directory at `../../scripts/gardener.py` and pass an absolute path:

```powershell
python <plugin-root>\scripts\gardener.py record `
  --repo <repository-root> `
  --session-id <session-id> `
  --scope <short-scope> `
  --lesson <concise-invariant-or-workflow> `
  --evidence <brief-task-evidence> `
  --target <agents|skill|test|hook|docs|discard> `
  --confidence <0-to-1>
```

5. If nothing is reusable, run `python <plugin-root>\scripts\gardener.py review-complete --session-id <session-id>`.
6. Report what was recorded, or state that the review produced no candidate.

## Boundaries

- Do not modify `AGENTS.md`, Skills, tests, Hooks, docs, global configuration, or this plugin during capture.
- Do not copy raw prompts, transcripts, tool output, secrets, credentials, personal data, or large code excerpts into evidence.
- Use one sentence for `lesson` and one short sentence for `evidence`.
- Use the repository path and session ID supplied by the Stop continuation. For manual invocation, use the current repository root and the current session ID when available; otherwise use `manual`.
