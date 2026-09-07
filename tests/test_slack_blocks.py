"""Slack Block Kit limits, asserted locally.

A card that breaches a Slack limit fails at post time with `invalid_blocks`, and
because each card posts separately the symptom is a digest HEADER with no cards
under it -- the message that arrives implies the cards did too, so the failure
looks like Slack being flaky rather than a bug.

That shipped once: the reject-reason overflow menu had six options against a
documented maximum of five, and every job card silently failed to post. These
tests check the documented limits without needing a network call.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import feedback
from agent.slack_notify import (
    _MAX_OVERFLOW_OPTIONS,
    _REJECT_REASONS,
    job_card,
    resolved_card,
)

# Slack's documented caps for the pieces these cards use.
MAX_BLOCKS_PER_MESSAGE = 50
MAX_ELEMENTS_PER_ACTIONS = 25
MAX_SECTION_TEXT = 3000
MAX_BUTTON_TEXT = 75
MAX_HEADER_TEXT = 150

JOB = {
    "job_id": "4462938838",
    "title": "Senior AI Engineer – Semantic Search & Product Intelligence",
    "company": "ThreatXIntel",
    "location": "Bengaluru, Karnataka, India",
    "url": "https://www.linkedin.com/jobs/view/4462938838/",
    "match_score": 78,
    "status": "pending",
}
ANSWERS = [
    {"question_text": "Email address", "value": "rajiv.jha.0003@gmail.com",
     "provenance": "deterministic", "category": "contact", "reason": ""},
    {"question_text": "What is your Current CTC?", "value": "9.6",
     "provenance": "deterministic", "category": "salary", "reason": ""},
    {"question_text": "Describe your experience with agentic systems in detail",
     "value": "", "provenance": "llm", "category": "free_text",
     "reason": "no deterministic fact, no bank match"},
]


def test_reject_reasons_within_overflow_limit():
    """The bug that broke every card: six options where Slack allows five."""
    assert len(_REJECT_REASONS) <= _MAX_OVERFLOW_OPTIONS, (
        f"{len(_REJECT_REASONS)} reject reasons exceeds Slack's overflow cap of "
        f"{_MAX_OVERFLOW_OPTIONS}; every job card will fail with invalid_blocks"
    )


def test_reject_reasons_match_the_handler():
    """A key the card offers but the handler cannot name shows a raw slug."""
    for key in _REJECT_REASONS:
        assert key in feedback.REASONS, f"{key!r} has no label in feedback.REASONS"


def test_job_card_respects_slack_limits():
    blocks = job_card(JOB, ANSWERS)
    assert len(blocks) <= MAX_BLOCKS_PER_MESSAGE

    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= MAX_SECTION_TEXT
        if b["type"] == "header":
            assert len(b["text"]["text"]) <= MAX_HEADER_TEXT
        if b["type"] == "actions":
            elements = b["elements"]
            assert len(elements) <= MAX_ELEMENTS_PER_ACTIONS
            for el in elements:
                if el["type"] == "overflow":
                    assert len(el["options"]) <= _MAX_OVERFLOW_OPTIONS
                    for opt in el["options"]:
                        assert len(opt["text"]["text"]) <= MAX_BUTTON_TEXT
                        # value carries "job_id|reason" and must round-trip
                        jid, _, reason = opt["value"].partition("|")
                        assert jid == JOB["job_id"]
                        assert reason in feedback.REASONS
                if el["type"] == "button":
                    assert len(el["text"]["text"]) <= MAX_BUTTON_TEXT


def test_job_card_survives_absurdly_long_content():
    """Long JDs and long questions must not push a section over 3000 chars."""
    job = {**JOB, "title": "X" * 600, "company": "Y" * 400}
    answers = [
        {"question_text": "Q" * 900, "value": "V" * 900,
         "provenance": "llm", "category": "free_text", "reason": "R" * 900}
        for _ in range(12)
    ]
    for b in job_card(job, answers):
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= MAX_SECTION_TEXT


@pytest.mark.parametrize("verdict", ["applied", "rejected", "answered",
                                     "in_progress", "failed", "pending"])
def test_resolved_card_has_no_buttons(verdict):
    """Once acted on, nothing must remain clickable -- that is what stops a
    second click from starting a second application."""
    blocks = resolved_card(JOB, verdict, "some detail")
    assert not any(b["type"] == "actions" for b in blocks), (
        "a resolved card still offers buttons; a repeat click would get through"
    )
    for b in blocks:
        if b["type"] == "section":
            assert len(b["text"]["text"]) <= MAX_SECTION_TEXT
