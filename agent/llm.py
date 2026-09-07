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
import re
import time
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

# Vertex AI list prices, USD per 1,000,000 tokens. Kept as visible constants so a
# price change is a one-line edit rather than a mystery.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    # model            (input, output)
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-embedding-001": (0.15, 0.00),
}

#: Accumulated in-process; flushed to Firestore by `flush_usage()` at run end.
_USAGE: list[dict] = []


def record_usage(model: str, prompt_tokens: int, output_tokens: int,
                 purpose: str) -> None:
    """Note what one call actually consumed."""
    inp, out = PRICE_PER_MTOK.get(model, (0.0, 0.0))
    cost = (prompt_tokens / 1e6) * inp + (output_tokens / 1e6) * out
    _USAGE.append({
        "model": model, "purpose": purpose,
        "prompt_tokens": int(prompt_tokens), "output_tokens": int(output_tokens),
        "usd": round(cost, 8),
    })


def pending_usage() -> list[dict]:
    return list(_USAGE)


def flush_usage(store, run_id: str) -> dict:
    """Write this run's token usage to Firestore and clear the buffer."""
    if not _USAGE:
        return {"calls": 0, "usd": 0.0}
    total = {
        "run_id": run_id,
        "calls": len(_USAGE),
        "prompt_tokens": sum(u["prompt_tokens"] for u in _USAGE),
        "output_tokens": sum(u["output_tokens"] for u in _USAGE),
        "usd": round(sum(u["usd"] for u in _USAGE), 6),
        "by_purpose": {},
    }
    for u in _USAGE:
        b = total["by_purpose"].setdefault(u["purpose"], {"calls": 0, "usd": 0.0})
        b["calls"] += 1
        b["usd"] = round(b["usd"] + u["usd"], 8)
    try:
        store.write_usage(run_id, total)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist usage: %s", exc)
    _USAGE.clear()
    log.info("run %s used %d calls, %d in / %d out tokens, $%.5f",
             run_id, total["calls"], total["prompt_tokens"],
             total["output_tokens"], total["usd"])
    return total


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
    # Embeddings are billed on input tokens; ~4 chars/token is the usual rule.
    record_usage(config.MODEL_EMBED,
                 sum(len(t) for t in texts) // 4, 0, purpose="embed")
    return [_unit(list(e.values)) for e in resp.embeddings]


# --------------------------------------------------------------------------- #
# Structured generation harness
# --------------------------------------------------------------------------- #
#: Counts why calls fail, so a silent 37% loss can never happen unnoticed again.
FAILURES: dict[str, int] = {}


def _note_failure(kind: str) -> None:
    FAILURES[kind] = FAILURES.get(kind, 0) + 1


def _structured(
    model: str,
    prompt: str,
    schema: type[T],
    *,
    max_tokens: int = 8192,
    think: bool = False,
    attempts: int = 3,
) -> T | None:
    """One schema-constrained call, validated, with retries, then None.

    `think=False` by default and it matters. Gemini 2.5 spends "thinking" tokens
    out of max_output_tokens; a 6,647-character job description burned 3,931 of
    them, hit MAX_TOKENS, truncated the JSON and failed validation. These are
    rubric tasks with a fixed output shape, so thinking bought nothing and cost
    ~1,900 tokens a call.

    None remains the honest answer when no valid object can be produced -- callers
    treat it as "ask a human", never as a default.
    """
    client = config.genai_client()

    # 2.5-pro refuses thinking_budget=0 outright ("The model does not support
    # setting thinking_budget to 0"), so a blanket "thinking off" broke every Pro
    # call. Only Flash can be told not to think.
    can_disable_thinking = "flash" in model.lower()

    def build(tok: int, thinking: bool) -> types.GenerateContentConfig:
        kwargs: dict = dict(
            temperature=0.0,
            max_output_tokens=tok,
            response_mime_type="application/json",
            response_schema=schema,
        )
        if not thinking and can_disable_thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kwargs)

    tokens = max_tokens
    thinking = think
    for attempt in range(1, attempts + 1):
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=build(tokens, thinking))
            um = getattr(resp, "usage_metadata", None)
            if um is not None:
                record_usage(model, getattr(um, "prompt_token_count", 0) or 0,
                             (getattr(um, "candidates_token_count", 0) or 0)
                             + (getattr(um, "thoughts_token_count", 0) or 0),
                             purpose=schema.__name__)

            # Truncation is a budget problem, not a model failure. Name it and
            # grow the budget rather than reporting a confusing validation error.
            finish = ""
            if getattr(resp, "candidates", None):
                finish = str(getattr(resp.candidates[0], "finish_reason", "") or "")
            if "MAX_TOKENS" in finish:
                _note_failure("max_tokens_truncated")
                log.warning("%s hit MAX_TOKENS (attempt %d, budget %d); retrying "
                            "with a larger budget and no thinking", model, attempt, tokens)
                tokens = min(32768, tokens * 2)
                thinking = False
                continue

            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, schema):
                return parsed
            if resp.text:
                return schema.model_validate_json(resp.text)
            _note_failure("no_content")
            log.warning("%s returned no parseable content (attempt %d)", model, attempt)

        except (ValidationError, ValueError) as exc:
            _note_failure("schema_invalid")
            log.warning("%s schema validation failed (attempt %d): %s",
                        model, attempt, str(exc)[:200])
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            # A rate limit needs waiting, not an immediate retry -- which is what
            # made 16 of these fatal.
            if "RESOURCE_EXHAUSTED" in text or "429" in text:
                _note_failure("rate_limited")
                wait = min(30.0, 2.0 ** attempt)
                log.warning("%s rate limited (attempt %d); backing off %.0fs",
                            model, attempt, wait)
                time.sleep(wait)
                continue
            _note_failure("call_failed")
            log.warning("%s call failed (attempt %d): %s", model, attempt, text[:200])
    log.error("%s gave up after %d attempts for %s", model, attempts, schema.__name__)
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
        max_tokens=8192,
    )
    if result is None:
        return None

    # Evidence is demanded of a claimed MATCH, not of a claimed rejection.
    #
    # A high score with nothing to quote is the hallucination case this check
    # exists for. A low score with nothing to quote is simply correct: there is
    # nothing in a Material Science posting to cite in favour of an AI engineer.
    # Treating both as failures dropped genuinely-bad matches into a silent hole
    # instead of recording them as "scored too low", which made the funnel look
    # broken when it was working.
    EVIDENCE_REQUIRED_ABOVE = 40
    if not result.evidence_spans:
        if result.score >= EVIDENCE_REQUIRED_ABOVE:
            log.info("job %s claims %d but cites nothing -> discarding as unsound",
                     job.job_id, result.score)
            return None
        log.info("job %s scored %d with nothing to cite -- a confident no, kept",
                 job.job_id, result.score)
        return result
    # Verify the quotes really came from the posting -- a hallucinated quote
    # invalidates a score exactly as an absent one does. But match on token
    # overlap rather than a verbatim 80-character prefix: the strict version threw
    # away 7 otherwise sound scores because the model tidied whitespace or
    # punctuation while quoting accurately.
    haystack = set(re.findall(r"[a-z0-9]+", (job.jd_text + " " + job.title).lower()))
    grounded = []
    for span in result.evidence_spans:
        words = re.findall(r"[a-z0-9]+", span.lower())
        if not words:
            continue
        overlap = sum(1 for w in words if w in haystack) / len(words)
        if overlap >= 0.7:
            grounded.append(span)
    if not grounded:
        if result.score >= EVIDENCE_REQUIRED_ABOVE:
            log.warning("job %s: claims %d but none of %d quotes are in the JD "
                        "-> discarding as unsound", job.job_id, result.score,
                        len(result.evidence_spans))
            return None
        log.info("job %s scored %d with unverifiable quotes -- low score, kept",
                 job.job_id, result.score)
        return result
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
        max_tokens=8192,    # Pro always thinks; budget for it
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
_GAP_PROMPT = """Below is a gap analysis that has ALREADY been computed by
string-matching a curated skill vocabulary against {n} job postings and against
the candidate's resume. The analysis is authoritative.

Your ONLY job is to write a short, factual summary of it, 3-4 sentences.

Hard constraints:
- Do NOT add any skill that is not in the list below.
- Do NOT remove or reorder them; the counts are the ranking.
- Do NOT claim a skill is missing if it appears under ALREADY ON THE RESUME.
- Copy `missing_skills` through EXACTLY as given, same order.
- No career advice, no salary speculation, no encouragement.

ROLES ANALYSED (these are the only postings that count):
{titles}

MISSING FROM THE RESUME (skill — how many of the {n} postings ask for it):
{gaps}

ALREADY ON THE RESUME (do not call these gaps):
{covered}
"""


def resume_gap_report(store, postings: list[dict], resume_text: str,
                      *, min_score: int = 40) -> ResumeGapReport | None:
    """Gaps from code; prose from the model.

    The model is given the computed gap list and forbidden from changing it. It
    previously invented Databricks and Azure as gaps when both are on the resume,
    and ranked Data Engineer skills top because the corpus included every posting
    the user had rejected.
    """
    from agent import market

    analysis = market.analyse(store, postings, resume_text, min_score=min_score)
    gaps = analysis["gaps"]

    if not analysis["analysed"]:
        return ResumeGapReport(
            missing_skills=[],
            summary=("No relevant postings in this window. "
                     f"{analysis['dropped']} were set aside as rejected, "
                     "title-excluded, low-scoring or without a description."),
            jobs_analyzed=0)
    if not gaps:
        return ResumeGapReport(
            missing_skills=[],
            summary=(f"Across {analysis['analysed']} relevant postings, every skill "
                     "in the vocabulary already appears on the resume."),
            jobs_analyzed=analysis["analysed"])

    top = gaps[:10]
    result = _structured(
        config.MODEL_DEEP,
        _GAP_PROMPT.format(
            n=analysis["analysed"],
            titles="\n".join(f"  - {t}" for t in analysis["titles"][:12]),
            gaps="\n".join(f"  {g['skill']} — {g['postings']}" for g in top),
            covered=", ".join(c["skill"] for c in analysis["covered"][:15]) or "(none)",
        ),
        ResumeGapReport,
        max_tokens=16384,   # Pro always thinks; leave room for it plus the JSON
    )

    computed = [g["skill"] for g in top]
    if result is None:
        # The facts stand on their own; the prose is a nicety.
        return ResumeGapReport(
            missing_skills=computed,
            summary=(f"Across {analysis['analysed']} relevant postings, the most "
                     f"frequently requested skills absent from the resume are: "
                     f"{', '.join(computed[:5])}."),
            jobs_analyzed=analysis["analysed"])

    # Overwrite whatever the model produced for the list. It is not allowed a say.
    if [x.strip() for x in result.missing_skills] != computed:
        log.warning("gap model altered the skill list; restoring the computed one")
    result.missing_skills = computed
    result.jobs_analyzed = analysis["analysed"]
    return result
