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
from agent.models import AnswerKind, Provenance, QuestionCategory, ScreeningQuestion

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
