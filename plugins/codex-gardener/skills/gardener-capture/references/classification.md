# Candidate classification

| Target | Use for |
| --- | --- |
| `agents` | Stable, concise repository invariants or navigation guidance that must be visible on most relevant tasks. |
| `skill` | A repeatable multi-step workflow that benefits from instructions, references, or deterministic scripts. |
| `test` | A reproduced defect or invariant that can be expressed as an automated regression test. |
| `hook` | A deterministic rule that should warn or block at a lifecycle boundary. Blocking hooks always require user confirmation. |
| `docs` | Architecture, rationale, operational background, or detailed guidance that should not inflate `AGENTS.md`. |
| `discard` | One-off needs, generic advice, uncertain claims, sensitive material, or facts already covered. |

Prefer a machine-enforced test, Hook, lint rule, or formatter over an instruction when enforcement is deterministic. Prefer docs over `AGENTS.md` for detail. A candidate is evidence, not permission to edit its recommended target.
