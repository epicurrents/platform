# Local test data

Real recordings for checks that cannot be written against a fake — chiefly the
signal-loader smoke checks in `compute/signal_smoke.py`, which exist to catch
what MNE does to an actual EDF/BDF header (annotation-channel exclusion,
channel-name de-duplication, per-channel sampling-rate upsampling, unit scaling)
rather than to what an in-memory stub returns.

**Nothing in this folder is committed.** The root `.gitignore` excludes
`/testdata/*` and re-includes only this README; `.dockerignore` excludes the
folder from the image build context, so a recording placed here is never baked
into an image. The test containers still see it: `test` and `test-postgres`
bind-mount the repo at `/code`, so `testdata/test.edf` on the host is
`/code/testdata/test.edf` in the container, and a `BASE_DIR`-relative path
resolves identically on both sides.

## Convention

Drop an EDF or BDF in as `test.edf` (or `test.bdf`). The smoke checks are
Django-free and take a path, so they run from the project venv without a stack:

    .venv/bin/python -c "from compute.signal_smoke import run_checks, worst_status; \
        c = run_checks('testdata/test.edf'); print(*c, sep='\n'); print(worst_status(c))"

A `FAIL` is a loader defect, a `WARN` is a property of the file the loader does
not account for (a discontinuous timeline, mixed sampling rates), and `INFO`
lines carry the numbers worth eyeballing. The reference recording the
segmentation tests and `recordings/continuity-and-timelines.md` describe by
number — 307 s, EDF+D, one 1 s gap at data position 29 s — is such a file; the
tests encode its numbers rather than reading it, so a clean checkout and CI stay
green without it.

The `require_edf` marker declared in `pytest.ini` is for test modules that do
read a file from here; such a module must skip, never fail, when the file is
absent. None is registered today, and no management command wraps
`run_checks` yet — both are listed as follow-ups in `compute/signal_smoke.py`.
The developer commands that read real files (`eeg_clean`, for one) take an
explicit `--input`, and this folder is a convenient place to point them.

## What belongs here

De-identified signal data only. An EDF header carries patient name, ID, and
birth date in its first 88 bytes, and `.gitignore` protects only against
committing the file — it does nothing about the copy sitting on this disk, in
backups, or in a synced folder. Scrub the header before a recording lands here,
and prefer a short excerpt over a full-length study: these files are read on
every smoke run and nothing here needs to be long.

Anything a test can construct instead — a synthetic EDF, a fake raw object —
belongs in the test module, not here. This folder is for the cases where the
point *is* that the bytes came from real hardware.
