"""Resolve screening questions into answers carrying provenance.

The resolution order is fixed and each step is strictly more permissive than the
one before, so the safest available source always wins:

  1. deterministic   -- regex classification + `user_facts` lookup. Preferred.
  2. bank_match      -- vector retrieval, but ONLY for categories where a
                        near-synonym cannot invert the meaning.
  3. llm             -- labelled as such, and therefore never auto-submitted.

Step 2's restriction is the important one. Measured on the live embedding model,
"Are you legally authorized to work in India?" and "Do you require visa
sponsorship to work in India?" sit at 0.9097 cosine similarity with *opposite*
correct answers, while a genuine paraphrase pair sits at 0.9979. Only 0.08
separates them, so no threshold can tell them apart -- retrieval must simply be
forbidden for that class of question rather than tuned.
"""
from __future__ import annotations

import logging

from agent import facts as facts_mod
from agent import llm
from agent.models import (
    BANK_AND_LLM_FORBIDDEN,
    HUMAN_ONLY,
    ProposedAnswer,
    Provenance,
    QuestionCategory,
    ScreeningQuestion,
)
from agent.store import Store

log = logging.getLogger("agent.answering")


class AnswerEngine:
    def __init__(self, store: Store, fact_table: facts_mod.FactTable, threshold: float):
        self.store = store
        self.facts = fact_table
        self.threshold = threshold

    def answer(self, question: ScreeningQuestion) -> ProposedAnswer:
        """Always returns an answer object. Provenance says how much to trust it."""
        # ---- 1. deterministic -------------------------------------------- #
        det = facts_mod.resolve(question, self.facts)
        if det is not None:
            return det

        category, _skill = facts_mod.classify(question.text)

        # The regex did not recognise it; ask the model to *label* it only. The
        # label can route the question but can never become its answer.
        if category is QuestionCategory.UNKNOWN:
            classified = llm.classify_question(question.text, question.options)
            if classified is not None and classified.category is not QuestionCategory.UNKNOWN:
                category = classified.category
                # Retry with the label supplied explicitly. Passing the category in
                # is the whole point -- re-running resolve() without it would just
                # re-run the same regex and land on UNKNOWN again.
                retry = facts_mod.resolve(
                    question, self.facts,
                    category_override=category,
                    skill_override=classified.skill_token,
                )
                if retry is not None:
                    # DOWNGRADE. The value came from user_facts, but the *route* to
                    # it came from a model, and a wrong route yields a wrong value
                    # out of the right table. Observed live: "Do you have a valid
                    # driving licence for heavy vehicles?" was labelled `education`
                    # and answered "Bachelor's Degree".
                    #
                    # Keeping the value is still useful -- the Slack card shows it as
                    # a one-click suggestion -- but LLM provenance means it can never
                    # submit without a human.
                    log.info("regex missed %r; classifier routed it to %s "
                             "(downgraded to human review)",
                             question.text[:60], category.value)
                    return retry.model_copy(update={
                        "provenance": Provenance.LLM,
                        "reason": (f"category '{category.value}' was inferred by the "
                                   f"classifier, not matched by rule -- confirm the "
                                   f"suggested answer before it is sent"),
                    })

        if category in HUMAN_ONLY:
            return self._needs_human(question, category, "policy: always human-reviewed")

        # ---- 2. question bank -------------------------------------------- #
        if category not in BANK_AND_LLM_FORBIDDEN:
            hit = self._bank_lookup(question, category)
            if hit is not None:
                return hit
        else:
            log.info("bank lookup skipped for %s (retrieval-forbidden category)",
                     category.value)

        # ---- 3. give up: a human answers -------------------------------- #
        if category in BANK_AND_LLM_FORBIDDEN:
            return self._needs_human(
                question, category,
                f"{category.value}: deterministic facts insufficient, retrieval forbidden",
            )
        return self._needs_human(question, category, "no deterministic fact, no bank match")

    # ------------------------------------------------------------------ #
    def _bank_lookup(
        self, question: ScreeningQuestion, category: QuestionCategory
    ) -> ProposedAnswer | None:
        try:
            vec = llm.embed([question.text])[0]
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding failed (%s); no bank lookup", exc)
            return None
        hits = self.store.bank_nearest(vec, similarity_threshold=self.threshold, limit=3)
        if not hits:
            return None
        best = hits[0]
        # Guard against a bank entry that was written under a different category.
        if best.get("category") and best["category"] != category.value:
            log.info("bank hit %s rejected: category %s != question category %s",
                     best["_id"], best["category"], category.value)
            return None
        value = best["answer_text"]
        if question.options:
            matched = facts_mod.pick_option(value, question.options)
            if matched is None:
                return None
            value = matched
        self.store.bank_mark_used(best["_id"])
        return ProposedAnswer(
            question_text=question.text,
            category=category,
            value=value,
            provenance=Provenance.BANK_MATCH,
            evidence=f"question_bank/{best['_id']} sim={best['similarity']:.4f} "
                     f"src={best['question_text'][:80]!r}",
            similarity=best["similarity"],
        )

    @staticmethod
    def _needs_human(
        question: ScreeningQuestion, category: QuestionCategory, why: str
    ) -> ProposedAnswer:
        """An unanswered question, marked so the two-key gate must block it.

        Both `provenance=LLM` and the empty `evidence` independently fail
        `auto_submittable()`, so this cannot slip through even if one check is
        later loosened.
        """
        return ProposedAnswer(
            question_text=question.text,
            category=category,
            value="",
            provenance=Provenance.LLM,
            evidence="",
            reason=why,
        )


def learn_salary_fact(store: Store, question_text: str, answer_text: str) -> str | None:
    """Store a salary the user typed as a FACT, so it is never asked again.

    Salary cannot go in the vector bank -- "current CTC" and "expected CTC" are
    near-identical strings with different correct answers, exactly the failure mode
    that keeps retrieval out of this category. But it is a perfectly good
    deterministic fact once stated, so it goes to `user_facts` instead.

    Returns the field written, or None.
    """
    import re as _re

    low = question_text.lower()
    wants_expected = bool(_re.search(
        r"\b(expect|desired|asking|require|looking\s+for|preferred)\w*", low))
    wants_current = bool(_re.search(
        r"\b(current|present|existing|drawing|latest)\w*", low))
    if wants_expected == wants_current:
        return None

    nums = _re.findall(r"\d+(?:\.\d+)?", answer_text.replace(",", ""))
    if not nums:
        return None
    val = float(nums[0])
    # Accept either "20" (LPA) or "2000000" (absolute rupees).
    if val > 1000:
        val = round(val / 100000, 2)
    if not (0.5 <= val <= 200):
        log.warning("salary %r out of plausible range; not stored", answer_text[:40])
        return None

    field = "expected_ctc_lpa" if wants_expected else "current_ctc_lpa"
    store.db.collection("user_facts").document("me").set(
        {field: val, "salary_confirmed": True}, merge=True)
    log.info("learned %s = %s LPA from your Slack answer", field, val)
    return field


def learn_from_human(
    store: Store, question_text: str, answer_text: str
) -> str | None:
    """Bank a human-supplied answer so the same question is silent next time.

    Refuses to bank retrieval-forbidden categories -- see the module docstring.
    Returns the bank doc id, or None if the answer was not bankable.
    """
    generalized = llm.generalize_qa(question_text, answer_text)
    if generalized is None or not generalized.reusable:
        log.info("not banking: generalizer declined or marked non-reusable")
        return None
    try:
        vec = llm.embed([generalized.normalized_question])[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("embedding failed (%s); not banking", exc)
        return None
    doc_id = store.bank_upsert(
        question_text=generalized.normalized_question,
        answer_text=generalized.answer_text,
        category=generalized.category.value,
        embedding=vec,
        origin="human_slack",
    )
    log.info("banked %s as %s", doc_id, generalized.category.value)
    return doc_id
