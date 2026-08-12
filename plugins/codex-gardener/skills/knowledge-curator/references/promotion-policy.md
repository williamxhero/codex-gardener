# Promotion policy

## Automatic changes

- Require at least three unique session IDs, aggregate confidence of `0.85` or higher, and no conflict with current code, tests, instructions, or docs.
- Add or merge instead of duplicating.
- Preserve existing repository conventions and backward compatibility.
- Keep `AGENTS.md` entries concise and scoped; move rationale and workflows to docs or Skills.
- Create a Skill only through the installed `skill-creator` workflow and validate it.
- Add a regression test only with a demonstrated red-green result.

## Proposal-only changes

- Deleting or weakening existing instructions, tests, or enforcement.
- Adding or changing a blocking Hook.
- Modifying global configuration, personal plugins, credentials, deployment, or production infrastructure.
- Any change with unresolved conflicts, uncertain scope, or incomplete validation.

## Resolution

Record `promoted` only after the file change and validation succeed. A promoted non-discard candidate is added to the ignored retrieval index used by `UserPromptSubmit`; unresolved inbox records remain append-only evidence.
