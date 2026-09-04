# Recording lifecycle — post-processing states and job progress

Agreed design for making a recording's post-processing visible and safe. Nothing here is
implemented except the atomic write (§6), which shipped separately because it fixes a
live data-integrity bug.

## 1. The problem

A recording becomes `READY` as soon as its format is parsed, and that transition is what
*fires* post-processing (`post_save` → `apply_eog_regression_task`, `extract_impedance_task`).
So `READY` can never mean "post-processing finished" — it is the thing that starts it.

Three consequences, observed in practice:

- The file is immediately viewable, but without corrected channels, with no indication
  that anything further is coming.
- No completion signal: a rater cannot tell whether they are looking at final data.
- `apply_eog_regression_task` rewrote the EDF **in place** while the UI had already
  published it as viewable. A concurrent reader could see a truncated or half-rewritten
  file. Measured on a synthetic reproduction: 1663 torn reads under concurrent access,
  versus 0 with an atomic replace.

## 2. Core principle

**Draw the state boundary at file mutation, not at "all work done."**

The hazard is reading a file while it is being written. Jobs that only *read* the EDF and
write model rows pose no such hazard, so gating openability on them costs availability for
nothing — and index computation on a multi-hour recording takes minutes.

This is why the file is not simply withheld until everything completes: that would make
availability hostage to the slowest derived job.

## 3. States

```
PENDING ──► PROCESSING ──► AVAILABLE ──► READY
                │              │
                └──────────────┴──► (failures surfaced on the badge, not as FAILED)
```

| state | meaning | UI |
|---|---|---|
| `PENDING` | queued for ingest | not listed / spinner |
| `PROCESSING` | **file-modifying** jobs running; bytes may change under a reader | listed, **disabled**, "Processing" badge |
| `AVAILABLE` | file is final and immutable; derived jobs may still run | **openable**, "N/X background jobs done" badge |
| `READY` | everything complete | openable, no badge |
| `FAILED` | ingest/format failure — the file itself is unusable | error state |

`READY` keeps its current meaning ("everything done") because existing code and callers
already rely on it; `AVAILABLE` is the new intermediate state. Renaming `READY` would be
the riskier change.

**Failure semantics.** A failed *derived* job does not set `FAILED`. The file is intact —
and since §6, it is intact even if a modifying job dies mid-write. Such a recording stays
`AVAILABLE` with the failure shown on the badge. `FAILED` remains reserved for "the file
is unusable", which is what it means today. The badge therefore shows state, not just a
count: `3/4 done, 1 failed` is honest where `3/4` is not.

## 4. Job ledger

Lives in **`recordings`**, not in a project. Tasks *declare* their kind; the platform does
not know what "EOG regression" is. Otherwise a platform model ends up encoding
project-specific stages and the next project or plugin rebuilds the same thing.

Each row: `(recording, job_name, kind, state, started_at, finished_at, error)` with

- `kind` ∈ `MODIFYING` (rewrites the file) | `DERIVED` (reads it, writes model rows)
- `state` ∈ `QUEUED` | `RUNNING` | `DONE` | `FAILED`

Rows are created **at ingest** by enumerating the jobs applicable to the active project and
enabled plugins — that is what makes `X` knowable up front, which the progress badge needs.

State derives from the ledger rather than being set ad hoc:

- any `MODIFYING` row not `DONE`/`FAILED` → `PROCESSING`
- all `MODIFYING` settled, any `DERIVED` outstanding → `AVAILABLE`
- all settled → `READY`

The ledger also gives post-processing a natural audit surface, consistent with the
project's existing stance on derived state.

### Current inventory

| job | kind |
|---|---|
| a project's `apply_eog_regression` | **MODIFYING** — rewrites the EDF with corrected channels + `_orig` |
| a project's `extract_impedance` | DERIVED — reads the EDF, writes `ImpedanceMask` |
| a project's `compute_eeg_indices` | DERIVED — reads the EDF, writes `IndexTimeSeries` / `IndexResult` |

Exactly **one** modifying job exists today. That is why the `PROCESSING` gate is cheap to
build (one task sets and clears it) while the ledger is really driven by the `AVAILABLE`
progress badge.

## 5. Requirements that do not bite yet, but must be designed in

**Serialize the modifying phase.** With one modifying job there is no clobbering risk. Two
concurrent read-transform-write tasks means last-write-wins and one correction silently
disappears. Dispatch modifying jobs as a Celery **chain**, not in parallel. This costs
nothing now and prevents a subtle data-loss bug precisely when a second modifying job is
added and nobody reconsiders the dispatch.

**Re-entrancy.** Re-running a modifying job on an already-`READY` recording must return it
to `PROCESSING` and then forward again. This is not hypothetical — re-triggering
`apply_eog_regression_task` to pick up a new correction algorithm is an active workflow.
A one-way state machine would break it.

**Idempotency is already handled** by the `_orig` convention: the middleware recomputes
from `{label}_orig` rather than re-correcting corrected data.

## 6. Atomic write — implemented

`apply_eog_regression_task` now writes to a sibling temp file and `os.replace()`s it onto
the target, so a reader sees either the whole old file or the whole new one. The temp file
must stay in the **same directory**: `os.replace` is only atomic within a filesystem, and a
`/tmp` staging path would silently degrade to a cross-device copy and reopen the window.
The temp file is removed on failure.

This is independent of the state machine and fixes a real bug regardless of what the UI
does.

## 7. Suggested build order

1. **`PROCESSING` gate** — the single modifying task sets/clears it; viewer disables the
   entry and shows a "Processing" badge. Small, and removes the torn-read exposure from the
   user's point of view as well as the file's.
2. **Ledger + `AVAILABLE`** — model, migration, registry populated at ingest, state derived
   from ledger rows, API exposes `done/total/failed`.
3. **Frontend badge and completion toast** — the `notifications` app and the shared toast
   stack already exist to carry the completion announcement.

## 8. Open questions

- Where does the job registry get its list? Probably an app-config hook mirroring
  `register_derived_state_digester`, so each app declares its own jobs.
- Should `AVAILABLE` recordings be openable in the viewer with derived data still missing
  (indices absent), or should the viewer degrade gracefully per-feature? Leaning toward the
  latter — the badge already communicates incompleteness.
- Retry policy: a `FAILED` derived job is currently terminal in the ledger. Manual retry
  from the UI is probably wanted, which implies the ledger rows need to be re-queueable.
