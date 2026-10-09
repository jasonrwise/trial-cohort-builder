"""Slack `chat.postMessage` adapter (04-02-PLAN.md Task 1, research.md §12).

Mirrors `src/services/trials.py`'s adapter shape exactly: a fresh
`httpx.AsyncClient` is constructed inside `post_message` and never held at
module scope; exactly one plain request per call, with no repeated-attempt
or failure-threshold machinery of any kind wrapped around it (that class of
hardening is Phase 5's DEGR-03 job alone — AD-1/AD-8); and every
distinguishable failure mode (missing/invalid bot token, an `ok: false`
response, any other non-200 status, or a transport error) returns a distinct
`SlackPostError` member, never an unhandled exception. httpx is the only
HTTP dependency used here — no third-party Slack client SDK (AD-5).

Unlike `trials.py`/`fhir.py`, this module does not read any env var itself —
`bot_token` is an explicit parameter, mirroring
`src/services/access_control.py`'s `resolve_role(api_key, keys_file_path)`
convention, so the caller (`src/tools/dispatch_screening_alert.py`) is the
single place that decides where the token comes from.

`post_message` always embeds `nct_id`, `screened_at`, and `dispatching_role`
directly into both the Slack `text` fallback and a leading header block —
FR-014's self-describing-without-lookup guarantee holds regardless of what
`blocks` the caller supplies for the rest of the message body. A reserved demo
ID's header carries the synthetic line (AD-28, DEMO-02).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

import httpx

from src.services import synthetic_label

_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_REQUEST_TIMEOUT = httpx.Timeout(10.0)

# Slack's own "auth-shaped" error codes (https://api.slack.com/methods/chat.postMessage
# `errors` reference) — anything else on an `ok: false` response is a distinct,
# non-auth upstream failure (e.g. `channel_not_found`, `rate_limited`).
_AUTH_ERROR_CODES = frozenset(
    {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"}
)


class SlackPostError(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"


def _build_body(
    channel_id: str,
    nct_id: str,
    screened_at: datetime,
    dispatching_role: str,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    header_text = (
        f"Trial {nct_id} — screened {screened_at.isoformat()} — dispatched by {dispatching_role}"
    )
    if synthetic_label.is_synthetic(nct_id):
        header_text += f" — {synthetic_label.SYNTHETIC_LABEL} demo protocol"
    header_block = {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}
    return {
        # `text` is Slack's required accessibility/notification fallback — kept
        # self-describing on its own, without relying on `blocks` rendering.
        "channel": channel_id,
        "text": header_text,
        "blocks": [header_block, *blocks],
    }


async def post_message(
    channel_id: str,
    nct_id: str,
    screened_at: datetime,
    dispatching_role: str,
    blocks: list[dict[str, Any]],
    bot_token: str | None,
) -> str | SlackPostError:
    """POSTs one `chat.postMessage` request. Never raises. A missing/empty
    `bot_token` maps straight to `UNAUTHORIZED` with zero requests issued —
    the same actionable-error shape a real 401 produces, per AD-6. A 200
    response with `"ok": true` returns the message `ts` string; `"ok": false`
    with an auth-shaped `error` code returns `UNAUTHORIZED`; any other
    `ok: false`, non-200 status, or a body that is unparsable or that parses
    but is not a JSON object returns `UPSTREAM_ERROR`;
    a transport-level failure (e.g. connection refused/timeout) returns
    `TRANSPORT_ERROR`."""
    if not bot_token:
        return SlackPostError.UNAUTHORIZED

    body = _build_body(channel_id, nct_id, screened_at, dispatching_role, blocks)

    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            response = await client.post(
                _SLACK_POST_MESSAGE_URL,
                json=body,
                headers={"Authorization": f"Bearer {bot_token}"},
            )
    except httpx.HTTPError:
        return SlackPostError.TRANSPORT_ERROR

    if response.status_code != 200:
        return SlackPostError.UPSTREAM_ERROR

    try:
        payload = response.json()
    except ValueError:
        return SlackPostError.UPSTREAM_ERROR

    if not isinstance(payload, dict):
        return SlackPostError.UPSTREAM_ERROR

    if not payload.get("ok"):
        error_code = payload.get("error", "")
        if error_code in _AUTH_ERROR_CODES:
            return SlackPostError.UNAUTHORIZED
        return SlackPostError.UPSTREAM_ERROR

    ts = payload.get("ts")
    if not isinstance(ts, str):
        return SlackPostError.UPSTREAM_ERROR
    return ts
