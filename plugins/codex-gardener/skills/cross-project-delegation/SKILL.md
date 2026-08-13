---
name: cross-project-delegation
description: Coordinate writes across project boundaries or concurrent writers in one Git repository. Use when a task targets another project, spans repositories, delegates implementation, or multiple Codex threads or agents may modify the same repository or overlapping files.
---

# Cross-Project and Concurrent-Writer Delegation

Keep every write in an execution context owned by the project whose files will change. The originating task may inspect other projects and coordinate work, but it must not become their implementation context.

When multiple threads or agents may write to the same Git repository or overlapping plugin-related files, isolation is mandatory even if every writer belongs to the same project. Each writer must use one unique Git worktree and branch; writers must not share a working tree or edit the main checkout concurrently.

## Workflow

1. Identify the current primary project and every independent target project. Use distinct Git roots and project configuration as the default ownership boundary; ordinary subdirectories within one repository are not separate projects.
2. Read enough target-project context to define a bounded task, including its goal, relevant paths, constraints, acceptance criteria, and expected verification.
3. Before assigning writers, inspect `git worktree list`, relevant branches, and checkout status. Pin an exact base commit. If multiple writers may touch one repository or overlapping paths, allocate one unique Git worktree and branch per writer from that commit. Never assign overlapping writes to the same checkout.
4. Delegate implementation through an available agent, task, or session mechanism whose working directory and primary project are the target project. Give each concurrent writer its exact isolated worktree path and branch.
5. Instruct each worker to load and follow the target project's `AGENTS.md`, skills, configuration, tests, and local conventions before editing. It must preserve unrelated user changes, commit independently, and report changed files, checks run, results, and remaining risks.
6. Give one coordinator a dedicated, clean integration worktree and branch pinned to the agreed base commit. Merge or cherry-pick completed branches serially there; never integrate concurrent work in the shared or main checkout. Resolve conflicts only in that integration worktree, review the combined diff, rerun relevant validation after each merge, and run the full acceptance suite after the final integration.
7. Send corrections back to the owning worker or integration context; do not repair target files from the originating coordinator. Remove worktrees only after confirming their branches are merged and resolving the exact cleanup paths.
8. Close or archive delegated tasks when the requested outcome is complete and no follow-up remains, if the available task mechanism supports it.

## Guardrails

- Read-only cross-project inspection is allowed when it helps define or review the work.
- Do not use a secondary workspace folder as proof that the current task owns that project's writes.
- Do not delegate vague objectives. State the exact target project and acceptance criteria.
- Do not silently fall back to direct cross-project edits when no delegation mechanism can establish a target-project execution context. Stop and ask the user to open or authorize a task in that project.
- Do not proceed with concurrent writes if unique worktrees and branches cannot be established. Serialize the work in one owning context or stop and report the blocker.
- Do not delete a worktree or branch until its merge status and resolved absolute path have been verified.
- Do not broaden authority: delegation changes where authorized work is performed, not what work is authorized.

## Handoff Template

Provide the worker with:

- Target project and working directory
- Assigned worktree path, branch, base, and integration owner when writers may overlap
- Requested outcome and in-scope files or components
- Constraints and compatibility requirements
- Acceptance criteria
- Required tests or validation
- Expected completion report
