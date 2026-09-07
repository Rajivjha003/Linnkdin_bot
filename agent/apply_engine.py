"""Patchright-driven Easy Apply state machine.

This is the primary apply path, not a fallback: no LinkedIn MCP server can submit
an application, so submitting is entirely ours. Patchright (a stealth-patched
Playwright fork) is used rather than vanilla Playwright because the MCP server
already drives it, so there is one browser stack and one fingerprint.

Three design commitments:

* **Navigation is an explicit state machine, never model-driven.** The model is
  never asked which button to click. Steps are bounded by MAX_STEPS and every
  transition is an assertion about the DOM.
* **Dry run stops at Submit.** In dry-run mode the modal is opened, every question
  read and every answer computed, and then the modal is *discarded*. Nothing is
  sent. This is where selector bugs surface for free.
* **Failures dump evidence.** On any unexpected state a screenshot and the modal's
  HTML are written to `run_traces/`, because a selector that broke silently is the
  most expensive kind of bug here.

Concurrency: this takes its own Chromium profile (`apply-profile`), seeded from the
MCP server's `storage_state.json`. That deliberately avoids contending for the lock
on the MCP server's profile -- the two must never run simultaneously.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agent import config
from agent.models import (
    AnswerKind,
    ApplyOutcome,
    ApplyResult,
    JobPosting,
    ProposedAnswer,
    ScreeningQuestion,
)

log = logging.getLogger("agent.apply")

APPLY_PROFILE = pathlib.Path.home() / ".linkedin-mcp" / "apply-profile"
STORAGE_STATE = pathlib.Path.home() / ".linkedin-mcp" / "storage_state.json"
TRACE_DIR = config.ROOT / "run_traces"

MAX_STEPS = 10
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

# Selector strategy: ARIA and roles ONLY, never CSS classes.
#
# Verified against the live site: LinkedIn now ships hashed, build-generated class
# names -- an apply button carried `class="_34d25300 _692f8ab4 _508938c3 ..."`. Any
# selector like `button.jobs-apply-button` is therefore dead on arrival and, worse,
# fails silently. Roles and aria-labels are the accessibility contract and are far
# more stable, so they are the only hooks used here.
#
# `:has-text()` is kept as a last resort because visible wording changes less often
# than markup, and `i` flags make the aria matches case-insensitive.
SEL_EASY_APPLY = [
    "button[aria-label*='Easy Apply' i]",
    "button:has-text('Easy Apply')",
    # An external application renders as "Apply on company website"; matching a
    # bare "Apply" here would open the wrong flow, so it is deliberately absent.
]
SEL_MODAL = [
    "div[role='dialog']:has(form)",
    "div[role='dialog']",
]
SEL_NEXT = [
    "button[aria-label*='Continue to next step' i]",
    "button[aria-label*='next step' i]",
    "div[role='dialog'] button:has-text('Next')",
    "div[role='dialog'] button:has-text('Continue')",
]
SEL_REVIEW = [
    "button[aria-label*='Review your application' i]",
    "button[aria-label*='review' i]",
    "div[role='dialog'] button:has-text('Review')",
]
SEL_SUBMIT = [
    "button[aria-label*='Submit application' i]",
    "div[role='dialog'] button:has-text('Submit application')",
]
SEL_DISMISS = [
    "div[role='dialog'] button[aria-label*='Dismiss' i]",
    "button[aria-label*='Dismiss' i]",
    "div[role='dialog'] button[aria-label*='close' i]",
]
SEL_DISCARD = [
    "button:has-text('Discard')",
    "div[role='alertdialog'] button:has-text('Discard')",
]
#: Form controls inside the modal. Reading controls directly and deriving each
#: label from its ARIA wiring avoids depending on any wrapper element, which is
#: what the hashed classes made unreliable.
SEL_CONTROLS = (
    "div[role='dialog'] input:not([type='hidden']):not([type='file']), "
    "div[role='dialog'] select, "
    "div[role='dialog'] textarea, "
    "div[role='dialog'] fieldset"
)
AUTH_MARKERS = ("/login", "/checkpoint", "/authwall", "/uas/login")


class AuthFailure(RuntimeError):
    """Session dead. The caller must halt everything, not retry."""


def _jitter(lo: float = 0.4, hi: float = 1.4) -> None:
    """Randomised pauses. Uniform machine-speed timing is itself a signal."""
    time.sleep(random.uniform(lo, hi))


def _first(scope, selectors: list[str], timeout: int = 2500):
    """Return the first selector that resolves to a visible element, else None."""
    for sel in selectors:
        try:
            loc = scope.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:  # noqa: BLE001 -- selector miss is expected
            continue
    return None


@dataclass
class StepTrace:
    step: int
    kind: str
    questions: list[str] = field(default_factory=list)
    action: str = ""
    note: str = ""


class ApplyEngine:
    """Owns one browser context for the lifetime of a run."""

    def __init__(self, *, headless: bool = True, dry_run: bool = True):
        self.headless = headless
        self.dry_run = dry_run
        self._pw = None
        self._ctx = None
        self.page = None

    # ------------------------------------------------------------------ #
    def __enter__(self) -> ApplyEngine:
        from patchright.sync_api import sync_playwright

        APPLY_PROFILE.mkdir(parents=True, exist_ok=True)
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        # Patchright applies its own stealth patches; adding the usual
        # --disable-blink-features flags actively makes detection easier, so the
        # launch args are deliberately minimal.
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(APPLY_PROFILE),
            headless=self.headless,
            no_viewport=True,
            locale="en-IN",
            timezone_id=config.TIMEZONE,
        )
        self._seed_cookies()
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(20000)
        log.info("browser up (headless=%s, dry_run=%s)", self.headless, self.dry_run)
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _seed_cookies(self) -> None:
        """Copy cookies from the MCP server's storage_state on first run."""
        if not STORAGE_STATE.exists():
            log.warning("no storage_state.json; browser may not be logged in")
            return
        try:
            state = json.loads(STORAGE_STATE.read_text(encoding="utf-8"))
            cookies = state.get("cookies") or []
            if cookies:
                self._ctx.add_cookies(cookies)
                log.info("seeded %d cookies from storage_state.json", len(cookies))
        except Exception as exc:  # noqa: BLE001
            log.warning("cookie seeding failed: %s", exc)

    # ------------------------------------------------------------------ #
    def _check_auth(self) -> None:
        url = (self.page.url or "").lower()
        if any(m in url for m in AUTH_MARKERS):
            raise AuthFailure(f"redirected to {url}")

    def _dump(self, job_id: str, tag: str) -> str:
        stamp = dt.datetime.now().strftime("%H%M%S")
        base = TRACE_DIR / f"{job_id}_{tag}_{stamp}"
        try:
            self.page.screenshot(path=f"{base}.png", full_page=False)
            html = ""
            modal = _first(self.page, SEL_MODAL, timeout=1200)
            if modal is not None:
                html = modal.inner_html()
            (pathlib.Path(f"{base}.html")).write_text(html or self.page.content(),
                                                      encoding="utf-8")
            log.info("evidence dumped: %s.{png,html}", base.name)
            return str(base)
        except Exception as exc:  # noqa: BLE001
            log.warning("dump failed: %s", exc)
            return ""


    def _validation_errors(self) -> list[str]:
        """Inline validation messages LinkedIn is showing inside the dialog.

        When the form refuses to advance despite every field looking answered, the
        page itself states the reason -- so read it rather than guessing.
        """
        found: list[str] = []
        for sel in ("div[role='dialog'] [role='alert']",
                    "div[role='dialog'] [aria-invalid='true']",
                    "div[role='dialog'] .artdeco-inline-feedback--error"):
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 6)):
                    txt = " ".join((loc.nth(i).inner_text() or "").split())
                    if txt and txt not in found:
                        found.append(txt[:160])
            except Exception:  # noqa: BLE001
                continue
        return found

    # ------------------------------------------------------------------ #
    def read_questions(self, modal) -> list[ScreeningQuestion]:
        """Read the current step's questions by enumerating form controls.

        Control-first rather than wrapper-first: LinkedIn's wrapper elements now
        carry hashed class names, so anything that keys off them breaks silently.
        Each control's label is resolved through its ARIA wiring instead --
        aria-labelledby, then aria-label, then a `<label for=...>`, then the
        enclosing fieldset's legend.

        `locator_hint` records how to write back to this exact control, so filling
        never has to re-find it by matching label text.
        """
        questions: list[ScreeningQuestion] = []
        try:
            controls = self.page.locator(SEL_CONTROLS)
            total = min(controls.count(), 40)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not enumerate controls: %s", exc)
            return questions

        seen: set[str] = set()
        for i in range(total):
            c = controls.nth(i)
            try:
                tag = (c.evaluate("e => e.tagName") or "").lower()
                ctype = (c.get_attribute("type") or "").lower()

                # Radios are read via their fieldset, not one-by-one.
                if tag == "input" and ctype == "radio":
                    continue

                label = self._resolve_label(c, tag)
                if not label or len(label) < 3:
                    continue
                key = label[:80].lower()
                if key in seen:
                    continue
                seen.add(key)

                kind, options, prefilled = self._shape(c, tag, ctype)
                if kind is AnswerKind.UNKNOWN:
                    continue
                q = ScreeningQuestion(
                    text=label, kind=kind, options=options,
                    prefilled=prefilled, locator_hint=f"{tag}:{ctype}:{i}",
                )
                log.info("  Q kind=%s opts=%s label=%r", kind.value,
                         (options[:6] if options else []), label[:70])
                questions.append(q)
            except Exception as exc:  # noqa: BLE001
                log.debug("control %d unreadable: %s", i, exc)
        return questions

    def _resolve_label(self, control, tag: str) -> str:
        """Label from ARIA wiring, in order of reliability."""
        try:
            ids = control.get_attribute("aria-labelledby")
            if ids:
                parts = []
                for token in ids.split():
                    node = self.page.locator(f"#{token}").first
                    if node.count() > 0:
                        parts.append(" ".join((node.inner_text() or "").split()))
                joined = " ".join(p for p in parts if p).strip()
                if joined:
                    return joined[:300]
        except Exception:  # noqa: BLE001
            pass
        for attr in ("aria-label", "title", "placeholder", "name"):
            try:
                v = control.get_attribute(attr)
                if v and len(v.strip()) > 2:
                    return " ".join(v.split())[:300]
            except Exception:  # noqa: BLE001
                continue
        try:
            cid = control.get_attribute("id")
            if cid:
                lab = self.page.locator(f"label[for='{cid}']").first
                if lab.count() > 0:
                    t = " ".join((lab.inner_text() or "").split())
                    if t:
                        return t[:300]
        except Exception:  # noqa: BLE001
            pass
        if tag == "fieldset":
            try:
                leg = control.locator("legend").first
                if leg.count() > 0:
                    return " ".join((leg.inner_text() or "").split())[:300]
            except Exception:  # noqa: BLE001
                pass
        return ""

    def _shape(self, control, tag: str, ctype: str) -> tuple[AnswerKind, list[str], str]:
        """Control type plus any option strings."""
        try:
            if tag == "select":
                opts = [" ".join((o.inner_text() or "").split())
                        for o in control.locator("option").all()]
                opts = [o for o in opts if o
                        and not re.match(r"^(select an option|choose|--)", o, re.I)]
                return AnswerKind.SINGLE_SELECT, opts, ""
            if tag == "fieldset":
                radios = control.locator("input[type='radio']")
                n = radios.count()
                if n == 0:
                    return AnswerKind.UNKNOWN, [], ""
                opts: list[str] = []
                for r in radios.all():
                    rid = r.get_attribute("id") or ""
                    txt = ""
                    if rid:
                        lab = self.page.locator(f"label[for='{rid}']").first
                        if lab.count() > 0:
                            txt = " ".join((lab.inner_text() or "").split())
                    opts.append(txt or (r.get_attribute("value") or ""))
                return AnswerKind.SINGLE_SELECT, [o for o in opts if o], ""
            if tag == "textarea":
                return AnswerKind.TEXT, [], control.input_value() or ""
            if ctype == "checkbox":
                return AnswerKind.BOOLEAN, ["Yes", "No"], ""
            if ctype == "number":
                return AnswerKind.NUMERIC, [], control.input_value() or ""
            if ctype in ("text", "email", "tel", "") or tag == "input":
                return AnswerKind.TEXT, [], control.input_value() or ""
        except Exception as exc:  # noqa: BLE001
            log.debug("shape detection failed: %s", exc)
        return AnswerKind.UNKNOWN, [], ""

    # ------------------------------------------------------------------ #
    def fill_answer(self, modal, question: ScreeningQuestion, answer: ProposedAnswer) -> bool:
        """Write one answer into the modal. Returns False if it could not be set.

        The control is addressed by the index recorded in `locator_hint` during
        reading, then re-verified by label before anything is typed. That guard
        matters: if the DOM shifted between reading and filling, writing to a
        stale index would put an answer in the wrong field -- silently.
        """
        hint = question.locator_hint or ""
        try:
            tag, ctype, idx_s = (hint.split(":") + ["", "", ""])[:3]
            idx = int(idx_s)
        except (ValueError, AttributeError):
            log.warning("unusable locator hint %r", hint)
            return False

        try:
            control = self.page.locator(SEL_CONTROLS).nth(idx)
            current = self._resolve_label(control, tag)
            if current[:60].lower() != question.text[:60].lower():
                log.warning("control %d moved (expected %r, found %r); not writing",
                            idx, question.text[:40], current[:40])
                return False

            if tag == "select":
                try:
                    control.select_option(label=answer.value)
                except Exception:  # noqa: BLE001
                    control.select_option(value=answer.value)
            elif tag == "fieldset":
                radios = control.locator("input[type='radio']")
                target = None
                for r in radios.all():
                    rid = r.get_attribute("id") or ""
                    txt = ""
                    if rid:
                        lab = self.page.locator(f"label[for='{rid}']").first
                        if lab.count() > 0:
                            txt = " ".join((lab.inner_text() or "").split())
                    if txt.strip().lower() == answer.value.strip().lower():
                        target = lab if rid else r
                        break
                if target is None:
                    return False
                target.click()
            elif tag == "textarea":
                control.fill("")
                control.type(answer.value, delay=random.randint(18, 55))
            elif ctype == "checkbox":
                want = answer.value.strip().lower() in ("yes", "true", "on")
                if control.is_checked() != want:
                    control.click()
            else:
                control.fill("")
                control.type(answer.value, delay=random.randint(18, 55))
            _jitter(0.15, 0.5)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("could not fill %r: %s", question.text[:60], exc)
            return False

    # ------------------------------------------------------------------ #
    def select_saved_resume(self, modal, filename: str) -> bool:
        """Choose an already-uploaded resume by its visible filename.

        Selecting a saved resume rather than uploading each time is both fewer
        steps and a smaller footprint. If the requested file is not offered we
        return False rather than picking a different one.
        """
        wanted = filename.strip().lower()
        if not wanted:
            return True
        try:
            # Class-free: find the radio whose own accessible name mentions the
            # filename. LinkedIn's document cards carry hashed classes, so the
            # filename text itself is the only dependable handle.
            radios = self.page.locator("div[role='dialog'] input[type='radio']")
            for r in radios.all():
                rid = r.get_attribute("id") or ""
                text = (r.get_attribute("aria-label") or "")
                if rid:
                    lab = self.page.locator(f"label[for='{rid}']").first
                    if lab.count() > 0:
                        text += " " + " ".join((lab.inner_text() or "").split())
                if wanted in text.lower():
                    (lab if rid and lab.count() > 0 else r).click()
                    _jitter()
                    log.info("selected saved resume %r", filename)
                    return True
            # A .pdf mentioned anywhere in the dialog means there IS a resume step
            # and we simply failed to match it -- worth flagging rather than
            # silently continuing with whatever LinkedIn preselected.
            body = " ".join((self.page.locator("div[role='dialog']").first
                             .inner_text() or "").split()).lower()
            if ".pdf" in body:
                import re as _re
                offered = _re.findall(r"[\w\-. ]+\.pdf", body)
                log.warning("resume step present but %r not matched; offered=%s",
                            filename, sorted(set(offered))[:6])
                return False
            return True  # no resume step on this application
        except Exception as exc:  # noqa: BLE001
            log.warning("resume selection failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    def open_modal(self, job: JobPosting) -> Any | None:
        """Open the Easy Apply dialog by navigating straight to the SDUI flow.

        Deliberately does NOT hunt for a button. Verified against the live site:
        the apply control is an anchor (not a button), its classes are hashed, and
        its visible text is plain "Apply" for external postings too -- so clicking
        "the apply button" is both hard to locate and unsafe to identify.

        The anchor's href is `/jobs/view/<id>/apply/?openSDUIApplyFlow`, and
        navigating there directly opens a real `div[role=dialog]` with the form in
        it. Whether the job is Easy Apply at all is decided beforehand by
        `voyager.is_easy_apply()`, which reads applyMethod.$type rather than
        guessing from markup.
        """
        from agent.voyager import apply_flow_url

        self.page.goto(apply_flow_url(job.job_id), wait_until="domcontentloaded")
        # The dialog is server-driven and arrives after the shell paints, so wait
        # for the dialog itself rather than a fixed sleep.
        try:
            self.page.locator("div[role='dialog']").first.wait_for(
                state="visible", timeout=20000)
        except Exception:  # noqa: BLE001
            self._check_auth()
            log.info("job %s: apply dialog did not appear", job.job_id)
            self._dump(job.job_id, "no_modal")
            return None
        _jitter(0.8, 1.6)
        self._check_auth()
        modal = _first(self.page, SEL_MODAL, timeout=5000)
        if modal is None:
            self._dump(job.job_id, "no_modal")
            return None
        try:
            head = " ".join((modal.inner_text() or "").split())[:120]
            log.info("job %s: dialog open -> %r", job.job_id, head)
        except Exception:  # noqa: BLE001
            pass
        return modal

    def discard(self) -> None:
        """Close the modal without submitting, confirming the discard prompt."""
        try:
            x = _first(self.page, SEL_DISMISS, timeout=2500)
            if x is not None:
                x.click()
                _jitter(0.5, 1.1)
            d = _first(self.page, SEL_DISCARD, timeout=2500)
            if d is not None:
                d.click()
                _jitter(0.4, 0.9)
            log.info("modal discarded without submitting")
        except Exception as exc:  # noqa: BLE001
            log.warning("discard failed: %s", exc)

    # ------------------------------------------------------------------ #
    def apply(
        self, job: JobPosting, engine, resume_filename: str, *, run_id: str = "",
        match_score: int | None = None,
    ) -> ApplyResult:
        """Walk the modal. Submits only when dry_run is False AND the gate passes.

        `engine` is an AnswerEngine; it is asked for one answer per question and
        its provenance decides everything.
        """
        result = ApplyResult(job_id=job.job_id, outcome=ApplyOutcome.FAILED,
                             run_id=run_id, match_score=match_score)
        traces: list[StepTrace] = []
        try:
            modal = self.open_modal(job)
            if modal is None:
                result.outcome = ApplyOutcome.FAILED
                result.error = "no Easy Apply modal (not an Easy Apply job)"
                return result
            result.modal_opened = True

            seen_signatures: list[str] = []
            for step in range(1, MAX_STEPS + 1):
                self._check_auth()
                questions = self.read_questions(modal)
                st = StepTrace(step=step, kind="form",
                               questions=[q.text[:90] for q in questions])

                # No-progress guard. LinkedIn silently refuses to advance while a
                # required field is empty, so clicking Next just re-renders the same
                # step. Without this the loop burns all MAX_STEPS on one page, which
                # is both useless and a lot of pointless activity on the account.
                signature = "|".join(sorted(q.text[:60] for q in questions))
                if signature and seen_signatures[-1:] == [signature]:
                    st.kind = "stalled"
                    st.action = "discarded"
                    errors = self._validation_errors()
                    st.note = ("same questions after clicking Next; "
                               f"form errors: {errors or 'none reported'}")
                    traces.append(st)
                    log.info("job %s stalled at step %d; form says: %s",
                             job.job_id, step, errors or "(nothing)")
                    self._dump(job.job_id, f"stalled_step{step}")
                    self.discard()
                    result.outcome = ApplyOutcome.ABANDONED_NEEDS_REVIEW
                    return result
                seen_signatures.append(signature)

                for q in questions:
                    ans = engine.answer(q)
                    # Deduplicate: a re-rendered step must not append the same
                    # answer twice, or the Slack card shows each question N times.
                    if not any(a.question_text == ans.question_text for a in result.answers):
                        result.answers.append(ans)
                    if ans.value:
                        if not self.fill_answer(modal, q, ans):
                            ans.reason = (ans.reason or "") + " [could not be written to the form]"

                if resume_filename:
                    self.select_saved_resume(modal, resume_filename)

                submit = _first(self.page, SEL_SUBMIT, timeout=1500)
                if submit is not None:
                    st.kind = "submit_reached"
                    blocking = result.blocking_answers()
                    if blocking:
                        st.action = "discarded"
                        st.note = f"{len(blocking)} answer(s) not auto-submittable"
                        traces.append(st)
                        self.discard()
                        result.outcome = ApplyOutcome.ABANDONED_NEEDS_REVIEW
                        return result
                    if self.dry_run:
                        st.action = "dry_run_discarded"
                        st.note = "gate PASSED; would have submitted"
                        traces.append(st)
                        self._dump(job.job_id, "dryrun_at_submit")
                        self.discard()
                        result.outcome = ApplyOutcome.ABANDONED_NEEDS_REVIEW
                        result.error = "dry_run: gate passed, submit withheld"
                        return result
                    submit.click()
                    _jitter(1.5, 3.0)
                    st.action = "submitted"
                    traces.append(st)
                    result.outcome = ApplyOutcome.SUBMITTED
                    result.submitted_at = dt.datetime.now(dt.timezone.utc)
                    log.info("job %s SUBMITTED", job.job_id)
                    self.discard()  # closes the post-submit confirmation
                    return result

                nxt = _first(self.page, SEL_REVIEW, timeout=1200) or \
                      _first(self.page, SEL_NEXT, timeout=1200)
                if nxt is None:
                    st.action = "stuck"
                    traces.append(st)
                    self._dump(job.job_id, f"stuck_step{step}")
                    self.discard()
                    result.outcome = ApplyOutcome.FAILED
                    result.error = f"no Next/Review/Submit at step {step}"
                    return result
                st.action = "next"
                traces.append(st)
                nxt.click()
                _jitter(1.0, 2.0)

            self._dump(job.job_id, "max_steps")
            self.discard()
            result.outcome = ApplyOutcome.FAILED
            result.error = f"exceeded {MAX_STEPS} steps"
            return result

        except AuthFailure as exc:
            result.outcome = ApplyOutcome.AUTH_FAILURE
            result.error = str(exc)
            log.error("AUTH FAILURE on job %s: %s", job.job_id, exc)
            return result
        except Exception as exc:  # noqa: BLE001
            self._dump(job.job_id, "exception")
            result.outcome = ApplyOutcome.FAILED
            result.error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("apply failed for %s", job.job_id)
            try:
                self.discard()
            except Exception:  # noqa: BLE001
                pass
            return result
        finally:
            # Attached on every path, including the exception paths above, so a
            # failed run is still diagnosable.
            result.steps = [s.__dict__ for s in traces]
