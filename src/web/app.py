"""The FastAPI web app (06-03-PLAN.md Task 1, T015 (b)-(f); the AD-26
fail-fast half, (a), arrives in Task 2) — one ASGI worker only (AD-21).

`create_app()` loads config, builds the audit logger, constructs the FastAPI
app with its docs/redoc/openapi surface disabled (a browser app exposes no
API schema), wires the Jinja2 template environment and the vendored static
assets, registers the `SessionEnded`/`WebAccessDenied` exception handlers,
and mounts `query_screening.router` behind a single, centrally-applied
`require_web_endpoint` gate (AD-14 steps 2-3, AD-18) — the gate runs before
any handler body, exactly mirroring `src/server.py`'s `RBACGateMiddleware`
shape for the web surface.

`current_role` re-derives the caller's role fresh on every single request
from the signed session cookie plus `access_control.resolve_role` — never
cached on `app.state` or at module scope (AD-2, AD-15). Module-level
`__getattr__` provides the lazily-constructed `app` attribute so
`uvicorn src.web.app:app` works while importing this module has no side
effects.

07-01-PLAN.md Task 1 mounts `approve_finalize.router` behind its own
`require_web_endpoint("approve_finalize")` gate (PI-only, AD-18) — the
phase's first irreversible web action. Task 2 adds the `_forbid_framing`
middleware: the first irreversible web action made framing protection
necessary, revisiting the clickjacking risk Phase 6 accepted as T-06-25
("no irreversible action before Phase 7").

08-01-PLAN.md Task 1 mounts `list_scorecards.router` and `get_scorecard.
router` behind their own `require_web_endpoint("list_scorecards"/
"get_scorecard")` gates (CRC/PI, AD-18) — every CRC or PI session lists
every saved scorecard and opens any one at its stable per-reference link.

08-03-PLAN.md Task 1 mounts `rerun_scorecard.router` behind its own
`require_web_endpoint("rerun_scorecard")` gate (CRC/PI, AD-18) — a CRC or PI
re-runs a saved, not-yet-finalized scorecard into a brand-new, separately
referenced one.

09-01-PLAN.md Task 1 mounts `export_pdf.router` behind its own
`require_web_endpoint(
"export_pdf")` gate (CRC/PI, AD-18): any CRC or PI
session downloads any scorecard it can view as a PDF, read-only.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from src import config
from src.config import Config
from src.models.access import EventType, Role
from src.models.errors import WebAccessDeniedError
from src.services import access_control, audit, session
from src.web import (
    approve_finalize,
    export_pdf,
    get_scorecard,
    list_scorecards,
    login,
    query_screening,
    rendering,
    rerun_scorecard,
)

_PACKAGE_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_STATIC_DIR = _PACKAGE_DIR / "static"

# 07-01-PLAN.md Task 2: applied to every response by the `_forbid_framing`
# middleware below. A tuple of pairs, never a dict (test_no_caching_layer.py's
# AST guard flags a module-level dict/list literal as a disguised-cache
# risk). Only the frame-ancestors directive is set -- no script/style policy,
# since htmx and app.js must keep loading unchanged.
ANTI_FRAMING_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
)


class SessionEnded(Exception):
    """Raised by `current_role` when no valid, currently-mapped role can be
    resolved from the request's session cookie — a missing cookie, a
    tampered/malformed one, or one signed for a key no longer mapped to a
    role are all indistinguishable (AD-15)."""

    def __init__(self, had_cookie: bool) -> None:
        super().__init__("Session ended")
        self.had_cookie = had_cookie


class WebAccessDenied(Exception):
    """Raised by `require_web_endpoint`'s dependency when the resolved role
    is not on `ALLOWED_ROLES_BY_WEB_ENDPOINT`'s allow-list for this
    endpoint (AD-18)."""

    def __init__(self, endpoint_name: str, role: Role) -> None:
        super().__init__(f"Access denied for {endpoint_name}")
        self.endpoint_name = endpoint_name
        self.role = role


async def current_role(request: Request) -> Role:
    """Reads the `tb_session` cookie, verifies it, and re-resolves the
    caller's role fresh from the key file on every call — never cached on
    `request.app.state` or at module scope (AD-2, AD-15)."""
    settings: Config = request.app.state.settings
    cookie_value = request.cookies.get(session.SESSION_COOKIE_NAME)

    api_key = session.verify_session_cookie(cookie_value, settings.session_signing_secret)
    role = access_control.resolve_role(api_key, settings.keys_file) if api_key is not None else None

    if role is None:
        raise SessionEnded(had_cookie=cookie_value is not None)
    return role


def require_web_endpoint(endpoint_name: str) -> Callable[..., Role]:
    """Returns a FastAPI dependency gating `endpoint_name` against
    `ALLOWED_ROLES_BY_WEB_ENDPOINT` (AD-18) — applied once, centrally, at
    router-inclusion time, never per-handler."""

    async def _dependency(request: Request, role: Role = Depends(current_role)) -> Role:
        if not access_control.is_allowed_web_endpoint(endpoint_name, role):
            audit.write_entry(
                role=role,
                nct_id=None,
                event_type=EventType.ACCESS_DENIED,
                detail=f"denied on web endpoint {endpoint_name}",
            )
            raise WebAccessDenied(endpoint_name, role)
        request.state.role = role
        return role

    return _dependency


async def _session_ended_handler(request: Request, exc: SessionEnded) -> Response:
    """06-06-PLAN.md Task 2: replaces the tracer's minimal 401 (FR-006). A
    request whose key was rotated or revoked ends on its very next request,
    with no restart -- a GET/HEAD request (a page) redirects (303) to the
    login page; every other method (the mutating contract endpoints) keeps
    its 401 status regardless of request kind (AD-20/AD-24) -- only the
    body/header differ by htmx vs. full-page. A prior session cookie is
    always cleared, with the same attributes it was set with."""
    target = "/web/login?notice=session_ended" if exc.had_cookie else "/web/login"

    response: Response
    if request.method in ("GET", "HEAD"):
        response = RedirectResponse(target, status_code=303)
    elif rendering.is_htmx(request):
        templates = request.app.state.templates
        error = WebAccessDeniedError(error="access_denied", reason="Session ended. Sign in again.")
        response = templates.TemplateResponse(
            request,
            "_inline_error.html",
            {"message": error.reason, "retry_href": None, "error_id": None},
            status_code=401,
            headers={"HX-Redirect": target},
        )
    else:
        templates = request.app.state.templates
        notice = "Session ended. Sign in again." if exc.had_cookie else None
        response = templates.TemplateResponse(
            request,
            "page.html",
            {"content_template": "_login.html", "notice": notice, "error": None, "locked": False},
            status_code=401,
        )

    if exc.had_cookie:
        settings: Config = request.app.state.settings
        response.delete_cookie(
            session.SESSION_COOKIE_NAME,
            **session.cookie_attributes(settings.session_cookie_secure),
        )
    return response


async def _web_access_denied_handler(request: Request, exc: WebAccessDenied) -> Response:
    """06-04-PLAN.md Task 3: renders through `_error.html` at 403. The
    reason is the fixed WebAccessDeniedError content -- it never echoes the
    resolved role, and `role` is deliberately absent from the render
    context so no banner renders for a denied caller (T-06-23)."""
    error = WebAccessDeniedError(error="access_denied", reason="Access denied for this action.")
    templates = request.app.state.templates
    return rendering.render(
        request,
        templates,
        "_error.html",
        {"error": error.error, "reason": error.reason, "detail": None},
        status_code=403,
    )


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
    """06-04-PLAN.md Task 3: renders through `_error.html` at 422. `detail`
    is the framework-idiomatic list from `exc.errors()` (AD-24/web-errors.json
    ValidationError), reduced to a joined `loc` and `msg` string pair."""
    templates = request.app.state.templates
    detail = [
        {"loc": " -> ".join(str(part) for part in error["loc"]), "msg": error["msg"]}
        for error in exc.errors()
    ]
    return rendering.render(
        request,
        templates,
        "_error.html",
        {
            "error": "validation_error",
            "reason": "The submitted form is invalid.",
            "detail": detail,
        },
        status_code=422,
    )


def check_web_boot_config(settings: Config) -> None:
    """AD-26's web-process-own fail-fast half (T015 (a)) — the shared
    `config.load_config()` deliberately never validates these four fields
    (Pitfall 5), so this is the ONLY place a misconfigured web deployment
    dies at process start. Names the purpose/env var only — never the
    secret/key value itself (T-06-17).

    A fifth check (AD-35, WD-3) runs last so the four checks above keep their
    failure-message order: `SESSION_COOKIE_SECURE` must be unset, ``true`` or
    ``false`` — anything else exits naming the variable but never echoing the
    value, and ``false`` logs one warning that the cookie loses Secure."""
    for value, purpose, env_var in (
        (settings.session_signing_secret, "session signing secret", "TRIALBRIDGE_SESSION_SECRET"),
        (settings.anthropic_api_key, "Anthropic API key", "ANTHROPIC_API_KEY"),
        (settings.anthropic_model, "Anthropic model id", "ANTHROPIC_MODEL"),
    ):
        if not value:
            sys.exit(f"FATAL: {purpose} not set (set via {env_var}). Web server will not start.")

    store_path = settings.scorecard_store_path
    parent = os.path.dirname(store_path) or "." if store_path else None
    file_exists_and_unwritable = (
        bool(store_path) and os.path.exists(store_path) and not os.access(store_path, os.W_OK)
    )
    store_unwritable = (
        not store_path
        or not os.path.isdir(parent)
        or not os.access(parent, os.W_OK)
        or file_exists_and_unwritable
    )
    if store_unwritable:
        sys.exit(
            f"FATAL: scorecard store not writable at '{store_path}' "
            "(set via TRIALBRIDGE_SCORECARD_STORE_PATH). Web server will not start."
        )

    cookie_secure = session.parse_cookie_secure(settings.session_cookie_secure)
    if cookie_secure is None:
        sys.exit(
            "FATAL: invalid SESSION_COOKIE_SECURE (expected 'true' or 'false'; "
            "unset means true). Web server will not start."
        )
    if cookie_secure is False:
        logging.getLogger(__name__).warning(
            "SESSION_COOKIE_SECURE=false sends the session cookie without the Secure "
            "attribute; this is meant only for the loopback demo stack."
        )


def create_app(settings: Config | None = None) -> FastAPI:
    settings = settings or config.load_config()
    check_web_boot_config(settings)
    audit.build_audit_logger(settings.audit_log_path)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.templates = rendering.build_templates(_TEMPLATES_DIR)

    app.mount("/web/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.middleware("http")
    async def _forbid_framing(request: Request, call_next: Callable) -> Response:
        """07-01-PLAN.md Task 2 (T-07-04): sets `ANTI_FRAMING_HEADERS` on
        every response this app produces -- every route, static files,
        exception-handler responses and redirects -- since middleware wraps
        the whole ASGI call chain."""
        response = await call_next(request)
        for name, value in ANTI_FRAMING_HEADERS:
            response.headers[name] = value
        return response

    app.add_exception_handler(SessionEnded, _session_ended_handler)
    app.add_exception_handler(WebAccessDenied, _web_access_denied_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)

    # Login is pre-authentication (AD-18: its allow-list row admits no
    # roles) -- gated by AD-16's lockout instead, never by
    # require_web_endpoint.
    app.include_router(login.router)

    app.include_router(
        query_screening.router,
        dependencies=[Depends(require_web_endpoint("query_screening"))],
    )

    app.include_router(
        approve_finalize.router,
        dependencies=[Depends(require_web_endpoint("approve_finalize"))],
    )

    app.include_router(
        list_scorecards.router,
        dependencies=[Depends(require_web_endpoint("list_scorecards"))],
    )

    app.include_router(
        get_scorecard.router,
        dependencies=[Depends(require_web_endpoint("get_scorecard"))],
    )

    app.include_router(
        rerun_scorecard.router,
        dependencies=[Depends(require_web_endpoint("rerun_scorecard"))],
    )

    app.include_router(
        export_pdf.router,
        dependencies=[Depends(require_web_endpoint("export_pdf"))],
    )

    @app.get("/")
    async def redirect_root() -> Response:
        return RedirectResponse("/web/scorecards", status_code=303)

    @app.get("/web")
    async def redirect_web_root() -> Response:
        return RedirectResponse("/web/scorecards", status_code=303)

    return app


def __getattr__(name: str):
    if name == "app":
        return create_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
