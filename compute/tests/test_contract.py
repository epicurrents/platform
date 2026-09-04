"""Sanity checks for the analysis-processor contract (compute/contract.py).

Pure — no Django. Confirms the spec constructs with the intended defaults,
channel granularity is first-class, and a plain callable satisfies the Processor
protocol (the contract is a shape, not a base class to inherit).
"""

from compute.contract import (
    AnalysisOutput,
    EventSpec,
    LabelSpec,
    Processor,
    RunContext,
    SignalWindow,
)


def test_event_defaults_and_channel_granularity():
    e = EventSpec(kind="spike_events", onset_s=12.5)
    assert e.duration_s is None
    assert e.channels == ()  # () = whole montage / global
    assert e.confidence is None
    assert e.extra == {}
    localised = EventSpec(kind="spike_events", onset_s=12.5, channels=("F7", "T7"))
    assert localised.channels == ("F7", "T7")


def test_label_spans_and_scoping():
    stage = LabelSpec(kind="sleep_stage", value="N2", onset_s=30.0, duration_s=30.0)
    assert stage.channels == ()  # whole montage
    bad = LabelSpec(kind="signal_quality", value="bad", channels=("T7",))
    assert bad.onset_s is None and bad.duration_s is None  # whole window
    assert bad.channels == ("T7",)


def test_output_holds_events_and_or_labels():
    out = AnalysisOutput(
        events=(EventSpec(kind="spike_events", onset_s=1.0),),
        labels=(LabelSpec(kind="sleep_stage", value="W"),),
    )
    assert len(out.events) == 1 and len(out.labels) == 1
    empty = AnalysisOutput()
    assert empty.events == () and empty.labels == ()


def test_run_context_structure_and_gated_demographics():
    ctx = RunContext(produces_kind="spike_events", params={"threshold": 0.4})
    assert ctx.params == {"threshold": 0.4}
    # Demographics are a named, egress-gated field defaulting to unpopulated.
    assert ctx.subject_age_years is None


def test_plain_callable_satisfies_processor_protocol():
    def proc(window: SignalWindow, context: RunContext) -> AnalysisOutput:
        return AnalysisOutput()

    # No inheritance required — the contract is structural.
    assert isinstance(proc, Processor)
    window = SignalWindow(data=[[0.0, 1.0]], channels=("Fp1",), fs=256.0, t0_s=0.0, n_samples=2)
    assert proc(window, RunContext(produces_kind="spike_events")) == AnalysisOutput()
