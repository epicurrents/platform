# Version-fetch contract (step 0) — DRAFT for review

The bridge artifact of `signal-pipeline-plan.md`. Both the backend endpoint and the core
viewer are built against this; the Pyodide client consumes the same shapes. Pin it before
either side is built.

Status: **draft.** Open sub-decisions are marked ⟨OPEN⟩ inline and collected in §8 for you
to amend. Nothing here is implemented yet.

Settled going in: manifests live in the audit chain (`ObjectChangeLog`), not git; storage is
source + manifest + evictable cache; versions are content-addressed.

## 1. Resource model

A recording exposes an ordered set of **versions**. Each version is one manifest — an ordered
list of reconstruction-stage applications over the immutable source.

- `version_id` — the manifest's content hash (hex, truncated for display). Immutable: the
  same stages with the same params and code produce the same id. This is what a rater's
  annotation cites, so it must be stable and content-addressed, not a serial number.
- There is always a synthetic version **`source`** — the ingested original, empty manifest,
  `version_id = "source"`. It is the default overlay.
- One version is flagged `is_default` — the latest enabled manifest. The viewer loads it as
  primary and `source` as overlay unless told otherwise.

The contract is **generic**: it speaks of versions, channels, and stage *names* as opaque
strings. It carries no EEG or project semantics — labels like `"eog+wqn"` are strings the
registry supplies, and the viewer renders them without interpreting them.

## 2. Endpoints

Two reads. Both live in the existing django-ninja API (`recordings/api/v1/`) and follow its
conventions (recording addressed by `{hash}`).

### 2.1 List versions

```
GET /recordings/{hash}/versions
```

```jsonc
{
  "recording": "a1b2…",
  "source_hash": "sha256:9f…",          // content hash of the ingested EDF
  "duration_seconds": 7192.0,            // window addressing is in seconds (§2.3)
  "record_duration_sec": 1.0,            // the fetch grid — windows snap to this (§2.3)
  "default_version": "c3d4…",
  "versions": [
    {
      "version_id": "source",
      "label": "Original",
      "state": "ready",
      "manifest": [],
      "channels": [
        {"name": "F7",  "fs": 250.0,  "sample_count": 1798000, "modified_by": []},
        {"name": "ECG", "fs": 1000.0, "sample_count": 7192000, "modified_by": []}
      ]
    },
    {
      "version_id": "c3d4…",
      "label": "EOG + WQN",
      "state": "ready",
      "manifest": [
        {"stage": "eog_regression", "code_version": "3", "params_hash": "7a…"},
        {"stage": "wqn_repair",     "code_version": "1", "params_hash": "b2…"}
      ],
      "channels": [
        {"name": "F7",  "fs": 250.0,  "sample_count": 1798000, "modified_by": ["eog_regression"]},
        {"name": "T9",  "fs": 250.0,  "sample_count": 1798000, "modified_by": ["eog_regression", "wqn_repair"]},
        {"name": "Fp1", "fs": 250.0,  "sample_count": 1798000, "modified_by": []},
        {"name": "ECG", "fs": 1000.0, "sample_count": 7192000, "modified_by": []}
      ]
    }
  ]
}
```

`modified_by` is the per-channel provenance the viewer needs (§8.3 of the plan): an empty
list means the channel is byte-identical to source in this version, so the viewer draws no
overlay for it and never fetches its original.

`fs` and `sample_count` are **per channel**, because EDF permits different rates per signal
(an EEG bank at 250 Hz alongside an ECG at 1000 Hz). `source_hash` and `duration_seconds` at
the top level are the **alignment guard**: every version of one recording shares them by
construction, and the viewer refuses to overlay two versions whose `source_hash` differ.

### 2.2 Fetch signal (per-channel, per-window)

```
GET /recordings/{hash}/versions/{version_id}/signal
      ?channels=F7,F8,ECG       # required; comma-separated, order preserved in payload
      &start_sec=0              # window start, integer multiple of record_duration_sec
      &duration_sec=10         # window length, integer multiple; default = whole recording
```

`start_sec` / `duration_sec` match the established viewer convention (and the management
commands' `--start-sec` / `--max-duration-sec`). They must be **integer multiples of
`record_duration_sec`** (§2.3), so the window lands on exact sample boundaries for every
channel; a non-aligned value is a `400`.

Materialised → `200`, binary body, self-describing framing (§3).

Not materialised → `202 Accepted`, JSON:

```jsonc
{ "state": "building", "version_id": "c3d4…", "poll_after_ms": 1000, "progress": 0.4 }
```

The viewer polls the same URL until `200`. This is the §3.5 rebuild-on-miss surfaced — the
one place a user waits on the pipeline (§8.4). ⟨OPEN: 202-poll vs a streamed/websocket
progress channel; poll is simpler and proposed as default.⟩

Requesting a channel the version does not contain → `400` (the listing is authoritative
about which channels exist). A *passthrough* channel still exists and returns normally —
identical to source — so the viewer can serve it from a source fetch it already has.

### 2.3 Seconds on the record grid, and mixed sampling rates

The request window is a **time interval in seconds** — `[start_sec, start_sec + duration_sec)`
— constrained so both bounds are **integer multiples of `record_duration_sec`** (currently
1 s). Every channel then returns its own native samples covering that interval, with per-
channel `fs`/`start`/`count` in the response header (§3): a 250 Hz channel over a 1 s window
holds 250 floats, a 1000 Hz channel holds 1000.

**Why the record grid eliminates between-samples entirely, at any rate.** EDF stores an
integer number of samples per data record for every signal. So at a record boundary — a whole
second, while records are 1 s — each channel's sample index is `start_sec · fs`, an
integer × integer, exact for *all* channels simultaneously regardless of rate. There is no
rounding, no per-channel edge divergence, nothing for the server to hedge. This is the
exactness a rate-independent integer base (`base_fs` samples) only reached for channels whose
rate divided the base; the record grid reaches it for every channel because integer-samples-
per-record is a format invariant, not a coincidence of the rate.

**It constrains fetch granularity, not display.** Reconstruction and rendering already fetch
with filter padding, so the retrieved window is wider than what is drawn anyway. The viewer
snaps its fetch window *out* to the record grid and displays whatever sub-range the viewport
wants — record-aligned fetch, arbitrary display. The constraint is invisible to the user.

**The one dependency, stated so it fails loudly.** The guarantee holds because request bounds
are integer multiples of `record_duration_sec`. The API validates against the recording's
actual record duration (in the listing), so a future format with a different record length
either keeps the guarantee (whole records still align) or is rejected with a `400` — never
silently rounded. A non-integer-second record length would need this section revisited.

When all channels share one rate — the common case — every per-channel block is the same
length and the per-channel header collapses to what a single-rate reader would expect; the
mixed-rate handling costs the uniform case nothing.

## 3. Binary signal framing

One round-trip, self-describing, feeds a `Float32Array` cache directly.

```
Content-Type: application/octet-stream

[ uint32  header_len          ]   little-endian
[ header_len bytes  JSON utf-8]
[ float32le payload           ]   per-channel blocks in request order, each of the
                                  channel's own native count (see header)
```

Header JSON — channel metadata is a **list**, because per-channel `count` varies with rate:

```jsonc
{
  "version_id": "c3d4…",
  "source_hash": "sha256:9f…",
  "start_sec": 0.0,                    // request window, seconds
  "duration_sec": 1.0,
  "dtype": "float32",
  "endian": "little",
  "channels": [                        // payload order; blocks concatenated in this order
    {"name": "F7",  "fs": 250.0,  "start": 0, "count": 250,  "units": "uV"},
    {"name": "ECG", "fs": 1000.0, "start": 0, "count": 1000, "units": "mV"}
  ]
}
```

The payload is the channel blocks concatenated in listed order; each block is that channel's
own native `count` float32le samples (`= fs · duration_sec`, exact because the window is
record-aligned, §2.3), so the client slices it by walking the per-channel counts. `start`/
`count` are the native sample window for that channel — exact integers, not rounded. `units`
is per channel (µV for EEG, mV for ECG) — another thing a single top-level scalar got wrong.

Rationale: the header repeats `version_id` and `source_hash` so a cached binary blob is
self-validating without the listing. `float32` is the viewer's native cache dtype; EDF's
int16 is decoded server-side once, per §8's "decode once, in code you trust."

**Resolved (was open): float32 on the wire.** int16+scale is deferred and reconsidered only
if a concrete need appears. The standing constraint this creates: a Tier-B client stage must
read **source bytes**, never a fetched float32 window, or the determinism contract (plan
§3.3) breaks — float32 has dropped EDF's exact int16 quantisation by the time it reaches the
client. This is a rule on future client code, recorded here so it is not rediscovered late.

## 4. Immutability and caching

`version_id` is content-addressed, so a materialised signal window is **immutable**. Set
`Cache-Control: public, max-age=31536000, immutable` and `ETag: {version_id}:{channels}:{start}:{count}`.
The browser HTTP cache then does most per-window caching for free, and a re-view of the same
version/window is a local hit. `source` is the one mutable-labelled id, but its *bytes* are
equally immutable (the ingested file never changes), so it caches identically.

## 5. Write side: annotations cite the version

Not an endpoint here, but a contract obligation: an annotation may record the `version_id`
it was scored on. Because the id is content-addressed and the manifest lives in the audit
chain, "this finding was scored on version c3d4…" is permanently reconstructible.

**`version_id` is OPTIONAL on annotation write, so the viewer is not coupled to the backend's
version model.** A version-aware core viewer sends the currently-displayed id (it already
holds it from the fetch). A simpler or third-party client that knows nothing of versions
omits it. The backend must accept the write either way.

When it is **omitted, the backend stores null / "unspecified" — never a silent `source`.**
Recording `source` for an annotation whose provenance is actually unknown would be a false
claim, the exact mislabelling trap the retrofit note below guards against. Presence means
provenance; absence means honestly-unknown.

**Retrofit:** existing annotations predate versioning and get an explicit `null` version_id
on migration — *not* an implicit `source`. Only annotations whose served version at
scoring-time is genuinely recoverable from deployment history may be backfilled with a real
id; the rest stay honestly unspecified.

## 6. Errors

| case | status | body |
|---|---|---|
| unknown recording | 404 | `{detail}` |
| unknown version_id | 404 | `{detail}` |
| channel not in version | 400 | `{detail, channels_available}` |
| `start_sec`/`duration_sec` out of range | 400 | `{detail, duration_seconds}` |
| `start_sec`/`duration_sec` not a multiple of `record_duration_sec` | 400 | `{detail, record_duration_sec}` |
| version building | 202 | `{state:"building", poll_after_ms, progress}` |
| stage failed, version unbuildable | 409 | `{state:"failed", stage, error}` |

## 7. What this does NOT cover

- **Job control** (enable/disable/reorder) — that is `PATCH /jobs` in plan §7, a separate
  surface. This contract is read-only signal access.
- **Stage semantics** — deliberately. Labels and stage names are opaque here.
- **Materialisation triggering** — the 202 path builds lazily on demand. A `POST
  …/materialize` to warm the cache without fetching is possible but not required for v1.

## 8. Sub-decisions — resolved

All five settled in review; recorded here with the resolution.

1. **Building progress → 202-poll.** Simpler, no long-lived connection, rebuilds are minutes
   not seconds. WebSocket revisited only if warranted by some *other* need later, not for
   this alone. (§2.2)
2. **Wire dtype → float32.** int16+scale deferred, reconsidered only on concrete need. The
   standing constraint: Tier-B client stages read source bytes, never fetched float32 windows
   (§3). (was §8.2)
3. **Window addressing → seconds on the record grid.** `start_sec` / `duration_sec` in
   seconds (established viewer convention), constrained to integer multiples of
   `record_duration_sec`. Because EDF stores integer samples per record, a record-aligned
   window lands on exact sample boundaries for *every* channel at *any* rate — eliminating
   between-samples entirely rather than tolerating it out of view. Fetch granularity is
   record-aligned; display granularity is unconstrained (padding over-fetches anyway). (§2.3)
4. **Mixed sampling rates → record-aligned seconds window, per-channel native return.** Each
   channel returns `fs · duration_sec` native samples — exact, not rounded — reported
   per-channel (`fs`, `start`, `count`) in the response header. (§2.3, §3)
5. **Annotation `version_id` → optional on write, null when absent, never silent `source`.**
   Keeps the viewer decoupled from the backend's version model; presence means provenance,
   absence means honestly-unknown. Retrofit uses explicit `null`, not implicit `source`. (§5)
6. **Channel selector → referential names only.** Montages are derived client-side (plan
   §8.2); the server never sees `F7-F8`.

## 9. Why these shapes

- **Per-channel-per-window, not per-file:** the original is inspected in spots; whole-file
  fetch moves 100+ MB on first glance (plan §8.2).
- **Provenance in the listing, not the signal:** the viewer decides *what to fetch* from the
  listing (one small request) before fetching *any* signal, so it never pulls originals for
  passthrough channels.
- **Source-hash everywhere:** the alignment guard is cheap and catches the one class of bug
  (a future resampling stage) that would silently misalign an index-wise overlay.
- **Content-addressed ids:** make caching immutable, annotations citeable, and the whole
  thing reconcilable against the audit-chain manifest.
