"""Environment-driven configuration and the boot-time fail-fast check (D-02, D-04, D-05).

D-05 reading: the boot-time check is a *path-readability* assertion, not a
per-request one. For ``TRIALBRIDGE_KEYS_FILE`` the file itself must already exist
and be readable at boot. For ``TRIALBRIDGE_AUDIT_LOG_PATH`` the *file* legitimately
does not exist yet before the first write (rotation creates it lazily) — what must
already exist and be writable is the file's *parent directory*. Conflating these
two checks, or reusing this boot-time function on the per-request path, is exactly
the mistake RESEARCH.md Pitfall 5 warns against: `require_readable_path` below
only ever runs once, at process start, and always exits the process on failure —
`src/services/access_control.py`'s per-request `resolve_role` never calls it and
never exits.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

DEFAULT_AUDIT_LOG_PATH = "./logs/audit.log"

# Read-but-unused in Phase 1 (tasks.md T006) — later phases wire these upstream
# credentials. Never given a literal default, and never checked at boot: Phase 1
# makes no upstream call, so failing fast on them would make the gateway
# unbootable for its own demo.
#
# SLACK_BOT_TOKEN was promoted out of this tuple in 04-02-PLAN.md Task 1: it is
# now a real `Config.slack_bot_token` field below, still read with no default
# and still never fail-fast-checked at boot (AD-6) — a CRC-only deployment that
# never dispatches to Slack must remain bootable without it.
_LATER_PHASE_ENV_VARS = (
    "FHIR_BASE_URL",
    "FHIR_TOKEN_URL",
    "FHIR_CLIENT_ID",
    "FHIR_CLIENT_SECRET",
)


@dataclass(frozen=True)
class Config:
    """Frozen settings resolved once at boot."""

    keys_file: str
    audit_log_path: str
    api_key: str
    slack_bot_token: str | None
    # AD-26 (06-02-PLAN.md Task 2): web-only fields, read but never
    # boot-validated here — mirrors the slack_bot_token/AD-6 precedent
    # exactly, so an MCP-only deployment stays bootable without them. The
    # web ASGI process's own startup (src/web/app.py, plan 06-03) performs
    # its own fail-fast checks for these four.
    session_signing_secret: str | None = None
    scorecard_store_path: str | None = None
    anthropic_api_key: str | None = None
    anthropic_model: str | None = None
    # AD-35 (10-06-PLAN.md): the raw SESSION_COOKIE_SECURE string, never parsed
    # or validated here — the web process's own startup does that
    # (`check_web_boot_config`), so an MCP-only deployment stays bootable.
    session_cookie_secure: str | None = None


def require_readable_path(env_var: str, purpose: str) -> str:
    """Boot-time only. Exits the process on failure; never call this from a
    per-request path. Names the purpose, the resolved path, and the controlling
    env var in the failure message (D-05)."""
    path = os.environ.get(env_var)
    if not path or not os.path.exists(path) or not os.access(path, os.R_OK):
        sys.exit(
            f"FATAL: {purpose} not found or unreadable at '{path}' "
            f"(set via {env_var}). Server will not start."
        )
    return path


def _require_writable_parent_dir(env_var: str, purpose: str, default: str | None) -> str:
    """Boot-time only. The *file* at this path may not exist yet (it is created
    lazily on first write), but its parent directory must already exist and be
    writable. If the file already exists (e.g. carried over from a prior deploy),
    it must be writable too — otherwise `TimedRotatingFileHandler` opens it eagerly
    and raises an unhandled `PermissionError` instead of failing fast here. Either
    condition exits the process, naming the purpose, the resolved path, and the
    controlling env var (D-05)."""
    path = os.environ.get(env_var, default)
    parent = os.path.dirname(path) or "."
    file_exists_and_unwritable = os.path.exists(path) and not os.access(path, os.W_OK)
    if (
        not path
        or not os.path.isdir(parent)
        or not os.access(parent, os.W_OK)
        or file_exists_and_unwritable
    ):
        sys.exit(
            f"FATAL: {purpose} not writable at '{path}' "
            f"(set via {env_var}). Server will not start."
        )
    return path


def load_config() -> Config:
    """Resolve and validate all Phase-1 env vars. Performs the D-05 boot check
    for both file-backed dependencies before returning."""
    keys_file = require_readable_path("TRIALBRIDGE_KEYS_FILE", "key->role file")
    audit_log_path = _require_writable_parent_dir(
        "TRIALBRIDGE_AUDIT_LOG_PATH", "audit log", DEFAULT_AUDIT_LOG_PATH
    )

    api_key = os.environ.get("TRIALBRIDGE_API_KEY")
    if not api_key:
        sys.exit(
            "FATAL: no caller API key configured (set via TRIALBRIDGE_API_KEY). "
            "Server will not start."
        )

    # AD-6: read with no default and no fail-fast — a missing/empty token must
    # only surface when a PI caller actually attempts a dispatch, not at boot.
    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")

    # Read-but-unused: later phases consume these. No literal default, no
    # fail-fast, and no code needs to run here — see module docstring.

    # AD-26 (06-02-PLAN.md Task 2, Pitfall 5): read with no default and no
    # fail-fast, mirroring slack_bot_token above exactly — an MCP-only
    # deployment must stay bootable without any of these. The web ASGI
    # process's own startup (src/web/app.py, plan 06-03) performs its own
    # fail-fast checks for these four.
    session_signing_secret = os.environ.get("TRIALBRIDGE_SESSION_SECRET")
    scorecard_store_path = os.environ.get("TRIALBRIDGE_SCORECARD_STORE_PATH")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    anthropic_model = os.environ.get("ANTHROPIC_MODEL")

    # AD-35: raw, no default, never validated here (AD-26 split).
    session_cookie_secure = os.environ.get("SESSION_COOKIE_SECURE")

    return Config(
        keys_file=keys_file,
        audit_log_path=audit_log_path,
        api_key=api_key,
        slack_bot_token=slack_bot_token,
        session_signing_secret=session_signing_secret,
        scorecard_store_path=scorecard_store_path,
        anthropic_api_key=anthropic_api_key,
        anthropic_model=anthropic_model,
        session_cookie_secure=session_cookie_secure,
    )
