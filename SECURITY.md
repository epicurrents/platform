# Security policy

Epicurrents is a self-hosted platform for neurophysiological recordings that routinely handle protected health information (PHI). We take security reports seriously and appreciate coordinated disclosure.

## Reporting a vulnerability

Report security issues **privately** — do not open a public issue, pull request, or discussion for a suspected vulnerability.

Use GitHub's private vulnerability reporting, via the repository's **Security → Advisories → Report a vulnerability** form. The report reaches the maintainers privately, and the resulting advisory thread is where triage, the fix, and coordinated disclosure are tracked.

Include what you found, the impact, and the steps (or a proof of concept) to reproduce. If the issue involves PHI exposure, describe the affected data class — never include real patient data in a report.

## What to expect

- Acknowledgement within 3 business days.
- An initial assessment (severity, affected components) within 10 business days.
- Coordinated disclosure: we agree a timeline with you and credit you in the release notes unless you prefer to remain anonymous.

## Scope

- Test only against your own deployment. Do not test, scan, or attempt to access any other organisation's instance, and never use real patient data.
- In scope: the platform code in this repository (the Django apps, the API, federation, the frontend, and the viewer), the deployment scripts, and the default configuration.
- Out of scope: third-party dependency vulnerabilities with no platform-specific exploit path — report those upstream (we track dependency CVEs via pip-audit in CI and automated dependency-update PRs) — and issues that require a pre-compromised host or valid operator credentials.

## Supported versions

The platform is delivered as source and deployed from this repository. Security fixes land on the default branch (`main`); operators track it and redeploy with [scripts/update.sh](scripts/update.sh). There is no separate long-term-support branch at this time.

## Secrets and PHI

A report concerning secret material (keys, tokens) or PHI exposure is treated at the highest severity. Because deployments are self-hosted and we cannot patch them directly, a fix is paired with a coordinated operator advisory where warranted.
