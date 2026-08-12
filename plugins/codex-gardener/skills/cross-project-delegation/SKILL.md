---
name: cross-project-delegation
description: Coordinate work that requires changing files in a project other than the current primary project. Use when a task spans independent repositories, a requested write target belongs to another project, or the current task must supervise implementation in a target project's own execution context.
---

# Cross-Project Delegation

Keep every write in an execution context owned by the project whose files will change. The originating task may inspect other projects and coordinate work, but it must not become their implementation context.

## Workflow

1. Identify the current primary project and every independent target project. Use distinct Git roots and project configuration as the default ownership boundary; ordinary subdirectories within one repository are not separate projects.
2. Read enough target-project context to define a bounded task, including its goal, relevant paths, constraints, acceptance criteria, and expected verification.
3. Delegate implementation through an available agent, task, or session mechanism whose working directory and primary project are the target project.
4. Instruct the worker to load and follow the target project's `AGENTS.md`, skills, configuration, tests, and local conventions before editing. It must preserve unrelated user changes.
5. Require the worker to implement and verify the change, then report changed files, checks run, results, and remaining risks.
6. Review the returned result from the originating task. Send correction or follow-up work back to the same target-project context; do not repair target files from the coordinator.
7. Close or archive the delegated task when the requested outcome is complete and no follow-up remains, if the available task mechanism supports it.

## Guardrails

- Read-only cross-project inspection is allowed when it helps define or review the work.
- Do not use a secondary workspace folder as proof that the current task owns that project's writes.
- Do not delegate vague objectives. State the exact target project and acceptance criteria.
- Do not silently fall back to direct cross-project edits when no delegation mechanism can establish a target-project execution context. Stop and ask the user to open or authorize a task in that project.
- Do not broaden authority: delegation changes where authorized work is performed, not what work is authorized.

## Handoff Template

Provide the worker with:

- Target project and working directory
- Requested outcome and in-scope files or components
- Constraints and compatibility requirements
- Acceptance criteria
- Required tests or validation
- Expected completion report
