# Contributing

Contributions are welcome through focused issues and pull requests.

1. Fork and clone the repository.
2. Create a topic branch. If multiple people, Codex threads, or agents may write to this repository or overlapping plugin files at the same time, each writer must use a unique Git worktree and branch. Never share a checkout for concurrent writes.
3. Keep runtime dependencies limited to the Python standard library unless a change has a compelling, documented reason.
4. Preserve backward compatibility and add regression coverage for behavior changes.
5. Run the complete validation suite:

   ```bash
   python scripts/validate_repository.py
   python -m unittest discover -s tests -v
   ```

6. Review the final diff for generated files, local paths, transcripts, learning JSONL, credentials, and other private data before opening a pull request.

Changes to hooks deserve extra scrutiny: explain the event, data consumed, failure mode, timeout, and why the behavior remains opt-in and safe to trust. Changes to a Skill should keep `SKILL.md` concise, preserve progressive disclosure, update `agents/openai.yaml` when its UI metadata changes, and pass Codex's `quick_validate.py` when available.

Merge concurrent branches serially through one integration worktree. Review and resolve conflicts there, rerun validation after each merge, and remove a worktree only after confirming its branch is merged and its resolved path is the intended cleanup target. If isolated worktrees cannot be established, serialize the work instead of allowing concurrent writes.

By contributing, you agree that your contribution is licensed under the repository's MIT License.
