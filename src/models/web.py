"""Session cookie payload model (06-02-PLAN.md Task 1, AD-15).

Not a stored record — carried entirely inside the AD-15 signed cookie
(`base64url(api_key) + "." + base64url(hmac_sha256(secret, api_key))`,
`src/services/session.py`), never stored server-side. `SessionCookiePayload`
has exactly one field, `api_key` — no `role` field (Pitfall 4/AD-15): role is
always re-derived fresh, per request, from this key via the existing
`access_control.resolve_role` (AD-2). Caching or trusting a role carried in
the cookie itself would make a rotated/revoked key continue to work until
some other invalidation fires — the field's structural absence here makes
that impossible, not merely discouraged.
"""

from pydantic import BaseModel, ConfigDict, Field


class SessionCookiePayload(BaseModel):
    """The one value carried by the AD-15 signed session cookie."""

    model_config = ConfigDict(strict=True, extra="forbid")

    api_key: str = Field(min_length=1)
