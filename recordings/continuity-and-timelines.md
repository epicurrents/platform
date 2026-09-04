# Continuity and timelines

A discontinuous recording has **two timelines**, and every bug in this area comes from
a value crossing between them without being translated. This note names them, records
which one is canonical, and states what the platform does — and deliberately does not
do — about the gaps themselves.

## The two timelines

**Data position.** The offset of a sample from the first sample, measured in seconds
of *recorded signal*. It is total and contiguous: `sample = t * fs` always holds, every
data position between 0 and the recording duration addresses a real sample, and there
are no holes. This is what a reader gives you when it concatenates data records, which
is what MNE does.

**Wall clock.** Time as the amplifier experienced it, measured from the recording's
start timestamp. In an EDF+D file this is what the per-record time-keeping TALs and the
annotation onsets are expressed in. It *has* holes: the intervals when acquisition was
paused are real elapsed time containing no samples.

The two agree exactly up to the first gap and diverge by the accumulated gap duration
thereafter. For `testdata/test.edf` — one 1 s gap — every annotation after 30 s sits 1 s
later in wall clock than in data position.

The distinction that matters most is what a gap *is* in each timeline. In wall clock a
gap is an interval: it has a start, a duration, and dead space inside it. In data
position a gap is not an interval at all — it is an **instantaneous splice**, a
zero-width seam at which the sample before and the sample after are physically
unrelated, recorded minutes or hours apart. There is no "inside" a splice to address.

## Decision: data position is canonical

Everything the platform stores, computes with, or hands to a processor is a data
position. The reasons, in order of weight:

1. `Interruption` rows already use it — the gap map is keyed
   `{data_position: gap_duration}`, so the persisted description of the gaps was
   already on this timeline before anything else was.
2. It is the timeline the data is actually in. A reader hands back a contiguous array;
   any other timeline requires a translation the reader does not perform, and an
   untranslated value is silently wrong rather than loudly wrong.
3. It is total. Wall clock has values that address nothing, so "the sample at t" is a
   partial function there and a total one here — and partial functions in this position
   get papered over with a nearest-sample fallback that reintroduces the skew.
4. `SignalWindow.t0_s` is produced by the loader from a sample offset. Making wall
   clock canonical would mean the loader translating on every window, needing the gap
   map at read time, for a value most processors only use to label their output.

The cost is that a data position is not what a clinician reads off a clock. That is a
presentation concern: the gap map is a complete, durable translation table, so any
display layer can convert on the way out. Converting on the way *in* — at ingest, once
— is cheaper and happens in one place.

## Translation

`recordings.processors.edf` carries both directions:

- `_compute_record_onsets(n, D, gaps)` — data position → wall clock, the forward map,
  used when writing records back out.
- `wall_clock_to_data_position(onset, gaps)` — its exact inverse, used at ingest.

They are pinned against each other by
`test_round_trips_against_compute_record_onsets`, which asserts that translating each
record's computed wall-clock onset back yields exactly `r * D`. Two functions that must
agree, tested by composing them, is the only arrangement that stays correct as the gap
representation changes.

One case is lossy and deliberately so. An onset falling *inside* dead time — an
annotation timestamped during the pause — has no data position, because no sample was
recorded then. `wall_clock_to_data_position` collapses it onto the splice. Rounding to
the seam is honest about where the information went; the alternatives (dropping the
annotation, or letting it drift past the splice into unrelated signal) lose more.

## What ingest does

`AnnotationEntry.onset` stays **wall clock**, because the file rewriter has to write it
back into a TAL and the file's own timeline is wall clock. Converting it in place would
corrupt every round-trip through `write_edf`.

`_save_edf_results` translates at the boundary instead: the persisted
`content["events"]` onsets are data positions, sharing the timeline with the
`Interruption` rows from the same file. The original TAL value is retained as
`wall_clock_onset` only when it differs, so the file's own statement is never lost but
also never confused for the canonical one.

Before this, `content["events"]` held raw TAL onsets while `Interruption` rows held
data positions — the same file's annotations and its gaps were on two different
timelines, with nothing recording which was which.

## What we do not do

**We do not fill gaps.** Padding a splice with zeros, or with anything else, converts a
known absence into data. A filter run across a zero-filled gap produces a transient
that looks like a signal, a spectral estimate over it is computed against samples that
were never measured, and nothing downstream can tell the difference. The gap stays a
gap.

**We do not rewrite source files to suit the reader.** The relationship is:

- *Ingest describes.* It parses the header, records the gaps, and stores what the file
  says. It does not normalise samples.
- *The loader normalises what is free at read time.* Per-channel unit scaling, label
  and type resolution, and refusing what it cannot do honestly. All of it derived from
  the header, none of it requiring a second copy of the data.
- *Materialisation is reserved for sample-changing work.* Today that is exactly one
  thing: resampling a mixed-rate recording, which produces samples that did not exist
  in the source and therefore needs a derived version, a content-addressed manifest,
  and a cache entry. Splitting a discontinuous recording into contiguous runs would
  also qualify, if we ever decide we want that — we currently do not.

So: no second pipeline, and no rewriting of originals. The question "must the import
pipeline modify files to be MNE-compatible?" resolves to no, because the
incompatibilities that matter are all *descriptive* — MNE's exact-string unit table,
its silent contiguity assumption — and are correctly handled by not believing what MNE
reports and going back to the header instead.

## Splice-aware segmentation

Splices are **hard boundaries** in segmentation: no segment interior spans one, and no
halo reaches across one. `compute/segmentation.py` cuts `[0, duration)` at every splice
into maximal contiguous **runs** and segments each run as if it were its own recording
— which is what it physically is — clamping the halo to the *run's* bounds rather than
the recording's.

`compute.tasks.splices_for` supplies them, reading the recording's `Interruption` rows
straight off the canonical timeline with no translation, and
`launch_analysis_run` calls it itself rather than accepting splices as an argument. That
is deliberate: most of the archive is discontinuous, so the safe behaviour has to be the
one every launch path gets for free. A discontinuous recording quietly segmented as if
it were continuous is the failure mode worth designing against.

Three things follow from cutting at splices rather than skipping over them:

- **The partition invariant survives.** Cutting *subdivides* the partition; it does not
  perforate it. The union of the runs is still exactly `[0, duration)`, so ownership
  stays total and resegmentation-invariant — the correctness spine of
  analysis-execution-plan.md §3 is untouched, and an onset landing exactly on a splice
  belongs to the run that follows by the same half-open rule as any other seam.
- **`segment_length` stays the only throughput knob.** Splices come from the recording,
  not the dispatcher, so they do not enter run identity (§3.1). Two runs of the same
  recording at different fan-out widths still cut at the same places.
- **A short halo is now distinguishable from a clamped one.** `SegmentPlan` carries
  `context_starts_at_splice` / `context_ends_at_splice`, which mean something the
  recording-edge clamp does not: there *is* signal beyond this context, deliberately
  withheld because it is not continuous with this segment. A processor needing its full
  receptive field can tell the two cases apart.

A `global` (holistic) stage is exempt — it declared itself holistic, gets the whole
recording, and can read the `Interruption` rows itself.

The smoke suite gained a `splice_segmentation` check asserting all of this against
`testdata/test.edf`, whose single splice at data position 29 s cuts it into a 29 s run
and a 278 s one. `continuity` remains a WARN and should: it is a property of the file,
not a defect. The recording really is several recordings, and no segmentation makes a
detector needing 60 s of context work on a 29 s run.

### How short is too short

Cutting at splices raises a question skipping over them never had to answer: what to do
with a run that is only seconds long. It has a derived answer rather than a chosen one.

A band-wise denoiser cleans each dyadic band at an epoch of some number of cycles of that
band's lower edge and drops any band whose epoch will not fit in the data, so **a run's
length sets the lowest frequency that can be cleaned in it at all**. Turned around, the
shortest run that loses nothing is `epoch_size_in_cycles / lower[last]`, where *last* is
the lowest band the low-cut leaves in play. The denoiser should compute that threshold
itself, against the same band grid it cleans with: two copies of the arithmetic would
drift, and the threshold would stop describing the pipeline it is a threshold for. No such
denoiser ships in core today, so the threshold has no in-repo implementation.

The value is **rate-dependent**, which is the reason it is computed instead of written
down: with the defaults it is 24.0 s at 256 Hz but 24.576 s at 500 Hz, so a hardcoded 24
would silently drop a band on half the archive. `testdata/test.edf` is 500 Hz, so its 29 s
run clears the 24.576 s threshold and cleans the same nine bands down to 0.488 Hz that its
278 s run does — the short run is genuinely not degraded, which a number picked by taste
would have been unable to say.

A run below the threshold is **passed through**, not refused: it would still denoise, just
over a narrower band than the rest of the recording, and under-cleaning quietly is the
failure worth designing against. `write_edf` carries no annotations, so the cleaned EDF
cannot state its own coverage; every run therefore appears in an unconditional
`<output>.edf.coverage.json` sidecar (per-run bounds, bands reached, lowest band edge,
and why a run was skipped) and in a table on stdout. Unconditional because a sidecar
behind a flag would leave the common invocation producing a file that implies end-to-end
cleaning. `--min-run-length N` overrides the derived value and `0` disables the check;
`--force` denoises straight across the gaps and records that it did.

### Refusing, in two places, on the same terms

`load_signal_window` now has its own guard. A plan reaching it was normally built by
`launch_analysis_run`, which already cut at every splice — but a hand-built plan, a
resegmentation oracle, or a plan replayed from before the cutting existed carries no such
guarantee, and the samples cannot show it: a splice leaves no trace in the array, only a
step where two instants recorded hours apart sit side by side.

The guard could not be a database query. This module is reachable from a plain script —
the smoke checks drive it with a stand-in run object and no app registry — so
`compute.tasks.splices_for` would break its one caller that has no database. The splices
therefore come from the file, which is where they were recorded in the first place and
what ingest wrote the `Interruption` rows from. `recordings.processors.edf.read_record_gaps`
is the shared reader, promoted out of the smoke check: a consecutive-onset-difference scan
carries no cross-record state, so restricting it to the records a window overlaps is
**exact, not approximate** — unlike `parse_annotations`, which accumulates `prior_offset`
across the whole file and cannot be range-restricted. A continuous recording costs nothing
at all: the EDF+D marker is checked first and the file is never reopened.

Splices strictly *inside* the window count. A splice is a zero-width seam in data
position, so a window ending at one — exactly what a planner cutting at splices produces
— contains no discontinuity; counting the boundary would refuse every correctly planned
segment.

Where the two consumers meet the same condition they answer it the same way. A file
marked EDF+D that carries no annotation channel has real gaps and no timeline to place
them with, and both the loader and any denoising command must refuse rather than treat it as
continuous, because the whole point of a guard is that *"I could not tell"* must not read
as *"safe"*. The smoke check is the deliberate exception: it is tolerant, because a check
that cannot read the timeline reports that through its own `continuity` result instead of
raising. `--force` is the documented override on the command side, and the sidecar
distinguishes what the file said (`splices_s`, `null` when unplaceable) from what was
actually cut at (`cut_at_s`).

### Still open

- **The window does not carry the flags to the processor.** `SignalWindow` has no
  splice field, so a detector cannot yet see that its left context was withheld. Adding
  one is a contract change and belongs with the first real processor, which is the point
  at which we will know whether any detector actually wants it. The loader's guard makes
  this less urgent than it was: a splice-spanning window is now refused rather than
  quietly returned short, so the flags are informational — "your left context stops here
  deliberately" — rather than the only thing standing between a detector and a step
  discontinuity it cannot see.
