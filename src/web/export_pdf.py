"""`GET /web/scorecards/{ref}/pdf` (09-01-PLAN.md Task 1, tasks.md T049/T051,
contracts/export_pdf.json, AD-19, FR-027/FR-028).

Renders the stored `ScreenedRecord` only -- never a live re-query -- together
with the fresh `audit.get_finalization` fact, through `src/services/pdf.py`.
The route is read-only on every outcome: it calls only the two read-side
store lookups, never a write function. It is gated centrally in
`src/web/app.py` behind the `export_pdf` allow-list row (AD-18), never per
handler.

Its 200 is a direct browser download and is not part of AD-24's htmx dispatch:
it never goes through `rendering.render` and never reads the HX-Request
header. Only the 404 reuses the shared HTML rendering (the Scorecard-not-found
panel).

09-02: a render failure or an unreadable store (ScorecardStoreCorruptError,
OSError) is mapped to the contract's fixed 500 `pdf_generation_failed` page
(shared `_error.html`); the exception class (and a corrupt line number) goes
to stderr only and nothing is written on those paths.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.models.errors import NotFoundError, PdfGenerationFailedError
from src.models.scorecard import SCORECARD_REF_PATTERN, ScreenedRecord
from src.services import audit, pdf
from src.web import rendering
from src.web.get_scorecard import render_scorecard_not_found

router = APIRouter(prefix="/web")

# A tuple of pairs, never a dict (test_no_caching_layer.py's AST guard flags a
# module-level dict/list literal; ANTI_FRAMING_HEADERS is the precedent).
# no-store because the PDF carries candidate-level evidence off-system.
PDF_RESPONSE_HEADERS: tuple[tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
)


# EXPERIENCE.md "PDF failed" copy; never carries exception text.
PDF_GENERATION_FAILED_REASON = "PDF could not be generated."


def render_pdf_generation_failed(request: Request) -> Response:
    """The contract's 500 body through the shared error page (full page
    without HX-Request, fragment with it). Carries the role like the
    authenticated 404 does; no detail list."""
    error = PdfGenerationFailedError(
        error="pdf_generation_failed", reason=PDF_GENERATION_FAILED_REASON
    )
    return rendering.render(
        request,
        request.app.state.templates,
        "_error.html",
        {
            "role": request.state.role.value,
            "error": error.error,
            "reason": error.reason,
            "detail": None,
        },
        status_code=500,
    )


def _export_failed(request: Request, ref: str, exc: Exception) -> Response:
    """The one failure path for an unreadable store and a render failure.
    Only the exception class (and a corrupt store's line number) reaches
    stderr; the response carries the fixed reason and nothing from `exc`."""
    line_number = getattr(exc, "line_number", None)
    line_detail = f" line {line_number}" if line_number is not None else ""
    print(f"PDF EXPORT FAILED ref={ref}: {type(exc).__name__}{line_detail}", file=sys.stderr)
    return render_pdf_generation_failed(request)


def pdf_filename(record: ScreenedRecord) -> str:
    """Both fields are validated by `ScreenedRecord` itself (NCT_ID_PATTERN,
    SCORECARD_REF_PATTERN), so the name is plain ASCII and cannot carry a
    quote, CR, LF or path separator."""
    return f"trialbridge-scorecard-{record.nct_id}-{record.ref}.pdf"


@router.get("/scorecards/{ref}/pdf")
async def export_pdf(request: Request, ref: str) -> Response:
    store_path = request.app.state.settings.scorecard_store_path

    if not SCORECARD_REF_PATTERN.match(ref):
        error = NotFoundError(error="not_found", reason="Scorecard not found.")
        return render_scorecard_not_found(request, error)

    try:
        record = audit.get_screened_record(ref, store_path)
        approval = audit.get_finalization(ref, store_path)
    except (audit.ScorecardStoreCorruptError, OSError) as exc:
        return _export_failed(request, ref, exc)

    if record is None:
        error = NotFoundError(error="not_found", reason=f"Scorecard {ref} not found.")
        return render_scorecard_not_found(request, error)

    try:
        body = pdf.render_scorecard_pdf(record, approval)
    except Exception as exc:  # noqa: BLE001 - any render failure maps to the fixed 500
        return _export_failed(request, ref, exc)

    headers = dict(PDF_RESPONSE_HEADERS)
    headers["Content-Disposition"] = f'attachment; filename="{pdf_filename(record)}"'
    return Response(content=body, media_type="application/pdf", headers=headers)
