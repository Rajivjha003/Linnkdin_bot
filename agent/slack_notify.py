"""Outbound Slack: the batched digest, auth alerts, and the daily report.

Digests are batched -- one message per run, with a Block Kit card per job -- rather
than one message per job, because a stream of notifications is the fastest way to
make a human stop reading them.
"""
from __future__ import annotations

import logging
from typing import Any

from slack_sdk import WebClient

from agent import config
from agent.models import ApplyResult, JobPosting, Provenance

#: Kept here so the card and the handler cannot drift apart.
_REJECT_REASONS: dict[str, str] = {
    "wrong_role": "Wrong role / tech stack",
    "underqualified": "I am underqualified",
    "overqualified": "I am overqualified",
    "company": "Company not acceptable",
    "salary": "Salary too low",
    "other": "Other",
}

log = logging.getLogger("agent.slack")

_PROV_ICON = {
    Provenance.DETERMINISTIC.value: "🔒",
    Provenance.BANK_MATCH.value: "📚",
    Provenance.HUMAN.value: "🙋",
    Provenance.LLM.value: "⚠️",
}


def client() -> WebClient:
    return WebClient(token=config.secret("slack-bot-token"))


def _channel() -> str:
    return config.SLACK_CHANNEL


def post(blocks: list[dict], text: str, channel: str | None = None) -> str | None:
    try:
        resp = client().chat_postMessage(
            channel=channel or _channel(), blocks=blocks, text=text, unfurl_links=False
        )
        return resp.get("ts")
    except Exception as exc:  # noqa: BLE001
        log.error("slack post failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
def job_card(job: dict[str, Any], answers: list[dict[str, Any]]) -> list[dict]:
    """One reviewable job. Blocking answers are shown with why they blocked."""
    score = job.get("match_score")
    header = f"*<{job.get('url','')}|{job.get('title','(untitled)')}>*\n{job.get('company','')} · {job.get('location','')}"
    if score is not None:
        header += f"\n*Match:* {score}/100"

    blocking = [a for a in answers if a.get("provenance") in ("llm",) or not a.get("value")]
    lines = []
    for a in answers:
        icon = _PROV_ICON.get(a.get("provenance", ""), "•")
        val = a.get("value") or "_needs your answer_"
        q = (a.get("question_text") or "")[:110]
        line = f"{icon} *{q}*\n     {val}"
        if a.get("reason"):
            line += f"\n     _{a['reason'][:120]}_"
        lines.append(line)

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]
    if lines:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}})
    blocks.append({
        "type": "actions",
        "block_id": f"job_{job.get('job_id')}",
        "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "✅ Approve & Apply"},
             "style": "primary", "action_id": "approve",
             "value": str(job.get("job_id"))},
            # An overflow menu rather than a plain button: rejecting without a
            # reason taught the filter nothing, so four Data Engineer roles got
            # rejected in a row and the next run surfaced three more.
            {"type": "overflow", "action_id": "reject_reason",
             "options": [
                 {"text": {"type": "plain_text", "text": f"❌ {label}"},
                  "value": f"{job.get('job_id')}|{key}"}
                 for key, label in _REJECT_REASONS.items()
             ]},
            {"type": "button", "text": {"type": "plain_text", "text": "✏️ Answer questions"},
             "action_id": "edit_answers", "value": str(job.get("job_id"))},
        ],
    })
    if blocking:
        blocks.append({"type": "context", "elements": [{
            "type": "mrkdwn",
            "text": f"⚠️ {len(blocking)} question(s) need your answer before this can be submitted",
        }]})
    blocks.append({"type": "divider"})
    return blocks


def send_digest(pending: list[dict[str, Any]], stats: dict[str, Any]) -> str | None:
    """A short header, then ONE message per job.

    Per-job messages rather than one batched message, so that clicking a button
    can update exactly that card in place. With the batched layout a click left
    the message unchanged, which made it impossible to tell whether you had acted
    -- and led to the same job being clicked twice and two concurrent
    applications starting.
    """
    if not pending:
        return None
    from agent.store import Store

    header = post([
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"🎯 {len(pending)} job(s) awaiting review"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"submitted last 24h: *{stats.get('submitted_24h', 0)}/{stats.get('cap', 20)}* · "
            f"searched: *{stats.get('searched', 0)}* · "
            f"scored: *{stats.get('scored', 0)}* · "
            f"answer bank: *{stats.get('bank_size', 0)}* entries"}]},
    ], f"{len(pending)} job(s) awaiting review")

    store = Store()
    for job in pending[:12]:
        ts = post(job_card(job, job.get("answers", [])),
                  f"{job.get('title', 'job')} — needs review")
        if ts:
            # Remember where this card lives so a click can rewrite it in place.
            store.set_pending_status(job["job_id"], job.get("status", "pending"),
                                     slack_ts=ts, slack_channel=_channel())
    return header


def resolved_card(job: dict[str, Any], verdict: str, detail: str = "") -> list[dict]:
    """The card a job becomes once you have acted on it. No buttons."""
    icons = {"approved": "✅", "applied": "✅", "rejected": "❌",
             "answered": "✍️", "in_progress": "⏳", "failed": "⚠️"}
    icon = icons.get(verdict, "•")
    body = (f"{icon} *{verdict.replace('_', ' ').title()}* — "
            f"<{job.get('url', '')}|{job.get('title', '(untitled)')}>\n"
            f"{job.get('company', '')} · match {job.get('match_score', '-')}/100")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": body}}]
    if detail:
        blocks.append({"type": "context",
                       "elements": [{"type": "mrkdwn", "text": detail[:280]}]})
    blocks.append({"type": "divider"})
    return blocks


def update_card(channel: str, ts: str, blocks: list[dict], text: str) -> bool:
    """Rewrite a posted card in place. Removing the buttons is the point."""
    try:
        client().chat_update(channel=channel, ts=ts, blocks=blocks, text=text)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("could not update card %s/%s: %s", channel, ts, exc)
        return False


def send_auto_submitted(results: list[ApplyResult], jobs: dict[str, JobPosting]) -> None:
    """Notify about applications that cleared the gate without a human."""
    if not results:
        return
    lines = []
    for r in results:
        j = jobs.get(r.job_id)
        title = j.title if j else r.job_id
        company = j.company if j else ""
        url = j.url if j else ""
        lines.append(f"• <{url}|{title}> — {company} (match {r.match_score or '-'}/100)")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"✅ {len(results)} application(s) submitted"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            "🔒 every answer was deterministic or a banked match — no model-authored answers"}]},
    ]
    post(blocks, f"{len(results)} application(s) submitted")


def send_auth_failure(detail: str) -> None:
    """Workflow D. Also DMs, because this halts everything until fixed."""
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🚨 AUTH FAILURE — agent paused"}},
        {"type": "section", "text": {"type": "mrkdwn", "text":
            "*LinkedIn session is no longer valid. All applications are halted.*\n\n"
            "*To fix:*\n"
            "1. Log in to LinkedIn in Chrome\n"
            "2. `F12` → Application → Cookies → `https://www.linkedin.com`\n"
            "3. Copy the *Value* of the `li_at` cookie\n"
            "4. Run `/update_cookie <value>` here\n\n"
            f"_Detail:_ `{detail[:300]}`"}},
    ]
    post(blocks, "AUTH FAILURE — agent paused")


def send_gap_report(report: dict[str, Any], file_url: str | None) -> None:
    skills = report.get("missing_skills") or []
    text = "*Most-requested skills missing from your resume:*\n" + "\n".join(
        f"{i}. {s}" for i, s in enumerate(skills, 1)
    ) if skills else "_No gaps identified._"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 Daily resume gap report"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
    ]
    if report.get("summary"):
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"_{report['summary'][:1500]}_"}})
    if file_url:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"📄 <{file_url}|Download job data (link expires in 1 hour)>"}})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"based on {report.get('jobs_analyzed', 0)} posting(s) in the last 24h"}]})
    post(blocks, "Daily resume gap report")
