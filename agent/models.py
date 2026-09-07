"""Typed contracts for the whole agent.

Every value that crosses a module boundary is one of these. The LLM never sees or
emits a raw dict -- it is always constrained to one of the response schemas at the
bottom of this file, validated on return.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------- #
# Provenance -- the spine of the two-key submit rule
# --------------------------------------------------------------------------- #
class Provenance(str, Enum):
    """Where an answer's value came from. This gates auto-submit.

    DETERMINISTIC -- computed by code from `user_facts`. No model involved.
    BANK_MATCH    -- retrieved from `question_bank` above the similarity threshold.
    HUMAN         -- typed by the user in Slack.
    LLM           -- authored by a model. NEVER auto-submits.
    """

    DETERMINISTIC = "deterministic"
    BANK_MATCH = "bank_match"
    HUMAN = "human"
    LLM = "llm"


#: Provenances that may be submitted without a human looking first.
AUTO_SUBMITTABLE: frozenset[Provenance] = frozenset(
    {Provenance.DETERMINISTIC, Provenance.BANK_MATCH, Provenance.HUMAN}
)


class QuestionCategory(str, Enum):
    WORK_AUTH = "work_auth"
    SPONSORSHIP = "sponsorship"
    SALARY = "salary"
    NOTICE_PERIOD = "notice_period"
    YEARS_EXPERIENCE = "years_experience"
    EEO = "eeo"
    RELOCATION = "relocation"
    CONTACT = "contact"
    EDUCATION = "education"
    LOCATION = "location"
    FOLLOW_COMPANY = "follow_company"
    YES_NO_GENERIC = "yes_no_generic"
    FREE_TEXT = "free_text"
    UNKNOWN = "unknown"


#: Categories the vector bank and the LLM may NEVER answer.
#:
#: Note carefully: this forbids *bank_match* and *llm*, not automation as such.
#: Deterministic code reading an explicit fact from `user_facts` is still allowed
#: and is the intended path -- work-authorisation questions appear on nearly every
#: Easy Apply, so routing them all to a human would defeat the agent entirely.
#: The measured danger is semantic retrieval, not automation:
#: "are you authorized to work" vs "do you require sponsorship" score 0.9097
#: cosine similarity while having *opposite* correct answers, and only 0.08
#: separates that from a genuine match -- so no threshold can separate them.
BANK_AND_LLM_FORBIDDEN: frozenset[QuestionCategory] = frozenset(
    {
        QuestionCategory.WORK_AUTH,
        QuestionCategory.SPONSORSHIP,
        QuestionCategory.SALARY,
        QuestionCategory.NOTICE_PERIOD,
        QuestionCategory.YEARS_EXPERIENCE,
        QuestionCategory.EEO,
        QuestionCategory.RELOCATION,
    }
)

#: Categories that must always reach a human, even deterministically.
#:
#: Salary used to be here. It no longer is: a salary you have explicitly stated is
#: a fact like any other, and re-asking it on every application was the single
#: most repetitive interruption in the system. It remains in
#: BANK_AND_LLM_FORBIDDEN -- retrieval must never supply it, because "current CTC"
#: and "expected CTC" are near-identical strings with different correct answers --
#: and `facts.resolve` answers it only when `user_facts.salary_confirmed` is True,
#: which happens solely because the user typed the numbers in Slack.
HUMAN_ONLY: frozenset[QuestionCategory] = frozenset()


class AnswerKind(str, Enum):
    TEXT = "text"
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    FILE = "file"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class JobPosting(BaseModel):
    job_id: str
    title: str = ""
    company: str = ""
    location: str = ""
    url: str = ""
    jd_text: str = ""
    easy_apply: bool = False
    posted_text: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("job_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not str(v).strip():
            raise ValueError("job_id must be non-empty -- it is the idempotency key")
        return str(v).strip()


class ScreeningQuestion(BaseModel):
    """One question read out of a live Easy Apply modal."""

    text: str
    kind: AnswerKind = AnswerKind.UNKNOWN
    options: list[str] = Field(default_factory=list)
    required: bool = True
    #: Stable handle back to the DOM element, so the filler does not re-query by text.
    locator_hint: str = ""
    prefilled: str = ""


class ProposedAnswer(BaseModel):
    question_text: str
    category: QuestionCategory
    value: str
    provenance: Provenance
    #: Why this value. For DETERMINISTIC, the fact key used. For BANK_MATCH, the
    #: question_bank doc id and similarity. For LLM, the JD span quoted.
    #: An answer with no evidence is treated as no answer.
    evidence: str = ""
    similarity: float | None = None
    #: Why a human is needed, when one is. Shown on the Slack card. Purely
    #: explanatory -- it never affects the gate.
    reason: str = ""
    #: Equivalent spellings of the same value, most-preferred first. Used when a
    #: form rejects the primary form: live employers demand contradictory formats
    #: for years of experience ("whole number" vs "decimal number"), so the engine
    #: re-fills from here rather than guessing one and losing the application.
    value_alternates: list[str] = Field(default_factory=list)

    def auto_submittable(self) -> bool:
        if self.category in HUMAN_ONLY and self.provenance is not Provenance.HUMAN:
            return False
        if not self.evidence.strip():
            return False
        return self.provenance in AUTO_SUBMITTABLE


class ApplyOutcome(str, Enum):
    SUBMITTED = "submitted"
    ABANDONED_NEEDS_REVIEW = "abandoned_needs_review"
    FAILED = "failed"
    SKIPPED_CAP = "skipped_cap"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    SKIPPED_LOW_SCORE = "skipped_low_score"
    AUTH_FAILURE = "auth_failure"


class ApplyResult(BaseModel):
    job_id: str
    outcome: ApplyOutcome
    answers: list[ProposedAnswer] = Field(default_factory=list)
    match_score: int | None = None
    error: str = ""
    run_id: str = ""
    modal_opened: bool = False
    submitted_at: datetime | None = None
    #: The match threshold in force when this job was skipped for scoring too low.
    #: Persisted so lowering the threshold later makes the job eligible again.
    threshold_at_skip: int | None = None
    #: One entry per modal step: what was seen and what was clicked. Persisted to
    #: `run_traces` so a broken selector can be diagnosed after the fact.
    steps: list[dict[str, Any]] = Field(default_factory=list)

    def blocking_answers(self) -> list[ProposedAnswer]:
        """Answers that prevented auto-submit -- what the Slack card must ask about."""
        return [a for a in self.answers if not a.auto_submittable()]


# --------------------------------------------------------------------------- #
# LLM response schemas -- the model is constrained to exactly these
# --------------------------------------------------------------------------- #
class ScoredJob(BaseModel):
    """Structured output for job scoring. No free-form text is accepted."""

    score: int = Field(ge=0, le=100)
    #: Verbatim spans from the JD supporting the score. Empty => treated as no score.
    evidence_spans: list[str] = Field(default_factory=list, max_length=5)
    missing_must_haves: list[str] = Field(default_factory=list, max_length=8)
    one_line_reason: str = ""

    @field_validator("evidence_spans", "missing_must_haves", mode="before")
    @classmethod
    def _coerce(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


class ClassifiedQuestion(BaseModel):
    """Enum-constrained classification. The model picks a label, never writes one."""

    category: QuestionCategory
    #: For YEARS_EXPERIENCE: the skill token the question is asking about, lowercased.
    #: Used only to *look up* a number in user_facts.skill_years -- the model is never
    #: permitted to state the number itself.
    skill_token: str = ""


class GeneralizedQA(BaseModel):
    """A human answer, rewritten into a reusable bank entry."""

    normalized_question: str
    answer_text: str
    category: QuestionCategory
    reusable: bool = True


class ResumeGapReport(BaseModel):
    missing_skills: list[str] = Field(default_factory=list, max_length=10)
    summary: str = ""
    jobs_analyzed: int = 0
