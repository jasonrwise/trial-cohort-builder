"""Session cookie sign/verify (06-03-PLAN.md Task 1, AD-15; lockout added by
plan 06-05).

Stateless signed cookie: ``base64url(api_key) + "." +
base64url(hmac_sha256(secret, api_key))``. `verify_session_cookie` recomputes
the MAC and compares it with `hmac.compare_digest` — never `==`, a
documented timing-attack pitfall AD-15 closes explicitly — and never raises,
never logs, and gives no distinct signal about which half failed (missing,
malformed, undecodable, non-UTF-8, wrong signature, or an `api_key` that
fails `SessionCookiePayload` validation are all indistinguishable failures).
Mirrors `access_control.resolve_role`'s own never-raise, fail-closed
discipline.
"""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import hmac
import time

from pydantic import ValidationError

from src.models.web import SessionCookiePayload

SESSION_COOKIE_NAME = "tb_session"

# AD-16 login lockout thresholds. Module constants, following the
# TERMINOLOGY_MATCH_THRESHOLD precedent (`src/services/terminology.py`) —
# ARCHITECTURE-SPINE.md leaves the constant-vs-Config home open and no
# artifact fixes these numbers; quickstart Scenario 7 (20 attempts) observes
# the per-key trip at attempt 6.
LOCKOUT_WINDOW_SECONDS = 900
LOCKOUT_PER_KEY_FAILURE_THRESHOLD = 5
LOCKOUT_GLOBAL_FAILURE_THRESHOLD = 20
LOCKOUT_DURATION_SECONDS = 900


class _FailureWindow:
    """Plain in-process state: failure timestamps (monotonic seconds) inside
    the sliding window, plus this window's own active-trip timestamp (or
    `None`). Not a Pydantic model — never serialized to a caller, mirrors
    `fhir.py`'s `_FhirBreakerState` shape."""

    def __init__(self) -> None:
        self.failures: collections.deque[float] = collections.deque()
        self.tripped_at: float | None = None


class _LoginLockoutState:
    """The second Constitution-V-exempt module-state instance (AD-16, after
    AD-11's FHIR breaker `_FhirBreakerState` in `src/services/fhir.py`).
    Stores only failure timestamps and trip state, never authorization data.
    `by_key_hash` is keyed by the SHA-256 hex digest of the attempted key —
    never the raw key itself (T-06-27). Valid only in the single-process
    deployment (AD-21)."""

    def __init__(self) -> None:
        self.by_key_hash: dict[str, _FailureWindow] = {}
        self.global_window: _FailureWindow = _FailureWindow()


_lockout = _LoginLockoutState()


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _expire_stale_trip(window: _FailureWindow, now: float) -> None:
    """A trip older than LOCKOUT_DURATION_SECONDS self-clears, along with the
    failures that caused it — a fresh window starts counting from zero."""
    if window.tripped_at is not None and (now - window.tripped_at) >= LOCKOUT_DURATION_SECONDS:
        window.tripped_at = None
        window.failures.clear()


def _drop_stale_failures(window: _FailureWindow, now: float) -> None:
    cutoff = now - LOCKOUT_WINDOW_SECONDS
    while window.failures and window.failures[0] < cutoff:
        window.failures.popleft()


def record_login_attempt(key: str, success: bool) -> None:
    """Records one login outcome for `key`. Never raises, never logs, never
    stores the raw key. On failure, appends to both the per-key and global
    sliding windows and trips whichever threshold is reached. On success,
    clears only that key's own failures — the global counter is untouched.
    Every by_key_hash entry with no in-window failure and no active trip is
    pruned before returning (T-06-29), for every key, not just this one."""
    now = time.monotonic()
    key_hash = _key_hash(key)
    key_window = _lockout.by_key_hash.setdefault(key_hash, _FailureWindow())

    for window in [*_lockout.by_key_hash.values(), _lockout.global_window]:
        _expire_stale_trip(window, now)
        _drop_stale_failures(window, now)

    if success:
        key_window.failures.clear()
        key_window.tripped_at = None
    else:
        key_window.failures.append(now)
        if len(key_window.failures) >= LOCKOUT_PER_KEY_FAILURE_THRESHOLD:
            key_window.tripped_at = now

        _lockout.global_window.failures.append(now)
        if len(_lockout.global_window.failures) >= LOCKOUT_GLOBAL_FAILURE_THRESHOLD:
            _lockout.global_window.tripped_at = now

    for hashed_key, window in list(_lockout.by_key_hash.items()):
        if not window.failures and window.tripped_at is None:
            del _lockout.by_key_hash[hashed_key]


def is_locked_out(key: str) -> bool:
    """True when the global trip is active or this key's own trip is
    active. Never mutates counts beyond expiring stale trips (and the
    failures a stale trip's expiry clears with it)."""
    now = time.monotonic()
    _expire_stale_trip(_lockout.global_window, now)

    key_window = _lockout.by_key_hash.get(_key_hash(key))
    if key_window is not None:
        _expire_stale_trip(key_window, now)

    global_locked = _lockout.global_window.tripped_at is not None
    key_locked = key_window is not None and key_window.tripped_at is not None
    return global_locked or key_locked


def parse_cookie_secure(raw: str | None) -> bool | None:
    """Parses the raw `SESSION_COOKIE_SECURE` value (AD-35, WD-3): unset means
    Secure on, exactly ``"true"`` is on, exactly ``"false"`` is off, and every
    other string (including the empty string, ``"True"``, ``" false"``) is
    invalid and returns `None` so web startup can refuse it."""
    if raw is None or raw == "true":
        return True
    if raw == "false":
        return False
    return None


def cookie_attributes(raw_flag: str | None) -> dict[str, str | bool]:
    """The transport attributes of the `tb_session` cookie (AD-35): the single
    place they are written. Every response that sets or clears the cookie must
    spread this dict into `set_cookie`/`delete_cookie` so set and clear can never
    drift apart. `raw_flag` is `Config.session_cookie_secure`; Secure is off only
    when it parses to False — an invalid value never reaches here (startup
    rejects it) and, if it somehow did, Secure stays on."""
    return {
        "path": "/web",
        "httponly": True,
        "samesite": "lax",
        "secure": parse_cookie_secure(raw_flag) is not False,
    }


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded)


def sign_session_cookie(api_key: str, secret: str) -> str:
    """Returns ``base64url(api_key) + "." + base64url(hmac_sha256(secret,
    api_key))``."""
    api_key_bytes = api_key.encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), api_key_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(api_key_bytes)}.{_b64url_encode(signature)}"


def verify_session_cookie(cookie_value: str | None, secret: str | None) -> str | None:
    """Returns the verified `api_key`, or `None` for every failure mode."""
    if not cookie_value or not secret:
        return None

    parts = cookie_value.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None

    payload_part, signature_part = parts

    try:
        api_key_bytes = _b64url_decode(payload_part)
        signature_bytes = _b64url_decode(signature_part)
    except (binascii.Error, ValueError):
        return None

    try:
        api_key = api_key_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None

    expected_signature = hmac.new(secret.encode("utf-8"), api_key_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, signature_bytes):
        return None

    try:
        payload = SessionCookiePayload(api_key=api_key)
    except ValidationError:
        return None

    return payload.api_key
