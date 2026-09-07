"""Monitoring dashboard. Reads Firestore; writes only the two safety toggles.

Run with:  .venv/Scripts/streamlit.exe run dashboard/streamlit_app.py

Chart choices follow the form heuristic rather than habit:
  * The 24h cap is a single headline, so it is a stat tile, not a gauge. A gauge
    spends a lot of pixels on one number and reads less precisely than the number.
  * Answer provenance is four discrete identities, so it is a categorical bar
    chart. An earlier attempt used a sequential blue ramp -- the validator failed
    it (adjacent normal-vision dE 14.4, below the 15 floor), which is the classic
    sequential-misused-as-categorical error.
  * Question-bank growth is one series over time, so it is a line with no legend;
    the title names the series.

Palettes below are the validated categorical slots in fixed order, with the
reserved status-critical red for `llm` because that provenance is not a peer
series -- it is the state that blocks submission. Every category also carries an
emoji and a direct value label, which is the secondary encoding required by the
validator's contrast WARN (light) and CVD WARN (dark).
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd
import streamlit as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.store import Store  # noqa: E402

st.set_page_config(page_title="Job Agent", page_icon="🎯", layout="wide")

# Validated palettes -- see module docstring. Fixed order, never cycled.
PROV_LIGHT = {"deterministic": "#2a78d6", "bank_match": "#eb6834",
              "human": "#1baf7a", "llm": "#d03b3b"}
PROV_DARK = {"deterministic": "#3987e5", "bank_match": "#d95926",
             "human": "#199e70", "llm": "#d03b3b"}
PROV_ICON = {"deterministic": "🔒", "bank_match": "📚", "human": "🙋", "llm": "⚠️"}
PROV_ORDER = ["deterministic", "bank_match", "human", "llm"]
SEQ_BLUE = "#2a78d6"


def palette() -> dict[str, str]:
    try:
        return PROV_DARK if st.context.theme.type == "dark" else PROV_LIGHT
    except Exception:  # noqa: BLE001
        return PROV_LIGHT


@st.cache_resource
def store() -> Store:
    return Store()


@st.cache_data(ttl="60s")
def load_state() -> dict:
    s = store()
    cfg = s.get_agent_config()
    facts = s.get_facts()
    subs_left, modals_left = s.cap_headroom()
    return {
        "cfg": cfg,
        "submitted_24h": s.submits_last_24h(),
        "subs_left": subs_left,
        "modals_left": modals_left,
        "pending": s.list_pending(limit=50),
        "bank_size": s.bank_size(),
        "signed_off": facts.skill_years_signed_off,
        "skill_years": facts.skill_years,
        "resume_file": s.get_profile().get("resume_linkedin_filename", ""),
    }


@st.cache_data(ttl="60s")
def load_jobs(hours: int) -> list[dict]:
    return store().recent_jobs(hours=hours, limit=300)


@st.cache_data(ttl="60s")
def load_traces() -> list[dict]:
    return store().recent_traces(limit=25)


@st.cache_data(ttl="120s")
def load_bank() -> list[dict]:
    db = store().db
    return [d.to_dict() | {"_id": d.id} for d in db.collection("question_bank").stream()]


state = load_state()
cfg = state["cfg"]

# --------------------------------------------------------------------------- #
# Header + the two controls that actually change behaviour
# --------------------------------------------------------------------------- #
left, right = st.columns([3, 2], vertical_alignment="center")
with left:
    st.title("🎯 Job Agent")
    st.caption(f"resume selected in Easy Apply: `{state['resume_file'] or 'not set'}`")
with right:
    paused = st.toggle("⏸️ Paused", value=bool(cfg.get("paused")),
                       help="Halts all applications immediately.")
    dry = st.toggle("🧪 Dry run", value=bool(cfg.get("dry_run", True)),
                    help="Opens modals and computes answers but never clicks Submit.")
    if paused != bool(cfg.get("paused")):
        store().set_paused(paused, reason="toggled from dashboard")
        load_state.clear()
        st.rerun()
    if dry != bool(cfg.get("dry_run", True)):
        store().set_agent_config(dry_run=dry)
        load_state.clear()
        st.rerun()

if cfg.get("paused"):
    st.error(f"Agent is paused — {cfg.get('pause_reason', 'no reason recorded')}", icon="🛑")
if not state["signed_off"]:
    st.warning(
        "`skill_years` is not signed off, so **every** years-of-experience question "
        "routes to you for review instead of being answered. Review the table below "
        "and sign off to enable automatic answers.",
        icon="⚠️",
    )

# --------------------------------------------------------------------------- #
# KPI row -- stat tiles, not gauges
# --------------------------------------------------------------------------- #
jobs_24h = load_jobs(24)
submitted = [j for j in jobs_24h if j.get("status") == "submitted"]

with st.container(horizontal=True):
    st.metric("Submitted (24h)",
              f"{state['submitted_24h']}/{cfg['max_submits_24h']}",
              f"{state['subs_left']} left", border=True, delta_color="off")
    st.metric("Modal opens left", state["modals_left"],
              f"cap {cfg['max_modal_opens_24h']}", border=True, delta_color="off")
    st.metric("Awaiting review", len(state["pending"]), border=True)
    st.metric("Question bank", state["bank_size"],
              "quieter as it grows", border=True, delta_color="off")

st.divider()

# --------------------------------------------------------------------------- #
# Provenance + bank growth
# --------------------------------------------------------------------------- #
col_a, col_b = st.columns(2)

with col_a:
    with st.container(border=True):
        st.subheader("How answers were sourced")
        st.caption("Only 🔒 / 📚 / 🙋 may auto-submit. A single ⚠️ forces human review.")
        counts: dict[str, int] = {k: 0 for k in PROV_ORDER}
        for j in jobs_24h:
            for a in j.get("answers", []) or []:
                p = a.get("provenance")
                if p in counts:
                    counts[p] += 1
        total = sum(counts.values())
        if total == 0:
            st.info("No answers recorded yet. Run the agent to populate this.")
        else:
            pal = palette()
            # One column per provenance so each carries its own validated hue and
            # appears in the legend; stack=False gives four grouped bars rather
            # than one stacked bar, which reads better for four independent counts.
            wide = pd.DataFrame([{f"{PROV_ICON[k]} {k}": counts[k] for k in PROV_ORDER}])
            st.bar_chart(
                wide,
                color=[pal[k] for k in PROV_ORDER],
                horizontal=True, stack=False, height=200,
            )
            # Direct value labels: the relief the contrast WARN requires, and the
            # secondary encoding the dark-mode CVD WARN requires.
            st.dataframe(
                pd.DataFrame({
                    "": [PROV_ICON[k] for k in PROV_ORDER],
                    "provenance": PROV_ORDER,
                    "answers": [counts[k] for k in PROV_ORDER],
                    "share": [f"{counts[k]/total:.0%}" for k in PROV_ORDER],
                    "may auto-submit": ["yes" if k != "llm" else "no" for k in PROV_ORDER],
                }),
                hide_index=True, width="stretch",
            )

with col_b:
    with st.container(border=True):
        st.subheader("Question bank growth")
        st.caption("Each banked answer is one fewer Slack interruption later.")
        bank = load_bank()
        if not bank:
            st.info("Bank is empty. It fills as you answer questions in Slack.")
        else:
            ts = [b.get("created_at") for b in bank if b.get("created_at")]
            if ts:
                s = pd.Series(1, index=pd.to_datetime(pd.Series(ts), utc=True))
                growth = s.resample("D").sum().cumsum().rename("entries")
                st.line_chart(growth, color=SEQ_BLUE, height=240)
            cats = pd.Series([b.get("category", "?") for b in bank]).value_counts()
            st.dataframe(cats.rename("entries").reset_index(names="category"),
                         hide_index=True, width="stretch")

# --------------------------------------------------------------------------- #
# Pending review
# --------------------------------------------------------------------------- #
with st.container(border=True):
    st.subheader(f"Awaiting your review ({len(state['pending'])})")
    if not state["pending"]:
        st.success("Nothing waiting. Approve/reject happens in Slack.", icon="✅")
    else:
        for job in state["pending"]:
            answers = job.get("answers", []) or []
            blocking = [a for a in answers
                        if a.get("provenance") == "llm" or not a.get("value")]
            title = f"{job.get('title', '?')} — {job.get('company', '?')}"
            badge = f" · ⚠️ {len(blocking)} unanswered" if blocking else " · ✅ ready"
            with st.expander(f"{title}  ·  match {job.get('match_score', '-')}/100{badge}"):
                st.markdown(f"[open on LinkedIn]({job.get('url', '')})")
                if answers:
                    st.dataframe(pd.DataFrame([{
                        "": PROV_ICON.get(a.get("provenance", ""), ""),
                        "question": (a.get("question_text") or "")[:90],
                        "answer": a.get("value") or "— needs you —",
                        "provenance": a.get("provenance"),
                        "category": a.get("category"),
                        "why blocked": (a.get("reason") or "")[:70],
                    } for a in answers]), hide_index=True, width="stretch")
                else:
                    st.caption("No screening questions were captured.")

# --------------------------------------------------------------------------- #
# Recent runs
# --------------------------------------------------------------------------- #
with st.container(border=True):
    st.subheader("Recent runs")
    traces = load_traces()
    if not traces:
        st.info("No runs recorded yet.")
    else:
        st.dataframe(pd.DataFrame([{
            "run": (t.get("run_id") or "")[:28],
            "when": t.get("created_at"),
            "dry": (t.get("summary") or {}).get("dry_run"),
            "searched": (t.get("summary") or {}).get("searched"),
            "scored": (t.get("summary") or {}).get("scored"),
            "modals": (t.get("summary") or {}).get("modals"),
            "submitted": (t.get("summary") or {}).get("submitted"),
            "queued": (t.get("summary") or {}).get("queued"),
            "failed": (t.get("summary") or {}).get("failed"),
            "aborted": (t.get("summary") or {}).get("aborted") or "",
        } for t in traces]), hide_index=True, width="stretch")

# --------------------------------------------------------------------------- #
# skill_years sign-off
# --------------------------------------------------------------------------- #
with st.container(border=True):
    st.subheader("Years-of-experience fact table")
    st.caption(
        "Derived from employment dates, never estimated. This table answers every "
        "'how many years of X?' question; a skill absent from it is always routed "
        "to you rather than guessed from a neighbouring skill."
    )
    sy = state["skill_years"]
    st.dataframe(
        pd.DataFrame(sorted(sy.items(), key=lambda kv: (-kv[1], kv[0])),
                     columns=["skill", "years"]),
        hide_index=True, width="stretch", height=300,
    )
    if state["signed_off"]:
        st.success("Signed off — years questions are answered automatically.", icon="✅")
        if st.button("Revoke sign-off"):
            store().db.collection("user_facts").document("me").set(
                {"skill_years_signed_off": False}, merge=True)
            load_state.clear()
            st.rerun()
    else:
        st.warning("Not signed off — years questions all route to you.", icon="⚠️")
        if st.button("✅ I confirm these numbers are correct", type="primary"):
            store().db.collection("user_facts").document("me").set(
                {"skill_years_signed_off": True}, merge=True)
            load_state.clear()
            st.rerun()

st.caption("Reads Firestore directly · caches for 60s · $0 to run")
