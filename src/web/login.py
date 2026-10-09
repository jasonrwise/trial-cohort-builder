"""`GET`/`POST /web/login`, `GET /web/logout` (06-06-PLAN.md Task 1,
WEBAPP-01, `specs/004-web-app-interface/contracts/login.json`;
08.1-01-PLAN.md Task 1 adds `GET /web/logout`, WEBAPP-08, SPEC R2/D-01).

Pre-authentication endpoint (AD-18: no role gate) — guarded by AD-16's
lockout instead. Mirrors `src/tools/get_protocol_criteria.py`'s
validate-then-delegate-then-map-errors shape (RESEARCH.md Pattern 1): the
lockout check runs first (before the key is ever resolved to a role), then
`access_control.resolve_role`, then a distinct response per outcome.
`record_login_attempt` runs on every resolved (non-locked-out) outcome, and
one `ACCESS_DENIED` audit line is written per refusal — never on a
locked-out attempt, so a flood of locked-out attempts cannot fill the log
(spec.md FR-004).

The raw `api_key` never enters a template context, an audit `detail`
string, or an exception message (T-06-38) — it exists only inside the
client-held HMAC-signed cookie and, for lockout, as a one-way SHA-256
hash (`session._key_hash`).

Login is never itself an htmx-fragment interaction (contracts/login.json)
— every response is a full page or a redirect, regardless of `HX-Request`.

08.2-05 (FR-005): a successful sign-in answers 303 to `/web/scorecards`
(the home screen) instead of rendering the new-screening form in place.

`GET /web/logout` (08.1-01 Task 1): always redirects to
`/web/login?notice=logged_out` and unconditionally clears `tb_session`
(same attributes `submit_login` sets it with) — with or without a valid
cookie present (D-01). The session is a stateless signed cookie with no
server-side table, so logout can only ever act on the requesting browser's
own cookie; it cannot invalidate any other session. It never reads,
verifies, or decodes the request cookie, so nothing in it can echo key
material, and it writes no audit line (successful logins aren't audited
either).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict

from src.config import Config
from src.models.access import EventType, Role
from src.models.errors import WebAccessDeniedError
from src.services import access_control, audit, session

router = APIRouter(prefix="/web")

# The two roles login admits (spec.md FR-15) — SITE_ADMIN and any future
# unregistered role are refused via the "no web app access" branch below.
_ALLOWED_LOGIN_ROLES = frozenset({Role.CRC, Role.PI})

_KEY_NOT_RECOGNIZED_REASON = "Key not recognized."
_NO_WEB_ACCESS_REASON = "This role has no web app access."
_LOCKED_OUT_REASON = (
    "Too many attempts. Try again later. Contact the site operator if you've lost your key."
)
_SESSION_ENDED_NOTICE = "Session ended. Sign in again."
_LOGGED_OUT_NOTICE = "Signed out."

# Literal-to-constant lookup for `show_login`'s `notice` query param (D-01).
# A tuple of pairs, never a dict — test_no_caching_layer.py's AST guard
# flags a module-level dict/list literal as a disguised-cache risk (06-07).
_LOGIN_NOTICES: tuple[tuple[str, str], ...] = (
    ("session_ended", _SESSION_ENDED_NOTICE),
    ("logged_out", _LOGGED_OUT_NOTICE),
)


class LoginForm(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    api_key: str


def _render_login(
    request: Request,
    *,
    notice: str | None = None,
    error: WebAccessDeniedError | None = None,
    locked: bool = False,
    status_code: int = 200,
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "page.html",
        {
            "content_template": "_login.html",
            "notice": notice,
            "error": error,
            "locked": locked,
        },
        status_code=status_code,
    )


@router.get("/login")
async def show_login(request: Request, notice: str | None = None) -> Response:
    """Only the literal values in `_LOGIN_NOTICES` (`session_ended`,
    `logged_out`) map to a notice — any other value (including an attempted
    script injection) is ignored, never reflected back into the page
    (T-06-40)."""
    notice_text = next((text for literal, text in _LOGIN_NOTICES if literal == notice), None)
    return _render_login(request, notice=notice_text)


@router.get("/logout")
async def logout(request: Request) -> Response:
    """Always redirects to `/web/login?notice=logged_out` and unconditionally
    clears `tb_session` — with or without a valid cookie present (SPEC R2,
    D-01). The stateless signed-cookie session model means this can only
    ever act on the requesting browser's own cookie; there is no
    server-side session table to invalidate another caller's session from."""
    response = RedirectResponse("/web/login?notice=logged_out", status_code=303)
    settings: Config = request.app.state.settings
    response.delete_cookie(
        session.SESSION_COOKIE_NAME,
        **session.cookie_attributes(settings.session_cookie_secure),
    )
    return response


@router.post("/login")
async def submit_login(request: Request, form: Annotated[LoginForm, Form()]) -> Response:
    """Refusals render the login page (401/403/429); a success answers 303 to
    `/web/scorecards` with `tb_session` set on that response (FR-005, D3)."""
    settings: Config = request.app.state.settings
    api_key = form.api_key

    if session.is_locked_out(api_key):
        error = WebAccessDeniedError(error="access_denied", reason=_LOCKED_OUT_REASON)
        return _render_login(request, error=error, locked=True, status_code=429)

    role = access_control.resolve_role(api_key, settings.keys_file)

    if role is None:
        session.record_login_attempt(api_key, success=False)
        audit.write_entry(
            role=None,
            nct_id=None,
            event_type=EventType.ACCESS_DENIED,
            detail="web login refused: key not recognized",
        )
        error = WebAccessDeniedError(error="access_denied", reason=_KEY_NOT_RECOGNIZED_REASON)
        return _render_login(request, error=error, status_code=401)

    if role not in _ALLOWED_LOGIN_ROLES:
        session.record_login_attempt(api_key, success=False)
        audit.write_entry(
            role=role,
            nct_id=None,
            event_type=EventType.ACCESS_DENIED,
            detail="web login refused: role has no web app access",
        )
        error = WebAccessDeniedError(error="access_denied", reason=_NO_WEB_ACCESS_REASON)
        return _render_login(request, error=error, status_code=403)

    session.record_login_attempt(api_key, success=True)

    # FR-005 / D3 (08.2-05): land on the Scorecards list. The target is a fixed
    # literal (no request input reaches it) and the cookie rides on the redirect.
    response = RedirectResponse("/web/scorecards", status_code=303)
    response.set_cookie(
        session.SESSION_COOKIE_NAME,
        session.sign_session_cookie(api_key, settings.session_signing_secret),
        **session.cookie_attributes(settings.session_cookie_secure),
    )
    return response
