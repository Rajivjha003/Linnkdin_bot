"""Firestore access. The only place state lives.

Every invocation of the agent is stateless -- there is no in-process pause and no
resumable session. That is deliberate: an in-memory pause cannot survive a process
restart, so Firestore is the single source of truth and every write is idempotent.

Cap queries use aggregation (`.count()`) rather than fetching documents, because a
count is billed as a handful of reads regardless of how many rows it spans.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from agent import config
from agent.facts import FactTable
from agent.models import ApplyOutcome, ApplyResult, JobPosting, ProposedAnswer

log = logging.getLogger("agent.store")

C_PROFILE = "user_profile"
C_FACTS = "user_facts"
C_SEARCH = "search_config"
C_AGENT = "agent_config"
C_APPLIED = "applied_jobs"
C_BANK = "question_bank"
C_PENDING = "pending_review"
C_TRACES = "run_traces"
C_USAGE = "llm_usage"
DOC_ME = "me"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _cutoff_24h() -> dt.datetime:
    return _now() - dt.timedelta(hours=24)


def normalize_question(text: str) -> str:
    """Canonical form for bank keys: lowercase, punctuation-free, single-spaced."""
    t = text.lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def question_id(text: str) -> str:
    return hashlib.sha256(normalize_question(text).encode()).hexdigest()[:32]


class Store:
    def __init__(self) -> None:
        self.db = config.firestore_client()

    # ------------------------------------------------------------------ #
    # Config reads
    # ------------------------------------------------------------------ #
    def _doc(self, collection: str, doc: str = DOC_ME) -> dict[str, Any]:
        snap = self.db.collection(collection).document(doc).get()
        return snap.to_dict() or {} if snap.exists else {}

    def get_facts(self) -> FactTable:
        return FactTable.from_dict(self._doc(C_FACTS))

    def get_profile(self) -> dict[str, Any]:
        return self._doc(C_PROFILE)

    def get_search_config(self) -> dict[str, Any]:
        d = self._doc(C_SEARCH)
        d.setdefault("titles", ["AI Engineer"])
        d.setdefault("locations", ["Bengaluru, Karnataka, India"])
        d.setdefault("date_posted", "past_24_hours")
        d.setdefault("experience_levels", ["mid_senior"])
        d.setdefault("easy_apply_only", True)
        return d

    def get_agent_config(self) -> dict[str, Any]:
        d = self._doc(C_AGENT)
        d.setdefault("vector_threshold", 0.80)
        d.setdefault("match_score_threshold", 70)
        d.setdefault("paused", False)
        d.setdefault("digest_hour_ist", 21)
        # Firestore may lower a limit but never raise it above the code ceiling.
        d["max_submits_24h"] = min(
            int(d.get("max_submits_24h", config.HARD_MAX_SUBMITS_24H)),
            config.HARD_MAX_SUBMITS_24H,
        )
        d["max_modal_opens_24h"] = min(
            int(d.get("max_modal_opens_24h", config.HARD_MAX_MODAL_OPENS_24H)),
            config.HARD_MAX_MODAL_OPENS_24H,
        )
        return d

    def set_agent_config(self, **fields: Any) -> None:
        self.db.collection(C_AGENT).document(DOC_ME).set(
            {**fields, "updated_at": _now()}, merge=True
        )

    def is_paused(self) -> bool:
        return bool(self.get_agent_config().get("paused"))

    def set_paused(self, paused: bool, reason: str = "") -> None:
        self.set_agent_config(paused=paused, pause_reason=reason)
        log.warning("agent paused=%s reason=%s", paused, reason)

    # ------------------------------------------------------------------ #
    # Caps -- integer comparisons, never model-decided
    # ------------------------------------------------------------------ #
    def submits_last_24h(self) -> int:
        q = (
            self.db.collection(C_APPLIED)
            .where(filter=FieldFilter("status", "==", ApplyOutcome.SUBMITTED.value))
            .where(filter=FieldFilter("submitted_at", ">=", _cutoff_24h()))
        )
        return int(q.count().get()[0][0].value)

    def modal_opens_last_24h(self) -> int:
        """Counted in Python to avoid needing a second composite index for ~40 rows."""
        q = self.db.collection(C_APPLIED).where(
            filter=FieldFilter("created_at", ">=", _cutoff_24h())
        )
        return sum(1 for d in q.stream() if (d.to_dict() or {}).get("modal_opened"))

    def cap_headroom(self) -> tuple[int, int]:
        """(submits_remaining, modal_opens_remaining). Either at 0 stops the run."""
        cfg = self.get_agent_config()
        return (
            max(0, cfg["max_submits_24h"] - self.submits_last_24h()),
            max(0, cfg["max_modal_opens_24h"] - self.modal_opens_last_24h()),
        )

    # ------------------------------------------------------------------ #
    # Dedup
    # ------------------------------------------------------------------ #
    def already_handled(
        self, job_ids: list[str], *, current_threshold: int | None = None
    ) -> set[str]:
        """Job ids already applied to, or already queued for review.

        A job previously skipped for scoring below the threshold is NOT considered
        handled once that threshold has been lowered -- it was rejected under a rule
        that no longer applies. Jobs skipped under a threshold that is still in force
        stay skipped, so lowering nothing re-scores nothing.
        """
        seen: set[str] = set()
        for collection in (C_APPLIED, C_PENDING):
            col = self.db.collection(collection)
            for chunk in (job_ids[i:i + 30] for i in range(0, len(job_ids), 30)):
                refs = [col.document(str(j)) for j in chunk]
                for snap in self.db.get_all(refs):
                    if not snap.exists:
                        continue
                    doc = snap.to_dict() or {}
                    if (
                        current_threshold is not None
                        and doc.get("status") == ApplyOutcome.SKIPPED_LOW_SCORE.value
                    ):
                        was = doc.get("threshold_at_skip")
                        if isinstance(was, (int, float)) and current_threshold < was:
                            log.info("job %s re-eligible: threshold %s -> %s",
                                     snap.id, was, current_threshold)
                            continue
                    seen.add(snap.id)
        return seen

    # ------------------------------------------------------------------ #
    # Applications -- idempotent by construction
    # ------------------------------------------------------------------ #
    def record_attempt(self, job: JobPosting, result: ApplyResult) -> bool:
        """Write the outcome. Returns False if this job was already submitted.

        The transaction is what makes a retry safe: a crash between submitting on
        LinkedIn and writing here leaves the doc absent, but a *second* submit
        attempt is blocked by the read inside the transaction.
        """
        ref = self.db.collection(C_APPLIED).document(job.job_id)

        @firestore.transactional
        def _txn(txn: firestore.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            if snap.exists and (snap.to_dict() or {}).get("status") == ApplyOutcome.SUBMITTED.value:
                return False
            payload: dict[str, Any] = {
                "job_id": job.job_id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "url": job.url,
                "jd_text": job.jd_text[:20000],
                "easy_apply": job.easy_apply,
                "status": result.outcome.value,
                "match_score": result.match_score,
                "modal_opened": result.modal_opened,
                "error": result.error[:1000],
                "run_id": result.run_id,
                "answers": [a.model_dump(mode="json") for a in result.answers],
                "created_at": _now(),
            }
            if result.outcome is ApplyOutcome.SKIPPED_LOW_SCORE and result.threshold_at_skip:
                payload["threshold_at_skip"] = result.threshold_at_skip
            if result.outcome is ApplyOutcome.SUBMITTED:
                payload["submitted_at"] = result.submitted_at or _now()
            txn.set(ref, payload, merge=True)
            return True

        return _txn(self.db.transaction())

    # ------------------------------------------------------------------ #
    # Concurrency claim
    # ------------------------------------------------------------------ #
    def claim_for_apply(self, job_id: str, *, ttl_seconds: int = 600) -> bool:
        """Atomically claim a job for applying. False means someone else has it.

        Necessary because a double-click in Slack starts two handler threads, and
        both would otherwise open a browser and race to the Submit button --
        `record_attempt`'s transaction only protects the write, which happens after
        the application has already been sent.

        The claim expires after `ttl_seconds` so a crashed or killed run cannot
        wedge a job permanently.
        """
        ref = self.db.collection(C_PENDING).document(str(job_id))

        @firestore.transactional
        def _txn(txn: firestore.Transaction) -> bool:
            snap = ref.get(transaction=txn)
            doc = (snap.to_dict() or {}) if snap.exists else {}
            held = doc.get("apply_claim_at")
            if held is not None:
                try:
                    age = (_now() - held).total_seconds()
                except TypeError:
                    age = ttl_seconds + 1  # unparseable => treat as stale
                if age < ttl_seconds:
                    return False
                log.info("job %s claim was stale (%.0fs); taking it", job_id, age)
            txn.set(ref, {"apply_claim_at": _now()}, merge=True)
            return True

        won = _txn(self.db.transaction())
        if not won:
            log.warning("job %s is already being applied to; refusing to start again",
                        job_id)
        return won

    def release_apply_claim(self, job_id: str) -> None:
        self.db.collection(C_PENDING).document(str(job_id)).set(
            {"apply_claim_at": None}, merge=True
        )

    # ------------------------------------------------------------------ #
    # Review queue
    # ------------------------------------------------------------------ #
    def queue_for_review(
        self, job: JobPosting, answers: list[ProposedAnswer], match_score: int | None
    ) -> None:
        self.db.collection(C_PENDING).document(job.job_id).set(
            {
                "job_id": job.job_id, "title": job.title, "company": job.company,
                "location": job.location, "url": job.url,
                "jd_text": job.jd_text[:20000], "match_score": match_score,
                "answers": [a.model_dump(mode="json") for a in answers],
                "status": "pending", "created_at": _now(),
            },
            merge=True,
        )

    def get_pending(self, job_id: str) -> dict[str, Any] | None:
        snap = self.db.collection(C_PENDING).document(str(job_id)).get()
        return snap.to_dict() if snap.exists else None

    def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        q = (
            self.db.collection(C_PENDING)
            .where(filter=FieldFilter("status", "==", "pending"))
            .limit(limit)
        )
        return [d.to_dict() | {"_id": d.id} for d in q.stream()]

    def set_pending_status(self, job_id: str, status: str, **extra: Any) -> None:
        # Drop None values: callers pass `answers=None` to mean "leave the existing
        # answers alone", and writing None would erase the blocking questions the
        # Slack card needs.
        fields = {k: v for k, v in extra.items() if v is not None}
        self.db.collection(C_PENDING).document(str(job_id)).set(
            {"status": status, "updated_at": _now(), **fields}, merge=True
        )

    def recent_jobs(self, hours: int = 24, limit: int = 200) -> list[dict[str, Any]]:
        cutoff = _now() - dt.timedelta(hours=hours)
        out: list[dict[str, Any]] = []
        for collection in (C_APPLIED, C_PENDING):
            q = (
                self.db.collection(collection)
                .where(filter=FieldFilter("created_at", ">=", cutoff))
                .limit(limit)
            )
            out.extend(d.to_dict() | {"_src": collection} for d in q.stream())
        return out

    # ------------------------------------------------------------------ #
    # Question bank
    # ------------------------------------------------------------------ #
    def bank_upsert(
        self, question_text: str, answer_text: str, category: str,
        embedding: list[float], origin: str,
    ) -> str:
        from google.cloud.firestore_v1.vector import Vector

        did = question_id(question_text)
        ref = self.db.collection(C_BANK).document(did)
        existing = ref.get()
        ref.set(
            {
                "question_text": question_text,
                "question_normalized": normalize_question(question_text),
                "question_embedding": Vector(embedding),
                "category": category,
                "answer_text": answer_text,
                "provenance_origin": origin,
                "times_used": (existing.to_dict() or {}).get("times_used", 0) if existing.exists else 0,
                "created_at": (existing.to_dict() or {}).get("created_at", _now()) if existing.exists else _now(),
                "updated_at": _now(),
            },
            merge=True,
        )
        return did

    def bank_size(self) -> int:
        return int(self.db.collection(C_BANK).count().get()[0][0].value)

    def bank_mark_used(self, doc_id: str) -> None:
        self.db.collection(C_BANK).document(doc_id).set(
            {"times_used": firestore.Increment(1), "last_used_at": _now()}, merge=True
        )

    def bank_nearest(
        self, embedding: list[float], *, similarity_threshold: float, limit: int = 3
    ) -> list[dict[str, Any]]:
        """Vector search. Note the conversion: Firestore returns *distance*.

        cosine_distance = 1 - cosine_similarity, so a similarity floor of 0.80
        becomes a distance ceiling of 0.20. Getting this backwards would make the
        agent confidently answer near-unrelated questions.
        """
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        distance_ceiling = 1.0 - float(similarity_threshold)
        try:
            snaps = (
                self.db.collection(C_BANK)
                .find_nearest(
                    vector_field="question_embedding",
                    query_vector=Vector(embedding),
                    distance_measure=DistanceMeasure.COSINE,
                    limit=limit,
                    distance_threshold=distance_ceiling,
                    distance_result_field="_distance",
                )
                .get()
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("vector search unavailable (%s); treating as no match", exc)
            return []
        out = []
        for s in snaps:
            d = s.to_dict() or {}
            dist = float(d.get("_distance", 1.0))
            out.append({
                "_id": s.id, "similarity": 1.0 - dist,
                "question_text": d.get("question_text", ""),
                "answer_text": d.get("answer_text", ""),
                "category": d.get("category", ""),
            })
        return out

    # ------------------------------------------------------------------ #
    # LLM cost, measured
    # ------------------------------------------------------------------ #
    def write_usage(self, run_id: str, total: dict[str, Any]) -> None:
        self.db.collection(C_USAGE).document(run_id).set(
            {**total, "created_at": _now()}, merge=True
        )

    def usage_rollup(self, days: int = 30) -> dict[str, Any]:
        """Actual measured spend. Not an estimate and not the billing account.

        Covers Vertex AI only, which is the sole line item that is not on a free
        tier: Firestore, GCS, Secret Manager and Scheduler all sit inside their
        always-free allowances at this volume.
        """
        cutoff = _now() - dt.timedelta(days=days)
        out = {"runs": 0, "calls": 0, "prompt_tokens": 0, "output_tokens": 0,
               "usd": 0.0, "by_purpose": {}}
        for d in self.db.collection(C_USAGE).stream():
            x = d.to_dict() or {}
            created = x.get("created_at")
            if created is not None:
                try:
                    if created < cutoff:
                        continue
                except TypeError:
                    pass
            out["runs"] += 1
            out["calls"] += int(x.get("calls") or 0)
            out["prompt_tokens"] += int(x.get("prompt_tokens") or 0)
            out["output_tokens"] += int(x.get("output_tokens") or 0)
            out["usd"] += float(x.get("usd") or 0.0)
            for k, v in (x.get("by_purpose") or {}).items():
                b = out["by_purpose"].setdefault(k, {"calls": 0, "usd": 0.0})
                b["calls"] += int(v.get("calls") or 0)
                b["usd"] += float(v.get("usd") or 0.0)
        out["usd"] = round(out["usd"], 6)
        if out["runs"]:
            out["usd_per_run"] = round(out["usd"] / out["runs"], 6)
            # 7 scheduled runs a day at the current cadence.
            out["projected_usd_per_month"] = round(out["usd_per_run"] * 7 * 30, 4)
        return out

    # ------------------------------------------------------------------ #
    # Traces
    # ------------------------------------------------------------------ #
    def write_trace(self, run_id: str, steps: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        self.db.collection(C_TRACES).document(run_id).set(
            {"run_id": run_id, "steps": steps, "summary": summary, "created_at": _now()},
            merge=True,
        )

    def recent_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        q = (
            self.db.collection(C_TRACES)
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(limit)
        )
        return [d.to_dict() | {"_id": d.id} for d in q.stream()]
