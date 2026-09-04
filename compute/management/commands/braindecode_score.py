"""Score an EEG file with a braindecode model — rough testing / plumbing check.

Builds a BraindecodeModelSpec from flags, reads an ``.edf``/``.bdf``/``.set``
recording with MNE, scans it, and writes per-window model scores as CSV. Touches
no database.

    python manage.py braindecode_score --input rec.edf --output scores.csv \
        --arch EEGNetv4 --sfreq 128 --window-seconds 4 --n-outputs 2 \
        --checkpoint /path/to/model.pt   # or --repo-id braindecode/...  or --random-init

Weights come from --repo-id (HuggingFace from_pretrained), --checkpoint (local),
or --random-init (untrained, meaningless output — tests the scan/preprocess path).
Requires torch + braindecode (a separate ML extra) and MNE to read the input.
"""

from __future__ import annotations

import csv
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Score an EEG recording with a braindecode model and write per-window scores."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Input .edf/.bdf/.set recording.")
        parser.add_argument("--output", required=True, help="Destination .csv for per-window scores.")
        # Weights source (exactly one, or --random-init):
        parser.add_argument("--repo-id", default=None, help="HuggingFace repo id for from_pretrained.")
        parser.add_argument("--checkpoint", default=None, help="Local checkpoint path.")
        parser.add_argument(
            "--random-init", action="store_true", help="Untrained model to test the pipeline (meaningless output)."
        )
        # Spec:
        parser.add_argument("--arch", default=None, help="braindecode model class, e.g. EEGNetv4.")
        parser.add_argument("--sfreq", type=float, required=True, help="Model sampling rate (Hz).")
        parser.add_argument(
            "--n-chans", type=int, default=None, help="Channel count (default: len(--channels) or all)."
        )
        parser.add_argument("--channels", default=None, help="Comma-separated channel labels in model order.")
        parser.add_argument("--window-seconds", type=float, default=None, help="Window length (s).")
        parser.add_argument(
            "--n-times", type=int, default=None, help="Window length (samples); alt to --window-seconds."
        )
        parser.add_argument("--hop-seconds", type=float, default=1.0, help="Sliding hop (s).")
        parser.add_argument("--n-outputs", type=int, default=2, help="Model output dimension.")
        parser.add_argument("--output-type", default="logits", choices=["logits", "probs", "raw"])
        parser.add_argument("--normalization", default="none", choices=["none", "zscore", "percentile", "exp_moving"])
        parser.add_argument("--notch", type=float, default=None, help="Mains notch Hz (50/60), or omit.")
        parser.add_argument(
            "--positive-index", type=int, default=None, help="Class index to turn into a per-sample score + events."
        )
        parser.add_argument("--threshold", type=float, default=0.5, help="Event threshold for --positive-index.")
        parser.add_argument(
            "--noncommercial",
            action="store_true",
            help="Mark the weights non-commercial (gates on EPICURRENTS_NONCOMMERCIAL_USE).",
        )

    def handle(self, *args, **options):
        from compute.braindecode.detect import score_recording
        from compute.braindecode.spec import BraindecodeModelSpec

        input_path = Path(options["input"])
        output_path = Path(options["output"])
        if not input_path.exists():
            raise CommandError(f"Input not found: {input_path}")
        if not any([options["repo_id"], options["checkpoint"], options["random_init"]]):
            raise CommandError("Provide --repo-id, --checkpoint, or --random-init.")

        channels = tuple(c.strip() for c in options["channels"].split(",")) if options["channels"] else None
        n_chans = options["n_chans"] or (len(channels) if channels else None)
        if n_chans is None:
            raise CommandError("Give --n-chans or --channels so the model input width is known.")

        try:
            spec = BraindecodeModelSpec(
                name=options["arch"] or "braindecode-model",
                n_chans=n_chans,
                sfreq=options["sfreq"],
                arch=options["arch"],
                channels=channels,
                window_seconds=options["window_seconds"],
                n_times=options["n_times"],
                hop_seconds=options["hop_seconds"],
                n_outputs=options["n_outputs"],
                output=options["output_type"],
                notch_hz=options["notch"],
                normalization=options["normalization"],
                positive_index=options["positive_index"],
                noncommercial=options["noncommercial"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        raw = self._read_raw(input_path)
        srate = float(raw.info["sfreq"])
        ch_names = list(raw.ch_names)
        data_uv = raw.get_data() * 1e6  # volts -> microvolts

        self.stdout.write(
            f"Scoring {input_path.name} with {spec.name} "
            f"({spec.n_chans}ch @ {spec.sfreq:g}Hz, window {spec.window_samples()} samples)"
            + (" [RANDOM-INIT]" if options["random_init"] else "")
        )

        try:
            result = (
                score_recording(
                    data_uv,
                    srate,
                    ch_names,
                    spec,
                    repo_id=options["repo_id"],
                    checkpoint=options["checkpoint"],
                    threshold=options["threshold"],
                )
                if not options["random_init"]
                else self._score_random(data_uv, srate, ch_names, spec, options["threshold"])
            )
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise CommandError(str(exc)) from exc

        with open(output_path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["onset_s"] + [f"class_{i}" for i in range(spec.n_outputs)])
            for onset, row in zip(result.window_onsets_s, result.window_scores):
                writer.writerow([f"{onset:.3f}"] + [f"{v:.4f}" for v in row])

        msg = f"Wrote {output_path}: {len(result.window_onsets_s)} windows scored."
        if result.events:
            msg += f" {len(result.events)} events at threshold {result.threshold}."
        self.stdout.write(self.style.SUCCESS(msg))

    @staticmethod
    def _score_random(data_uv, srate, ch_names, spec, threshold):
        from compute.braindecode.detect import score_recording
        from compute.braindecode.model import load_model

        model = load_model(spec, allow_random_init=True)
        return score_recording(data_uv, srate, ch_names, spec, model=model, threshold=threshold)

    @staticmethod
    def _read_raw(path: Path):
        import mne

        suffix = path.suffix.lower()
        if suffix == ".set":
            return mne.io.read_raw_eeglab(str(path), preload=True, verbose="ERROR")
        if suffix == ".bdf":
            return mne.io.read_raw_bdf(str(path), preload=True, verbose="ERROR")
        return mne.io.read_raw_edf(str(path), preload=True, verbose="ERROR")
