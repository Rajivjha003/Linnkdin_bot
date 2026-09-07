"""Tests for the deterministic answer engine.

The engine's job is to be right or silent. Most of these tests assert silence --
that it declines rather than guessing -- because a wrong answer on a real
application is unrecoverable while a decline merely costs a Slack notification.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.facts import FactTable, classify, mentions_foreign_country, pick_option, resolve
from agent.models import AnswerKind, Provenance, QuestionCategory, ScreeningQuestion

FACTS = FactTable(
    full_name="Rajiv Ranjan Jha",
    email="rajiv.jha.0003@gmail.com",
    phone="8825330125",
    city="Bengaluru, India",
    total_years_experience=4.2,
    work_auth_india=True,
    requires_sponsorship_india=False,
    notice_period_days=15,
    immediate_joiner=True,
    current_ctc_lpa=9.6,
    expected_ctc_lpa=20,
    willing_to_relocate=True,
    relocate_scope="anywhere_in_india",
    skill_years={"python": 4.2, "genai_llm": 2.0, "sql": 4.2, "docker": 2.0},
    skill_years_signed_off=True,
)


def q(text: str, kind: AnswerKind = AnswerKind.TEXT, options: list[str] | None = None):
    return ScreeningQuestion(text=text, kind=kind, options=options or [])


# --------------------------------------------------------------------------- #
# Classification: the trap pair must land in DIFFERENT categories
# --------------------------------------------------------------------------- #
def test_trap_pair_classified_distinctly():
    """These score 0.9097 cosine similarity but have opposite correct answers."""
    auth, _ = classify("Are you legally authorized to work in India?")
    spon, _ = classify("Do you require visa sponsorship to work in India?")
    assert auth is QuestionCategory.WORK_AUTH
    assert spon is QuestionCategory.SPONSORSHIP
    assert auth is not spon


def test_trap_pair_gets_opposite_answers():
    auth = resolve(q("Are you legally authorized to work in India?", options=["Yes", "No"]), FACTS)
    spon = resolve(q("Do you require visa sponsorship to work in India?", options=["Yes", "No"]), FACTS)
    assert auth is not None and auth.value == "Yes"
    assert spon is not None and spon.value == "No"
    assert auth.value != spon.value  # the whole point


@pytest.mark.parametrize("text,expected", [
    ("How many years of experience do you have with Python?", QuestionCategory.YEARS_EXPERIENCE),
    ("What is your expected CTC?", QuestionCategory.SALARY),
    ("What is your notice period?", QuestionCategory.NOTICE_PERIOD),
    ("Are you willing to relocate to Hyderabad?", QuestionCategory.RELOCATION),
    ("Do you have a disability?", QuestionCategory.EEO),
    ("What is your phone number?", QuestionCategory.CONTACT),
    ("Are you legally authorised to work in India?", QuestionCategory.WORK_AUTH),
    ("Will you now or in the future require sponsorship?", QuestionCategory.SPONSORSHIP),
    ("Describe why you are a good fit for this role.", QuestionCategory.UNKNOWN),
])
def test_classification(text, expected):
    assert classify(text)[0] is expected


def test_eeo_beats_everything():
    """An EEO question mentioning work must still classify as EEO."""
    cat, _ = classify("Are you a protected veteran authorized to work?")
    assert cat is QuestionCategory.EEO


# --------------------------------------------------------------------------- #
# Refusals -- the engine must decline rather than approximate
# --------------------------------------------------------------------------- #
def test_salary_never_answered():
    for text in ("What is your expected CTC?", "Desired salary?", "Expected compensation"):
        assert resolve(q(text), FACTS) is None, text


def test_unknown_skill_is_declined_not_estimated():
    """Kubernetes is absent from skill_years. Must NOT borrow docker's 2.0."""
    assert resolve(q("How many years of experience do you have with Kubernetes?"), FACTS) is None


def test_foreign_jurisdiction_declined():
    assert resolve(q("Are you legally authorized to work in the United States?"), FACTS) is None
    assert resolve(q("Do you require H-1B sponsorship?"), FACTS) is None
    assert resolve(q("Are you authorized to work in the US?"), FACTS) is None


def test_compound_jurisdiction_declined():
    assert resolve(q("Are you authorized to work in India or the United States?"), FACTS) is None


def test_bare_lowercase_us_is_not_a_country():
    """'work with us' must not be read as the United States."""
    assert not mentions_foreign_country("Are you excited to work with us in India?")


def test_unsigned_skill_table_blocks_years_answers():
    unsigned = FactTable(**{**FACTS.__dict__, "skill_years_signed_off": False})
    assert resolve(q("How many years of Python experience?"), unsigned) is None


def test_unmappable_options_declined():
    """If our canonical answer fits none of the offered options, ask a human."""
    question = q("Are you authorized to work in India?", AnswerKind.SINGLE_SELECT,
                 options=["Citizen", "Permanent resident", "Other"])
    assert resolve(question, FACTS) is None


# --------------------------------------------------------------------------- #
# Correct deterministic answers
# --------------------------------------------------------------------------- #
def test_years_are_whole_numbers():
    """LinkedIn validates these fields numerically and rejects decimals outright:
    "Enter a whole number between 0 and 99". Flooring (4.2 -> 4) also keeps the
    claim from ever exceeding what the employment dates support."""
    a = resolve(q("How many years of experience do you have with Python?"), FACTS)
    assert a is not None
    assert a.value == "4"
    assert a.provenance is Provenance.DETERMINISTIC
    assert "skill_years[python]" in a.evidence


def test_genai_alias_resolution():
    for phrasing in ("large language models", "Generative AI", "LLM"):
        a = resolve(q(f"How many years of experience do you have with {phrasing}?"), FACTS)
        assert a is not None, phrasing
        assert a.value == "2", phrasing  # 2.0 -> "2", not "2.0"


def test_total_experience_needs_general_wording():
    assert resolve(q("How many years of professional experience do you have?"), FACTS).value == "4"
    # A bare years question naming nothing general and no skill -> decline
    assert resolve(q("How many years?"), FACTS) is None


def test_notice_period_numeric_vs_text():
    assert resolve(q("Notice period in days?", AnswerKind.NUMERIC), FACTS).value == "15"
    assert "15 days" in resolve(q("What is your notice period?"), FACTS).value


def test_eeo_declines_politely():
    a = resolve(q("Do you identify as having a disability?", AnswerKind.SINGLE_SELECT,
                  options=["Yes", "No", "I prefer not to answer"]), FACTS)
    assert a is not None
    assert a.value == "I prefer not to answer"


def test_contact_fields():
    assert resolve(q("Email address"), FACTS).value == FACTS.email
    assert resolve(q("Mobile number"), FACTS).value == FACTS.phone
    assert resolve(q("First name"), FACTS).value == "Rajiv"
    assert resolve(q("Last name"), FACTS).value == "Jha"


def test_every_answer_carries_evidence():
    """An answer with no evidence is not auto-submittable by contract."""
    for text in ("Email address", "What is your notice period?",
                 "Are you willing to relocate?", "How many years of SQL experience?"):
        a = resolve(q(text), FACTS)
        assert a is not None and a.evidence.strip(), text
        assert a.auto_submittable(), text


# --------------------------------------------------------------------------- #
# Option matching
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("desired,options,expected", [
    ("yes", ["Yes", "No"], "Yes"),
    ("no", ["Yes", "No"], "No"),
    ("yes", ["I am authorized to work", "I am not authorized"], "I am authorized to work"),
    ("__decline__", ["Yes", "No", "Prefer not to say"], "Prefer not to say"),
    ("yes", ["Maybe", "Unsure"], None),
])
def test_pick_option(desired, options, expected):
    assert pick_option(desired, options) == expected


# --------------------------------------------------------------------------- #
# category_override: a model-supplied label routes, but never supplies a value
# --------------------------------------------------------------------------- #
def test_category_override_enables_a_deterministic_answer():
    """Wording the regex misses can still be answered from the fact table."""
    weird = q("Kindly indicate your availability for onboarding.")
    assert resolve(weird, FACTS) is None          # regex does not recognise it
    forced = resolve(weird, FACTS, category_override=QuestionCategory.NOTICE_PERIOD)
    assert forced is not None
    assert "15 days" in forced.value
    assert forced.provenance is Provenance.DETERMINISTIC
    assert "notice_period_days" in forced.evidence


def test_category_override_still_refuses_salary():
    weird = q("What are your remuneration expectations, broadly?")
    assert resolve(weird, FACTS, category_override=QuestionCategory.SALARY) is None


def test_skill_override_is_still_checked_against_the_table():
    """A model naming an unknown skill must not unlock an invented number."""
    weird = q("Rate your hands-on depth with Kubernetes in years.")
    assert resolve(weird, FACTS, category_override=QuestionCategory.YEARS_EXPERIENCE,
                   skill_override="kubernetes") is None
    ok = resolve(weird, FACTS, category_override=QuestionCategory.YEARS_EXPERIENCE,
                 skill_override="python")
    assert ok is not None and ok.value == "4"


def test_category_override_respects_jurisdiction_guard():
    weird = q("Confirm your eligibility for employment in the United States.")
    assert resolve(weird, FACTS, category_override=QuestionCategory.WORK_AUTH) is None
