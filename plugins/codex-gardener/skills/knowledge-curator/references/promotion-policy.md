# Promotion policy

## Automatic changes

- Apply automatically only within a repository. Require at least three unique session IDs, aggregate confidence of `0.85` or higher, and no conflict with current code, tests, instructions, or docs.
- Add or merge instead of duplicating.
- Preserve existing repository conventions and backward compatibility.
- Keep `AGENTS.md` entries concise and scoped; move rationale and workflows to docs or Skills.
- Create a Skill only through the installed `skill-creator` workflow and validate it.
- Add a regression test only with a demonstrated red-green result.

## Global changes

- Require at least three unique session IDs, aggregate confidence of `0.85` or higher, evidence from at least two distinct project fingerprints, and no project-specific conflict before eligibility.
- Keep one global session `candidate`, two or more below eligibility `confirmed`, and confidence-qualified evidence from only one project `proposed`, unless the user explicitly directs promotion.
- Treat user direction as an eligibility override only; still inspect conflicts, sensitivity, scope, and target.
- Propose the exact change and obtain explicit user confirmation before writing `$CODEX_HOME/AGENTS.md` or `$CODEX_HOME/skills/`.
- Keep global `AGENTS.md` principles concise. Prefer a global Skill for a portable multi-step workflow and validate it with `quick_validate.py`.
- Record the global resolution only after the confirmed change and validation succeed.

## Proposal-only changes

- Deleting or weakening existing instructions, tests, or enforcement.
- Adding or changing a blocking Hook.
- Adding or changing global Hooks, configuration, plugins, credentials, deployment, or production infrastructure.
- Any change with unresolved conflicts, uncertain scope, or incomplete validation.

## Resolution

Record `promoted` only after the file change and validation succeed. Record it in the same knowledge scope as the candidate. A promoted candidate is added to that scope's ignored retrieval index used by `UserPromptSubmit`; unresolved inbox records remain append-only evidence.

Effectiveness counts and hit rates may identify noisy triggers, unused context, or review backlog. They do not count as independent candidate evidence and must not relax confidence, cross-project diversity, conflict, sensitivity, validation, or confirmation requirements.
