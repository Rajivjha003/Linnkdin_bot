"""Client for the pinned `mcp-server-linkedin` 4.23.3 (search + scrape only).

Everything in this module was written against payloads captured from the live
server, saved under `probe_output/`. Two facts drove the design:

1. The server exposes **no apply tool**. `search_jobs`, `get_job_details` and
   `get_saved_jobs` are the whole job surface; the maintainer rejected Easy Apply
   automation outright. Submitting is therefore ours to do (see apply_engine.py).

2. **`easy_apply=True` is not trustworthy.** A live search with `f_EA=true`
   returned a posting whose button reads "Apply" and which says "Responses managed
   off LinkedIn" -- i.e. an off-site application. So Easy Apply is re-verified per
   job from the parsed button text before anything is attempted.

Concurrency note: the server holds a lock on the Chromium profile at
`~/.linkedin-mcp/profile`. Our own Patchright engine needs that same profile, so
the two must never run at once -- call `close_session()` (or exit the context
manager) before starting an apply pass.
"""
from __future__ import annotations

import json
import logging
import re
from types import TracebackType

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent import config
from agent.models import JobPosting

log = logging.getLogger("agent.mcp")

#: Substrings that mean "the browser is not up yet, retry" rather than a real error.
_TRANSIENT = (
    "downloading", "not ready", "not complete yet", "setup attempt",
    "login is still in progress",
)
#: Substrings that mean the LinkedIn session is dead -> Workflow D halt.
_AUTH_DEAD = (
    "no linkedin session", "session expired", "not logged in", "login required",
    "authentication failed", "checkpoint", "challenge", "captcha",
    "please sign in", "429", "403",
)

_DOT = re.compile(r"[··•]")
#: Lines after which the posting body has ended and LinkedIn chrome begins.
_FOOTER = re.compile(
    r"^(restart premium|building a team\?|post a job|cancel anytime"
    r"|people you can reach out to|similar jobs|more jobs|set alert"
    r"|show more|see more jobs|premium)", re.I,
)
_BODY_START = re.compile(r"^(about the job|job description|role description)$", re.I)


class LinkedInAuthError(RuntimeError):
    """The session is dead. Callers must halt, not retry."""


class LinkedInNotReady(RuntimeError):
    """Browser still starting. Callers may retry after a wait."""


class LinkedInMCP:
    """Async context manager wrapping the stdio MCP subprocess."""

    def __init__(self) -> None:
        self._stack: list = []
        self._session: ClientSession | None = None

    async def __aenter__(self) -> LinkedInMCP:
        params = StdioServerParameters(
            command=config.MCP_SERVER_EXE,
            args=["--transport", "stdio"],
            env=config.mcp_env(),
        )
        self._cm = stdio_client(params)
        read, write = await self._cm.__aenter__()
        self._sess_cm = ClientSession(read, write)
        self._session = await self._sess_cm.__aenter__()
        await self._session.initialize()
        log.info("MCP server up (mcp-server-linkedin 4.23.3)")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Release the Chromium profile lock so the apply engine can take it.
        try:
            if self._session is not None:
                await self._session.call_tool("close_session", {})
        except Exception as e:  # noqa: BLE001
            log.debug("close_session failed (harmless on shutdown): %s", e)
        try:
            await self._sess_cm.__aexit__(exc_type, exc, tb)
        finally:
            await self._cm.__aexit__(exc_type, exc, tb)

    # ----------------------------------------------------------------- #
    async def _call(self, tool: str, args: dict) -> dict | str:
        assert self._session is not None, "use inside `async with`"
        res = await self._session.call_tool(tool, args)
        text = "\n".join(c.text for c in res.content if getattr(c, "text", None))
        if res.isError:
            low = text.lower()
            if any(k in low for k in _AUTH_DEAD):
                raise LinkedInAuthError(text[:400])
            if any(k in low for k in _TRANSIENT):
                raise LinkedInNotReady(text[:400])
            raise RuntimeError(f"{tool} failed: {text[:400]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    # ----------------------------------------------------------------- #
    async def search_jobs(
        self,
        keywords: str,
        location: str | None = None,
        *,
        easy_apply: bool = True,
        date_posted: str | None = "past_24_hours",
        experience_level: str | None = None,
        work_type: str | None = None,
        job_type: str | None = None,
        sort_by: str | None = "date",
        max_pages: int = 1,
    ) -> list[tuple[str, str]]:
        """Return [(job_id, title)]. Filter values are the server's exact enums:

        date_posted       past_hour | past_24_hours | past_week | past_month
        experience_level  internship | entry | associate | mid_senior | director | executive
        work_type         on_site | remote | hybrid
        job_type          full_time | part_time | contract | temporary | volunteer
                          | internship | other
        sort_by           date | relevance
        """
        args: dict = {"keywords": keywords, "easy_apply": easy_apply,
                      "max_pages": max(1, min(10, max_pages))}
        for k, v in (("location", location), ("date_posted", date_posted),
                     ("experience_level", experience_level), ("work_type", work_type),
                     ("job_type", job_type), ("sort_by", sort_by)):
            if v:
                args[k] = v
        payload = await self._call("search_jobs", args)
        if not isinstance(payload, dict):
            log.warning("search_jobs returned non-dict; treating as empty")
            return []

        ids: list[str] = [str(i) for i in payload.get("job_ids", [])]
        titles: dict[str, str] = {}
        for ref in payload.get("references", {}).get("search_results", []):
            if ref.get("kind") != "job":
                continue
            m = re.search(r"/jobs/view/(\d+)", ref.get("url", ""))
            if m:
                titles.setdefault(m.group(1), (ref.get("text") or "").strip())
        out = [(jid, titles.get(jid, "")) for jid in ids]
        log.info("search_jobs(%r, %r) -> %d ids", keywords, location, len(out))
        return out

    async def get_job_details(self, job_id: str) -> JobPosting:
        payload = await self._call("get_job_details", {"job_id": str(job_id)})
        if not isinstance(payload, dict):
            raise RuntimeError("get_job_details returned non-dict")
        blob = payload.get("sections", {}).get("job_posting", "") or ""
        parsed = parse_job_posting(blob)
        company_ref = ""
        for ref in payload.get("references", {}).get("job_posting", []):
            if ref.get("kind") == "company":
                company_ref = (ref.get("text") or "").strip()
                break
        return JobPosting(
            job_id=str(job_id),
            title=parsed["title"],
            company=company_ref or parsed["company"],
            location=parsed["location"],
            url=payload.get("url", f"https://www.linkedin.com/jobs/view/{job_id}/"),
            jd_text=parsed["jd_text"],
            easy_apply=parsed["easy_apply"],
            posted_text=parsed["posted"],
            raw={"blob_lines": blob.count("\n") + 1},
        )


# --------------------------------------------------------------------------- #
# Parser -- written against probe_output/job_details_raw.txt
#
#   0  UST                                             <- company
#   2  Lead Data Engineer                               <- title
#   4  Bengaluru, Karnataka, India · 7 minutes ago · 2 people clicked apply
#   6  Promoted by hirer · Responses managed off LinkedIn
#   8  On-site
#   9  Full-time
#  10  Apply                                            <- or "Easy Apply"
#  22  About the job                                    <- body starts after
# --------------------------------------------------------------------------- #
def parse_job_posting(blob: str) -> dict:
    lines = [ln.rstrip() for ln in blob.split("\n")]
    nonempty = [(i, ln.strip()) for i, ln in enumerate(lines) if ln.strip()]

    company = nonempty[0][1] if nonempty else ""
    title = nonempty[1][1] if len(nonempty) > 1 else ""

    location = posted = ""
    for _, ln in nonempty[:8]:
        if _DOT.search(ln) and ("ago" in ln.lower() or "," in ln):
            parts = [p.strip() for p in _DOT.split(ln) if p.strip()]
            if parts:
                location = parts[0]
            posted = next((p for p in parts[1:] if "ago" in p.lower()), "")
            break

    # Easy Apply detection: the button's own text, in the header region only.
    head = "\n".join(ln for _, ln in nonempty[:24])
    easy_apply = bool(re.search(r"^\s*easy apply\s*$", head, re.I | re.M))
    off_site = bool(re.search(r"responses managed off linkedin|apply on company", head, re.I))
    if off_site:
        easy_apply = False

    work_type = ""
    for _, ln in nonempty[:20]:
        if re.fullmatch(r"(on-site|remote|hybrid)", ln, re.I):
            work_type = ln
            break

    # Body: from the "About the job" marker to the first footer line.
    start = None
    for i, ln in enumerate(lines):
        if _BODY_START.match(ln.strip()):
            start = i + 1
            break
    jd_lines: list[str] = []
    if start is not None:
        for ln in lines[start:]:
            if _FOOTER.match(ln.strip()):
                break
            jd_lines.append(ln)
    jd_text = "\n".join(jd_lines).strip()
    jd_text = re.sub(r"\n{3,}", "\n\n", jd_text)

    return {
        "company": company, "title": title, "location": location, "posted": posted,
        "easy_apply": easy_apply, "work_type": work_type, "jd_text": jd_text,
    }
