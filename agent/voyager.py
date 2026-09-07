"""LinkedIn Voyager API client -- the authoritative Easy Apply detector.

Why this module exists
----------------------
Detecting Easy Apply from the DOM turned out to be impossible, not merely awkward.
Measured against the live site:

* LinkedIn ships **hashed class names** (`class="_34d25300 _692f8ab4 ..."`), so no
  structural selector survives.
* The apply control is an **anchor, not a button**, so button scans found nothing.
* Its visible text is just **"Apply"** for BOTH Easy Apply and external postings --
  the words "Easy Apply" appear nowhere on the page. Nine different searches and
  ~50 postings produced zero text matches, which looked like "Easy Apply is
  extinct" and was in fact a detection failure.
* The MCP server's text scrape reports "Apply" for every posting, Easy Apply or not.

Voyager answers directly. `applyMethod.$type` is a discriminated union:

    com.linkedin.voyager.jobs.ComplexOnsiteApply  -> Easy Apply  (apply on LinkedIn)
    com.linkedin.voyager.jobs.SimpleOnsiteApply   -> Easy Apply
    com.linkedin.voyager.jobs.OffsiteApply        -> external ATS

Measured rate on a real sample: **6 of 15 (40%) Easy Apply** -- so there is plenty
to apply to; the earlier "0%" was entirely an artefact of DOM scraping.

Auth uses the same cookies the browser holds; Voyager requires a `csrf-token`
header whose value is the JSESSIONID cookie with quotes stripped. No browser is
needed, which makes detection ~200ms per job instead of a full page load.
"""
from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import requests

from agent.models import JobPosting

log = logging.getLogger("agent.voyager")

STATE = pathlib.Path.home() / ".linkedin-mcp" / "storage_state.json"
BASE = "https://www.linkedin.com/voyager/api"
JOB_DECO = "com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

#: applyMethod.$type suffixes that mean "apply inside LinkedIn".
_ONSITE = ("ComplexOnsiteApply", "SimpleOnsiteApply")


class VoyagerAuthError(RuntimeError):
    """Session rejected. Callers must halt, not retry."""


class VoyagerClient:
    def __init__(self, state_path: pathlib.Path = STATE):
        self._state_path = state_path
        self._session: requests.Session | None = None
        self._csrf = ""

    # ------------------------------------------------------------------ #
    def _ensure(self) -> requests.Session:
        if self._session is not None:
            return self._session
        if not self._state_path.exists():
            raise VoyagerAuthError(f"no session state at {self._state_path}")
        cookies = json.loads(self._state_path.read_text(encoding="utf-8")).get("cookies", [])
        jar = {c["name"]: c["value"] for c in cookies
               if "linkedin" in (c.get("domain") or "")}
        if "li_at" not in jar:
            raise VoyagerAuthError("li_at cookie missing from session state")
        self._csrf = jar.get("JSESSIONID", "").replace('"', "")
        if not self._csrf:
            raise VoyagerAuthError("JSESSIONID missing -- cannot build csrf token")
        s = requests.Session()
        s.cookies.update(jar)
        s.headers.update({
            "csrf-token": self._csrf,
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
            "user-agent": UA,
            "accept-language": "en-IN,en;q=0.9",
        })
        self._session = s
        return s

    def _get(self, url: str) -> dict[str, Any]:
        s = self._ensure()
        r = s.get(url, timeout=30)
        # 999 is LinkedIn's bespoke "you look automated" status.
        if r.status_code in (401, 403, 999):
            raise VoyagerAuthError(f"HTTP {r.status_code} from Voyager: {r.text[:200]}")
        if r.status_code == 429:
            raise VoyagerAuthError("HTTP 429 rate limited by Voyager")
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------ #
    def job_posting(self, job_id: str) -> JobPosting:
        """Structured posting, with `easy_apply` decided by applyMethod.$type."""
        payload = self._get(f"{BASE}/jobs/jobPostings/{job_id}?decorationId={JOB_DECO}")
        d = payload.get("data") or payload
        apply_method = d.get("applyMethod") or {}
        type_suffix = str(apply_method.get("$type", "")).split(".")[-1]
        easy = any(t in type_suffix for t in _ONSITE)

        desc = d.get("description") or {}
        jd_text = desc.get("text") if isinstance(desc, dict) else str(desc or "")

        # The normalized (+json+2.1) format does not inline the company:
        # `companyDetails` holds only a URN, and the resolved entity is hoisted into
        # a sibling `included` array. So read it from there.
        company = ""
        for entity in payload.get("included") or []:
            if not isinstance(entity, dict):
                continue
            if str(entity.get("$type", "")).endswith("Company") and entity.get("name"):
                company = entity["name"]
                break

        return JobPosting(
            job_id=str(job_id),
            title=d.get("title") or "",
            company=company,
            location=d.get("formattedLocation") or "",
            url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            jd_text=(jd_text or "").strip(),
            easy_apply=easy,
            posted_text=str(d.get("listedAt") or ""),
            raw={
                "apply_type": type_suffix,
                "closed": bool(d.get("closed")),
                "remote_allowed": bool(d.get("workRemoteAllowed")),
                "applies": d.get("applies"),
            },
        )

    def is_easy_apply(self, job_id: str) -> tuple[bool, str]:
        """(is_easy_apply, apply_type). Cheap: one HTTP call, no browser."""
        try:
            job = self.job_posting(job_id)
        except VoyagerAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("voyager lookup failed for %s: %s", job_id, exc)
            return False, "lookup_failed"
        return job.easy_apply, str(job.raw.get("apply_type", ""))

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


def apply_flow_url(job_id: str) -> str:
    """The URL that opens the Easy Apply dialog directly.

    Discovered from the anchor LinkedIn renders on the job page:
        <a href="/jobs/view/<id>/apply/?openSDUIApplyFlow">Apply</a>

    Navigating straight here opens a real `div[role=dialog]` containing the form,
    which avoids having to locate and click a control whose classes are hashed and
    whose element type is not what you would guess.
    """
    return f"https://www.linkedin.com/jobs/view/{job_id}/apply/?openSDUIApplyFlow"
