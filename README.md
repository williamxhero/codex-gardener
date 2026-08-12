# Codex Gardener

Codex Gardener turns lessons from completed Codex tasks into small, verified, repository-scoped knowledge. It captures candidates without changing promoted project artifacts, aggregates independent evidence over time, and helps curate proven lessons into concise `AGENTS.md` guidance, project Skills, regression tests, hooks, or documentation.

It also includes an opt-in cross-project write guard. When a task attempts to write into a different Git repository, the guard points Codex to a bundled delegation workflow so the target project remains the implementation context.

> Codex Gardener is an independent community project. It is not an official OpenAI product.

## Requirements

- Codex CLI with `codex plugin` marketplace support
- Python 3.10 or newer on `PATH` (`python` on Windows, `python3` on Linux/macOS)
- Git, for repository detection and Git marketplace installation

The runtime and installer use only the Python standard library.

## Install from Git

Add the public marketplace and install the plugin:

```bash
codex plugin marketplace add williamxhero/codex-gardener
codex plugin add codex-gardener@codex-gardener
```

Start a new Codex task after installation. Run `/hooks`, inspect every command in `hooks/hooks.json`, and explicitly trust the hooks only if you are comfortable with their local behavior. Hooks remain opt-in; installing the plugin is not a substitute for reviewing them.

## Install from a clone

```bash
git clone https://github.com/williamxhero/codex-gardener.git
cd codex-gardener
python install.py --dry-run
python install.py
```

On Linux or macOS, use `python3` when `python` is unavailable. The installer locates `codex`, verifies the checkout, safely reuses a matching local marketplace, installs `codex-gardener@codex-gardener`, and prints hook-trust and new-task guidance. It stops if the marketplace name already points elsewhere.

中文快速开始：执行上面的两条 `codex plugin` 命令，安装后新建任务；先运行 `/hooks` 检查命令，再决定是否信任并启用 hooks。

## How it works

| Component | Role |
| --- | --- |
| `gardener.py` lifecycle hooks | Track bounded task signals, surface previously promoted context, and request at most one retrospective when useful. |
| `$gardener-capture` | Review the completed task and append concise evidence candidates without editing promoted artifacts. |
| `$knowledge-curator` | De-duplicate and challenge candidates, then promote only sufficiently supported repository knowledge. |
| `project_boundary.py` PreToolUse hook | Conservatively deny detected writes from one Git repository into another. |
| `$cross-project-delegation` | Move authorized implementation into a task or agent context owned by the target repository. |

The learning model deliberately separates observation from promotion:

1. One independent task produces a **candidate**.
2. Two independent tasks make it **confirmed**.
3. Three or more independent tasks with aggregate confidence of at least `0.85` make it **eligible**, not automatically correct.
4. The curator checks current code, tests, docs, conflicts, scope, and sensitivity before applying safe changes. Risky changes remain proposals requiring confirmation.

## Usage

Normal use is passive after you trust the hooks. A completed task with useful signals may be held briefly so `$gardener-capture` can record a bounded retrospective. You can also invoke the workflows directly:

```text
Use $gardener-capture to review this completed task for reusable repository knowledge.
Use $knowledge-curator to review and promote accumulated repository lessons.
Use $cross-project-delegation to coordinate this change in the project that owns it.
```

Candidate files live under `<repository>/.codex/learning/` and are ignored by a local `.gitignore`. Runtime state defaults to `$CODEX_HOME/codex-gardener-data/`; Codex may instead provide `PLUGIN_DATA`, and advanced users can set `CODEX_GARDENER_DATA` explicitly. See [PRIVACY.md](PRIVACY.md) for the complete data behavior.

## Update and uninstall

For a Git marketplace installation:

```bash
codex plugin marketplace upgrade codex-gardener
codex plugin add codex-gardener@codex-gardener
```

For a cloned installation, run `git pull` and then `python install.py` again.

To uninstall:

```bash
codex plugin remove codex-gardener@codex-gardener
codex plugin marketplace remove codex-gardener
```

Uninstalling does not delete runtime or repository learning data. Review and remove those directories separately if desired.

## Privacy and security

The plugin makes no network requests and includes no telemetry. Hook payloads are processed locally; the plugin persists derived signals and concise candidates, not transcript contents or raw tool output. A pending-review record may contain local repository/transcript paths supplied by Codex. Do not put secrets or personal data in candidate summaries.

Hooks execute local commands with your Codex permissions. Review them before trust, install only from a ref you trust, and keep ordinary code review and CI controls in place. The cross-project detector is a useful guardrail, not a complete shell parser or security boundary. See [PRIVACY.md](PRIVACY.md) and [SECURITY.md](SECURITY.md).

## Limitations

- Signal detection is heuristic and may miss useful lessons or request an unnecessary review.
- The write guard cannot understand every shell construct, symlink, worktree, nested repository, or custom mutating tool.
- Promotion thresholds establish repeated evidence, not truth; the curator must still inspect the repository and final diff.
- Transcript formats are unstable. The plugin stores only a supplied path for optional later inspection and never copies transcript contents into its learning inbox.

## Development

Run the dependency-free validation and tests on Python 3.10+:

```bash
python scripts/validate_repository.py
python -m unittest discover -s tests -v
```

CI runs both commands on Windows and Ubuntu. The repository validator checks marketplace and plugin metadata, lifecycle hooks, all three bundled Skills, Python syntax, and packaging hygiene. Contributors with the Codex system Skill tooling can additionally run `quick_validate.py` for each Skill and `validate_plugin.py` for the distributable plugin. See [CONTRIBUTING.md](CONTRIBUTING.md).

Licensed under the [MIT License](LICENSE).
