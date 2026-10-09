"""htmx full-page-vs-fragment dispatch helper (06-03-PLAN.md Task 1,
AD-24 §8).

Defined once, shared by every `src/web/*.py` endpoint's response-assembly
step — not reinvented per endpoint. HTTP status code and underlying data are
identical either way; only the surrounding HTML differs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from src.services import anchor, synthetic_label


def build_templates(directory: Path) -> Jinja2Templates:
    """The one template-environment factory (WD-4). Render-time derivations are
    Jinja globals registered here once, so `create_app` and any test that builds
    its own environment share them and no handler passes a flag. Later plans add
    their globals in this function."""
    templates = Jinja2Templates(directory=str(directory))
    templates.env.globals["is_synthetic"] = synthetic_label.is_synthetic
    templates.env.globals["query_key_positions"] = anchor.query_key_positions
    templates.env.globals["unhonored_criteria_count"] = anchor.unhonored_criteria_count
    templates.env.globals["coverage_gap_warning"] = anchor.coverage_gap_warning
    templates.env.globals["unevaluated_exclusion_count"] = anchor.unevaluated_exclusion_count
    return templates


def is_htmx(request: Request) -> bool:
    """True when the request carries htmx's own `HX-Request` header."""
    return "HX-Request" in request.headers


def render(
    request: Request,
    templates: Jinja2Templates,
    fragment_template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    full_page_template: str = "page.html",
    headers: dict[str, str] | None = None,
) -> Response:
    """Renders `fragment_template` alone when the request is an htmx
    request, or `full_page_template` (with `fragment_template` as its
    `content_template`) otherwise. Always adds `Vary: HX-Request`."""
    response_headers = dict(headers or {})
    response_headers["Vary"] = "HX-Request"

    if is_htmx(request):
        return templates.TemplateResponse(
            request,
            fragment_template,
            context,
            status_code=status_code,
            headers=response_headers,
        )

    full_context = {**context, "content_template": fragment_template}
    return templates.TemplateResponse(
        request,
        full_page_template,
        full_context,
        status_code=status_code,
        headers=response_headers,
    )
