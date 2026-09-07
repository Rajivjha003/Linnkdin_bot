"""Seed Firestore with the profile, fact table, search config and agent config.

Idempotent: re-running overwrites the config documents and touches nothing else
(applied_jobs, question_bank and pending_review are never modified here).

`skill_years` is DERIVED from employment dates in the resume, never guessed. It
ships with `skill_years_signed_off=False`, and facts.py refuses to answer any
years-of-experience question until a human flips that to True -- because a wrong
number here becomes a wrong number on a real application.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import config  # noqa: E402
from agent.store import C_AGENT, C_FACTS, C_PROFILE, C_SEARCH, DOC_ME  # noqa: E402

RESUME_PDF = ROOT / "Resume" / "Rajiv_Ranjan_Jha_Resume.pdf"


def resume_text() -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(RESUME_PDF))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


# Career start Jul 2022 (SK Associates); Alkye from Sep 2024.
# Everything below is arithmetic on those two dates, not estimation.
_CAREER_START = dt.date(2022, 7, 1)
_ALKYE_START = dt.date(2024, 9, 1)
_SK_SPAN = round((_ALKYE_START - _CAREER_START).days / 365.25, 1)   # 2.2


def _years_since(d: dt.date) -> float:
    return round((dt.date.today() - d).days / 365.25, 1)


TOTAL = _years_since(_CAREER_START)      # 4.2 as of 2026-09
ALKYE = _years_since(_ALKYE_START)       # 2.0 as of 2026-09

SKILL_YEARS: dict[str, float] = {
    # whole career
    "python": TOTAL, "machine_learning": TOTAL, "data_science": TOTAL, "sql": TOTAL,
    # Live postings ask for "Artificial Intelligence" and "Deep Learning" by name.
    # Both span the whole career: YOLO/ArcFace from 2022, LLM work from 2024.
    "artificial_intelligence": TOTAL, "deep_learning": TOTAL,
    "nlp": ALKYE,
    # Alkye / Merchmix era (GenAI platform work)
    "genai_llm": ALKYE, "rag": ALKYE, "google_adk": ALKYE, "mcp": ALKYE,
    "langchain_agents": ALKYE, "fine_tuning_peft": ALKYE,
    "time_series": ALKYE, "lightgbm": ALKYE, "bigquery": ALKYE,
    "fastapi": ALKYE, "django": ALKYE, "docker": ALKYE, "gcp_cloud_run": ALKYE,
    "pytorch": ALKYE,
    # SK Associates era (computer vision)
    "computer_vision_yolo": _SK_SPAN,
    # partial / secondary
    "react_nextjs": 1.0,
}

FACTS = {
    "full_name": "Rajiv Ranjan Jha",
    "email": "rajiv.jha.0003@gmail.com",
    "phone": "8825330125",
    "phone_country_code": "India (+91)",
    "city": "Bengaluru, India",
    "linkedin_url": "https://www.linkedin.com/in/rajivranjan-jha",
    "total_years_experience": TOTAL,
    "work_auth_india": True,
    "requires_sponsorship_india": False,
    "notice_period_days": 15,
    "immediate_joiner": True,
    "current_ctc_lpa": 9.6,
    "expected_ctc_lpa": 20.0,
    "willing_to_relocate": True,
    "relocate_scope": "anywhere_in_india",
    "highest_degree": "Bachelor's Degree",
    "skill_years": SKILL_YEARS,
    # Deliberately False. facts.py declines all years questions until reviewed.
    "skill_years_signed_off": False,
}

SEARCH = {
    "titles": [
        "AI Engineer", "GenAI Engineer", "Machine Learning Engineer",
        "Senior Data Scientist", "LLM Engineer",
    ],
    "locations": ["Bengaluru, Karnataka, India", "India"],
    "experience_levels": ["mid_senior", "associate"],
    "work_types": ["remote", "hybrid", "on_site"],
    "job_type": "full_time",
    "date_posted": "past_24_hours",
    "sort_by": "date",
    "easy_apply_only": True,
    "remote_scope": "india_only",
    "max_pages": 2,
    "must_have_any": ["python", "ai", "ml", "machine learning", "llm", "genai",
                      "data scien", "nlp", "deep learning"],
    "title_exclude": ["intern", "manager", "director", "principal", "head of",
                      "sales", "recruiter", "embedded c", "frontend", "android", "ios"],
}

AGENT = {
    "vector_threshold": 0.80,       # cosine SIMILARITY (Firestore distance = 0.20)
    "match_score_threshold": 70,
    "max_submits_24h": 20,
    "max_modal_opens_24h": 40,
    "paused": False,
    "digest_hour_ist": 21,
    "dry_run": True,               # starts safe: never clicks Submit
}


def main() -> None:
    db = config.firestore_client()
    now = dt.datetime.now(dt.timezone.utc)

    text = resume_text()
    print(f"resume extracted: {len(text)} chars from {RESUME_PDF.name}")

    db.collection(C_PROFILE).document(DOC_ME).set({
        "resume_text": text,
        # Verified against the live picker: LinkedIn offers
        # "Rajiv_Ranjan_Jha_Resume_2026.pdf" and "Rajiv Updated Resume.pdf".
        "resume_linkedin_filename": "Rajiv_Ranjan_Jha_Resume_2026.pdf",
        "resume_pdf_path": str(RESUME_PDF),
        "updated_at": now,
    }, merge=True)
    print(f"  {C_PROFILE}/{DOC_ME} written")

    db.collection(C_FACTS).document(DOC_ME).set({**FACTS, "updated_at": now}, merge=True)
    print(f"  {C_FACTS}/{DOC_ME} written ({len(SKILL_YEARS)} skills, "
          f"signed_off={FACTS['skill_years_signed_off']})")

    db.collection(C_SEARCH).document(DOC_ME).set({**SEARCH, "updated_at": now}, merge=True)
    print(f"  {C_SEARCH}/{DOC_ME} written ({len(SEARCH['titles'])} titles)")

    db.collection(C_AGENT).document(DOC_ME).set({**AGENT, "updated_at": now}, merge=True)
    print(f"  {C_AGENT}/{DOC_ME} written (dry_run={AGENT['dry_run']})")

    print(f"\ntotal_years_experience = {TOTAL}   (Jul 2022 -> today)")
    print("derived skill_years:")
    for k, v in sorted(SKILL_YEARS.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {k:<24} {v}")
    print("\nNOTE: skill_years_signed_off is False, so every years-of-experience")
    print("      question will route to human review until you confirm the table.")


if __name__ == "__main__":
    main()
