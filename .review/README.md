# Automated review

Tool-agnostic home for the project's automated review surface.

## What lives here

```
.review/
├── README.md             # this file
├── agents/               # one markdown file per review agent
│   ├── audit-trail-completeness.md
│   ├── csrf-coverage.md
│   ├── documentation-style.md
│   ├── gdpr-compliance.md
│   ├── load-bearing-diff-reviewer.md
│   └── phi-exposure.md
├── exemptions/           # per-agent exemption registries; one file per agent that has one
│   ├── README.md
│   ├── audit-trail-completeness.md
│   ├── csrf-coverage.md
│   ├── gdpr-compliance.md
│   └── phi-exposure.md
└── findings/             # one markdown file per agent; EMPTY when clean
    ├── audit-trail-completeness.md
    ├── csrf-coverage.md
    ├── documentation-style.md
    ├── gdpr-compliance.md
    ├── load-bearing-diff-reviewer.md
    └── phi-exposure.md
```

Each file under `agents/` is an instruction set — a system prompt plus a
procedure — that a code-reviewing AI assistant can follow to verify one
invariant in the codebase. The agents are narrow on purpose: one
agent, one rule. General "review this PR for everything" output tends
to be shallow; narrow agents catch their one thing reliably.

Each file under `findings/` is the report from the agent's most recent
run. **An empty file means the agent has nothing to flag.**  Any
non-empty content blocks `git commit` via the pre-commit hook.

## How the commit gate works

`scripts/git-hooks/pre-commit` checks every file under `findings/` for
non-zero size. If any one of them has content, the commit is refused
with a `cat .review/findings/<agent>.md` pointer.

The hook only checks file size — it does not parse the report. Three
resolution paths from the human side:

1. **Fix the underlying issue** and re-run the agent. The agent
   re-evaluates the diff and writes a fresh empty result; commit
   proceeds.
2. **Discard as irrelevant.**  Empty the file manually
   (`: > .review/findings/<agent>.md`). Commit proceeds, but the
   next agent run will re-flag the same finding — mild pressure to
   either fix it or convert it to a roadmap item.
3. **Add to ROADMAP for later.**  Add the deferred work to
   `ROADMAP.md`, then empty the file. Same re-flag behaviour as
   discard; the roadmap entry is the durable record.

Bypass with `git commit --no-verify` when knowingly mid-flight. The
next non-bypassed commit will still be blocked.

## Why not under `.claude/`?

The instructions are tool-agnostic — any AI assistant capable of
reading markdown and running shell commands can follow them. Keeping
them under a tool-prefixed path would tie the project to one specific
AI surface. Hidden top-level `.review/` mirrors the pattern of
`.github/` (tooling-config tracked in git, out of the way of normal
browsing).

Tools that *do* expect a specific path (Claude Code looks under
`.claude/agents/`) get a relative symlink at setup time —
`scripts/install-dev-tools.sh` creates it. The substantive content
stays at `.review/agents/`.

## Agent file format

Each agent is a markdown file with a YAML frontmatter block. The
frontmatter currently uses Claude Code's field set (`name`,
`description`, `model`, `tools`), but the prompt body is generic
markdown — another AI tool would only need to translate the
frontmatter, not the content.

Required frontmatter:

| Field | Notes |
|---|---|
| `name` | Filename stem; used to address the agent. |
| `description` | When the calling assistant should route to this agent. Phrased so other agents can decide whether to invoke. |
| `model` | Tool-specific model identifier. `sonnet` for Claude Code. |
| `tools` | Comma-separated tool allowlist. Review agents typically need `Bash, Read, Grep, Write` (Write only because they update their findings file — the system prompt forbids editing source code). |

## Adding a new agent

1. Create `.review/agents/<name>.md` with frontmatter + a procedure.
   Follow the pattern of the two existing agents — narrow scope, one
   invariant, references to the canonical rule statement (typically
   in `AGENTS.md` or an app README).
2. Create an empty `.review/findings/<name>.md`.
3. Re-run `scripts/install-dev-tools.sh` so the new file is visible
   to Claude Code via the `.claude/agents/` symlink.
4. Document the agent in this README's table below.

## Current agents

| File | Invariant |
|---|---|
| [agents/audit-trail-completeness.md](agents/audit-trail-completeness.md) | Every in-diff Ninja endpoint that is not on [exemptions/audit-trail-completeness.md](exemptions/audit-trail-completeness.md) annotates its `Activity` row with a verb in the established taxonomy and, where applicable, target + metadata. |
| [agents/documentation-style.md](agents/documentation-style.md) | In-diff prose (Python docstrings + Markdown) follows the AGENTS.md style ruleset — no double-spaces after periods, no migration-state language, no per-parameter `:param` lines, no backtick-wrapped file paths in Markdown body, restrained prose bold, no restating or "draw a picture" codas. |
| [agents/gdpr-compliance.md](agents/gdpr-compliance.md) | Every in-diff change keeps personal data erasable, minimized, and inventoried: new PII-bearing fields registered with `register_subject_pii` / `register_masked_fields` (C1), no raw identifiers in log or audit-metadata writes (C2), a retention or erasure path for every new persistent store (C3), no audit-row mutation outside the sanctioned erasure engine (C4), and the [docs/gdpr-compliance.md](../docs/gdpr-compliance.md) data / processor inventories extended in the same commit (C5). Exemptions in [exemptions/gdpr-compliance.md](exemptions/gdpr-compliance.md). |
| [agents/load-bearing-diff-reviewer.md](agents/load-bearing-diff-reviewer.md) | When any ⚠️ LOAD-BEARING file (per AGENTS.md) appears in the diff, the file's contract test runs first; the agent refuses to bless the change if the test is red. Also flags input-filter tightening that requires the "enumerate prior matches" rule. |
| [agents/phi-exposure.md](agents/phi-exposure.md) | Every in-diff endpoint, schema, and byte-serving path that is not on [exemptions/phi-exposure.md](exemptions/phi-exposure.md) honours the seven PHI-exposure invariants: opaque hashes in URLs (C1), forbidden fields absent from Out schemas (C2), `_can_see_original_name` gating original_name / processing_error (C3), `Content-Disposition` filename built from display_name (C4), `_failed_hidden_for_caller` applied to Recording lookups (C5), `apply_middleware` resolved on byte serving (C6), originals volume not read for content (C7). |

## Exemption registries

`exemptions/` holds one file per agent that needs to exempt some inputs
from its default rule. The file's name matches the agent's name
exactly, and the agent's spec reads its own exemption file before
flagging anything. Agents that don't need an exemption surface have no
file there. See [exemptions/README.md](exemptions/README.md) for the
convention.
