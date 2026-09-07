# LinkedIn Easy Apply Agent

A semi-autonomous LinkedIn Easy Apply agent with human-in-the-loop review over Slack.
Runs locally on a residential IP; state, vectors and secrets live in GCP.

**Design spec and the reasoning behind every decision: [`SPEC.md`](SPEC.md).**
**What you need to supply: [`setup/CREDENTIALS.md`](setup/CREDENTIALS.md).**

---

## The one invariant

> **No model-authored answer is ever submitted to a real employer.**

Every answer carries a `provenance`: `deterministic`, `bank_match`, `human`, or `llm`.
An application auto-submits only when the match score clears the threshold **and**
every answer's provenance is one of the first three **and** every answer cites
evidence. One `llm` answer abandons the modal and queues the job for Slack review.

This is not a stylistic preference. Measured on the live embedding model:

| Question pair | Cosine similarity |
|---|---|
| Two phrasings of *"years of Python experience"* | **0.9979** — a genuine match |
| *"Are you legally authorized to work in India?"* vs *"Do you require visa sponsorship?"* | **0.9097** — **opposite correct answers** |

The dangerous false match scores 0.91, above any usable threshold, and sits only
**0.08** from a real match. No threshold can separate them, so semantic retrieval is
*forbidden* for that class of question rather than tuned. Numbers, work
authorisation, notice period and EEO answers come from code reading an explicit
fact table — the model never sees or emits them.

---

## Architecture

Local agent, cloud state. Nothing is publicly reachable; Slack Socket Mode uses an
outbound WebSocket, which is what removed the need for any web service.

```
[Task Scheduler] ──▶ agent ──┬──▶ mcp-server-linkedin 4.23.3 ──▶ LinkedIn
   every 2h                  │       (job discovery / search only)
                             │
                             ├──▶ Voyager API (plain HTTP, no browser) ──▶ LinkedIn
                             │       applyMethod.$type => IS it Easy Apply?
                             │       + structured title / company / JD text
                             │
                             ├──▶ Patchright ──▶ /jobs/view/<id>/apply/?openSDUIApplyFlow
                             │       walks the dialog, ARIA selectors only
                             │
                             └──▶ Slack Bolt (Socket Mode, outbound WS) ◀──▶ Slack

      Firestore (state + 1536-dim vectors) · GCS (exports) · Vertex AI (Gemini)
```

| Component | Choice | Why |
|---|---|---|
| Job discovery | `mcp-server-linkedin==4.23.3` | Maintained search. Read-only — it has **no apply tool**, and its text scrape cannot tell Easy Apply from an external link |
| **Easy Apply detection + job details** | **Voyager API** (`agent/voyager.py`) | `applyMethod.$type` states it outright. The DOM cannot: classes are hashed, the control is an `<a>`, and both kinds say plain "Apply". ~200ms, no browser |
| Apply | **our own Patchright state machine** | No MCP server can submit; the maintainer rejected the feature. Enters via the SDUI apply URL, ARIA selectors only |
| LLM | **Vertex AI** `gemini-2.5-flash` / `2.5-pro` | AI Studio returns `429 prepayment credits depleted` on a billing-enabled project |
| Embeddings | `gemini-embedding-001` @ **1536** dims, unit-normalised | Firestore's vector index caps at 2048; native 3072 cannot be indexed |
| Trigger | **Windows Task Scheduler** | Pub/Sub would retain missed triggers and deliver a *burst* on reconnect |
| Slack | `slack-bolt` Socket Mode | Needs to *receive* button clicks; MCP cannot do inbound push |

---

## Install

Already done in this repo, but from scratch:

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Python **3.12** is required (`mcp-server-linkedin` needs ≥3.12.4).

### The browser path is not optional

Patchright refuses to launch from a directory whose ancestry an untrusted SID can
re-permission. On this machine both `C:\Users\rajiv\AppData` (an AppContainer
package SID) and `D:\` (Authenticated Users) fail that check. So Chromium lives in
a directory with a **protected DACL**:

```bash
powershell -ExecutionPolicy Bypass -File setup/harden_browser_dir.ps1
PLAYWRIGHT_BROWSERS_PATH=C:/pw-browsers .venv/Scripts/patchright.exe install chromium
```

### Seed Firestore

```bash
.venv/Scripts/python.exe setup/seed_firestore.py
```

### Verify everything

```bash
.venv/Scripts/python.exe -m agent.main probe
```

All 11 checks should read OK before you run anything.

---

## Use

```bash
.venv/Scripts/python.exe -m agent.main run --dry      # open modals, never submit
.venv/Scripts/python.exe -m agent.main run --live     # actually submit
.venv/Scripts/python.exe -m agent.main run --show     # visible browser, for debugging
.venv/Scripts/python.exe -m agent.main digest         # daily resume-gap report
.venv/Scripts/python.exe -m agent.main slack          # Socket Mode listener
.venv/Scripts/python.exe -m agent.main status
```

Dashboard:

```bash
.venv/Scripts/streamlit.exe run dashboard/streamlit_app.py
```

Schedule it (elevated PowerShell):

```bash
powershell -ExecutionPolicy Bypass -File setup/register_tasks.ps1
```

### Slack commands

`/status` · `/pause` · `/resume` · `/apply_now` · `/update_cookie <li_at>` ·
`/set_resume <filename>` · `/update_resume <text>` · `/add_location <loc>` ·
`/set_threshold match 70 | vector 0.85`

---

## Safety controls

| Control | Enforced by |
|---|---|
| ≤20 confirmed submits per rolling 24h | Firestore aggregation count, integer compare |
| ≤40 modal opens per 24h | a failure storm looks more bot-like than success |
| Residential IP | the browser only ever runs on your machine |
| No trigger backlog | Task Scheduler, not Pub/Sub |
| Auth-failure halt | 401/403/checkpoint → pause everything, Slack alert |
| Idempotent submits | job id is the Firestore doc id, guarded by a transaction |
| `dry_run` default **on** | `agent_config.dry_run`, and the dashboard toggle |

Firestore may *lower* a cap at runtime but never raise it above the ceiling in
`config.py`.

---

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q     # 70 tests
```

Most of them assert *silence* — that the engine declines rather than guessing.
A wrong answer on a real application is unrecoverable; a decline costs one Slack
notification.

---

## How Easy Apply detection was solved

For a while this looked like "Easy Apply no longer exists": nine searches, ~50
postings, **zero** Easy Apply found. That conclusion was wrong, and the way it was
wrong is worth recording, because three separate things were misleading at once.

**What the DOM actually does**

| Assumption | Reality on the live site |
|---|---|
| Classes identify the apply button | Classes are **hashed** per build: `class="_34d25300 _692f8ab4 _508938c3 …"` |
| The apply control is a `<button>` | It is an **`<a>`**, so every button scan returned nothing |
| Easy Apply postings say "Easy Apply" | They say plain **"Apply"** — identical to external postings. The words "Easy Apply" appear **nowhere** on the page |
| `f_EA=true` filters the search | It does not discriminate; filter on and off return the same postings |
| The MCP server's scrape can tell | It reports `"Apply"` for every posting either way |

So the DOM genuinely cannot distinguish Easy Apply from an external ATS link. Any
selector-based detector is guessing.

**What actually works: LinkedIn's own API.** `applyMethod.$type` is a discriminated
union that states the answer outright:

```
com.linkedin.voyager.jobs.ComplexOnsiteApply  ->  Easy Apply
com.linkedin.voyager.jobs.OffsiteApply        ->  external ATS
```

Measured on a real sample: **6 of 15 (40%) Easy Apply.** There was never a shortage
of jobs — only a broken detector. `agent/voyager.py` does this over plain HTTP using
the browser's own cookies, so a check costs ~200ms and needs no browser at all.

**And the way in.** The apply anchor's href is
`/jobs/view/<id>/apply/?openSDUIApplyFlow` (SDUI = LinkedIn's server-driven UI).
Navigating straight there opens a real `div[role="dialog"]` containing the form, so
the engine never has to find or click a control whose classes are hashed and whose
element type is not what you would guess.

**Selectors are ARIA-only.** Confirmed live: `aria-label="Continue to next step"`,
`aria-label="Submit application"`, `aria-label="Dismiss"`. Questions are read by
enumerating form controls inside the dialog and resolving each label through
`aria-labelledby` → `aria-label` → `<label for>` → `<legend>` — never through a
wrapper element.

### Validated end to end

A dry run against a live Easy Apply posting (`Machine Learning Engineer`,
InfoSpeed Services) walked the whole flow and reached:

```
step 5: kind=submit_reached  action=dry_run_discarded  note=gate PASSED; would have submitted
```

All six answers were auto-submittable and **every one deterministic** — email,
phone country code, mobile, AI years, ML years, follow-company. Nothing was sent,
because `dry_run` was on.

### Bugs the live runs exposed

Each of these is now a regression test in `tests/test_live_findings.py`:

* **"Enter a whole number between 0 and 99"** — LinkedIn's own message. Years fields
  are `<input type="text">` with a *numeric* validator, so `4.2` was rejected and
  the form stalled with every field apparently filled. Years now always floor to a
  whole number (4.2 → 4, never inflating).
* **A stall guard.** Without it the engine clicked Next ten times on the same page.
  It now detects an unchanged question set, reads LinkedIn's inline validation text,
  and abandons — which is how the whole-number bug was diagnosed in one run.
* **`Phone country code`** contains "phone", so the generic branch answered it with
  the raw mobile number.
* **"Artificial Intelligence (AI)"** was in no alias table, so an answerable
  question went to a human.
* **"comfortable commuting to this job's location"** was filed as LOCATION, which
  tried to answer a yes/no question with a city name.
* **"Are you currently residing in Bengaluru?"** now *compares* against the fact
  table — Bengaluru → Yes, Chennai → No.
* **`Follow <company> to stay up to date`** — a `[^?.]` character class could not
  cross the full stop in "Inc.", so this never matched and blocked submission.
* **A shell heredoc turned every `\b` in one regex into a literal backspace byte**
  (0x08). The pattern compiled perfectly and matched nothing. There is now a test
  asserting the source is free of control characters.

### Honest remaining limits

* Employers do misconfigure questions. One live posting asked *"Please mention your
  Notice Period"* as a dropdown whose only options were **Yes / No**. There is no
  correct answer, so the engine declines and asks you — which is the right outcome,
  not a bug.
* Skills absent from `skill_years` (Power BI, advanced Excel, Google Sheets) are
  always routed to you rather than estimated from a neighbouring skill.
* Salary is never auto-answered, by policy.
* **No real application has been submitted.** Every run so far has been a dry run.

---

## Channel quality: read this before trusting the pipeline to find you a job

The software works. Whether **Easy Apply** is a good channel for *your* profile is
a separate question, and the measurements say: not on its own.

**Good AI roles do not use Easy Apply.** Every strong posting sampled routed to the
employer's own applicant-tracking system, which Easy Apply automation cannot touch:

| Employer | Where "Apply" actually goes |
|---|---|
| PwC India | `pwc.wd3.myworkdayjobs.com` (Workday) |
| Bristol Myers Squibb | `tnl2.jometer.com` (Joveo) |
| UST | `usource.ripplehire.com` (RippleHire) |
| micro1 / Hired | `jobs.micro1.ai` |
| various | `candidateportal.ceipal.com` (Ceipal) |

**What Easy Apply surfaced instead.** The widest run measured:

```
229 job ids found  ->  80 looked up via Voyager  ->  6 Easy Apply (7.5%)
                   ->  5 passed prefilter  ->  2 scored  ->  0 above threshold
```

Every Easy Apply job found, scored against the resume:

| Score | Title | Why it was rejected |
|---|---|---|
| 35 | AWS Data Engineer (Python, PySpark) | needs AWS, Scala, Hive, Oozie |
| 25 | Senior AI-First IAM/CIAM Engineer | needs 8+ yrs specialised IAM/CIAM |
| 25 | FlexPLM Developer | needs FlexPLM / Windchill |
| 15 | Erlang Developer | needs Erlang/OTP |
| 5 | Senior Material Science Engineer | different discipline |
| 5 | Lead Engineer *(ASIC physical design)* | different discipline |
| 5 | Architect *(analog circuit design)* | different discipline |

Nothing near the threshold of 70, across two independent runs. Note the Easy Apply
rate itself varies hugely by search term: 7.5% on AI/ML titles, but 40% on a
`Data Analyst` search — which is the clue about where the supply actually is. The scorer is behaving correctly — those really
are unrelated jobs. The issue is the **input**: because `f_EA` does not filter,
the search returns loosely-matched postings and Voyager identifies whichever few
happen to be Easy Apply. Easy Apply skews heavily toward staffing and consulting
listings (PharmaACE, InfoSpeed, Korn Ferry, "Top Gen AI Jobs"), not product teams.

**So what is this good for?**

1. **A volume supplement, not the main channel.** It removes the tedium from the
   Easy Apply tail while you apply by hand to the ATS roles that are actually worth
   your time.
2. **Broaden the titles** if you want more volume: `Data Scientist`,
   `Python Developer`, `Data Engineer`, `Analytics Consultant` have far more Easy
   Apply supply than `LLM Engineer`. Use `/add_location` and edit
   `search_config.titles`. Expect lower relevance.
3. **Lower the threshold deliberately**, e.g. `/set_threshold match 55`. Skipped
   jobs are re-considered automatically when you lower it — the threshold in force
   at skip time is recorded, so nothing is permanently lost.
4. **Do not extend this to external ATS.** Workday, Greenhouse, RippleHire and
   Ceipal each need a separate adapter, they change often, and several explicitly
   prohibit automation. That is a much larger and riskier project than this one.

The honest summary: the agent reliably handles the Easy Apply pool that exists.
For senior AI roles in India, that pool is thin and mostly off-target.

---

## Layout

```
agent/
  models.py        typed contracts; Provenance and the gate live here
  config.py        env, clients, hard safety ceilings
  facts.py         deterministic answering — regex + fact lookup, no model
  answering.py     resolution order: deterministic → bank → human
  llm.py           Vertex: embeddings + 3 schema-constrained calls
  store.py         all Firestore access; caps, dedup, vectors, traces
  linkedin_mcp.py  MCP client (search only) + parser for captured payloads
  voyager.py       LinkedIn's own API -- authoritative Easy Apply detection
  apply_engine.py  Patchright Easy Apply state machine
  slack_notify.py  outbound Slack (digest, alerts)
  slack_app.py     Socket Mode listener (buttons, modal, slash commands)
  workflows.py     A: run · B: approve · C: digest · D: auth halt
  main.py          CLI
dashboard/streamlit_app.py
setup/             manifest, seed, task registration, credentials doc
tests/             70 tests
probe_output/      captured live payloads the parser is written against
```
