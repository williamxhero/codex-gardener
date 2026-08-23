---
name: gardener-capture
description: Manually review a completed Codex task for reusable knowledge, choose repository or global scope, and defer concise learning candidates without changing promoted artifacts. Use when the user explicitly asks to capture lessons from the current task.
---

# Gardener Capture

Capture only knowledge likely to help a future independent task. Choose the narrowest valid scope. Ordinary tasks do not invoke this Skill automatically; Gardener queues their bounded signals silently for scheduled maintenance.

## Workflow

1. Review the current conversation, work performed, failures, user corrections, repository conventions, diff, and verification results.
2. Reject one-off requirements, generic advice, facts already enforced or documented, and anything supported only by speculation.
3. Classify each lesson's knowledge scope:
   - Use `repository` for language, framework, layout, architecture, team, deployment, or other project-dependent knowledge.
   - Use `global` only when the lesson is portable across unrelated projects and does not depend on repository conventions. Default uncertainty to `repository`.
4. Classify its target as `agents`, `skill`, `test`, `hook`, `docs`, or `discard`. Read [classification.md](references/classification.md) when either classification is uncertain.
5. Defer each distinct lesson with the bundled plugin CLI. Resolve `scripts/gardener.py` relative to this Skill directory at `../../scripts/gardener.py` and pass an absolute path. `defer-record` writes only an ignored marker inside the current repository; the next unsandboxed Stop Hook validates it and writes the formal repository or global candidate without creating a continuation:

```powershell
python <plugin-root>\scripts\gardener.py defer-record `
  --repo <repository-root> `
  --session-id <session-id> `
  --knowledge-scope <repository|global> `
  --scope <short-scope> `
  --lesson <concise-invariant-or-workflow> `
  --evidence <brief-task-evidence> `
  --target <agents|skill|test|hook|docs|discard> `
  --confidence <0-to-1>
```

6. If nothing is reusable, do not run a command. State that the manual review produced no candidate. If the task already produced a pending signal, scheduled maintenance can close it later without interrupting this task.
7. Report what was deferred and at which knowledge scope, or state that the review produced no candidate.

The deferred marker contains only the candidate fields shown above and no repository path. The next Stop Hook records a low-sensitivity effectiveness event for a candidate outcome. Do not add prompts, tool data, paths, or transcript content to support that audit.

## Boundaries

- Do not modify repository or global `AGENTS.md`, Skills, tests, Hooks, docs, configuration, or this plugin during capture.
- Do not copy raw prompts, transcripts, tool output, secrets, credentials, personal data, or large code excerpts into evidence.
- Allow the CLI to derive the project fingerprint; do not record raw repository paths as evidence.
- Use one sentence for `lesson` and one short sentence for `evidence`.
- Use the repository path and session ID supplied by the Stop continuation. For manual invocation, use the current repository root and the current session ID when available; otherwise use `manual`.
