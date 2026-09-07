# Everything I need from you — complete list, one go

Short version: **2 Slack tokens + 1 LinkedIn login + 1 channel name.** That's the whole list.
Everything else I already have or have already created myself.

---

## 1. Slack — 2 tokens (the only things you need to paste)

### Why not a Slack MCP server?

You asked whether I'd prefer Slack MCP. **No — it's the wrong tool here, not a preference.**

An MCP server lets a *client* call *out* to Slack: send a message, list channels. What this
agent needs is the **opposite direction** — a long-lived process that **receives** events
*from* Slack: `block_actions` payloads when you click Approve/Reject, and slash commands.
MCP has no mechanism for inbound push; it is request/response initiated by the client.

`slack-bolt` in **Socket Mode** is the right choice, and it has a second benefit that shaped
the whole architecture: Socket Mode opens an **outbound WebSocket**, so nothing needs to be
publicly reachable. That is what let us delete the Cloud Run webhook service entirely.

### Steps (about 3 minutes)

1. Go to **https://api.slack.com/apps** → **Create New App** → **From an app manifest**
2. Pick your workspace → choose **YAML** → paste all of `setup/slack_app_manifest.yaml`
   → **Next** → **Create**
3. **Basic Information** → *App-Level Tokens* → **Generate Token and Scopes**
   - Name: `socket`  · Scope: **`connections:write`** · Generate
   - Copy the **`xapp-…`** token → **THIS IS TOKEN 1**
4. **OAuth & Permissions** → **Install to Workspace** → Allow
   - Copy the **Bot User OAuth Token** **`xoxb-…`** → **THIS IS TOKEN 2**
5. In Slack, create a **private channel** `#job-agent`, then run `/invite @Job Agent` in it.

Then paste both tokens to me. I store them in Secret Manager (`slack-bot-token`,
`slack-app-token`) — placeholders already exist and I'll overwrite them.

> Both tokens are already gitignored everywhere. Paste them in chat; don't put them in a file.

**Scopes requested and why** (least privilege — nothing beyond this):

| Scope | Used for |
|---|---|
| `chat:write` | post the digest and alerts |
| `commands` | receive the 9 slash commands |
| `groups:read` | resolve the private `#job-agent` channel ID |
| `files:write` | upload the 9 PM XLSX directly |
| `reactions:write` | tick a card once handled |
| `im:write` | DM you on auth failure (high priority) |

---

## 2. LinkedIn — no credential to paste

You do **not** send me your password or your `li_at` cookie. Instead, one interactive login:

```bash
.venv/Scripts/python.exe -m setup.bootstrap_session
```

This opens a real Chromium window (non-headless). **You** log in by hand — so 2FA and any
security challenge is handled by a human, never bypassed or automated. The session is then
saved to `.linkedin-mcp/` (gitignored) and reused headlessly from then on.

Because LinkedIn extends `li_at` on use and the agent runs every 2 hours, this **self-renews
indefinitely**. You should not need to touch it again unless you change your password or
LinkedIn issues a challenge — and if that happens, Workflow D halts everything and DMs you
recovery steps, with `/update_cookie` as the manual fallback.

---

## 3. Google Cloud — nothing needed, already done

| Item | Status |
|---|---|
| Service account (`roles/owner`) | ✅ you already provided `credentials.json` |
| Firestore `(default)` Native, `asia-south1` | ✅ created — claims the one free-tier DB |
| GCS `master-rajiv-linkedin-agent-exports`, `us-central1` | ✅ created, 30-day lifecycle |
| Vector index `question_bank.question_embedding` (1536, flat) | ✅ created |
| `apikeys.googleapis.com` | ✅ enabled by me (this was blocking) |
| Gemini API key, restricted to `generativelanguage` | ✅ minted by me, **validated** |
| Secret Manager: `gemini-api-key` | ✅ real value stored |
| Secret Manager: `slack-bot-token`, `slack-app-token`, `linkedin-li-at` | ⏳ placeholders awaiting §1 |

Your old `.env` had a `GOOGLE_API_KEY` that returned **403** — unusable. I minted a fresh key
scoped to only the Generative Language API rather than reusing it. Your existing `.env` was
left untouched; it belongs to a different project (Groq, Langfuse, pilot UI).

---

## 4. Anything else? No.

- **GitHub** — already configured, nothing needed.
- **Proxy** — not needed; Shape B runs on your home IP by design.
- **Vertex AI** — deliberately unused (no free tier for Gemini; AI Studio has one).
- **Cloud Run / Scheduler / Pub/Sub** — deliberately unused (see `SPEC.md` §0).

---

## 5. One decision left

Confirm the digest channel name. I've assumed **`#job-agent`** (private). Say the word if you
want something else, or a DM instead.

---

## 6. What happens after you paste the tokens

| Phase | Deliverable | Needs you? |
|---|---|---|
| 0 | Probe the pinned MCP server, dump real tool schemas + payloads | LinkedIn login |
| 1 | Firestore seed + deterministic answer engine + unit tests | no |
| 2 | Patchright Easy Apply state machine — **dry-run only, never submits** | no |
| 3 | Scoring + vector question bank | no |
| 4 | Slack Socket Mode, Block Kit cards, slash commands | Slack tokens |
| 5 | 9 PM export + Task Scheduler registration | no |
| 6 | **Streamlit monitoring dashboard** | no |
| 7 | **Live test: 5 real applications** | explicit go-ahead |

### On the 5-application test

I'll do this in two stages, because a submitted application is irreversible and goes to a real
employer under your name:

1. **Dry run** — the full pipeline runs against real job postings, opens real Easy Apply
   modals, reads real screening questions, computes every answer with its provenance, and
   **stops at the Submit button**. I show you exactly what would have been sent for all 5.
2. **Live** — only after you look at that output and say go. `max_submits_24h` is temporarily
   set to **5** so the cap itself makes overshoot impossible.

Stage 1 is where selector bugs and wrong answers surface, and it costs nothing to get wrong.
