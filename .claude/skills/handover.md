# handover

The mandatory checklist between "the change works" and "the change is presented as ready". Nothing is finished, staged, or described as done until every step below has run and the handover report says what each turned up.

## When this skill applies

Invoke before presenting any committable piece of work — a feature, a fix, a refactor, a documentation change. Run it once per coherent piece, over the finished state, not incrementally while still editing. The user does not need to ask; running this is a standing step in the workflow, like the tests.

Assistants that cannot invoke skills follow this file directly — it is a Markdown instruction set, not an executable.

## Step 1 — Review agents

**Run the review agents that match the change, without being asked.** Standing permission — this is a step in the workflow, not a favour to request.

Match by what the change touches rather than by which app it lives in:

| The change… | Run |
|---|---|
| adds or modifies a Ninja endpoint with an unsafe method, or a session-auth helper | `csrf-coverage` |
| adds or modifies any Ninja endpoint, read or write | `audit-trail-completeness` |
| touches a path serving recording data, annotation responses or recording bytes, an `Out` schema, a `Content-Disposition`, or the `apply_middleware` flag | `phi-exposure` |
| adds or modifies a model field, an audit or log metadata write, an outbound integration, or a deletion / retention path | `gdpr-compliance` |
| touches any file in the [load-bearing registry](../../AGENTS.md#files-currently-marked) | `load-bearing-diff-reviewer` |
| adds or modifies docstrings, READMEs, or other in-repo Markdown | `documentation-style` |

Several usually apply at once; run them in parallel. A finding is ordinary work — fix it and add the regression test, rather than arguing it into a different category. If you disagree with one, say so explicitly in the handover with the reasoning, so the disagreement is visible rather than silent.

Two things worth knowing before trusting a red result. A contract test running in the `test` compose service can fail on a stale image after a dependency-adding commit — `docker compose build test` first. And an agent reviewing committed work needs the commit range; one reviewing a fix in progress needs to be told the change is uncommitted and in the working tree.

## Step 2 — Clean-slate pass

**Mandatory, and it comes after step 1** — a green suite is not a substitute for it, and neither is a clean review-agent run; this pass exists to sit after both.

Once a change is written, tested and reviewed — after the review agents have run and their findings are cleared — read the finished code again with the goal of finding what is wrong with it. This is a separate pass, not a re-read of the diff, and it comes last because it needs the finished thing in front of it.

The distinction that makes it work is dropping the assumptions that produced the code. During implementation the reasoning is "does this do what I intended"; here it is "what does this actually do, on inputs I did not have in mind". Re-reading a diff confirms intent, which is exactly the thing already believed. Questions that tend to find something:

- What does each value this trusts do when it is absent, zero, negative, or larger than the buffer it indexes? Header fields, lengths and counts read from user-supplied files are the usual source.
- Which state does this create or destroy that the caller did not ask for — rows written where none existed, rows removed that something else owns?
- What does the equality or "unchanged" check *not* compare, and what drifts through that gap?
- Do the fixtures assert the property claimed, or do they pass because they were built to match the implementation?
- Does this follow the conventions of the code around it, or only the ones recalled while writing it?

The review agents are pattern matchers against known invariants and will not find a flaw that has no rule yet; this pass is where those surface. Findings from it are ordinary work: fix them and add the regression test.

**Mutation testing is not this pass, and mistaking one for the other is how the pass gets skipped.** Deleting a line to confirm a test goes red asks whether the tests notice changes to code already understood; it operates entirely inside the assumptions that produced the code. This pass asks what the code does on inputs never considered. Both are worth doing and neither substitutes for the other — a change can be fully mutation-tested and still fail every question in the list above.

Two observations from the passes that have run, offered as priors rather than rules. The finding is usually in the code just written rather than in what surrounds it, since that is the part no one has read cold yet. And it is disproportionately often in the *tests* — an assertion anchored to the one spelling that was in front of the author, a fixture that satisfies its own precondition, a proxy for the real property that the implementation happens to satisfy. Read the new tests as adversarially as the new code, and prefer asking "what would pass this that should not" over re-checking that it passes.

## Step 3 — ROADMAP and documentation refresh

The repository's documentation rots in one specific direction: the app README follows the code (the same-commit rule keeps it honest), but [ROADMAP.md](../../ROADMAP.md) and the cross-cutting docs lag, because nothing forced them into the loop. This step is that force.

- **Walk ROADMAP.md for every entry the change touches.** Close finished entries (the ✅-plus-date retitle the file already uses), update in-progress entries' state and breadcrumbs so a cold session can resume from them, and mark obsolete entries as such rather than leaving them describing solved problems. An entry that misstates the current state is worse than no entry — it sends the next session chasing work that is already done.
- **Run the matching docs skill per its own trigger**: `/update-platform-docs` after any platform change affecting documented behaviour, `/update-viewer-docs` after viewer-library changes. Their skip conditions (internal refactors, test-only changes, behaviour-restoring fixes) apply unchanged.
- **Confirm the same-commit rules held**: the app README updated with the code, and AGENTS.md itself when a *rule* changed — new convention, new cross-cutting invariant, new gotcha, new load-bearing file.

## Step 4 — The handover report

Every handover states, even when the answer is "nothing":

- which review agents ran and what each returned — a clean run is named, not implied;
- what the clean-slate pass turned up — "nothing found" said deliberately;
- which ROADMAP entries and docs were updated, or that the change touched none.

Silence cannot be distinguished from a pass that never happened; the report exists so it never has to be.
