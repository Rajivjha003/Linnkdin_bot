"""Learn from rejections, so the same wrong job stops appearing.

Until now a rejection was a dead end: the job was marked rejected and nothing
changed, so the next run happily surfaced three more like it. Rajiv rejected four
Data Engineer / PySpark / web-scraping roles in one sitting and the filter learned
nothing -- I had to notice the pattern by hand and edit the config.

This closes that loop. A rejection now carries a reason, and reasons accumulate
into concrete config changes:

  wrong_role       -> title keywords are counted; at THRESHOLD hits a keyword is
                      added to search_config.title_exclude, so those postings are
                      dropped before a browser ever opens
  underqualified   -> the scores of rejected jobs are tracked; if you keep
                      rejecting things above the current threshold, the threshold
                      is the problem and it is raised
  overqualified    -> the mirror case: suggests aiming higher, never auto-applied
  company          -> the company joins a skip list

Auto-applied changes are deliberately conservative (two independent rejections,
and only ever *narrowing* the search) and every one is logged so the weekly report
can show its work. Widening the search is never automatic -- that would mean the
bot deciding to apply to things you have not seen.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from collections import Counter
from typing import Any

log = logging.getLogger("agent.feedback")

C_REJECTIONS = "rejections"
C_TUNING = "tuning_log"

#: How many independent rejections before a keyword is auto-excluded.
AUTO_EXCLUDE_AFTER = 2

REASONS: dict[str, str] = {
    "wrong_role": "Wrong role / tech stack",
    "underqualified": "I'm underqualified",
    "overqualified": "I'm overqualified",
    "company": "Company not acceptable",
    "salary": "Salary too low",
    "other": "Other",
}

#: Words worth learning from a title. Generic ones ("senior", "engineer") would
#: exclude everything, so they are never candidates.
_STOP = {
    "senior", "sr", "junior", "jr", "lead", "staff", "engineer", "developer",
    "specialist", "consultant", "analyst", "architect", "manager", "and", "or",
    "the", "for", "with", "in", "of", "at", "a", "an", "i", "ii", "iii",
    "remote", "hybrid", "onsite", "india", "bengaluru", "bangalore", "contract",
    "fulltime", "full", "time", "years", "yrs", "exp", "experience", "job",
    "opportunity", "hiring", "urgent", "immediate", "walk", "drive", "new",
}
#: Multi-word phrases are far better signals than single words, so they are
#: checked first.
_PHRASES = [
    "data engineer", "data engineering", "web scraping", "full stack",
    "front end", "frontend", "back end", "backend", "devops", "site reliability",
    "business analyst", "product manager", "project manager", "qa", "test",
    "embedded", "firmware", "android", "ios", "salesforce", "sap", "oracle",
    "power platform", "sharepoint", "dotnet", ".net", "java developer",
    "php", "ruby", "golang", "etl developer", "informatica", "talend",
    "pyspark", "spark scala", "hadoop", "teradata", "cobol", "mainframe",
]


def record_rejection(store, job_id: str, reason: str, job: dict[str, Any]) -> None:
    """Persist one rejection with its reason."""
    store.db.collection(C_REJECTIONS).document(str(job_id)).set({
        "job_id": str(job_id),
        "reason": reason,
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "match_score": job.get("match_score"),
        "location": job.get("location", ""),
        "created_at": dt.datetime.now(dt.timezone.utc),
    }, merge=True)
    log.info("rejection recorded: %s (%s) %r", job_id, reason, job.get("title", "")[:50])


def title_keywords(title: str) -> list[str]:
    """Candidate exclusion keywords from a title, phrases first."""
    low = " ".join((title or "").lower().split())
    found = [p for p in _PHRASES if p in low]
    if found:
        return found
    words = [w for w in re.findall(r"[a-z][a-z.+#]{2,}", low) if w not in _STOP]
    return words[:3]


def analyse(store) -> dict[str, Any]:
    """What the rejections say, and what should change because of it."""
    rows = [d.to_dict() or {} for d in store.db.collection(C_REJECTIONS).stream()]
    cfg = store.get_agent_config()
    search = store.get_search_config()
    already = {w.lower() for w in search.get("title_exclude", [])}

    kw = Counter()
    companies = Counter()
    under, over = [], []
    for r in rows:
        reason = r.get("reason")
        if reason == "wrong_role":
            for k in title_keywords(r.get("title", "")):
                if k not in already:
                    kw[k] += 1
        elif reason == "underqualified" and r.get("match_score") is not None:
            under.append(int(r["match_score"]))
        elif reason == "overqualified" and r.get("match_score") is not None:
            over.append(int(r["match_score"]))
        elif reason in ("company", "salary") and r.get("company"):
            companies[r["company"]] += 1

    proposals: list[dict[str, Any]] = []
    for word, n in kw.items():
        if n >= AUTO_EXCLUDE_AFTER:
            proposals.append({
                "kind": "title_exclude", "value": word, "hits": n,
                "why": f"rejected {n} jobs whose title contained {word!r}",
                "auto": True,
            })
    for co, n in companies.items():
        if n >= AUTO_EXCLUDE_AFTER:
            proposals.append({
                "kind": "company_exclude", "value": co, "hits": n,
                "why": f"rejected {n} postings from {co}", "auto": True,
            })
    if len(under) >= 3:
        # If jobs ABOVE the bar keep getting rejected as too hard, the bar is low.
        floor = max(under)
        if floor >= int(cfg["match_score_threshold"]):
            proposals.append({
                "kind": "raise_threshold", "value": min(90, floor + 5),
                "hits": len(under),
                "why": (f"{len(under)} 'underqualified' rejections, highest scoring "
                        f"{floor} — the threshold is too low"),
                "auto": False,   # changing the bar is the user's call
            })
    if len(over) >= 3:
        proposals.append({
            "kind": "aim_higher", "value": min(over),
            "hits": len(over),
            "why": (f"{len(over)} 'overqualified' rejections — consider more senior "
                    f"titles (Staff / Principal / Lead)"),
            "auto": False,
        })

    return {
        "rejections": len(rows),
        "by_reason": dict(Counter(r.get("reason", "?") for r in rows)),
        "top_keywords": kw.most_common(8),
        "top_companies": companies.most_common(5),
        "proposals": proposals,
    }


def apply_auto(store, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply only the conservative, search-NARROWING proposals.

    Never widens the search: that would mean applying to jobs the user has not
    seen on the strength of an inference.
    """
    search = store.get_search_config()
    excl = list(search.get("title_exclude", []))
    cos = list(search.get("company_exclude", []))
    applied: list[dict[str, Any]] = []

    for p in analysis.get("proposals", []):
        if not p.get("auto"):
            continue
        if p["kind"] == "title_exclude" and p["value"] not in excl:
            excl.append(p["value"])
            applied.append(p)
        elif p["kind"] == "company_exclude" and p["value"] not in cos:
            cos.append(p["value"])
            applied.append(p)

    if applied:
        store.db.collection("search_config").document("me").set(
            {"title_exclude": excl, "company_exclude": cos}, merge=True)
        store.db.collection(C_TUNING).document(
            f"tune_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}"
        ).set({"applied": applied, "created_at": dt.datetime.now(dt.timezone.utc)})
        for p in applied:
            log.info("auto-tuned: %s += %r (%s)", p["kind"], p["value"], p["why"])
    return applied


def recent_tuning(store, limit: int = 20) -> list[dict[str, Any]]:
    from google.cloud import firestore

    q = (store.db.collection(C_TUNING)
         .order_by("created_at", direction=firestore.Query.DESCENDING)
         .limit(limit))
    return [d.to_dict() or {} for d in q.stream()]
