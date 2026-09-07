"""The four workflows.

A -- the run: search, filter, score, open modals, auto-submit or queue (every 2h)
B -- Slack approve: apply an already-reviewed job (see slack_app.py)
C -- the 21:00 IST digest: resume gap report + XLSX export
D -- auth failure: halt everything and alert

Ordering that matters: the MCP server and the apply engine each drive a Chromium
instance. They use separate profiles so neither blocks the other, but the run still
closes MCP before applying -- two concurrent authenticated sessions on the same
account is a pattern worth avoiding regardless of whether it technically works.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import uuid
from typing import Any

from agent import config, llm, slack_notify
from agent.answering import AnswerEngine
from agent.apply_engine import ApplyEngine, AuthFailure
from agent.linkedin_mcp import LinkedInAuthError, LinkedInMCP, LinkedInNotReady
from agent.voyager import VoyagerAuthError, VoyagerClient
from agent.models import ApplyOutcome, ApplyResult, JobPosting
from agent.store import Store

log = logging.getLogger("agent.workflows")

#: How many postings to look up per run. Voyager is cheap (~200ms, no browser),
#: so this is generous; the scarce budget is modal opens, not lookups.
VOYAGER_LOOKUP_BUDGET = 80


# --------------------------------------------------------------------------- #
# Deterministic pre-filter: cheap, and it keeps the model off most postings.
# --------------------------------------------------------------------------- #
def prefilter(job: JobPosting, search_cfg: dict[str, Any]) -> tuple[bool, str]:
    title = (job.title or "").lower()
    for bad in search_cfg.get("title_exclude", []):
        if bad.lower() in title:
            return False, f"title excludes {bad!r}"
    if not job.easy_apply:
        return False, "not an Easy Apply job"
    must = [m.lower() for m in search_cfg.get("must_have_any", [])]
    if must:
        hay = f"{title} {job.jd_text}".lower()
        if not any(m in hay for m in must):
            return False, "no must-have keyword present"
    return True, "passed prefilter"


# --------------------------------------------------------------------------- #
# Workflow A
# --------------------------------------------------------------------------- #
async def _search_all(mcp: LinkedInMCP, cfg: dict[str, Any]) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    titles = cfg.get("titles", [])[:5]
    locations = cfg.get("locations", [])[:2]
    levels = cfg.get("experience_levels") or [None]
    for title in titles:
        for loc in locations:
            try:
                hits = await mcp.search_jobs(
                    keywords=title, location=loc,
                    easy_apply=bool(cfg.get("easy_apply_only", True)),
                    date_posted=cfg.get("date_posted", "past_24_hours"),
                    experience_level=",".join(x for x in levels if x) or None,
                    job_type=cfg.get("job_type"),
                    sort_by=cfg.get("sort_by", "date"),
                    max_pages=int(cfg.get("max_pages", 1)),
                )
            except LinkedInNotReady as exc:
                log.warning("browser not ready, waiting once: %s", exc)
                await asyncio.sleep(45)
                continue
            for jid, t in hits:
                found.setdefault(jid, t)
    return list(found.items())


def run_workflow_a(*, dry_run: bool | None = None, headless: bool = True,
                   max_jobs: int | None = None) -> dict[str, Any]:
    """One application pass. Returns a summary dict for logging and the dashboard."""
    run_id = f"run_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:6]}"
    store = Store()
    cfg = store.get_agent_config()
    if dry_run is None:
        dry_run = bool(cfg.get("dry_run", True))

    summary: dict[str, Any] = {
        "run_id": run_id, "dry_run": dry_run, "searched": 0, "deduped": 0,
        "easy_apply": 0, "prefiltered": 0, "scored": 0, "above_threshold": 0,
        "modals": 0, "submitted": 0,
        "queued": 0, "failed": 0, "aborted": "",
    }
    steps: list[dict[str, Any]] = []

    if store.is_paused():
        summary["aborted"] = "agent is paused"
        log.warning("run aborted: agent is paused")
        return summary

    submits_left, modals_left = store.cap_headroom()
    steps.append({"step": "cap_check", "submits_left": submits_left,
                  "modal_opens_left": modals_left})
    if submits_left <= 0 or modals_left <= 0:
        summary["aborted"] = f"cap reached (submits_left={submits_left}, modals_left={modals_left})"
        log.info("run aborted silently: %s", summary["aborted"])
        store.write_trace(run_id, steps, summary)
        return summary

    search_cfg = store.get_search_config()
    profile = store.get_profile()
    facts = store.get_facts()
    resume_text = profile.get("resume_text", "")
    resume_filename = profile.get("resume_linkedin_filename", "")

    # ---- discovery: MCP for search, Voyager for details ------------------ #
    #
    # Details come from Voyager rather than the MCP server's text scrape, for two
    # reasons proven against the live site: Voyager states Easy Apply outright via
    # applyMethod.$type (the DOM cannot -- both kinds of posting render an anchor
    # labelled plainly "Apply"), and it needs no browser, so a lookup is ~200ms
    # instead of a full page load.
    async def search_only() -> list[tuple[str, str]]:
        async with LinkedInMCP() as mcp:
            return await _search_all(mcp, search_cfg)

    try:
        all_ids = asyncio.run(search_only())
    except LinkedInAuthError as exc:
        return _halt_auth(store, run_id, steps, summary, str(exc))

    handled = store.already_handled(
        [j for j, _ in all_ids],
        current_threshold=int(cfg["match_score_threshold"]),
    )
    fresh = [(j, t) for j, t in all_ids if j not in handled]
    log.info("%d ids, %d already handled, %d fresh", len(all_ids), len(handled), len(fresh))

    voyager = VoyagerClient()
    details: dict[str, JobPosting] = {}
    ea_count = 0
    try:
        # Sweep widely here: a lookup is ~200ms of HTTP and no browser, so there is
        # no reason to ration it. `max_jobs` bounds MODAL OPENS further down, which
        # is the step that costs ~40s and real activity on the account.
        lookup_budget = min(len(fresh), VOYAGER_LOOKUP_BUDGET)
        for jid, _t in fresh[:lookup_budget]:
            try:
                job = voyager.job_posting(jid)
            except VoyagerAuthError as exc:
                return _halt_auth(store, run_id, steps, summary, str(exc))
            except Exception as exc:  # noqa: BLE001
                log.warning("voyager job_posting(%s) failed: %s", jid, exc)
                continue
            if job.raw.get("closed"):
                continue
            if job.easy_apply:
                ea_count += 1
            details[jid] = job
    finally:
        voyager.close()
    log.info("voyager: %d postings, %d Easy Apply", len(details), ea_count)
    steps.append({"step": "voyager", "looked_up": len(details), "easy_apply": ea_count})

    summary["searched"] = len(all_ids)
    summary["deduped"] = len(details)
    summary["easy_apply"] = ea_count

    # ---- deterministic prefilter ---------------------------------------- #
    candidates: list[JobPosting] = []
    for job in details.values():
        ok, why = prefilter(job, search_cfg)
        if ok:
            candidates.append(job)
        else:
            log.info("job %s filtered: %s", job.job_id, why)
    summary["prefiltered"] = len(candidates)
    steps.append({"step": "prefilter", "kept": len(candidates)})

    # ---- score (model, borderline set only) ----------------------------- #
    threshold = int(cfg["match_score_threshold"])
    scored: list[tuple[JobPosting, int]] = []
    all_scores: list[dict[str, Any]] = []
    for job in candidates:
        sj = llm.score_job(job, resume_text)
        if sj is None:
            log.info("job %s: unusable score -> skipped", job.job_id)
            continue
        summary["scored"] += 1
        all_scores.append({"job_id": job.job_id, "score": sj.score,
                           "title": job.title[:60],
                           "reason": sj.one_line_reason[:120]})
        if sj.score < threshold:
            store.record_attempt(job, ApplyResult(
                job_id=job.job_id, outcome=ApplyOutcome.SKIPPED_LOW_SCORE,
                match_score=sj.score, run_id=run_id,
                error=f"score {sj.score} < {threshold}",
                threshold_at_skip=threshold,
            ))
            continue
        scored.append((job, sj.score))
    scored.sort(key=lambda p: -p[1])
    summary["above_threshold"] = len(scored)
    summary["scores"] = all_scores
    steps.append({"step": "score", "above_threshold": len(scored),
                  "threshold": threshold, "scores": all_scores})
    if all_scores and not scored:
        log.info("all %d Easy Apply job(s) scored below the threshold of %d: %s",
                 len(all_scores), threshold, all_scores)
    # max_jobs now limits the expensive step only.
    if max_jobs:
        scored = scored[:max_jobs]

    # ---- apply (our Patchright engine) ---------------------------------- #
    engine = AnswerEngine(store, facts, float(cfg["vector_threshold"]))
    auto_submitted: list[ApplyResult] = []
    jobs_by_id = {j.job_id: j for j, _ in scored}

    if scored:
        with ApplyEngine(headless=headless, dry_run=dry_run) as browser:
            for job, score in scored:
                if submits_left <= 0 or modals_left <= 0:
                    log.info("cap reached mid-run; stopping")
                    break
                result = browser.apply(job, engine, resume_filename,
                                       run_id=run_id, match_score=score)
                modals_left -= 1 if result.modal_opened else 0
                summary["modals"] += 1 if result.modal_opened else 0

                if result.outcome is ApplyOutcome.AUTH_FAILURE:
                    store.record_attempt(job, result)
                    return _halt_auth(store, run_id, steps, summary, result.error)

                store.record_attempt(job, result)
                if result.outcome is ApplyOutcome.SUBMITTED:
                    submits_left -= 1
                    summary["submitted"] += 1
                    auto_submitted.append(result)
                elif result.outcome is ApplyOutcome.ABANDONED_NEEDS_REVIEW:
                    store.queue_for_review(job, result.answers, score)
                    summary["queued"] += 1
                else:
                    summary["failed"] += 1
                steps.append({"step": "apply", "job_id": job.job_id,
                              "outcome": result.outcome.value, "score": score,
                              "error": result.error[:200]})

    # ---- notify ---------------------------------------------------------- #
    try:
        if auto_submitted:
            slack_notify.send_auto_submitted(auto_submitted, jobs_by_id)
        pending = store.list_pending(limit=12)
        if pending:
            slack_notify.send_digest(pending, {
                "submitted_24h": store.submits_last_24h(),
                "cap": cfg["max_submits_24h"],
                "searched": summary["searched"], "scored": summary["scored"],
                "bank_size": store.bank_size(),
            })
    except Exception as exc:  # noqa: BLE001
        log.warning("slack notification failed (run itself succeeded): %s", exc)

    usage = llm.flush_usage(store, run_id)
    summary["llm_calls"] = usage.get("calls", 0)
    summary["llm_usd"] = usage.get("usd", 0.0)
    store.write_trace(run_id, steps, summary)
    log.info("run %s done: %s", run_id, summary)
    return summary


def _halt_auth(store: Store, run_id: str, steps: list, summary: dict, detail: str) -> dict:
    """Workflow D: stop immediately, pause, alert. No retries, no further applies."""
    store.set_paused(True, reason=f"auth failure: {detail[:200]}")
    summary["aborted"] = f"AUTH FAILURE: {detail[:200]}"
    steps.append({"step": "auth_failure", "detail": detail[:400]})
    try:
        slack_notify.send_auth_failure(detail)
    except Exception as exc:  # noqa: BLE001
        log.error("could not send auth alert: %s", exc)
    store.write_trace(run_id, steps, summary)
    return summary


# --------------------------------------------------------------------------- #
# Workflow C -- 21:00 IST
# --------------------------------------------------------------------------- #
def run_workflow_c() -> dict[str, Any]:
    store = Store()
    profile = store.get_profile()
    jobs = store.recent_jobs(hours=24, limit=200)
    log.info("gap analysis over %d postings", len(jobs))

    # The corpus must be the roles actually targeted. Feeding in everything
    # scraped produced a report about the Data Engineer market Rajiv had rejected.
    report = llm.resume_gap_report(store, jobs, profile.get("resume_text", ""))
    payload = report.model_dump() if report else {
        "missing_skills": [], "summary": "Report unavailable.", "jobs_analyzed": len(jobs)
    }

    url = None
    try:
        url = _export_xlsx(jobs)
    except Exception as exc:  # noqa: BLE001
        log.warning("xlsx export failed: %s", exc)
    try:
        slack_notify.send_gap_report(payload, url)
    except Exception as exc:  # noqa: BLE001
        log.warning("slack gap report failed: %s", exc)
    return payload


def _export_xlsx(jobs: list[dict[str, Any]]) -> str | None:
    """Write the day's postings to GCS and return a 1-hour signed URL."""
    import io

    import pandas as pd

    if not jobs:
        return None
    rows = [{
        "job_id": j.get("job_id"), "title": j.get("title"), "company": j.get("company"),
        "location": j.get("location"), "match_score": j.get("match_score"),
        "status": j.get("status"), "url": j.get("url"),
        "jd_text": (j.get("jd_text") or "")[:32000],
    } for j in jobs]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, sheet_name="jobs")
    buf.seek(0)

    name = f"jobs_{dt.datetime.now(dt.timezone.utc):%Y%m%d_%H%M%S}.xlsx"
    blob = config.storage_client().bucket(config.GCS_BUCKET).blob(name)
    blob.upload_from_file(
        buf, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return blob.generate_signed_url(version="v4", expiration=dt.timedelta(hours=1),
                                    method="GET")


# --------------------------------------------------------------------------- #
# Workflow B -- apply one already-approved job (invoked from Slack)
# --------------------------------------------------------------------------- #
def apply_approved(job_id: str, *, headless: bool = True) -> ApplyResult:
    """Submit a job a human has approved. Overrides dry_run: approval is the intent."""
    store = Store()
    doc = store.get_pending(job_id)
    if doc is None:
        raise ValueError(f"{job_id} is not in pending_review")

    submits_left, modals_left = store.cap_headroom()
    if submits_left <= 0:
        store.set_pending_status(job_id, "pending", note="cap reached; not submitted")
        return ApplyResult(job_id=job_id, outcome=ApplyOutcome.SKIPPED_CAP,
                           error="24h submit cap reached")

    # Exactly one caller may apply to a given job at a time. A double-click in
    # Slack starts two handler threads, and without this both open a browser and
    # race to Submit.
    if not store.claim_for_apply(job_id):
        return ApplyResult(
            job_id=job_id, outcome=ApplyOutcome.SKIPPED_DUPLICATE,
            error="another apply attempt for this job is already in progress",
        )

    job = JobPosting(
        job_id=job_id, title=doc.get("title", ""), company=doc.get("company", ""),
        location=doc.get("location", ""), url=doc.get("url", ""),
        jd_text=doc.get("jd_text", ""), easy_apply=True,
    )
    facts = store.get_facts()
    cfg = store.get_agent_config()
    engine = AnswerEngine(store, facts, float(cfg["vector_threshold"]))

    # Human-supplied answers recorded on the pending doc take precedence over
    # anything recomputed, and carry HUMAN provenance so the gate accepts them.
    overrides = {a.get("question_text", ""): a for a in doc.get("answers", [])
                 if a.get("provenance") == "human" and a.get("value")}
    if overrides:
        engine = _WithOverrides(engine, overrides)

    try:
        with ApplyEngine(headless=headless, dry_run=False) as browser:
            result = browser.apply(job, engine, store.get_profile().get(
                "resume_linkedin_filename", ""), run_id=f"slack_{job_id}",
                match_score=doc.get("match_score"))
    except Exception:
        # Never leave the claim held on an unexpected failure, or the job is stuck
        # until the TTL expires.
        store.release_apply_claim(job_id)
        raise

    if result.outcome is ApplyOutcome.AUTH_FAILURE:
        store.set_paused(True, reason="auth failure during approved apply")
        slack_notify.send_auth_failure(result.error)
    store.release_apply_claim(job_id)
    store.record_attempt(job, result)
    # "Needs a human" must stay actionable. ABANDONED_NEEDS_REVIEW means the form
    # is blocked on an answer only the user can give -- salary, most often -- so it
    # goes back to `pending` rather than `failed`. Marking it failed would drop it
    # out of list_pending() and hide the very question that needs answering.
    if result.outcome is ApplyOutcome.SUBMITTED:
        new_status = "applied"
    elif result.outcome is ApplyOutcome.ABANDONED_NEEDS_REVIEW:
        new_status = "pending"
    elif result.outcome is ApplyOutcome.SKIPPED_CAP:
        new_status = "pending"
    else:
        new_status = "failed"
    store.set_pending_status(
        job_id, new_status,
        note=result.error[:300],
        answers=[a.model_dump(mode="json") for a in result.answers] or None,
    )
    return result


class _WithOverrides:
    """Wraps AnswerEngine so human answers win without mutating the engine."""

    def __init__(self, inner: AnswerEngine, overrides: dict[str, dict]):
        self._inner = inner
        self._overrides = overrides

    def answer(self, question):  # noqa: ANN001, ANN201
        from agent import facts as facts_mod
        from agent.models import ProposedAnswer, Provenance

        for q_text, a in self._overrides.items():
            if _norm(q_text) == _norm(question.text):
                # Classify locally rather than delegating: calling the inner
                # engine here would run a bank lookup (and bump its use counter)
                # for an answer we are about to discard anyway.
                category, _ = facts_mod.classify(question.text)
                return ProposedAnswer(
                    question_text=question.text,
                    category=category,
                    value=a["value"],
                    provenance=Provenance.HUMAN,
                    evidence=f"answered by you in Slack at {a.get('answered_at', 'n/a')}",
                )
        return self._inner.answer(question)


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower()).split())


# --------------------------------------------------------------------------- #
# Heartbeat: prove the agent is alive, and shout if it is not
# --------------------------------------------------------------------------- #
#: During these hours a long silence means something is broken, not just quiet.
ACTIVE_HOURS = (8, 22)
SILENCE_ALARM_HOURS = 6


def run_heartbeat(*, alarm_only: bool = False) -> dict[str, Any]:
    """Post a one-line status, and alarm if no run has completed recently.

    Without this, a sleeping laptop or a broken scheduled task is indistinguishable
    from a quiet job market -- and a week could pass before anyone noticed.
    """
    import datetime as _dt
    import zoneinfo

    from agent import feedback

    store = Store()
    now_ist = _dt.datetime.now(zoneinfo.ZoneInfo(config.TIMEZONE))
    traces = store.recent_traces(limit=30)

    last = None
    for t in traces:
        ts = t.get("created_at")
        if ts is not None:
            last = ts if last is None else max(last, ts)
    hours_since = None
    if last is not None:
        hours_since = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 3600

    cfg = store.get_agent_config()
    usage = store.usage_rollup(days=30)
    submits = store.submits_last_24h()
    pending = len(store.list_pending())
    runs_24h = sum(
        1 for t in traces
        if t.get("created_at") is not None
        and (dt.datetime.now(dt.timezone.utc) - t["created_at"]).total_seconds() < 86400
    )

    in_hours = ACTIVE_HOURS[0] <= now_ist.hour < ACTIVE_HOURS[1]
    silent = (hours_since is None or hours_since > SILENCE_ALARM_HOURS) and in_hours

    summary = {
        "runs_24h": runs_24h, "submits_24h": submits, "pending": pending,
        "hours_since_last_run": round(hours_since, 1) if hours_since else None,
        "silent": silent, "usd_30d": usage.get("usd", 0.0),
        "paused": bool(cfg.get("paused")),
    }

    try:
        if silent:
            slack_notify.post([
                {"type": "header", "text": {"type": "plain_text",
                                            "text": "⚠️ Agent has gone quiet"}},
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    (f"No run has completed in *"
                     f"{'ever' if hours_since is None else f'{hours_since:.1f}h'}*, "
                     f"during working hours.\n\n"
                     f"Usual causes: the PC was asleep, the `JobAgent-Run` task is "
                     f"disabled, or LinkedIn logged the session out.\n"
                     f"Check with `/status`, or run "
                     f"`Start-ScheduledTask -TaskName JobAgent-Run`.")}},
            ], "Agent has gone quiet")
        elif not alarm_only:
            rej = feedback.analyse(store)
            line = (f"*{runs_24h}* runs · *{submits}/{cfg['max_submits_24h']}* applied · "
                    f"*{pending}* waiting on you · bank *{store.bank_size()}* · "
                    f"${usage.get('usd', 0):.4f} spent (30d)")
            extra = ""
            if rej.get("proposals"):
                extra = ("\n_" + "; ".join(p["why"] for p in rej["proposals"][:2])
                         + "_")
            slack_notify.post([
                {"type": "section", "text": {"type": "mrkdwn",
                                             "text": "💚 " + line + extra}},
            ], "agent heartbeat")
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat post failed: %s", exc)

    log.info("heartbeat: %s", summary)
    return summary
