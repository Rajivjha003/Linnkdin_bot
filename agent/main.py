"""CLI entrypoint.

    python -m agent.main run            # Workflow A (respects agent_config.dry_run)
    python -m agent.main run --live     # force a real submit pass
    python -m agent.main run --show     # visible browser, useful for debugging
    python -m agent.main digest         # Workflow C (21:00 IST report)
    python -m agent.main slack          # Socket Mode listener (long-running)
    python -m agent.main status         # print state and exit
    python -m agent.main probe          # verify every dependency end to end
"""
from __future__ import annotations

import argparse
import json
import sys

from agent import config
from agent.store import Store

log = config.log


def cmd_run(args: argparse.Namespace) -> int:
    from agent import workflows

    dry = False if args.live else (True if args.dry else None)
    summary = workflows.run_workflow_a(
        dry_run=dry, headless=not args.show, max_jobs=args.max_jobs
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0 if not summary.get("aborted") else 1


def cmd_digest(_: argparse.Namespace) -> int:
    from agent import workflows

    print(json.dumps(workflows.run_workflow_c(), indent=2, default=str))
    return 0


def cmd_slack(_: argparse.Namespace) -> int:
    from agent import slack_app

    slack_app.start()
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    from agent import workflows

    print(json.dumps(workflows.run_heartbeat(alarm_only=args.alarm_only),
                     indent=2, default=str))
    return 0


def cmd_feedback(_: argparse.Namespace) -> int:
    from agent import feedback

    s = Store()
    a = feedback.analyse(s)
    print(json.dumps(a, indent=2, default=str))
    applied = feedback.apply_auto(s, a)
    if applied:
        print("\napplied:", json.dumps(applied, indent=2, default=str))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    s = Store()
    cfg = s.get_agent_config()
    facts = s.get_facts()
    subs, modals = s.cap_headroom()
    print(json.dumps({
        "paused": cfg.get("paused"),
        "dry_run": cfg.get("dry_run"),
        "submits_last_24h": s.submits_last_24h(),
        "submits_left": subs,
        "modal_opens_left": modals,
        "pending_review": len(s.list_pending()),
        "question_bank": s.bank_size(),
        "match_threshold": cfg["match_score_threshold"],
        "vector_threshold": cfg["vector_threshold"],
        "skill_years_signed_off": facts.skill_years_signed_off,
        "resume_filename": s.get_profile().get("resume_linkedin_filename"),
    }, indent=2, default=str))
    return 0


def cmd_probe(_: argparse.Namespace) -> int:
    """Check every dependency, printing OK/FAIL per component."""
    ok = True

    def check(name: str, fn) -> None:  # noqa: ANN001
        nonlocal ok
        try:
            detail = fn()
            print(f"  OK    {name:<26} {detail}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  FAIL  {name:<26} {type(exc).__name__}: {str(exc)[:110]}")

    print("dependency probe")
    s = Store()
    check("firestore: agent_config", lambda: f"threshold={s.get_agent_config()['vector_threshold']}")
    check("firestore: user_facts", lambda: f"{len(s.get_facts().skill_years)} skills")
    check("firestore: cap query", lambda: f"headroom={s.cap_headroom()}")
    check("firestore: vector index", lambda: f"bank={s.bank_size()} entries")

    def embed_check() -> str:
        from agent import llm

        v = llm.embed(["how many years of python experience"])[0]
        n = sum(x * x for x in v) ** 0.5
        return f"dim={len(v)} l2={n:.6f}"

    check("vertex: embeddings", embed_check)

    def gen_check() -> str:
        from google.genai import types

        r = config.genai_client().models.generate_content(
            model=config.MODEL_FAST, contents="Reply with OK",
            config=types.GenerateContentConfig(max_output_tokens=2000, temperature=0))
        return f"{config.MODEL_FAST} -> {(r.text or '').strip()[:20]!r}"

    check("vertex: generation", gen_check)
    check("secret: slack bot token",
          lambda: f"prefix={config.secret('slack-bot-token')[:5]}…")
    check("secret: slack app token",
          lambda: f"prefix={config.secret('slack-app-token')[:5]}…")
    check("gcs: exports bucket",
          lambda: f"exists={config.storage_client().bucket(config.GCS_BUCKET).exists()}")

    def browser_check() -> str:
        import pathlib

        p = pathlib.Path(config.BROWSERS_PATH)
        chrome = list(p.glob("chromium-*/chrome-win64/chrome.exe"))
        if not chrome:
            raise FileNotFoundError(f"no chromium under {p}")
        return f"{chrome[0].parent.parent.name} at {p}"

    check("patchright: chromium", browser_check)

    def session_check() -> str:
        import pathlib

        st = pathlib.Path.home() / ".linkedin-mcp" / "storage_state.json"
        if not st.exists():
            raise FileNotFoundError(str(st))
        return f"{st.stat().st_size} bytes"

    check("linkedin: session state", session_check)

    print("\nall dependencies OK" if ok else "\nSOME CHECKS FAILED — see above")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agent", description="LinkedIn Easy Apply agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="Workflow A: one application pass")
    r.add_argument("--live", action="store_true",
                   help="actually submit (overrides agent_config.dry_run)")
    r.add_argument("--dry", action="store_true", help="force dry run")
    r.add_argument("--show", action="store_true", help="visible browser")
    r.add_argument("--max-jobs", type=int, default=None, dest="max_jobs")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("digest", help="Workflow C: daily resume gap report").set_defaults(fn=cmd_digest)
    sub.add_parser("slack", help="run the Socket Mode listener").set_defaults(fn=cmd_slack)
    hb = sub.add_parser("heartbeat", help="post a status line; alarm if gone quiet")
    hb.add_argument("--alarm-only", action="store_true", dest="alarm_only",
                    help="only post if the agent has gone silent")
    hb.set_defaults(fn=cmd_heartbeat)
    sub.add_parser("feedback", help="what rejections taught, and apply it").set_defaults(fn=cmd_feedback)
    sub.add_parser("status", help="print current state").set_defaults(fn=cmd_status)
    sub.add_parser("probe", help="check every dependency").set_defaults(fn=cmd_probe)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
