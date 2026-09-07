"""Vertex AI access: embeddings and the three narrow LLM calls.

Design rules enforced here, not merely documented:

* Every generative call is schema-constrained and validated. A response that fails
  validation is retried once and then gives up -- returning None so the caller
  routes the question to a human. The model never gets to "sort of" answer.
* Nothing here emits a number that reaches an application form. Scoring produces a
  0-100 relevance score used only to decide whether to *look* at a job; question
  classification returns an enum plus a skill *token*, and the number attached to
  that token is looked up from `user_facts` by code in facts.py.
* Every scoring result must cite verbatim JD spans. No spans means no score, which
  means the job is skipped rather than guessed at.
"""
from __future__ import annotations

import logging
import math
from typing import TypeVar

from google.genai import types
from pydantic import BaseModel, ValidationError

from agent import config
from agent.models import (
    ClassifiedQuestion,
    GeneralizedQA,
    JobPosting,
    QuestionCategory,
    ResumeGapReport,
    ScoredJob,
)

log = logging.getLogger("agent.llm")
T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------- #
# Embeddings
# --------------------------------------------------------------------------- #
def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def embed(texts: list[str]) -> list[list[float]]:
    """Embed at 1536 dims, unit-normalised.

    Two non-obvious requirements, both verified against the live API:
      * Firestore's vector index caps at 2048 dimensions while
        gemini-embedding-001 emits 3072 natively, so an MRL truncation to 1536 is
        mandatory -- 3072 simply cannot be indexed.
      * MRL-truncated vectors are NOT unit length (measured L2 = 0.6935), so they
        must be re-normalised or every cosine distance is wrong.
    """
    if not texts:
        return []
    client = config.genai_client()
    resp = client.models.embed_content(
        model=config.MODEL_EMBED,
        contents=texts,
        config={
            "output_dimensionality": config.EMBED_DIM,
            "task_type": "SEMANTIC_SIMILARITY",
        },
    )
    return [_unit(list(e.values)) for e in resp.embeddings]


# --------------------------------------------------------------------------- #
# Structured generation harness
# --------------------------------------------------------------------------- #
def _structured(
    model: str, prompt: str, schema: type[T], *, max_tokens: int = 4096
) -> T | None:
    """One schema-constrained call, validated, single retry, then None.

    None is the honest answer when the model cannot produce a valid object. The
    caller must treat it as "ask a human", never as a default value.
    """
    client = config.genai_client()
    cfg = types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=max_tokens,
        response_mime_type="application/json",
        response_schema=schema,
    )
    for attempt in (1, 2):
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=cfg)
            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, schema):
                return parsed
            if resp.text:
                return schema.model_validate_json(resp.text)
            log.warning("%s returned no parseable content (attempt %d)", model, attempt)
        except (ValidationError, ValueError) as exc:
            log.warning("%s schema validation failed (attempt %d): %s", model, attempt, str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("%s call failed (attempt %d): %s", model, attempt, str(exc)[:200])
    return None


# --------------------------------------------------------------------------- #
# 1. Job scoring
# --------------------------------------------------------------------------- #
_SCORE_PROMPT = """You are screening one job posting against a candidate's resume.

Score 0-100 how well the CANDIDATE matches the JOB. Be strict and calibrated:
  85-100  strong match: core requirements are clearly met
  70-84   good match: most requirements met, gaps are learnable
  50-69   partial: relevant background, several hard requirements missing
  0-49    weak: different discipline, or seniority far off

Rules you must follow:
- `evidence_spans` MUST quote text VERBATIM from the JOB POSTING. Do not paraphrase
  and do not invent. If you cannot quote supporting text, return an empty list.
- `missing_must_haves` lists requirements the posting states that the resume does
  not evidence.
- Do not comment on salary, visa status, or years-of-experience arithmetic.

=== CANDIDATE RESUME ===
{resume}

=== JOB POSTING ===
Title: {title}
Company: {company}
Location: {location}

{jd}
"""


def score_job(job: JobPosting, resume_text: str) -> ScoredJob | None:
    """Relevance score. Returns None (=> skip the job) if unusable.

    A score with no verbatim evidence is discarded: the whole point of requiring
    quotes is that a model which cannot ground its score has not really read the
    posting.
    """
    result = _structured(
        config.MODEL_FAST,
        _SCORE_PROMPT.format(
            resume=resume_text[:12000],
            title=job.title, company=job.company, location=job.location,
            jd=job.jd_text[:14000],
        ),
        ScoredJob,
    )
    if result is None:
        return None
    if not result.evidence_spans:
        log.info("job %s scored %d with no evidence -> discarding score",
                 job.job_id, result.score)
        return None
    # Verify the quotes actually appear in the posting. A hallucinated quote
    # invalidates the score exactly as an absent one does.
    haystack = " ".join((job.jd_text + " " + job.title).lower().split())
    grounded = [
        s for s in result.evidence_spans
        if " ".join(s.lower().split())[:80] in haystack
    ]
    if not grounded:
        log.warning("job %s: none of %d evidence spans appear in the JD -> discarding",
                    job.job_id, len(result.evidence_spans))
        return None
    result.evidence_spans = grounded
    return result


# --------------------------------------------------------------------------- #
# 2. Question classification (enum-constrained)
# --------------------------------------------------------------------------- #
_CLASSIFY_PROMPT = """Classify this job-application screening question.

Return the single best `category`, and for a years-of-experience question also
return `skill_token`: the bare skill name in lowercase (e.g. "python",
"kubernetes", "aws").

CRITICAL: never state a number of years, a salary, or an answer of any kind. You
are labelling the question, not answering it.

Question: {question}
Offered options: {options}
"""


def classify_question(question_text: str, options: list[str]) -> ClassifiedQuestion | None:
    """Fallback classifier for questions the regex in facts.py did not recognise.

    Its output can only ever *route* a question. It cannot supply a value: a
    category never becomes an answer without a deterministic lookup or a human.
    """
    return _structured(
        config.MODEL_FAST,
        _CLASSIFY_PROMPT.format(question=question_text[:1000], options=options[:20]),
        ClassifiedQuestion,
        max_tokens=1024,
    )


# --------------------------------------------------------------------------- #
# 3. Q&A generalisation for the bank
# --------------------------------------------------------------------------- #
_GENERALIZE_PROMPT = """A candidate answered a job-application screening question.
Rewrite it as a reusable entry for an answer bank.

- `normalized_question`: the question with employer-specific wording removed, so a
  similar question at another company matches it.
- `answer_text`: the answer, VERBATIM. Do not embellish, expand, or "improve" it.
- `reusable`: false if the answer is specific to this one company or posting and
  would be wrong elsewhere.

Question: {question}
Answer the candidate gave: {answer}
"""


def generalize_qa(question_text: str, answer_text: str) -> GeneralizedQA | None:
    result = _structured(
        config.MODEL_DEEP,
        _GENERALIZE_PROMPT.format(question=question_text[:1000], answer=answer_text[:2000]),
        GeneralizedQA,
        max_tokens=2048,
    )
    if result is None:
        return None
    # Never bank a category whose answer must not come from retrieval. The measured
    # 0.9097 similarity between opposite-meaning auth questions is why.
    from agent.models import BANK_AND_LLM_FORBIDDEN

    if result.category in BANK_AND_LLM_FORBIDDEN:
        log.info("not banking a %s answer: category is retrieval-forbidden",
                 result.category.value)
        return None
    if result.answer_text.strip() != answer_text.strip():
        log.info("generalizer altered the answer text; keeping the human's original")
        result.answer_text = answer_text
    return result


# --------------------------------------------------------------------------- #
# 4. Resume gap analysis (Workflow C, once a day)
# --------------------------------------------------------------------------- #
_GAP_PROMPT = """Below are {n} job postings the candidate was matched against in the
last 24 hours, followed by their resume.

Identify the skills and experiences most frequently requested across these
postings that the resume does NOT evidence. Rank by how often they appear.
Return at most 10 in `missing_skills`, and a short factual `summary` (3-4
sentences). Do not give career advice and do not speculate about salary.

=== POSTINGS ===
{postings}

=== RESUME ===
{resume}
"""


def resume_gap_report(postings: list[dict], resume_text: str) -> ResumeGapReport | None:
    if not postings:
        return ResumeGapReport(missing_skills=[], summary="No postings in the last 24 hours.",
                               jobs_analyzed=0)
    blob = "\n\n---\n\n".join(
        f"{p.get('title','')} @ {p.get('company','')}\n{(p.get('jd_text') or '')[:2500]}"
        for p in postings[:25]
    )
    result = _structured(
        config.MODEL_DEEP,
        _GAP_PROMPT.format(n=len(postings), postings=blob[:120000], resume=resume_text[:12000]),
        ResumeGapReport,
        max_tokens=4096,
    )
    if result:
        result.jobs_analyzed = len(postings)
    return result
