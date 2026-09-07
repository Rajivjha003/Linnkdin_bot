"""Runtime configuration and singleton clients.

Everything environment-shaped lives here so no other module reads os.environ.
"""
from __future__ import annotations

import functools
import logging
import os
import pathlib
import sys

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --------------------------------------------------------------------------- #
# GCP
# --------------------------------------------------------------------------- #
PROJECT_ID = os.getenv("GCP_PROJECT", "master-rajiv")
CREDENTIALS_PATH = str(ROOT / "credentials.json")
# Only pin the key file when it is actually present. On Cloud Run it is not, and
# the service's own identity supplies Application Default Credentials -- but
# pointing GOOGLE_APPLICATION_CREDENTIALS at a missing path makes the client
# libraries fail outright instead of falling back to it.
if pathlib.Path(CREDENTIALS_PATH).exists():
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", CREDENTIALS_PATH)

FIRESTORE_DATABASE = "(default)"
GCS_BUCKET = os.getenv("GCS_BUCKET", "master-rajiv-linkedin-agent-exports")

# Vertex AI, not AI Studio: AI Studio returns 429 "prepayment credits are
# depleted" on a billing-enabled project and has no free tier to fall back on.
# Vertex authenticates with the service account and draws on the $300 credit.
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "global")
MODEL_FAST = os.getenv("MODEL_FAST", "gemini-2.5-flash")
MODEL_DEEP = os.getenv("MODEL_DEEP", "gemini-2.5-pro")
MODEL_EMBED = "gemini-embedding-001"

#: Firestore's vector index caps at 2048 dimensions; gemini-embedding-001 emits
#: 3072 natively, so we request an MRL truncation. Truncated vectors come back at
#: L2 ~= 0.69 (measured), NOT unit length -- embeddings.py re-normalises.
EMBED_DIM = 1536

# --------------------------------------------------------------------------- #
# Browser / MCP
# --------------------------------------------------------------------------- #
# Patchright refuses to launch from a directory whose ancestry an untrusted SID
# can re-permission. On this machine both C:\Users\rajiv\AppData (an AppContainer
# package SID) and D:\ (Authenticated Users) fail that check, so the browser lives
# in a purpose-made directory with a protected DACL. See setup/harden_browser_dir.ps1
BROWSERS_PATH = os.getenv("PLAYWRIGHT_BROWSERS_PATH", r"C:\pw-browsers")
BROWSER_TMP = str(pathlib.Path(BROWSERS_PATH) / "_tmp")

# EXPORT these, do not merely read them. Patchright reads PLAYWRIGHT_BROWSERS_PATH
# from the environment when it launches, and under Task Scheduler nothing sets it --
# so it would fall back to %LOCALAPPDATA%\ms-playwright and fail its ACL check,
# meaning every scheduled run silently never got a browser. Setting it here makes
# the agent behave identically however it is started.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = BROWSERS_PATH
pathlib.Path(BROWSER_TMP).mkdir(parents=True, exist_ok=True)
os.environ["TEMP"] = BROWSER_TMP
os.environ["TMP"] = BROWSER_TMP
os.environ.setdefault("LINKEDIN_MCP_CONTAINER", "false")
os.environ.setdefault("AUTO_IMPORT_FROM_BROWSER", "false")
LINKEDIN_STATE_DIR = ROOT / ".linkedin-mcp"
MCP_SERVER_EXE = str(ROOT / ".venv" / "Scripts" / "mcp-server-linkedin.exe")

def mcp_env() -> dict[str, str]:
    """Environment for the MCP subprocess and our own Patchright launches."""
    return {
        **os.environ,
        "PLAYWRIGHT_BROWSERS_PATH": BROWSERS_PATH,
        "TEMP": BROWSER_TMP,
        "TMP": BROWSER_TMP,
        "TMPDIR": BROWSER_TMP,
        "LINKEDIN_MCP_CONTAINER": "false",
        "TOOL_TIMEOUT": "300",
    }


# --------------------------------------------------------------------------- #
# Safety limits -- these are code-enforced, never model-decided.
# Firestore `agent_config/me` can lower them at runtime but never raise them
# above these ceilings.
# --------------------------------------------------------------------------- #
HARD_MAX_SUBMITS_24H = 20

#: A job with no screening questions carries no answer risk -- there is nothing
#: that can be filled in wrongly -- so relevance is the only thing at stake and a
#: marginal match still deserves an application.
NO_QUESTIONS_SCORE_FLOOR = 45
#: Below the main threshold but above this, a job is queued for review rather than
#: dropped: his verdict on borderline jobs is the preference signal the filter
#: needs, and dropping them silently produced none.
BORDERLINE_FLOOR = 40
#: LinkedIn has no two-week option, so we ask for past_month and cut at 14 days.
MAX_POSTING_AGE_DAYS = 14
HARD_MAX_MODAL_OPENS_24H = 40
TIMEZONE = "Asia/Kolkata"

# --------------------------------------------------------------------------- #
# Slack
# --------------------------------------------------------------------------- #
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL", "#job-agent")


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=None)
def secret(name: str) -> str:
    """Read a secret, preferring Secret Manager and falling back to .env.

    Secret Manager is the source of truth; .env exists so the dashboard and local
    scripts work without a network round-trip on every start.
    """
    env_alias = {
        "slack-bot-token": "SLACK_BOT_TOKEN",
        "slack-app-token": "SLACK_APP_TOKEN",
        "gemini-api-key": "GOOGLE_API_KEY",
        "linkedin-li-at": "LINKEDIN_LI_AT",
    }
    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        path = f"projects/{PROJECT_ID}/secrets/{name}/versions/latest"
        value = client.access_secret_version(request={"name": path}).payload.data.decode()
        if value and not value.startswith("PLACEHOLDER"):
            return value
        log.warning("secret %s is a placeholder in Secret Manager; trying .env", name)
    except Exception as exc:  # noqa: BLE001 -- fall back rather than crash
        log.warning("Secret Manager read failed for %s (%s); trying .env", name, exc)

    fallback = os.getenv(env_alias.get(name, name.upper().replace("-", "_")), "")
    if not fallback:
        raise RuntimeError(
            f"secret {name!r} unavailable in Secret Manager and .env. "
            f"See setup/CREDENTIALS.md"
        )
    return fallback


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=1)
def firestore_client():
    from google.cloud import firestore

    return firestore.Client(project=PROJECT_ID, database=FIRESTORE_DATABASE)


@functools.lru_cache(maxsize=1)
def genai_client():
    from google import genai

    return genai.Client(vertexai=True, project=PROJECT_ID, location=VERTEX_LOCATION)


@functools.lru_cache(maxsize=1)
def storage_client():
    from google.cloud import storage

    return storage.Client(project=PROJECT_ID)


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger("agent")
    if root.handlers:
        return root
    root.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                          datefmt="%H:%M:%S")
    )
    root.addHandler(handler)
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    fh = logging.FileHandler(logs / "agent.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s"))
    root.addHandler(fh)
    return root


log = setup_logging()
