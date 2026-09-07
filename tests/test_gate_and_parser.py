"""Tests for the two-key submit gate and the job-posting parser.

The gate tests are the ones that matter most: they assert the single invariant the
whole design rests on -- that no model-authored answer can reach a real employer
without a human seeing it first.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.linkedin_mcp import parse_job_posting
from agent.models import (
    AUTO_SUBMITTABLE,
    BANK_AND_LLM_FORBIDDEN,
    HUMAN_ONLY,
    ApplyOutcome,
    ApplyResult,
    ProposedAnswer,
    Provenance,
    QuestionCategory,
)


def ans(prov: Provenance, *, value: str = "Yes", evidence: str = "user_facts.x",
        category: QuestionCategory = QuestionCategory.WORK_AUTH) -> ProposedAnswer:
    return ProposedAnswer(question_text="q?", category=category, value=value,
                          provenance=prov, evidence=evidence)


# --------------------------------------------------------------------------- #
# The two-key gate
# --------------------------------------------------------------------------- #
def test_llm_provenance_never_auto_submits():
    """The core invariant. If this test ever passes with True, the design is broken."""
    assert not ans(Provenance.LLM).auto_submittable()


@pytest.mark.parametrize("prov", [Provenance.DETERMINISTIC, Provenance.BANK_MATCH,
                                  Provenance.HUMAN])
def test_safe_provenances_auto_submit(prov):
    assert ans(prov).auto_submittable()


def test_missing_evidence_blocks_even_a_safe_provenance():
    """Evidence is a second, independent lock -- not decoration."""
    assert not ans(Provenance.DETERMINISTIC, evidence="").auto_submittable()
    assert not ans(Provenance.BANK_MATCH, evidence="   ").auto_submittable()


def test_salary_requires_a_human_even_if_deterministic():
    a = ans(Provenance.DETERMINISTIC, category=QuestionCategory.SALARY, value="20")
    assert not a.auto_submittable()
    human = ans(Provenance.HUMAN, category=QuestionCategory.SALARY, value="20")
    assert human.auto_submittable()


def test_one_bad_answer_blocks_the_whole_application():
    """A single unanswered question must abandon the modal, not submit partially."""
    result = ApplyResult(job_id="1", outcome=ApplyOutcome.ABANDONED_NEEDS_REVIEW, answers=[
        ans(Provenance.DETERMINISTIC),
        ans(Provenance.BANK_MATCH),
        ans(Provenance.LLM, value="probably yes"),   # the poison pill
    ])
    blocking = result.blocking_answers()
    assert len(blocking) == 1
    assert blocking[0].provenance is Provenance.LLM


def test_all_good_answers_leave_nothing_blocking():
    result = ApplyResult(job_id="1", outcome=ApplyOutcome.SUBMITTED, answers=[
        ans(Provenance.DETERMINISTIC), ans(Provenance.HUMAN),
    ])
    assert result.blocking_answers() == []


def test_gate_constants_are_coherent():
    assert Provenance.LLM not in AUTO_SUBMITTABLE
    assert HUMAN_ONLY <= BANK_AND_LLM_FORBIDDEN, \
        "a human-only category must also be forbidden to the bank and the model"
    for c in (QuestionCategory.WORK_AUTH, QuestionCategory.SPONSORSHIP,
              QuestionCategory.SALARY, QuestionCategory.EEO,
              QuestionCategory.YEARS_EXPERIENCE):
        assert c in BANK_AND_LLM_FORBIDDEN, c


# --------------------------------------------------------------------------- #
# Parser -- against the real captured payload
# --------------------------------------------------------------------------- #
PAYLOAD = ROOT / "probe_output" / "job_details_raw.txt"


@pytest.mark.skipif(not PAYLOAD.exists(), reason="probe payload not captured")
def test_parses_the_real_captured_posting():
    blob = json.loads(PAYLOAD.read_text(encoding="utf-8"))["sections"]["job_posting"]
    p = parse_job_posting(blob)
    assert p["company"] == "UST"
    assert p["title"] == "Lead Data Engineer"
    assert "Bengaluru" in p["location"]
    assert "ago" in p["posted"]
    assert p["work_type"] == "On-site"
    assert len(p["jd_text"]) > 1000
    # This posting's button reads "Apply" and it says responses are managed off
    # LinkedIn, so it is NOT Easy Apply despite having come from an
    # easy_apply=True search. Measured: 8/8 sampled postings behaved this way.
    assert p["easy_apply"] is False


def test_easy_apply_detected_when_the_button_says_so():
    blob = "\n".join([
        "Acme Corp", "", "AI Engineer", "",
        "Bengaluru, Karnataka, India · 2 hours ago · 5 applicants", "",
        "Remote", "Full-time", "Easy Apply", "Save", "",
        "About the job", "", "We want an AI engineer.", "",
        "Restart Premium today",
    ])
    p = parse_job_posting(blob)
    assert p["easy_apply"] is True
    assert p["title"] == "AI Engineer"
    assert p["work_type"] == "Remote"
    assert "We want an AI engineer." in p["jd_text"]
    assert "Restart Premium" not in p["jd_text"]


def test_off_linkedin_overrides_an_easy_apply_string():
    """Belt and braces: if it says responses are managed off LinkedIn, believe that."""
    blob = "\n".join([
        "Acme", "", "AI Engineer", "",
        "India · 1 hour ago", "",
        "Promoted by hirer · Responses managed off LinkedIn", "",
        "Remote", "Easy Apply", "", "About the job", "", "Body text.",
    ])
    assert parse_job_posting(blob)["easy_apply"] is False


def test_footer_is_stripped_from_the_jd():
    blob = "\n".join([
        "Co", "", "Role", "", "India · now", "", "Remote", "Easy Apply", "",
        "About the job", "", "Real body.", "", "Building a team?", "Post a job",
    ])
    jd = parse_job_posting(blob)["jd_text"]
    assert "Real body." in jd
    assert "Post a job" not in jd
