"""Slack Socket Mode listener: buttons, the answer modal, and slash commands.

Socket Mode (an outbound WebSocket) rather than HTTP webhooks is what removed the
need for any publicly reachable endpoint, and with it the Cloud Run webhook service
the original design called for.

Slack requires an ack within 3 seconds, but opening a browser and walking an Easy
Apply modal takes tens of seconds. So every handler acks immediately and does the
real work on a worker thread, reporting back into the thread of the original
message.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from typing import Any

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from agent import config, slack_notify, workflows
from agent.answering import learn_from_human
from agent.models import ApplyOutcome
from agent.store import Store

log = logging.getLogger("agent.slack_app")

app = App(token=config.secret("slack-bot-token"), logger=log)


def _bg(fn, *args, **kwargs) -> None:
    """Run slow work off the ack path."""
    threading.Thread(target=_guard(fn), args=args, kwargs=kwargs, daemon=True).start()


def _guard(fn):
    def inner(*a, **kw):
        try:
            fn(*a, **kw)
        except Exception:  # noqa: BLE001
            log.exception("background handler failed")
    return inner


# --------------------------------------------------------------------------- #
# Buttons
# --------------------------------------------------------------------------- #
@app.action("approve")
def on_approve(ack, body, client, action):  # noqa: ANN001
    ack()
    job_id = action["value"]
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]

    def work() -> None:
        client.chat_postMessage(channel=channel, thread_ts=ts,
                                text=f"⏳ Applying to `{job_id}`…")
        try:
            result = workflows.apply_approved(job_id)
        except Exception as exc:  # noqa: BLE001
            client.chat_postMessage(channel=channel, thread_ts=ts,
                                    text=f"❌ `{job_id}` failed: `{exc}`")
            return
        if result.outcome is ApplyOutcome.SUBMITTED:
            msg = f"✅ Submitted `{job_id}`."
            try:
                client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
            except Exception:  # noqa: BLE001
                pass
        elif result.outcome is ApplyOutcome.SKIPPED_CAP:
            msg = f"🛑 `{job_id}` not submitted: 24h cap reached. It stays queued."
        else:
            msg = f"⚠️ `{job_id}` ended as *{result.outcome.value}*: `{result.error[:300]}`"
        client.chat_postMessage(channel=channel, thread_ts=ts, text=msg)

    _bg(work)


@app.action("reject")
def on_reject(ack, body, client, action):  # noqa: ANN001
    ack()
    job_id = action["value"]
    Store().set_pending_status(job_id, "rejected", note="rejected in Slack")
    client.chat_postMessage(channel=body["channel"]["id"],
                            thread_ts=body["message"]["ts"],
                            text=f"❌ Rejected `{job_id}` — it will not be applied to.")


@app.action("edit_answers")
def on_edit(ack, body, client, action):  # noqa: ANN001
    ack()
    job_id = action["value"]
    doc = Store().get_pending(job_id) or {}
    blocking = [
        a for a in doc.get("answers", [])
        if a.get("provenance") == "llm" or not a.get("value")
    ]
    if not blocking:
        client.chat_postMessage(channel=body["channel"]["id"],
                                thread_ts=body["message"]["ts"],
                                text=f"`{job_id}` has no unanswered questions.")
        return

    blocks: list[dict] = []
    for i, a in enumerate(blocking[:8]):
        q = a.get("question_text", "")
        blocks.append({
            "type": "input", "block_id": f"q{i}",
            "label": {"type": "plain_text", "text": q[:150] or f"Question {i+1}"},
            "element": {"type": "plain_text_input", "action_id": "val",
                        "initial_value": a.get("value", "")[:200]},
            "hint": {"type": "plain_text",
                     "text": (a.get("reason") or "")[:140] or "your answer"},
        })
    client.views_open(trigger_id=body["trigger_id"], view={
        "type": "modal", "callback_id": "answers_submitted",
        "private_metadata": job_id,
        "title": {"type": "plain_text", "text": "Answer questions"},
        "submit": {"type": "plain_text", "text": "Save & Apply"},
        "blocks": blocks,
    })


@app.view("answers_submitted")
def on_answers(ack, body, view, client):  # noqa: ANN001
    ack()
    job_id = view["private_metadata"]
    store = Store()
    doc = store.get_pending(job_id) or {}
    values = view["state"]["values"]

    blocking = [a for a in doc.get("answers", [])
                if a.get("provenance") == "llm" or not a.get("value")]
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    updated = list(doc.get("answers", []))
    typed: list[tuple[str, str]] = []

    for i, a in enumerate(blocking[:8]):
        got = values.get(f"q{i}", {}).get("val", {}).get("value")
        if not got:
            continue
        typed.append((a.get("question_text", ""), got))
        for j, existing in enumerate(updated):
            if existing.get("question_text") == a.get("question_text"):
                updated[j] = {**existing, "value": got, "provenance": "human",
                              "evidence": f"answered by you in Slack at {now}",
                              "answered_at": now, "reason": ""}
                break

    store.set_pending_status(job_id, "approved", answers=updated)
    user = body["user"]["id"]

    def work() -> None:
        # Bank the new answers so the same question is silent next time. Banking is
        # refused for categories where retrieval is unsafe -- see answering.py.
        for q, val in typed:
            try:
                learn_from_human(store, q, val)
            except Exception as exc:  # noqa: BLE001
                log.warning("banking %r failed: %s", q[:50], exc)
        try:
            result = workflows.apply_approved(job_id)
            text = (f"✅ Submitted `{job_id}` with your answers."
                    if result.outcome is ApplyOutcome.SUBMITTED
                    else f"⚠️ `{job_id}`: *{result.outcome.value}* `{result.error[:250]}`")
        except Exception as exc:  # noqa: BLE001
            text = f"❌ `{job_id}` failed: `{exc}`"
        client.chat_postMessage(channel=user, text=text)

    _bg(work)


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
@app.command("/status")
def cmd_status(ack, respond):  # noqa: ANN001
    ack()
    s = Store()
    cfg = s.get_agent_config()
    subs, modals = s.cap_headroom()
    facts = s.get_facts()
    respond(
        f"*Agent status*\n"
        f"• paused: `{cfg.get('paused')}`  ·  dry_run: `{cfg.get('dry_run')}`\n"
        f"• submitted last 24h: *{s.submits_last_24h()}/{cfg['max_submits_24h']}* "
        f"(*{subs}* left)\n"
        f"• modal opens left: *{modals}/{cfg['max_modal_opens_24h']}*\n"
        f"• pending review: *{len(s.list_pending())}*\n"
        f"• question bank: *{s.bank_size()}* entries\n"
        f"• match threshold: `{cfg['match_score_threshold']}`  ·  "
        f"vector: `{cfg['vector_threshold']}`\n"
        f"• skill_years signed off: `{facts.skill_years_signed_off}`"
        + ("" if facts.skill_years_signed_off else
           "  ⚠️ _years questions all route to you until this is true_")
    )


@app.command("/pause")
def cmd_pause(ack, respond):  # noqa: ANN001
    ack()
    Store().set_paused(True, reason="paused from Slack")
    respond("🛑 Paused. No further applications until `/resume`.")


@app.command("/resume")
def cmd_resume(ack, respond):  # noqa: ANN001
    ack()
    Store().set_paused(False, reason="resumed from Slack")
    respond("▶️ Resumed.")


@app.command("/apply_now")
def cmd_apply_now(ack, respond):  # noqa: ANN001
    ack()
    respond("⏳ Starting a run now…")

    def work() -> None:
        summary = workflows.run_workflow_a()
        respond(f"Run finished: `{summary}`")

    _bg(work)


@app.command("/update_cookie")
def cmd_update_cookie(ack, respond, command):  # noqa: ANN001
    ack()
    value = (command.get("text") or "").strip()
    if len(value) < 20:
        respond("Paste the `li_at` value: `/update_cookie AQEDA…`")
        return
    try:
        from google.cloud import secretmanager

        sm = secretmanager.SecretManagerServiceClient()
        sm.add_secret_version(request={
            "parent": f"projects/{config.PROJECT_ID}/secrets/linkedin-li-at",
            "payload": {"data": value.encode()},
        })
        Store().set_paused(False, reason="cookie updated from Slack")
        respond("🔐 Cookie stored and agent un-paused.\n"
                "_Note: the browser profile may still need one interactive login "
                "if LinkedIn issued a challenge — `/status` will show if runs keep failing._")
    except Exception as exc:  # noqa: BLE001
        respond(f"❌ Could not store the cookie: `{exc}`")


@app.command("/set_resume")
def cmd_set_resume(ack, respond, command):  # noqa: ANN001
    ack()
    name = (command.get("text") or "").strip()
    if not name:
        respond("Give the exact filename as LinkedIn shows it: "
                "`/set_resume Rajiv_Ranjan_Jha_Resume.pdf`")
        return
    Store().db.collection("user_profile").document("me").set(
        {"resume_linkedin_filename": name}, merge=True)
    respond(f"📄 Will now select `{name}` in the Easy Apply resume picker.")


@app.command("/update_resume")
def cmd_update_resume(ack, respond, command):  # noqa: ANN001
    ack()
    text = (command.get("text") or "").strip()
    if len(text) < 100:
        respond("Paste the full resume text after the command (min 100 chars). "
                "This text is used for match scoring, not for the uploaded file.")
        return
    Store().db.collection("user_profile").document("me").set(
        {"resume_text": text, "updated_at": dt.datetime.now(dt.timezone.utc)}, merge=True)
    respond(f"📝 Resume text updated ({len(text)} chars).")


@app.command("/add_location")
def cmd_add_location(ack, respond, command):  # noqa: ANN001
    ack()
    loc = (command.get("text") or "").strip()
    if not loc:
        respond("Usage: `/add_location Pune, Maharashtra, India`")
        return
    s = Store()
    cfg = s.get_search_config()
    locs = list(dict.fromkeys([*cfg.get("locations", []), loc]))
    s.db.collection("search_config").document("me").set({"locations": locs}, merge=True)
    respond(f"📍 Locations now: `{locs}`")


@app.command("/set_threshold")
def cmd_set_threshold(ack, respond, command):  # noqa: ANN001
    ack()
    parts = (command.get("text") or "").split()
    if len(parts) != 2:
        respond("Usage: `/set_threshold match 70` or `/set_threshold vector 0.85`")
        return
    kind, raw = parts[0].lower(), parts[1]
    s = Store()
    try:
        if kind.startswith("match"):
            v = max(0, min(100, int(float(raw))))
            s.set_agent_config(match_score_threshold=v)
            respond(f"🎯 Match threshold = `{v}`")
        elif kind.startswith("vector"):
            v = max(0.0, min(1.0, float(raw)))
            s.set_agent_config(vector_threshold=v)
            respond(f"📚 Vector similarity threshold = `{v}` "
                    f"(Firestore distance ceiling `{1 - v:.2f}`)")
        else:
            respond("First argument must be `match` or `vector`.")
    except ValueError:
        respond(f"`{raw}` is not a number.")


# --------------------------------------------------------------------------- #
def start() -> None:
    log.info("starting Slack Socket Mode listener (channel %s)", config.SLACK_CHANNEL)
    SocketModeHandler(app, config.secret("slack-app-token")).start()
