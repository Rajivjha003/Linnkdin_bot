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

from agent import config, feedback, slack_notify, workflows
from agent.answering import learn_from_human, learn_salary_fact
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
def _already_acted(store, job_id: str) -> str | None:
    """Return the existing verdict if this job has already been dealt with."""
    doc = store.get_pending(job_id) or {}
    status = doc.get("status")
    if status in ("applied", "rejected", "in_progress"):
        return status
    return None


def _replace_card(client, body, job_id: str, verdict: str, detail: str = "") -> None:
    """Swap the clicked card for a resolved one, so the buttons disappear."""
    try:
        channel = body["channel"]["id"]
        ts = body["message"]["ts"]
        doc = Store().get_pending(job_id) or {"job_id": job_id}
        blocks = slack_notify.resolved_card(doc, verdict, detail)
        client.chat_update(channel=channel, ts=ts, blocks=blocks,
                           text=f"{verdict}: {doc.get('title', job_id)}")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not replace card for %s: %s", job_id, exc)


@app.action("approve")
def on_approve(ack, body, client, action):  # noqa: ANN001
    ack()
    job_id = action["value"]
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]
    store = Store()

    prior = _already_acted(store, job_id)
    if prior:
        _replace_card(client, body, job_id, prior,
                      "already handled — this click was ignored")
        return

    # Claim it in the UI immediately: the buttons vanish before the browser even
    # starts, which is what stops a second click from starting a second apply.
    store.set_pending_status(job_id, "in_progress")
    _replace_card(client, body, job_id, "in_progress", "applying now…")

    def work() -> None:
        try:
            result = workflows.apply_approved(job_id)
        except Exception as exc:  # noqa: BLE001
            store.set_pending_status(job_id, "pending")
            _replace_card(client, body, job_id, "failed", f"`{exc}`"[:200])
            return
        if result.outcome is ApplyOutcome.SUBMITTED:
            _replace_card(client, body, job_id, "applied",
                          "every answer was deterministic or human-supplied")
        elif result.outcome is ApplyOutcome.SKIPPED_CAP:
            store.set_pending_status(job_id, "pending")
            _replace_card(client, body, job_id, "pending",
                          "24h cap reached — still queued, will retry")
        elif result.outcome is ApplyOutcome.SKIPPED_DUPLICATE:
            _replace_card(client, body, job_id, "in_progress",
                          "already being applied to — duplicate ignored")
        else:
            store.set_pending_status(job_id, "pending")
            _replace_card(client, body, job_id, "failed",
                          f"{result.outcome.value}: `{result.error[:200]}`")

    _bg(work)


@app.action("reject")
def on_reject(ack, body, client, action):  # noqa: ANN001
    ack()
    job_id = action["value"]
    store = Store()
    prior = _already_acted(store, job_id)
    if prior:
        _replace_card(client, body, job_id, prior,
                      "already handled — this click was ignored")
        return
    store.set_pending_status(job_id, "rejected", note="rejected in Slack")
    _replace_card(client, body, job_id, "rejected", "will not be applied to")


@app.action("reject_reason")
def on_reject_reason(ack, body, client, action):  # noqa: ANN001
    """Reject, and record WHY so the filter can tune itself."""
    ack()
    raw = (action.get("selected_option") or {}).get("value", "")
    job_id, _, reason = raw.partition("|")
    if not job_id:
        return
    store = Store()
    prior = _already_acted(store, job_id)
    if prior:
        _replace_card(client, body, job_id, prior,
                      "already handled — this click was ignored")
        return

    doc = store.get_pending(job_id) or {}
    store.set_pending_status(job_id, "rejected", note=f"rejected: {reason}")
    try:
        feedback.record_rejection(store, job_id, reason, doc)
        analysis = feedback.analyse(store)
        applied = feedback.apply_auto(store, analysis)
    except Exception as exc:  # noqa: BLE001
        log.warning("feedback failed for %s: %s", job_id, exc)
        applied = []

    label = feedback.REASONS.get(reason, reason)
    detail = f"reason: {label}"
    if applied:
        # Say what it learned, so the tuning is never invisible.
        learned = ", ".join(f"`{p['value']}`" for p in applied)
        detail += f" · now filtering {learned} from future runs"
    _replace_card(client, body, job_id, "rejected", detail)


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
    # private_metadata carries where the card lives, so submitting the modal can
    # rewrite that exact message rather than leaving stale buttons behind.
    meta = f"{job_id}|{body['channel']['id']}|{body['message']['ts']}"
    client.views_open(trigger_id=body["trigger_id"], view={
        "type": "modal", "callback_id": "answers_submitted",
        "private_metadata": meta,
        "title": {"type": "plain_text", "text": "Answer questions"},
        "submit": {"type": "plain_text", "text": "Save & Apply"},
        "blocks": blocks,
    })


@app.view("answers_submitted")
def on_answers(ack, body, view, client):  # noqa: ANN001
    ack()
    meta = (view["private_metadata"] or "").split("|")
    job_id = meta[0]
    card_channel = meta[1] if len(meta) > 1 else None
    card_ts = meta[2] if len(meta) > 2 else None
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

    store.set_pending_status(job_id, "in_progress", answers=updated)
    user = body["user"]["id"]

    # Replace the card straight away so the buttons are gone while we work.
    if card_channel and card_ts:
        doc = store.get_pending(job_id) or {"job_id": job_id}
        slack_notify.update_card(
            card_channel, card_ts,
            slack_notify.resolved_card(doc, "answered",
                                       f"{len(typed)} answer(s) saved — applying now…"),
            f"answered: {doc.get('title', job_id)}")

    def work() -> None:
        # Bank the new answers so the same question is silent next time. Banking is
        # refused for categories where retrieval is unsafe -- see answering.py.
        for q, val in typed:
            try:
                # Salary goes to user_facts (a fact), everything else to the
                # vector bank (a reusable answer). Salary must not be retrieved:
                # "current" and "expected" look almost identical to an embedding.
                field = learn_salary_fact(store, q, val)
                if field:
                    log.info("stored %s from your answer; salary will not be "
                             "asked again", field)
                else:
                    learn_from_human(store, q, val)
            except Exception as exc:  # noqa: BLE001
                log.warning("learning from %r failed: %s", q[:50], exc)
        try:
            result = workflows.apply_approved(job_id)
            ok = result.outcome is ApplyOutcome.SUBMITTED
            verdict = "applied" if ok else "failed"
            detail = ("submitted with your answers, which are now saved"
                      if ok else f"{result.outcome.value}: `{result.error[:180]}`")
            if not ok:
                store.set_pending_status(job_id, "pending")
        except Exception as exc:  # noqa: BLE001
            verdict, detail = "failed", f"`{exc}`"[:200]
            store.set_pending_status(job_id, "pending")
        if card_channel and card_ts:
            doc = store.get_pending(job_id) or {"job_id": job_id}
            slack_notify.update_card(
                card_channel, card_ts,
                slack_notify.resolved_card(doc, verdict, detail),
                f"{verdict}: {doc.get('title', job_id)}")
        client.chat_postMessage(channel=user,
                                text=f"{job_id}: {verdict} — {detail}")

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
