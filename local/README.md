# Deployment-specific documents

Everything in this directory except this file is gitignored, and it is excluded from the Docker build context.

The repository ships templates and design notes that describe the platform in the abstract — [docs/privacy-notice-template.md](../docs/privacy-notice-template.md), [docs/operator-runbook.md](../docs/operator-runbook.md), [docs/gdpr-compliance.md](../docs/gdpr-compliance.md). A filled-in copy of one of those is a different kind of document: it names a legal entity, its data protection officer, the agreements it operates under, and — while it is being worked on — the compliance questions it has not yet answered. That belongs to the deployment, not to the software, and a shared history is the wrong place for it.

Put here:

- The deployment's filled-in privacy notice, at whatever stage of completeness.
- Correspondence with a data protection officer, hosting provider or supervisory authority.
- Operator notes specific to one installation: contact lists, escalation paths, the local shape of a backup target.

Do **not** put here:

- Secrets. `.env` is already gitignored and is where credentials belong; a second location for them is a second thing to remember to protect.
- Anything the platform reads at runtime. Nothing in the image can see this directory.
- Recordings or other patient data — `testdata/` is the reviewed convention for that, and it carries its own warnings.

Keep a filled-in notice under version control on the deployment's side if it needs a history; this directory deliberately has none.
