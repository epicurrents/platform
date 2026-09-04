# Exemption registries

One file per review agent that needs to exempt some inputs from its
default rule. The file's name matches the agent's name exactly
(`<agent-name>.md`), and the agent's spec reads its own exemption file
before flagging anything.

## Convention

- **`<agent-name>.md`** — exemption registry for the agent named
  `<agent-name>`. Format is up to the agent: the audit-trail-
  completeness agent exempts endpoint paths; a future PHI-leak scout
  might exempt specific synthetic-data fixtures; the LOAD-BEARING
  reviewer might exempt files that are intentionally marked
  load-bearing but have no contract test yet. Each registry should
  document its own format at the top of the file.
- **An agent with no exemptions has no file here.**  Empty registries
  invite "just add it to the empty list" abuse; the absence of the
  file signals "this agent has no exemption surface".
- New exemptions go through the same review path as new agents — add
  a row with a one-line reason, justify the reason in the diff that
  introduces it. The exemption table is the source of truth; argue
  with it via PR, not via the agent's findings file.

## Current registries

| File | Agent | What it exempts |
|---|---|---|
| [audit-trail-completeness.md](audit-trail-completeness.md) | `.review/agents/audit-trail-completeness.md` | API endpoints that don't interact with stored data (health checks, public-key publication, computed artifacts that don't depend on a specific user's data, static API-shape lookups). |
| [csrf-coverage.md](csrf-coverage.md) | `.review/agents/csrf-coverage.md` | Unsafe-method endpoints no session-cookie caller can reach — pre-auth endpoints, and endpoints authenticated by a credential a browser does not attach automatically (body token, query-param share token, bearer JWT) — plus logout, whose forged call has no effect worth preventing. |
| [gdpr-compliance.md](gdpr-compliance.md) | `.review/agents/gdpr-compliance.md` | Personal-data-shaped model fields that are out of scope for subject-erasure registration or a retention path — recording-subject PHI erased with its row, user-chosen labels, and a remote controller's pseudonymous identifiers. |
| [phi-exposure.md](phi-exposure.md) | `.review/agents/phi-exposure.md` | Endpoints and response schemas that deviate from the de-identification defaults under a written structural constraint — author-only-by-construction uploads, responses carrying no PHI, and the dataset identifier exception. |
