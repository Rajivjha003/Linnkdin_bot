"""Deterministic screening-question answering. No model is involved anywhere here.

This module exists because a language model cannot be trusted with a number or with
a question whose near-synonym has the opposite correct answer. Classification is
regex; resolution is a dict lookup plus arithmetic. Anything this module cannot
answer with certainty returns None, which routes the question to a human -- it never
guesses and never approximates.

Two rules it will not break:
  1. If the question names a country other than India, it declines. The fact table
     only describes India, and "authorized to work in the US" is a different
     question with a different answer.
  2. If a years-of-experience question names a skill absent from `skill_years`, it
     declines rather than estimating from a related skill.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agent.models import (
    AnswerKind,
    ProposedAnswer,
    Provenance,
    QuestionCategory,
    ScreeningQuestion,
)

# --------------------------------------------------------------------------- #
# Skill vocabulary: maps the words a posting might use onto a `skill_years` key.
# Longest alias wins, so "machine learning" is not shadowed by "ml".
# --------------------------------------------------------------------------- #
SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "python": ("python",),
    "machine_learning": ("machine learning", "machine-learning", "ml"),
    "artificial_intelligence": ("artificial intelligence", "a.i.", "ai"),
    "deep_learning": ("deep learning", "neural network", "neural networks"),
    "nlp": ("nlp", "natural language processing"),
    "data_science": ("data science", "data scientist"),
    "genai_llm": (
        "generative ai", "gen ai", "genai", "llms", "llm",
        "large language model", "large language models", "foundation model",
    ),
    "rag": ("retrieval augmented generation", "retrieval-augmented", "rag"),
    "google_adk": ("google adk", "agent development kit", "adk"),
    "mcp": ("model context protocol", "mcp"),
    "langchain_agents": ("langchain", "langgraph", "agentic", "ai agents", "agent framework"),
    "fine_tuning_peft": (
        "fine tuning", "fine-tuning", "finetuning", "peft", "qlora", "lora", "sft",
    ),
    "time_series": ("time series", "time-series", "forecasting", "demand planning"),
    "lightgbm": ("lightgbm", "xgboost", "gradient boosting"),
    "bigquery": ("bigquery", "big query"),
    "sql": ("sql", "postgresql", "postgres", "alloydb"),
    "fastapi": ("fastapi",),
    "django": ("django",),
    "docker": ("docker", "containerization", "containerisation"),
    "gcp_cloud_run": ("google cloud platform", "cloud run", "gcp", "google cloud"),
    "computer_vision_yolo": ("computer vision", "yolo", "opencv", "object detection"),
    "pytorch": ("pytorch", "torch"),
    "react_nextjs": ("next.js", "nextjs", "reactjs", "react"),
}

# Ordered longest-first so a longer alias is tested before a shorter substring.
_ALIAS_ORDER: list[tuple[str, str]] = sorted(
    ((alias, key) for key, aliases in SKILL_ALIASES.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

# --------------------------------------------------------------------------- #
# Country detection. The fact table describes India only.
# --------------------------------------------------------------------------- #
_INDIA = re.compile(r"\b(india|indian|bharat)\b", re.I)
_FOREIGN = re.compile(
    r"\b(united states|u\.s\.a\.?|usa|america|american"
    r"|united kingdom|u\.k\.|britain|british"
    r"|canada|canadian|australia|australian|new zealand"
    r"|germany|german|netherlands|dutch|ireland|irish|france|french"
    r"|singapore|japan|japanese|china|chinese|uae|dubai|qatar|saudi"
    r"|switzerland|sweden|norway|denmark|poland|spain|italy"
    r"|european union|schengen|\beu\b|\bemea\b"
    r"|h-?1b|opt|cpt|tn visa|green card|ead)\b",
    re.I,
)
# Bare "US" only counts when actually capitalised, so the pronoun "us" is ignored.
_BARE_US = re.compile(r"\bUS\b")


def mentions_foreign_country(text: str) -> bool:
    if _FOREIGN.search(text) or _BARE_US.search(text):
        return True
    return False


# --------------------------------------------------------------------------- #
# Category patterns. Order matters: the list is evaluated top-down and the first
# match wins, so the more specific pattern must come first. Sponsorship is tested
# before work authorisation precisely because they co-occur.
# --------------------------------------------------------------------------- #
_PATTERNS: list[tuple[QuestionCategory, re.Pattern[str]]] = [
    # \w* suffixes matter: "disabilit\b" cannot match "disability", because the
    # trailing "y" is itself a word character.
    (QuestionCategory.EEO, re.compile(
        r"\b(disabilit\w*|veteran\w*|ethnicit\w*|race|racial|gender\w*"
        r"|sexual\s+orientation|transgender|hispanic|latino|pronouns?"
        r"|protected\s+(status|veteran))\b", re.I)),
    (QuestionCategory.SPONSORSHIP, re.compile(
        r"\bsponsor(ship|ing|ed)?\b|\bvisa\b|\bwork permit\b|\bh-?1b\b", re.I)),
    (QuestionCategory.WORK_AUTH, re.compile(
        r"\b(legally\s+(authoriz|authoris|entitled|permitted|eligible)"
        r"|(authoriz|authoris)\w*\s+to\s+work"
        r"|right\s+to\s+work|eligible\s+to\s+work|permitted\s+to\s+work"
        r"|work\s+authoriz)\w*", re.I)),
    (QuestionCategory.SALARY, re.compile(
        r"\b(salary|compensation|\bctc\b|remuneration|expected\s+pay"
        r"|desired\s+(pay|salary)|pay\s+expectation|hourly\s+rate"
        r"|expected\s+rate|current\s+package|expected\s+package)\b", re.I)),
    (QuestionCategory.NOTICE_PERIOD, re.compile(
        r"\b(notice\s+period|how\s+soon\s+can\s+you\s+(join|start)"
        r"|when\s+can\s+you\s+(join|start)|earliest\s+(start|joining)"
        r"|date\s+of\s+joining|availability\s+to\s+start"
        r"|available\s+to\s+start|joining\s+date)\b", re.I)),
    (QuestionCategory.YEARS_EXPERIENCE, re.compile(
        r"\bhow\s+many\s+years\b|\byears?\s+of\s+(professional\s+|work\s+|hands[\s-]?on\s+)?"
        r"experience\b|\byears'?\s+experience\b|\byears\b[^?.]{0,25}\bexperience\b", re.I)),
    # Commuting belongs here, not with LOCATION. "Are you comfortable commuting to
    # this job's location?" is a willingness yes/no, whereas LOCATION questions ask
    # *where* you are. Misfiling it let the classifier answer a yes/no question with
    # a city name; pick_option happened to reject that, but by luck rather than design.
    (QuestionCategory.RELOCATION, re.compile(
        r"\brelocat\w*|\bwilling\s+to\s+move\b|\bcommut\w*"
        r"|\bwilling\s+to\s+travel\b|\bopen\s+to\s+(relocation|moving)\b", re.I)),
    (QuestionCategory.EDUCATION, re.compile(
        r"\b(highest\s+(level\s+of\s+)?education|degree|bachelor|master|doctorate"
        r"|phd|graduat\w*|university|college)\b", re.I)),
    # A "follow this company" checkbox is not part of the application, so it
    # gets its own category and a deterministic "no" rather than blocking submit.
    #
    # NOTE: the gap below must allow ".", because company names contain full
    # stops -- "Follow InfoSpeed Services, Inc. to stay up to date...". An
    # earlier [^?.] class could not cross "Inc." and so never matched.
    (QuestionCategory.FOLLOW_COMPANY, re.compile(
        r"\bfollow\b.{0,80}?\b(to\s+stay\s+up\s+to\s+date|company|page)\b"
        r"|\bstay\s+up\s+to\s+date\s+with\b", re.I | re.S)),
    (QuestionCategory.CONTACT, re.compile(
        r"\b(phone|mobile\s+number|contact\s+number|email|e-mail"
        r"|first\s+name|last\s+name|full\s+name|your\s+name"
        r"|street\s+address|postal\s+code|\bzip\b)\b", re.I)),
    (QuestionCategory.LOCATION, re.compile(
        r"\b(current\s+(city|location|residence)"
        r"|currently\s+(residing|living|based|located)"
        r"|residing\s+in|do\s+you\s+(currently\s+)?(live|reside)"
        r"|where\s+are\s+you\s+(based|located)"
        r"|\bcity\b|based\s+in)\b", re.I)),
]


def classify(question_text: str) -> tuple[QuestionCategory, str]:
    """Pure-regex classification. Returns (category, skill_token).

    skill_token is populated only for YEARS_EXPERIENCE, and is "" when the question
    names no skill this fact table knows -- which the resolver treats as a refusal.
    """
    text = " ".join(question_text.split())
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            if category is QuestionCategory.YEARS_EXPERIENCE:
                return category, _extract_skill(text)
            return category, ""
    return QuestionCategory.UNKNOWN, ""


def _extract_skill(text: str) -> str:
    low = text.lower()
    for alias, key in _ALIAS_ORDER:
        # \b around a dotted alias like "next.js" needs escaping
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", low):
            return key
    return ""


# --------------------------------------------------------------------------- #
# Fact table
# --------------------------------------------------------------------------- #
@dataclass
class FactTable:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    phone_country_code: str = "India (+91)"
    city: str = ""
    linkedin_url: str = ""
    total_years_experience: float = 0.0
    work_auth_india: bool = True
    requires_sponsorship_india: bool = False
    notice_period_days: int = 15
    immediate_joiner: bool = True
    current_ctc_lpa: float = 0.0
    expected_ctc_lpa: float = 0.0
    willing_to_relocate: bool = True
    relocate_scope: str = "anywhere_in_india"
    highest_degree: str = "Bachelor's Degree"
    skill_years: dict[str, float] = field(default_factory=dict)
    #: Guards against acting on an unreviewed derived table.
    skill_years_signed_off: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FactTable:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------- #
# Option matching for select-type questions
# --------------------------------------------------------------------------- #
_YES = ("yes", "y", "true", "i am", "i do", "authorized", "authorised")
_NO = ("no", "n", "false", "i am not", "i do not", "not required")
_DECLINE = (
    "prefer not to", "decline", "i don't wish", "i do not wish",
    "choose not to", "not specified", "prefer not to say", "prefer not to answer",
)


#: An option carrying any of these cannot be the affirmative choice. Without this,
#: "yes" would happily match "I am NOT authorized to work".
_NEGATED = re.compile(r"\b(not|never|no|none|n't|cannot|don't|doesn't|do not)\b", re.I)


def _word_in(needle: str, haystack: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


def pick_option(desired: str, options: list[str]) -> str | None:
    """Map a canonical answer onto one of the modal's literal option strings.

    Returns None when no option is an unambiguous fit, which routes the question to
    a human. Being unable to choose is a safe outcome; choosing wrongly is not.
    """
    if not options:
        return desired
    low = [o.strip().lower() for o in options]
    d = desired.strip().lower()

    def find(cands: tuple[str, ...], want_negated: bool | None) -> str | None:
        # Exact equality first -- unambiguous by construction.
        for cand in cands:
            for i, o in enumerate(low):
                if o == cand:
                    return options[i]
        # Then whole-word containment, but only for aliases long enough to be
        # meaningful. A one-character alias like "y" substring-matches "Maybe",
        # which is exactly the class of error this guard exists to stop.
        for cand in (c for c in cands if len(c) >= 3):
            for i, o in enumerate(low):
                if want_negated is not None:
                    if bool(_NEGATED.search(o)) != want_negated:
                        continue
                if _word_in(cand, o):
                    return options[i]
        return None

    if d in ("yes", "true"):
        return find(_YES, want_negated=False)
    if d in ("no", "false"):
        return find(_NO, want_negated=None) or find(_YES, want_negated=True)
    if d == "__decline__":
        return find(_DECLINE, want_negated=None)

    for i, o in enumerate(low):
        if o == d:
            return options[i]
    if len(d) >= 3:
        for i, o in enumerate(low):
            if _word_in(d, o):
                return options[i]
    return None


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def resolve(
    question: ScreeningQuestion,
    facts: FactTable,
    *,
    category_override: QuestionCategory | None = None,
    skill_override: str = "",
) -> ProposedAnswer | None:
    """Answer deterministically, or return None to send the question to a human.

    None is a first-class outcome, not a failure. It is returned whenever certainty
    is unavailable: an unknown category, an unknown skill, a foreign jurisdiction,
    an unreviewed fact table, or an option list the canonical answer does not fit.

    `category_override` lets a caller supply a category the regex did not recognise
    (in practice, from the enum-constrained classifier). The resulting answer is
    still deterministic: the *label* came from elsewhere, but the *value* is looked
    up here from `user_facts` by this code. A label can route a question; it can
    never become its answer.
    """
    text = question.text
    if category_override is not None:
        category, skill = category_override, (skill_override or _extract_skill(text))
    else:
        category, skill = classify(text)

    if category in (QuestionCategory.UNKNOWN, QuestionCategory.FREE_TEXT):
        return None

    # Salary is never answered without a human, by policy: a wrong number cannot be
    # corrected once the application is submitted.
    if category is QuestionCategory.SALARY:
        return None

    def answer(value: str, evidence: str, *alternates: str) -> ProposedAnswer | None:
        """Build the answer, trying `alternates` when a choice list is offered.

        A free-text control takes `value` verbatim. A select/radio needs one of ITS
        strings, and the natural phrasing often will not match: "15 days (can join
        immediately)" fits no option in ['Immediate', '15 days', '30 days']. So
        callers pass progressively barer alternates, tried in order, and we give up
        (returning None -> human) only if none of them map cleanly.
        """
        needs_choice = (
            question.kind in (AnswerKind.SINGLE_SELECT, AnswerKind.MULTI_SELECT)
            or bool(question.options)
        )
        if needs_choice:
            matched = None
            for candidate in (value, *alternates):
                matched = pick_option(candidate, question.options)
                if matched is not None:
                    break
            if matched is None:
                return None  # cannot map cleanly -> human
            value = matched
        return ProposedAnswer(
            question_text=text,
            category=category,
            value=value,
            provenance=Provenance.DETERMINISTIC,
            evidence=evidence,
        )

    # -- EEO: always decline. Legal everywhere, and never a disadvantage.
    if category is QuestionCategory.EEO:
        return answer("__decline__", "policy: EEO questions are always declined")

    # -- Jurisdiction guard for the two auth categories.
    if category in (QuestionCategory.WORK_AUTH, QuestionCategory.SPONSORSHIP):
        if mentions_foreign_country(text) and not _INDIA.search(text):
            return None  # fact table covers India only
        if _INDIA.search(text) and mentions_foreign_country(text):
            return None  # compound question, e.g. "India or the US?"

    if category is QuestionCategory.WORK_AUTH:
        return answer(
            "yes" if facts.work_auth_india else "no",
            f"user_facts.work_auth_india={facts.work_auth_india}",
        )

    if category is QuestionCategory.SPONSORSHIP:
        return answer(
            "yes" if facts.requires_sponsorship_india else "no",
            f"user_facts.requires_sponsorship_india={facts.requires_sponsorship_india}",
        )

    if category is QuestionCategory.NOTICE_PERIOD:
        days = facts.notice_period_days
        evidence = f"user_facts.notice_period_days={days}"
        if question.kind is AnswerKind.NUMERIC:
            return answer(str(days), evidence)
        alternates = [f"{days} days", str(days)]
        if facts.immediate_joiner:
            alternates += ["Immediate", "Immediately", "Immediate joiner"]
        return answer(
            f"{days} days" + (" (can join immediately)" if facts.immediate_joiner else ""),
            evidence, *alternates,
        )

    if category is QuestionCategory.RELOCATION:
        return answer(
            "yes" if facts.willing_to_relocate else "no",
            f"user_facts.willing_to_relocate={facts.willing_to_relocate}"
            f" scope={facts.relocate_scope}",
        )

    if category is QuestionCategory.YEARS_EXPERIENCE:
        if not facts.skill_years_signed_off:
            return None  # derived table not yet reviewed by a human
        if not skill:
            # No skill named -> a total-experience question, but only when the
            # wording actually looks general. Otherwise decline.
            if re.search(r"\b(total|overall|professional|work|industry)\b", text, re.I):
                total = facts.total_years_experience
                ev = f"user_facts.total_years_experience={total}"
                # Whole number, for the same reason as the per-skill branch.
                return answer(str(int(total)), ev, f"{int(total)} years",
                              _fmt_years(total))
            return None
        years = facts.skill_years.get(skill)
        if years is None:
            return None  # never estimate from a neighbouring skill
        evidence = f"user_facts.skill_years[{skill}]={years}"
        # A numeric input is validated by LinkedIn and rejects "4.2" outright,
        # which stalls the form with every field apparently filled. Floor to an
        # integer -- flooring rather than rounding so the figure is never inflated.
        # ALWAYS a whole number. LinkedIn renders this as <input type="text"> but
        # validates it numerically, and told us so itself: "Enter a whole number
        # between 0 and 99". Keying off AnswerKind.NUMERIC was not enough because
        # the kind is TEXT. Floor, not round, so the figure never exceeds what the
        # employment dates support.
        return answer(str(int(years)), evidence, f"{int(years)} years", _fmt_years(years))

    if category is QuestionCategory.CONTACT:
        low = text.lower()
        # Check country code BEFORE the generic phone branch: the label is
        # "Phone country code", which contains "phone" and would otherwise be
        # answered with the raw number.
        if "country code" in low:
            return answer(facts.phone_country_code,
                          f"user_facts.phone_country_code={facts.phone_country_code}",
                          "+91", "India")
        if "email" in low or "e-mail" in low:
            return answer(facts.email, "user_facts.email")
        if any(k in low for k in ("phone", "mobile", "contact number")):
            return answer(facts.phone, "user_facts.phone")
        if "first name" in low:
            return answer(facts.full_name.split()[0] if facts.full_name else "",
                          "user_facts.full_name[0]")
        if "last name" in low:
            return answer(facts.full_name.split()[-1] if facts.full_name else "",
                          "user_facts.full_name[-1]")
        if "name" in low:
            return answer(facts.full_name, "user_facts.full_name")
        return None  # address / postal code are not in the fact table

    if category is QuestionCategory.LOCATION:
        # A yes/no phrasing ("Are you currently residing in Bengaluru?") is not
        # asking WHERE you live, so answering with the city would be nonsense --
        # and pick_option would reject it anyway. Compare instead.
        boolean_shaped = bool(question.options) and all(
            o.strip().lower() in ("yes", "no", "select an option")
            for o in question.options
        )
        if boolean_shaped:
            own = facts.city.lower()
            named = [tok for tok in re.findall(r"[A-Z][a-z]{3,}", text)
                     if tok.lower() not in ("Are", "You", "Currently", "Residing")]
            if not named:
                return None
            match = any(tok.lower() in own for tok in named)
            return answer("yes" if match else "no",
                          f"user_facts.city={facts.city!r} vs {named!r}")
        return answer(facts.city, "user_facts.city")

    if category is QuestionCategory.FOLLOW_COMPANY:
        # Declining is the neutral default: following a company is a social action
        # on the user's behalf, not part of the application.
        return answer("no", "policy: do not auto-follow companies")

    if category is QuestionCategory.EDUCATION:
        return answer(facts.highest_degree, "user_facts.highest_degree")

    return None


def _fmt_years(v: float) -> str:
    """Integer when integral. LinkedIn's numeric inputs reject '4.0'."""
    return str(int(v)) if float(v).is_integer() else str(v)
