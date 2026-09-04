"""Stage sleep (and optionally detect spindles/slow waves) on an EEG recording.

Reads an ``.edf``/``.bdf``/``.set`` file, remontages to the derivation YASA needs
(default ``C4-M1``), stages sleep, and writes a hypnogram CSV. With --spindles /
--slow-waves it also writes a micro-events CSV (restricted to N2/N3 via the
staged hypnogram). Touches no database.

    python manage.py sleep_stage --input night.edf --output hypno.csv \
        --eeg C4-M1 --eog E1-M2 --age 45 --male 1 --spindles --events-output sp.csv

Remontaging derives the montage from a full-head referential recording (see
compute.sleep.montage). Requires MNE + the sleep extra (``pip install yasa``).
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "YASA sleep staging (+ optional spindle/slow-wave detection) with 10-20 remontaging."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Input .edf/.bdf/.set recording.")
        parser.add_argument("--output", required=True, help="Destination hypnogram .csv.")
        parser.add_argument("--eeg", default="C4-M1", help="EEG derivation spec (default C4-M1).")
        parser.add_argument("--eog", default=None, help="EOG derivation spec (optional).")
        parser.add_argument("--emg", default=None, help="EMG derivation spec (optional).")
        parser.add_argument("--age", type=float, default=None, help="Subject age (years).")
        parser.add_argument("--male", type=int, default=None, choices=[0, 1], help="1 male, 0 female.")
        parser.add_argument("--spindles", action="store_true", help="Also detect spindles.")
        parser.add_argument("--slow-waves", action="store_true", help="Also detect slow waves.")
        parser.add_argument(
            "--events-output", default=None, help="Destination events .csv (with --spindles/--slow-waves)."
        )

    def handle(self, *args, **options):
        from compute.sleep.staging import stage_sleep

        input_path = Path(options["input"])
        output_path = Path(options["output"])
        if not input_path.exists():
            raise CommandError(f"Input not found: {input_path}")

        raw = self._read_raw(input_path)
        srate = float(raw.info["sfreq"])
        ch_names = list(raw.ch_names)
        data_uv = raw.get_data() * 1e6  # volts -> microvolts

        self.stdout.write(f"Staging {input_path.name}: eeg={options['eeg']} @ {srate:g} Hz")
        try:
            res = stage_sleep(
                data_uv,
                srate,
                ch_names,
                eeg=options["eeg"],
                eog=options["eog"],
                emg=options["emg"],
                age=options["age"],
                male=options["male"],
            )
        except (ValueError, RuntimeError) as exc:
            raise CommandError(str(exc)) from exc

        with open(output_path, "w", newline="") as fh:
            w = csv.writer(fh)
            head = ["epoch_onset_s", "stage", "stage_int"] + list(res.proba_classes)
            w.writerow(head)
            for i, (onset, stage, si) in enumerate(zip(res.epoch_onsets_s, res.stages, res.stages_int)):
                row = [f"{onset:.0f}", stage, si]
                if res.proba is not None:
                    row += [f"{p:.4f}" for p in res.proba[i]]
                w.writerow(row)

        from collections import Counter

        dist = ", ".join(f"{k}:{v}" for k, v in Counter(res.stages).most_common())
        self.stdout.write(self.style.SUCCESS(f"Wrote {output_path}: {len(res.stages)} epochs ({dist})."))

        if options["spindles"] or options["slow_waves"]:
            self._detect_events(data_uv, srate, ch_names, res, options)

    def _detect_events(self, data_uv, srate, ch_names, res, options):
        from compute.sleep.events import detect_slow_waves, detect_spindles
        from compute.sleep.staging import hypnogram_to_persample

        out = Path(options["events_output"] or (Path(options["output"]).with_suffix(".events.csv")))
        hypno = hypnogram_to_persample(res.stages_int, srate, data_uv.shape[1])
        rows = []
        if options["spindles"]:
            for e in detect_spindles(data_uv, srate, ch_names, channels=[options["eeg"]], hypno_persample=hypno):
                rows.append({"event": "spindle", **e})
        if options["slow_waves"]:
            for e in detect_slow_waves(data_uv, srate, ch_names, channels=[options["eeg"]], hypno_persample=hypno):
                rows.append({"event": "slow_wave", **e})
        if not rows:
            self.stdout.write("No micro-events detected.")
            return
        fields = ["event"] + sorted({k for r in rows for k in r if k != "event"})
        with open(out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        self.stdout.write(self.style.SUCCESS(f"Wrote {out}: {len(rows)} micro-events."))

    @staticmethod
    def _read_raw(path: Path):
        import mne

        suffix = path.suffix.lower()
        if suffix == ".set":
            return mne.io.read_raw_eeglab(str(path), preload=True, verbose="ERROR")
        if suffix == ".bdf":
            return mne.io.read_raw_bdf(str(path), preload=True, verbose="ERROR")
        return mne.io.read_raw_edf(str(path), preload=True, verbose="ERROR")
