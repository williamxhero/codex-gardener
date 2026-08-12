# Candidate classification

## Knowledge scope

| Scope | Use for |
| --- | --- |
| `repository` | Knowledge tied to a project's language, framework, architecture, layout, team conventions, deployment, data contract, or local tooling. This is the safe default. |
| `global` | Portable principles or workflows that remain correct across unrelated repositories without relying on project-specific facts. |

Choose the narrowest valid scope. A lesson seen in several repositories is not automatically global if it expresses a shared framework or organization convention. When uncertain, use `repository` and let curation narrow or broaden it after conflict checks.

## Promotion target

| Target | Use for |
| --- | --- |
| `agents` | Stable, concise repository invariants or navigation guidance that must be visible on most relevant tasks. |
| `skill` | A repeatable multi-step workflow that benefits from instructions, references, or deterministic scripts. |
| `test` | A reproduced defect or invariant that can be expressed as an automated regression test. |
| `hook` | A deterministic rule that should warn or block at a lifecycle boundary. Blocking hooks always require user confirmation. |
| `docs` | Architecture, rationale, operational background, or detailed guidance that should not inflate `AGENTS.md`. |
| `discard` | One-off needs, generic advice, uncertain claims, sensitive material, or facts already covered. |

Prefer a machine-enforced test, Hook, lint rule, or formatter over an instruction when enforcement is deterministic. Prefer docs over `AGENTS.md` for detail. Global deterministic Hooks, configuration, and plugin changes are proposal-only. A candidate is evidence, not permission to edit its recommended target.
