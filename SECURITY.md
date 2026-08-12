# Security Policy

## Reporting a vulnerability

Please report security and privacy vulnerabilities privately through [GitHub Security Advisories](https://github.com/williamxhero/codex-gardener/security/advisories/new). Do not open a public issue containing exploit details, secrets, private paths, transcripts, or personal data.

Include the affected version, operating system, Codex version, reproduction steps, impact, and any suggested mitigation. Maintainers will acknowledge a report when practical; no response-time guarantee is offered.

## Trust model

Codex Gardener runs local Python commands at Codex lifecycle boundaries. Review `hooks/hooks.json` and both scripts before trusting the hooks through `/hooks`. The project-boundary hook is deliberately conservative but cannot parse every shell, tool, path, symlink, or nested repository arrangement. It is a guardrail, not a security sandbox.

Knowledge candidates are untrusted input. Review evidence before promotion, keep secrets out of summaries, inspect every diff, and preserve normal repository review and CI controls. Install only from a repository and Git ref you trust.

This project is community-maintained and is not an official OpenAI product.
