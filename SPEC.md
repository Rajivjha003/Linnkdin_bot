# LinkedIn Easy Apply Agent — Locked Specification

Project: `master-rajiv` · Owner: Rajiv Ranjan Jha · Spec date: 2026-09-07

---

## 0. What changed from the original architecture doc, and why

The original doc rested on four assumptions that turned out to be false. Each was verified,
not assumed.

| Original assumption | Reality | Consequence |
|---|---|---|
| MCP server has an `apply_tool`; Playwright is a fallback | **No LinkedIn MCP server can submit an application.** `stickerdaniel/linkedin-mcp-server` exposes only `search_jobs`, `get_job_details`, `get_saved_jobs`. Maintainer rejected the feature outright (Discussion #175: *"Won't implement this, automating applications will get accounts banned."*) | **We own the apply engine.** MCP = search + scrape only. Patchright = primary apply path. |
| "MCP-First avoids browser fingerprinting" | The MCP server **is** a browser — it drives Patchright Chromium against real linkedin.com. There is no API path. | Comparison is *stealth* browser vs *vanilla* browser. So: **Patchright everywhere, vanilla Playwright dropped.** |
| Agent fetches screening questions via MCP | Screening questions exist **only inside the Easy Apply modal**, behind the click. Unreachable by any scrape tool. | Forces the one-pass opportunistic flow (§3). |
| ADK pauses natively awaiting Slack, resumes via endpoint | An in-process pause cannot survive Cloud Run scale-to-zero; the container and all in-memory state are reclaimed at request end. | **No pausing.** Firestore is the only state; every invocation is stateless and idempotent. |

Two further changes made for cost and safety:

- **Shape B (local agent, cloud state).** Because the browser must run on a residential IP
  (§2), and Slack Socket Mode uses an *outbound* WebSocket, nothing needs a public endpoint.
  Cloud Run disappears entirely. This also deletes the whole cookie-rotation problem — the
  session persists naturally on local disk.
- **No Cloud Scheduler / Pub/Sub.** If the PC is off for 6 hours, Pub/Sub retains 6 trigger
  messages and delivers them *all at once* on reconnect → burst of applications → the exact
  pattern that flags accounts. **Windows Task Scheduler** only fires when awake, no backlog.

---

## 1. Architecture (Shape B)

```text
                    ┌─────────────────── LOCAL (Windows, residential IP) ───────────────────┐
                    │                                                                       │
[Task Scheduler] ──▶│  ADK Agent  ──▶ mcp-server-linkedin 4.23.3 (stdio) ──▶ LinkedIn        │
   every 2h         │      │                    (Patchright Chromium: search + JD scrape)    │
                    │      │                                                                 │
                    │      └──────────▶ Patchright apply engine ─────────────▶ LinkedIn      │
                    │      │                (Easy Apply modal state machine)                 │
                    │      │                                                                 │
                    │      └──────────▶ Slack Bolt (Socket Mode, outbound WS) ◀──▶ Slack     │
                    └──────┬────────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┬─────────────────────┐
        ▼                  ▼                  ▼                     ▼
  [Firestore]         [GCS bucket]     [Secret Manager]     [Vertex AI]
  asia-south1         us-central1      slack-bot-token      location=global
  state + vectors     exports, 30d TTL slack-app-token      2.5-flash / 2.5-pro
  FREE TIER           FREE TIER        linkedin-li-at       $300 credit
```

**Cost:** Firestore free tier (1 GiB, 50k reads/20k writes per day — we use ~200 writes/day).
GCS free tier (5 GiB in us-central1, 30-day lifecycle). Secret Manager (6 free versions).
**Vertex AI ≈ $2–7/month** against the $300 credit — roughly 4+ years of runway.

> **Correction to an earlier decision.** I originally chose the Gemini **AI Studio** free tier
> over Vertex AI. Tested against the real project, AI Studio returns
> `429 RESOURCE_EXHAUSTED: Your prepayment credits are depleted` — because `master-rajiv` has
> billing enabled, AI Studio treats it as a prepay project with **no free tier to fall back
> on**. Vertex AI works with the existing service account at `location=global` (verified:
> `gemini-2.5-flash` responded, 5 in / 1 out tokens) and draws on the **$300 credit you
> already have**. So Vertex is the correct choice *for this project* — the reverse of the
> general rule. No API key is needed; auth is the service account. The `gemini-api-key` secret
> remains provisioned but unused, as a fallback if you ever add AI Studio credits.

---

## 2. Account-safety posture (Priority 1)

| Control | Mechanism | Enforced by |
|---|---|---|
| Residential IP | Browser runs on home ISP, never a GCP datacenter ASN | Shape B |
| Rolling 24h cap | ≤ **20 confirmed submits** in any trailing 24h from `now` | Firestore count query, code |
| Modal-open cap | ≤ **40 modal opens** / 24h — a failure storm looks more bot-like than success | Code |
| No trigger backlog | Task Scheduler, not Pub/Sub | §0 |
| Auth-failure halt | 401/403/challenge → immediate stop, all applies abandoned, Slack alert | Code |
| Stealth browser | Patchright (patched Chromium) for both scrape and apply | Dependency choice |
| Human jitter | Randomised inter-action delays and run offsets | Apply engine |

---

## 3. Workflow A — the one-pass opportunistic run (every 2h)

```
1. CAP CHECK      Firestore: count applied_jobs where submitted_at > now-24h
                  → if >= 20, abort silently. Integer compare, never an LLM.
2. LOAD CONFIG    search_config + user_profile + user_facts + agent_config
3. SEARCH         MCP search_jobs(hard filters, easy_apply=true)   [no LLM]
4. DEDUP          drop job_ids already in applied_jobs or pending_review  [set membership]
5. PRE-FILTER     deterministic title + must-have-skill match       [no LLM]
6. SCORE          Gemini 2.5 Flash on the BORDERLINE set only, structured output + evidence
7. OPEN MODAL     Patchright: click Easy Apply, read screening questions
8. ANSWER         per question, in strict order:
                     a. blocklist category?          -> provenance=human      (§6)
                     b. deterministic fact lookup?   -> provenance=deterministic
                     c. vector match >= 0.80?        -> provenance=bank_match
                     d. otherwise                    -> provenance=llm
9. TWO-KEY GATE   auto-submit IFF score >= threshold
                          AND every provenance in {deterministic, bank_match, human}
                  -> PASS: select saved resume, submit, log to applied_jobs
                  -> FAIL: ABANDON modal (discard, never submit), queue to pending_review
10. DIGEST        one batched Slack message, Block Kit card per queued job
```

Step 9 is the core invariant: **no LLM-authored answer is ever auto-submitted.**

**Workflow B (Slack approve)** — Socket Mode handler updates `pending_review`, then executes
the apply inline (same process). On approve, newly human-answered Q&A pairs are generalized
by Gemini 2.5 Pro, embedded, and upserted to `question_bank` — so the bot gets quieter over
time (Priority 4).

**Workflow C (21:00 IST)** — gather last-24h JDs, Gemini 2.5 Pro gap analysis vs `resume_text`,
write XLSX to GCS, Slack message with a 1-hour signed URL.

**Workflow D (auth failure)** — halt, Slack alert with `li_at` recovery steps, `/update_cookie`.

---

## 4. Firestore schema (exact)

Database: `(default)`, Native mode, `asia-south1`.

### `user_profile/me`
| field | type | value |
|---|---|---|
| `resume_text` | string | extracted from `Resume/Rajiv_Ranjan_Jha_Resume.pdf` |
| `resume_linkedin_filename` | string | `Rajiv_Ranjan_Jha_Resume.pdf` — label matched in LinkedIn's picker; change via `/set_resume` |
| `updated_at` | timestamp | |

### `user_facts/me` — the deterministic answer table (replaces the LLM on all numerics)
| field | type | value | source |
|---|---|---|---|
| `full_name` | string | Rajiv Ranjan Jha | resume |
| `email` | string | rajiv.jha.0003@gmail.com | resume |
| `phone` | string | 8825330125 | resume |
| `city` | string | Bengaluru, India | resume |
| `linkedin_url` | string | linkedin.com/in/rajivranjan-jha | resume |
| `total_years_experience` | number | **4.2** (Jul 2022 → Sep 2026) | derived from dates |
| `work_auth_india` | bool | `true` — authorized, no sponsorship | **you** |
| `requires_sponsorship_india` | bool | `false` | **you** |
| `notice_period_days` | number | **15** (immediate joiner) | **you** |
| `immediate_joiner` | bool | `true` | **you** |
| `current_ctc_lpa` | number | **9.6** (₹80k/mo in hand) | **you** |
| `expected_ctc_lpa` | number | **20** (floor 16; you said >13, market says anchor higher) | **you** + market |
| `willing_to_relocate` | bool | `true` | **you** |
| `relocate_scope` | string | `anywhere_in_india` | **you** |
| `skill_years` | map | see below — **total 4y CONFIRMED by you 2026-09-07** | derived from role dates |

`skill_years` derived strictly from employment dates, never guessed:
```
python: 4.2          machine_learning: 4.2      data_science: 4.2
genai_llm: 2.0       rag: 2.0                   google_adk: 2.0
mcp: 2.0             langchain_agents: 2.0      fine_tuning_peft: 2.0
time_series: 2.0     lightgbm: 2.0              bigquery: 2.0
sql: 4.2             fastapi: 2.0               django: 2.0
docker: 2.0          gcp_cloud_run: 2.0         computer_vision_yolo: 2.2
pytorch: 2.0         react_nextjs: 1.0
```
**Rule:** a years-question for a skill *absent* from this map routes to **human review**.
It is never inferred, never estimated, never sent to an LLM.

### `search_config/me`
```
titles:            [AI Engineer, GenAI Engineer, ML Engineer,
                    Senior Data Scientist, LLM Engineer]
locations:         [India (Remote), Bengaluru]
experience_levels: [Mid-Senior level]
date_posted:       past_24_hours
easy_apply_only:   true
remote_scope:      india_only
```

### `agent_config/me`
```
vector_threshold:      0.80   # cosine SIMILARITY. Firestore returns DISTANCE => 0.20
match_score_threshold: 70     # 0-100 from Gemini scoring
max_submits_24h:       20
max_modal_opens_24h:   40
paused:                false  # set true by Workflow D
digest_hour_ist:       21
```

### `applied_jobs/{job_id}` — doc ID **is** the LinkedIn job ID (idempotency key)
`job_id, title, company, location, jd_text, match_score, submitted_at, status
(submitted|abandoned|failed), answers[] (each with question/answer/provenance), run_id, error`

Cap query counts `status == "submitted"` only.

### `question_bank/{sha256(normalized_question)}`
`question_text, question_normalized, question_embedding (Vector, 1536),
category (enum), answer_text, provenance_origin, times_used, created_at, updated_at`

Vector index: `question_bank.question_embedding`, **dimension 1536, type flat**
(Firestore hard limit is 2048 — `gemini-embedding-001`'s native 3072 **cannot** be indexed;
we request `output_dimensionality=1536` via MRL and **re-normalize to unit length**, or
cosine distances come out subtly wrong).

> **Measured:** a raw 1536-dim MRL-truncated vector came back with **L2 norm = 0.6935**, not
> 1.0. Re-normalization is therefore required, confirmed against the live API rather than
> assumed. `find_nearest(COSINE, distance_threshold=0.20)` was verified to return exactly the
> docs with similarity ≥ 0.80 and exclude the rest.

### `pending_review/{job_id}` and `run_traces/{run_id}`
`pending_review`: job snapshot + `questions[]` + `status (pending|approved|rejected|expired)`
+ `slack_message_ts`. `run_traces`: full step-by-step trace for debugging.

---

## 5. The LLM boundary

| Task | Engine | Rationale |
|---|---|---|
| Cap check, dedup, submit gate | **Code** | Never trust a model with a safety limit |
| **All numerics** (years, salary, notice) | **Code** — regex → `user_facts` → arithmetic | The model never sees or emits a number |
| Work auth / sponsorship / relocation | **Code** — canonical-ID lookup | Opposite-meaning questions, zero tolerance |
| Modal navigation | **Explicit state machine** | Not LLM-driven clicking |
| Job scoring | Vertex AI `gemini-2.5-flash`, borderline set only | Cuts calls and hallucination surface |
| Question → category | Vertex AI `gemini-2.5-flash`, **enum-constrained** | Classification, not generation |
| Q&A generalization, resume gap analysis | Vertex AI `gemini-2.5-pro` | Real language tasks, low volume, non-safety-critical |

**Harness rules.** Every LLM call: Pydantic schema + `response_mime_type=application/json`,
validated on return, retried on validation failure, and **fails to human review rather than
guessing**. Every output must cite evidence (the JD span, or the `question_bank` doc matched);
no evidence → treated as no-answer. Submits are idempotent: deterministic doc IDs, guarded by
a Firestore transaction, so a retry can never double-apply.

---

## 6. Always-human-review blocklist

These bypass the vector bank entirely, regardless of match score.

> **This is now empirically proven, not a hunch.** Measured with the real
> `gemini-embedding-001` @1536, unit-normalized:
>
> | Pair | Cosine similarity |
> |---|---|
> | `"How many years of Python experience..."` vs near-duplicate phrasing | **0.9979** ← true match |
> | `"Are you legally authorized to work in India?"` vs `"Do you require visa sponsorship to work in India?"` | **0.9097** ← **opposite correct answers** |
>
> The dangerous false match scores **0.91** — far above the 0.80 threshold. Worse, only
> **0.08** separates it from a genuine match, so **no threshold value can separate them**:
> raising the bar to 0.95 would kill legitimate matches while barely helping. A similarity
> gate is structurally incapable of catching this class of error. The blocklist is therefore
> **mandatory, not defence-in-depth.**

- **Work authorization / visa sponsorship** — see the measurement above. A wrong auto-answer
  here is a disqualified application or a false legal statement.
- **Salary** (current, expected, rate) — a wrong number is permanent.
- **Notice period.**
- **Any numeric years-of-experience for a skill not in `skill_years`.**
- **All EEO questions** (disability, veteran status, race, gender) — never auto-filled;
  default **"prefer not to answer."**

---

## 7. Human action items (only two)

1. **Slack app.** I cannot create it — that requires authenticating to your Slack account.
   I will generate a paste-ready manifest; you create the app, enable Socket Mode, and hand me
   the `xapp-…` (app token, `connections:write`) and `xoxb-…` (bot token). I store both in
   Secret Manager. Digest target: private channel `#job-agent`.
2. **LinkedIn session bootstrap.** One-time: I provide `bootstrap_session.py` which opens
   Patchright non-headless; **you** log in by hand (so 2FA and any security challenge is
   handled by a human, never bypassed). Session persists to `.linkedin-mcp/` — gitignored.

Plus one sign-off: the `skill_years` map in §4.

---

## 8. Build order

- **Phase 0** — probe the pinned MCP server; dump exact tool schemas + a real `search_jobs`
  and `get_job_details` payload. Every tool definition, Firestore field, and selector is then
  written against observed reality, not documentation. ← *next*
- **Phase 1** — Firestore seed + deterministic answer engine + unit tests (no browser, no LLM)
- **Phase 2** — Patchright Easy Apply state machine, dry-run mode that never submits
- **Phase 3** — scoring + vector bank + embeddings
- **Phase 4** — Slack Socket Mode, Block Kit cards, slash commands
- **Phase 5** — Workflow C export, Task Scheduler registration, end-to-end dry run
- **Phase 6** — Streamlit monitoring dashboard (local, reads Firestore, $0)
- **Phase 7** — live test: 5 real applications, dry-run reviewed first (see `setup/CREDENTIALS.md` §6)
