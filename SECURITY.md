# Security Policy

## Reporting a vulnerability

Please report security and privacy vulnerabilities privately through [GitHub Security Advisories](https://github.com/williamxhero/codex-gardener/security/advisories/new). Do not open a public issue containing exploit details, secrets, private paths, transcripts, or personal data.

Include the affected version, operating system, Codex version, reproduction steps, impact, and any suggested mitigation. Maintainers will acknowledge a report when practical; no response-time guarantee is offered.

## Trust model

Codex Gardener runs local Python commands at Codex lifecycle boundaries. Review `hooks/hooks.json` and the three bundled scripts before trusting the hooks through `/hooks`. The project-boundary hook is deliberately conservative but cannot parse every shell, tool, path, symlink, or nested repository arrangement. It is a guardrail, not a security sandbox.

Knowledge candidates are untrusted input. Review evidence and scope before promotion, keep secrets out of summaries, inspect every diff, and preserve normal repository review and CI controls. Global promotion has wider impact and must remain confirmation-gated. Project fingerprints are diversity signals, not security identities or proof of project independence. Install only from a repository and Git ref you trust.

Effectiveness logs are local operational records, not a security boundary or tamper-proof audit trail. Their schema rejects fields outside a small allowlist and hashes correlating identifiers, but local users and processes with filesystem access can read, change, or delete them. Logging and rotation failures fail open so they never interrupt a Codex hook or Gardener CLI operation. Set `CODEX_GARDENER_EFFECTIVENESS_LOG=0` to disable these events.

Do not keep both `codex-gardener@personal` and `codex-gardener@codex-gardener` enabled: Codex may run matching Hooks from both installations. Version `0.4.0` reports this duplicate state and the cloned installer fails with an explicit removal command rather than deleting another installation. The health report prints local data, log, and standalone Skill paths for diagnosis; avoid publishing that report without reviewing those paths.

This project is community-maintained and is not an official OpenAI product.
