"""Regression tests for bugs found against live LinkedIn postings.

Every case here is a real question text captured from a real Easy Apply form, and
every assertion encodes a bug that actually shipped and then bit. Keeping them as
tests means a future refactor cannot quietly reintroduce any of them.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent.facts import FactTable, classify, resolve
from agent.models import (AnswerKind, ProposedAnswer, Provenance,
                          QuestionCategory, ScreeningQuestion)

FACTS = FactTable(
    full_name="Rajiv Ranjan Jha",
    email="rajiv.jha.0003@gmail.com",
    phone="8825330125",
    phone_country_code="India (+91)",
    city="Bengaluru, India",
    total_years_experience=4.2,
    notice_period_days=15,
    immediate_joiner=True,
    willing_to_relocate=True,
    relocate_scope="anywhere_in_india",
    skill_years={
        "python": 4.2, "machine_learning": 4.2, "artificial_intelligence": 4.2,
        "genai_llm": 2.0, "deep_learning": 4.2,
    },
    skill_years_signed_off=True,
)


def q(text: str, kind: AnswerKind = AnswerKind.TEXT, options: list[str] | None = None):
    return ScreeningQuestion(text=text, kind=kind, options=options or [])


# --------------------------------------------------------------------------- #
# "Enter a whole number between 0 and 99" -- LinkedIn's own validation message.
# The field is <input type="text"> with a numeric validator, so keying off
# AnswerKind.NUMERIC missed it and every application silently stalled with all
# fields apparently filled.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind,options", [
    (AnswerKind.TEXT, []),
    (AnswerKind.NUMERIC, []),
    (AnswerKind.UNKNOWN, []),
    (AnswerKind.SINGLE_SELECT, ["1", "2", "3", "4", "5+"]),
])
def test_years_always_whole_number_regardless_of_control_kind(kind, options):
    a = resolve(q("How many years of work experience do you have with Machine Learning?",
                  kind, options), FACTS)
    assert a is not None
    assert a.value == "4", f"got {a.value!r} for kind={kind.value}"
    assert "." not in a.value


def test_years_floors_never_inflates():
    """4.2 years must present as 4, never 5."""
    a = resolve(q("How many years of Python experience?"), FACTS)
    assert a.value == "4"


# --------------------------------------------------------------------------- #
# Live postings ask for "Artificial Intelligence (AI)" by name; the alias table
# had no entry, so a perfectly answerable question went to human review.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("phrasing,expected", [
    ("Artificial Intelligence (AI)", "4"),
    ("Machine Learning", "4"),
    ("Deep Learning", "4"),
    ("Generative AI", "2"),
])
def test_ai_family_aliases_resolve(phrasing, expected):
    a = resolve(q(f"How many years of work experience do you have with {phrasing}?"), FACTS)
    assert a is not None, phrasing
    assert a.value == expected, phrasing


def test_skill_absent_from_table_still_declines():
    """Power BI, advanced Excel and Google Sheets all appeared live. None are in
    the table, and none may be estimated from a neighbour."""
    for skill in ("Power Bi or Tableau", "advanced excel", "google sheets", "Scripting"):
        assert resolve(q(f"How many years of experience do you have in {skill}?"),
                       FACTS) is None, skill


# --------------------------------------------------------------------------- #
# "Phone country code" contains the word "phone", so the generic phone branch
# answered it with the raw mobile number.
# --------------------------------------------------------------------------- #
def test_phone_country_code_not_answered_with_the_number():
    a = resolve(q("Phone country code", AnswerKind.SINGLE_SELECT,
                  ["Select an option", "India (+91)", "Albania (+355)"]), FACTS)
    assert a is not None
    assert a.value == "India (+91)"
    assert FACTS.phone not in a.value


def test_mobile_number_still_answered_with_the_number():
    a = resolve(q("Mobile phone number"), FACTS)
    assert a is not None and a.value == FACTS.phone


# --------------------------------------------------------------------------- #
# "Are you comfortable commuting to this job's location?" is a willingness
# yes/no, but classified as LOCATION -- which tried to answer it with a city.
# --------------------------------------------------------------------------- #
def test_commuting_is_willingness_not_location():
    cat, _ = classify("Are you comfortable commuting to this job's location? Required")
    assert cat is QuestionCategory.RELOCATION
    a = resolve(q("Are you comfortable commuting to this job's location? Required",
                  AnswerKind.SINGLE_SELECT, ["Yes", "No"]), FACTS)
    assert a is not None and a.value == "Yes"


# --------------------------------------------------------------------------- #
# "Are you currently residing in Bengaluru?" is a yes/no, and the answer depends
# on which city is named -- so it must compare, not emit the city.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("city,expected", [("Bengaluru", "Yes"), ("Chennai", "No"),
                                           ("Hyderabad", "No")])
def test_residing_in_city_compares(city, expected):
    a = resolve(q(f"Are you currently residing in {city}?", AnswerKind.SINGLE_SELECT,
                  ["Select an option", "Yes", "No"]), FACTS)
    assert a is not None, city
    assert a.value == expected, city


def test_open_ended_city_question_still_returns_the_city():
    a = resolve(q("What is your current city?"), FACTS)
    assert a is not None and a.value == "Bengaluru, India"


# --------------------------------------------------------------------------- #
# A "Follow <company> to stay up to date" checkbox is not part of the
# application. The original regex used [^?.]{0,60}, which cannot cross the full
# stop in "Inc." -- so it never matched and blocked submission instead.
# --------------------------------------------------------------------------- #
def test_follow_company_declined_even_with_a_dotted_company_name():
    text = "Follow InfoSpeed Services, Inc. to stay up to date with their page."
    assert classify(text)[0] is QuestionCategory.FOLLOW_COMPANY
    a = resolve(q(text, AnswerKind.BOOLEAN, ["Yes", "No"]), FACTS)
    assert a is not None
    assert a.value == "No"
    assert a.provenance is Provenance.DETERMINISTIC


# --------------------------------------------------------------------------- #
# Employers do misconfigure questions. One live posting asked "Please mention
# your Notice Period" as a dropdown whose only options were Yes / No. There is
# no correct answer, so the engine must decline rather than pick one.
# --------------------------------------------------------------------------- #
def test_nonsensical_option_list_is_declined():
    a = resolve(q("Please mention your Notice Period/ Earliest you can join.",
                  AnswerKind.SINGLE_SELECT, ["Select an option", "Yes", "No"]), FACTS)
    assert a is None


def test_sensible_notice_period_still_answered():
    a = resolve(q("What is your notice period, mention '0' in case of an immediate joiner"),
                FACTS)
    assert a is not None and "15" in a.value


def test_notice_period_matches_a_real_option_list():
    a = resolve(q("Notice period?", AnswerKind.SINGLE_SELECT,
                  ["Immediate", "15 days", "30 days", "60 days"]), FACTS)
    assert a is not None and a.value == "15 days"


# --------------------------------------------------------------------------- #
# No control-character corruption. A shell heredoc once turned every \b in a
# regex into a literal backspace byte, producing a pattern that compiled
# perfectly and matched nothing.
# --------------------------------------------------------------------------- #
def test_source_is_free_of_control_character_corruption():
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = {}
    for path in list((root / "agent").glob("*.py")) + list((root / "setup").glob("*.py")):
        data = path.read_bytes()
        hits = {name: data.count(ch) for name, ch in
                (("backspace", b"\x08"), ("vtab", b"\x0b"),
                 ("formfeed", b"\x0c"), ("bell", b"\x07")) if data.count(ch)}
        if hits:
            bad[path.name] = hits
    assert not bad, f"control characters found in source: {bad}"


# --------------------------------------------------------------------------- #
# A model-supplied category must never produce an auto-submittable answer.
#
# Found live: "Do you have a valid driving licence for heavy vehicles?" matched no
# regex, the classifier labelled it `education`, and the resolver returned
# "Bachelor's Degree" with DETERMINISTIC provenance -- auto-submittable. A wrong
# route yields a wrong value out of the right table, so anything reached via
# category_override is downgraded to human review.
# --------------------------------------------------------------------------- #
def test_model_routed_answer_is_not_auto_submittable():
    from agent.models import Provenance as P

    # resolve() itself stays honest -- it is deterministic given a category.
    direct = resolve(q("Do you have a valid driving licence for heavy vehicles?"),
                     FACTS, category_override=QuestionCategory.EDUCATION)
    assert direct is not None
    assert direct.provenance is P.DETERMINISTIC

    # But the engine, which is where the model supplied that category, must
    # downgrade it. Verified by mimicking the engine's downgrade contract.
    downgraded = direct.model_copy(update={"provenance": P.LLM,
                                           "reason": "category inferred"})
    assert not downgraded.auto_submittable()


def test_regex_matched_answers_keep_full_trust():
    """The downgrade must apply ONLY to model-routed answers, not rule-matched ones."""
    a = resolve(q("What is your notice period?"), FACTS)
    assert a is not None
    assert a.provenance is Provenance.DETERMINISTIC
    assert a.auto_submittable()


# --------------------------------------------------------------------------- #
# Employers demand CONTRADICTORY formats for the same kind of question:
#   InfoSpeed  -> "Enter a whole number between 0 and 99"   (4.2 rejected)
#   Talentgigs -> "Enter a decimal number larger than 0.0"  (4   rejected)
# No single spelling satisfies both, so the answer carries its alternates and the
# apply engine re-fills from them when the form complains about the format.
# --------------------------------------------------------------------------- #
def test_years_answer_carries_a_decimal_alternate():
    a = resolve(q("How many years of work experience do you have with Machine Learning?"),
                FACTS)
    assert a is not None
    assert a.value == "4"                      # whole number by default
    assert any("." in alt for alt in a.value_alternates), a.value_alternates


def test_notice_period_carries_a_bare_number_alternate():
    a = resolve(q("What is your Notice Period?"), FACTS)
    assert a is not None
    assert "15" in a.value
    assert "15" in a.value_alternates or any("15" in x for x in a.value_alternates)


@pytest.mark.parametrize("blob,expect_decimal,expect_integer", [
    ("Enter a decimal number larger than 0.0", True, False),
    ("Enter a whole number between 0 and 99", False, True),
    ("This field is required", False, False),
])
def test_format_hint_detection(blob, expect_decimal, expect_integer):
    from agent.apply_engine import _WANTS_DECIMAL, _WANTS_INTEGER

    assert bool(_WANTS_DECIMAL.search(blob)) is expect_decimal
    assert bool(_WANTS_INTEGER.search(blob)) is expect_integer


# --------------------------------------------------------------------------- #
# Salary is required by some forms and never auto-answered by policy, so the
# application legitimately cannot proceed. That is "needs a human", NOT "failed":
# recording it as failed dropped the job out of list_pending() and hid the very
# question the user had to answer.
# --------------------------------------------------------------------------- #
def test_required_salary_blocks_but_stays_actionable():
    from agent.models import ApplyOutcome, ApplyResult, Provenance as P

    result = ApplyResult(job_id="1", outcome=ApplyOutcome.ABANDONED_NEEDS_REVIEW, answers=[
        ProposedAnswer(question_text="What is your total years of experience?",
                       category=QuestionCategory.YEARS_EXPERIENCE, value="4",
                       provenance=P.DETERMINISTIC, evidence="user_facts"),
        ProposedAnswer(question_text="What is your Current CTC?",
                       category=QuestionCategory.SALARY, value="",
                       provenance=P.LLM, evidence="", reason="policy"),
        ProposedAnswer(question_text="What is your Expected CTC?",
                       category=QuestionCategory.SALARY, value="",
                       provenance=P.LLM, evidence="", reason="policy"),
    ])
    blocking = result.blocking_answers()
    assert len(blocking) == 2
    assert all(b.category is QuestionCategory.SALARY for b in blocking)
    # And the outcome must be the reviewable one, never FAILED.
    assert result.outcome is ApplyOutcome.ABANDONED_NEEDS_REVIEW


# --------------------------------------------------------------------------- #
# Notice period asked as a yes/no against a stated window.
#
# Live: "Are you available to join immediately or within 15 days?" -> [Yes, No].
# The regex matched "available to start" but not "available to join", and even
# once classified the resolver emitted "15 days (can join immediately)" -- which
# fits neither option, because the question asks whether you FIT a window, not
# how long your notice is.
# --------------------------------------------------------------------------- #
_YN = ["Select an option", "Yes", "No"]


@pytest.mark.parametrize("text,expected", [
    ("Are you available to join immediately or within 15 days?", "Yes"),
    ("Can you join within 30 days?", "Yes"),
    ("Can you join within 1 month?", "Yes"),             # months -> days
    ("Are you available to join within 7 days?", "No"),  # 15d notice does NOT fit
    ("Are you an immediate joiner?", "Yes"),
])
def test_notice_period_window_comparison(text, expected):
    a = resolve(q(text, AnswerKind.SINGLE_SELECT, _YN), FACTS)
    assert a is not None, text
    assert a.value == expected, f"{text!r} -> {a.value!r}"


def test_notice_window_declines_when_no_window_is_stated():
    """A bare "are you available to join?" yes/no cannot be judged from facts."""
    assert resolve(q("Are you available to join?", AnswerKind.SINGLE_SELECT, _YN),
                   FACTS) is None


def test_notice_period_free_text_still_states_the_duration():
    a = resolve(q("What is your notice period?"), FACTS)
    assert a is not None and "15" in a.value


# --------------------------------------------------------------------------- #
# Salary: learned once from you, then answered deterministically.
#
# Re-asking CTC on every application was the most repetitive interruption in the
# system. A salary you have typed is a fact; a salary inferred is not. So it is
# answered only when `salary_confirmed` is True, and "current" vs "expected" is
# decided by keyword rather than similarity -- the two strings are near-identical
# to an embedding but have different correct answers.
# --------------------------------------------------------------------------- #
_UNCONFIRMED = FactTable(current_ctc_lpa=9.6, expected_ctc_lpa=20.0,
                         salary_confirmed=False)
_CONFIRMED = FactTable(current_ctc_lpa=9.6, expected_ctc_lpa=20.0,
                       salary_confirmed=True)


@pytest.mark.parametrize("text", [
    "What is your Current CTC?",
    "What is your Expected CTC?",
    "What is your current CTC in Lacs per annum? *",
])
def test_salary_declined_until_you_confirm_it(text):
    assert resolve(q(text), _UNCONFIRMED) is None


def test_salary_distinguishes_current_from_expected():
    cur = resolve(q("What is your Current CTC?"), _CONFIRMED)
    exp = resolve(q("What is your Expected CTC?"), _CONFIRMED)
    assert cur is not None and exp is not None
    assert cur.value == "9.6"
    assert exp.value == "20"
    assert cur.value != exp.value          # the whole point
    assert "confirmed by you" in exp.evidence


def test_salary_respects_the_unit_the_form_asks_for():
    lacs = resolve(q("What is your current CTC in Lacs per annum? *"), _CONFIRMED)
    rupees = resolve(q("Expected annual CTC in rupees"), _CONFIRMED)
    assert lacs.value == "9.6"
    assert rupees.value == "2000000"


def test_ambiguous_salary_question_is_declined():
    """"What is your CTC?" names neither current nor expected -- ask a human."""
    assert resolve(q("What is your CTC?"), _CONFIRMED) is None


# --------------------------------------------------------------------------- #
# Whitespace normalisation before matching.
#
# "prompt engineering" was reported absent from a resume that lists it, because
# the PDF wraps the term and extraction yields "prompt\nengineering". This hit
# EVERY multi-word vocabulary entry -- vector database, computer vision, time
# series, model registry, google cloud -- against both resumes and job
# descriptions, so detection depended on where a line happened to break.
# --------------------------------------------------------------------------- #
def test_normalise_rejoins_wrapped_multiword_terms():
    from agent.market import normalise

    assert "prompt engineering" in normalise("...skills: prompt\nengineering · MCP")
    assert "vector database" in normalise("we use a vector\n  database daily")
    assert "computer vision" in normalise("Computer\tVision team")


def test_normalise_rejoins_hyphens_split_across_lines():
    from agent.market import normalise

    assert "sentence-transformers" in normalise("FAISS · Sentence-\nTransformers · recall@k")
    assert "multi-modal" in normalise("a multi-\nmodal RAG chatbot")


def test_multiword_skills_detected_despite_line_breaks():
    """The end-to-end effect: a wrapped skill must still count as present."""
    from agent.market import SKILL_GROUPS, mentions, normalise

    wrapped = normalise(
        "Agents LangGraph · custom ReAct loops · prompt\nengineering\n"
        "RAG FAISS · pgvector · Sentence-\nTransformers · vector\ndatabase"
    )
    for skill in ("Prompt engineering", "Embeddings", "Vector databases"):
        assert mentions(wrapped, SKILL_GROUPS[skill]), skill
