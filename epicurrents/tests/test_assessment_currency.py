"""The anonymisation assessment's currency: one answer in the module, the document and the review agent.

``docs/anonymisation-compliance.md`` classifies the platform's output against a named version of
the EDPB anonymisation guidelines and is re-read on the half-yearly sweep. Its Currency block says
which version and when; ``epicurrents/assessment.py`` carries the same values so the sweep command
and the system check can report them; the ``anonymisation`` review agent pins the version it was
written against. Three copies, because three consumers read them from three places, and this file
is what keeps them one value. It also pins the reference-list rule: every guidelines paragraph
cited anywhere in the code is one the assessment discusses, so a re-numbering at final adoption
has one place to start from.

No assertion here depends on the calendar in the failing direction: the review date may be in
the past without failing the suite, since an overdue review is the check's warning and the
agent's finding, not a broken build.
"""

from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.utils import timezone

from epicurrents import assessment
from epicurrents.checks import check_assessment_currency
from epicurrents.management.commands.grant_assessments import DEFAULT_OLDER_THAN_DAYS

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "docs" / "anonymisation-compliance.md"
AGENT = ROOT / ".review" / "agents" / "anonymisation.md"

#: Read once, as the check and the command read it, so a test does not straddle midnight.
TODAY = timezone.localdate()

# Trees that are not this repository's code: vendored, generated, or separately versioned.
EXCLUDED_NAMES = {".venv", "node_modules", ".git", "dist", "viewer-dist", "__pycache__"}
EXCLUDED_PATHS = {Path("frontend/viewer"), Path("docs/epicurrents"), Path("projects")}
SCANNED_SUFFIXES = {".py", ".ts", ".vue"}

PARAGRAPH_IN_CODE = re.compile(r"(?:¶|paragraphs?)\s*(\d+(?:\s*(?:,|and|–|-|to)\s*\d+)*)", re.IGNORECASE)
PARAGRAPH_IN_DOCUMENT = re.compile(r"¶\s*(\d+(?:\s*(?:,|–|-)\s*\d+)*)")


def _fenced_yaml(text: str, heading: str) -> dict[str, str]:
    """The ``key: value`` lines of the fenced ``yaml`` block under the level-2 *heading* in *text*."""
    start = text.index(f"\n## {heading}\n")
    match = re.search(r"```yaml\n(.*?)\n```", text[start:], re.DOTALL)
    assert match, f"no fenced yaml block under {heading!r}"
    values = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        values[key.strip()] = value.strip()
    return values


def _expand(spec: str) -> set[int]:
    """Every paragraph number in a reference such as ``11–12, 30`` or ``35 and 88``."""
    numbers: set[int] = set()
    for part in re.split(r",|and", spec):
        bounds = [int(n) for n in re.findall(r"\d+", part)]
        if not bounds:
            continue
        if len(bounds) == 2 and re.search(r"–|-|to", part):
            numbers.update(range(bounds[0], bounds[1] + 1))
        else:
            numbers.update(bounds)
    return numbers


def _document_paragraphs() -> set[int]:
    text = DOCUMENT.read_text(encoding="utf-8")
    return set().union(*(_expand(spec) for spec in PARAGRAPH_IN_DOCUMENT.findall(text))) if text else set()


def _code_files():
    for directory, subdirs, files in os.walk(ROOT):
        relative = Path(directory).relative_to(ROOT)
        subdirs[:] = [
            name for name in subdirs if name not in EXCLUDED_NAMES and (relative / name) not in EXCLUDED_PATHS
        ]
        for name in files:
            path = Path(directory) / name
            if path.suffix in SCANNED_SUFFIXES:
                yield path


class TestOneValueEverywhere:
    def test_document_block_matches_the_module(self):
        block = _fenced_yaml(DOCUMENT.read_text(encoding="utf-8"), "Currency")
        assert block == {
            "guidelines": assessment.GUIDELINES,
            "guidelines_version": assessment.GUIDELINES_VERSION,
            "guidelines_status": assessment.GUIDELINES_STATUS,
            "reviewed_on": assessment.ASSESSMENT_REVIEWED_ON.isoformat(),
            "review_interval_days": str(assessment.ASSESSMENT_REVIEW_INTERVAL_DAYS),
        }, "docs/anonymisation-compliance.md → Currency and epicurrents/assessment.py disagree; move both together"

    def test_agent_pin_matches_the_module(self):
        pin = _fenced_yaml(AGENT.read_text(encoding="utf-8"), "Currency pin")
        assert pin == {"pinned_guidelines_version": assessment.GUIDELINES_VERSION}, (
            "the anonymisation agent was written against a different guidelines version; re-read its checks"
        )

    def test_the_review_date_is_not_in_the_future(self):
        assert assessment.ASSESSMENT_REVIEWED_ON <= TODAY

    def test_the_interval_is_the_sweep_cadence(self):
        assert assessment.ASSESSMENT_REVIEW_INTERVAL_DAYS == 183
        assert DEFAULT_OLDER_THAN_DAYS == assessment.ASSESSMENT_REVIEW_INTERVAL_DAYS


class TestAssessmentCurrency:
    def test_review_by_is_the_interval_after_the_review(self):
        currency = assessment.assessment_currency(today=assessment.ASSESSMENT_REVIEWED_ON)
        assert currency["review_by"] == assessment.ASSESSMENT_REVIEWED_ON + timedelta(days=183)
        assert currency["days_remaining"] == 183
        assert currency["overdue"] is False
        assert currency["guidelines_version"] == assessment.GUIDELINES_VERSION

    def test_not_overdue_on_the_day_and_overdue_the_day_after(self):
        review_by = assessment.ASSESSMENT_REVIEWED_ON + timedelta(days=183)
        on_the_day = assessment.assessment_currency(today=review_by)
        assert on_the_day["overdue"] is False
        assert on_the_day["days_remaining"] == 0
        after = assessment.assessment_currency(today=review_by + timedelta(days=1))
        assert after["overdue"] is True
        assert after["days_remaining"] == -1


class TestSystemCheck:
    def test_silent_while_current(self, monkeypatch):
        monkeypatch.setattr(assessment, "ASSESSMENT_REVIEWED_ON", TODAY)
        assert check_assessment_currency(None) == []

    def test_warns_once_overdue(self, monkeypatch):
        monkeypatch.setattr(assessment, "ASSESSMENT_REVIEWED_ON", TODAY - timedelta(days=200))
        issues = check_assessment_currency(None)
        assert [issue.id for issue in issues] == ["epicurrents.W020"]
        assert "17 day(s) ago" in issues[0].msg
        assert "ASSESSMENT_REVIEWED_ON" in issues[0].hint
        assert not issues[0].is_serious(), "an overdue review is a warning, never a boot failure"


@pytest.mark.django_db
class TestSweepHeader:
    def _run(self, *args):
        out = StringIO()
        call_command("grant_assessments", *args, stdout=out)
        return out.getvalue()

    def test_text_output_opens_with_the_currency(self, monkeypatch):
        monkeypatch.setattr(assessment, "ASSESSMENT_REVIEWED_ON", TODAY)
        header = self._run().splitlines()[1]
        assert header.startswith(f"Assessment against {assessment.GUIDELINES} version {assessment.GUIDELINES_VERSION}")
        assert f"reviewed {TODAY.isoformat()}" in header
        assert f"next review by {(TODAY + timedelta(days=183)).isoformat()} (183 days)" in header

    def test_overdue_is_said_in_the_header(self, monkeypatch):
        monkeypatch.setattr(assessment, "ASSESSMENT_REVIEWED_ON", TODAY - timedelta(days=190))
        header = self._run().splitlines()[1]
        assert "review OVERDUE since" in header
        assert "(7 days)" in header

    def test_json_carries_the_assessment(self):
        report = json.loads(self._run("--format", "json"))
        assert report["assessment"]["guidelines_version"] == assessment.GUIDELINES_VERSION
        assert report["assessment"]["reviewed_on"] == assessment.ASSESSMENT_REVIEWED_ON.isoformat()
        assert (
            report["assessment"]["review_by"] == (assessment.ASSESSMENT_REVIEWED_ON + timedelta(days=183)).isoformat()
        )
        assert isinstance(report["assessment"]["overdue"], bool)


class TestParagraphReferences:
    def test_the_assessment_cites_paragraphs(self):
        assert {26, 35, 41, 42, 89, 96} <= _document_paragraphs()

    def test_every_paragraph_cited_in_code_is_in_the_assessment(self):
        known = _document_paragraphs()
        unresolved = []
        for path in _code_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in PARAGRAPH_IN_CODE.finditer(text):
                missing = _expand(match.group(1)) - known
                if missing:
                    line = text.count("\n", 0, match.start()) + 1
                    unresolved.append(f"{path.relative_to(ROOT)}:{line} {match.group(0)!r} → {sorted(missing)}")
        assert not unresolved, (
            "guidelines paragraphs cited in code that docs/anonymisation-compliance.md does not discuss; "
            "add them to the assessment where their argument belongs:\n" + "\n".join(unresolved)
        )
