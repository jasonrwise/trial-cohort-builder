"""`POST /web/scorecards/{ref}/approve` — PI-only approve/finalize
(07-01-PLAN.md Task 1, T033, contracts/approve_finalize.json; every
rejection branch completed by 07-02-PLAN.md Task 2).

Gated centrally in `src/web/app.py` behind `require_web_endpoint(
"approve_finalize")` (AD-18) -- a CRC session never reaches this handler's
body. Approves a saved `ScreenedRecord` by reference + NCT ID only; the
request never carries candidate or eligibility data (FR-017). Writes exactly
one `ApprovalRecord` on success (AD-17) and nothing to the rotating audit log
on success -- the `ApprovalRecord` itself is the durable record. Sends no
notification of any kind, ever (FR-020) -- this module's own import
allow-list is enforced structurally by tests/unit/test_web_no_external_
notification.py (07-01-PLAN.md Task 2).

Every rejection branch (404 not_found / 409 reference_protocol_mismatch /
409 already_finalized) writes exactly one `FINALIZE_REJECTED` rotating-
audit-log line (data-model.md) via `_reject`, and renders an inline error
into the PI action bar's `#finalize-feedback` region (`HX-Retarget`/
`HX-Reswap` headers) -- except already_finalized, which reloads the
FINALIZED state of the ORIGINAL approval into `#screening-panel` (EXPERIENCE
"Already finalized"). The already-finalized check and the `ApprovalRecord`
append happen inside one hold of the store lock
(`audit.append_approval_if_absent`, research.md §9, AD-17) -- this closes
the concurrent double-submit race the 07-01 tracer left open.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from src.models.access import EventType
from src.models.errors import BusinessRejectionError, NotFoundError
from src.models.scorecard import SCORECARD_REF_PATTERN, ApprovalRecord, ScreenedRecord
from src.models.trial import CriterionKind
from src.services import audit
from src.services.anchor import derive_cohort_anchor
from src.web import rendering

router = APIRouter(prefix="/web")

# Header pairs for the two rejection swap targets (07-02-PLAN.md Task 2).
# Tuples-of-pairs, never a module-level dict/list literal
# (test_no_caching_layer.py's AST guard flags those as a disguised-cache
# risk unless already on its named role-allow-list).
_INLINE_REJECT_HEADERS: tuple[tuple[str, str], ...] = (
    ("HX-Retarget", "#finalize-feedback"),
    ("HX-Reswap", "innerHTML"),
)
_ALREADY_FINALIZED_HEADERS: tuple[tuple[str, str], ...] = (
    ("HX-Retarget", "#screening-panel"),
    ("HX-Reswap", "outerHTML"),
)


class FinalizeForm(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    nct_id: str


def _render_panel(
    request: Request,
    record: ScreenedRecord,
    approval: ApprovalRecord,
    *,
    focus_banner: bool = False,
    finalize_notice: str | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    anchor = derive_cohort_anchor(
        [c for c in record.criteria_snapshot if c.kind is CriterionKind.INCLUSION]
    )
    return rendering.render(
        request,
        request.app.state.templates,
        "_screening_step3.html",
        {
            "role": request.state.role.value,
            "record": record,
            "anchor": anchor,
            "approval": approval,
            "focus_banner": focus_banner,
            "finalize_notice": finalize_notice,
        },
        status_code=status_code,
        headers=headers,
    )


def _ref_for_log(ref: str) -> str:
    """Never lets an attacker-controlled path value reach a log line: `ref`
    verbatim when it matches `SCORECARD_REF_PATTERN`, else the fixed text
    `"malformed ref"` (T-07-16)."""
    return ref if SCORECARD_REF_PATTERN.match(ref) else "malformed ref"


def _reject(
    request: Request,
    ref: str,
    error: NotFoundError | BusinessRejectionError,
    *,
    status_code: int,
    nct_id_for_log: str | None,
) -> Response:
    """Writes one `FINALIZE_REJECTED` rotating-audit-log line -- never the
    submitted `nct_id`, only the stored record's (or `None` for a 404,
    T-07-16) -- then renders `_finalize_rejected.html` into the PI action
    bar's `#finalize-feedback` region (data-model.md, EXPERIENCE "Approve
    rejected by server")."""
    audit.write_entry(
        role=request.state.role,
        nct_id=nct_id_for_log,
        event_type=EventType.FINALIZE_REJECTED,
        detail=f"approve_finalize rejected: {error.error} for {_ref_for_log(ref)}",
    )
    return rendering.render(
        request,
        request.app.state.templates,
        "_finalize_rejected.html",
        {"role": request.state.role.value, "error": error.error, "reason": error.reason},
        status_code=status_code,
        headers=dict(_INLINE_REJECT_HEADERS),
    )


@router.post("/scorecards/{ref}/approve")
async def approve_finalize(
    request: Request, ref: str, form: Annotated[FinalizeForm, Form()]
) -> Response:
    settings = request.app.state.settings
    store_path = settings.scorecard_store_path

    if not SCORECARD_REF_PATTERN.match(ref):
        error = NotFoundError(error="not_found", reason="Scorecard not found.")
        return _reject(request, ref, error, status_code=404, nct_id_for_log=None)

    record = audit.get_screened_record(ref, store_path)
    if record is None:
        error = NotFoundError(error="not_found", reason=f"Scorecard {ref} not found.")
        return _reject(request, ref, error, status_code=404, nct_id_for_log=None)

    if form.nct_id != record.nct_id:
        error = BusinessRejectionError(
            error="reference_protocol_mismatch",
            reason=f"NCT ID does not match scorecard {ref}. Nothing was finalized.",
        )
        return _reject(request, ref, error, status_code=409, nct_id_for_log=record.nct_id)

    approval = ApprovalRecord(
        record_type="approval",
        screened_ref=record.ref,
        approved_by_role=request.state.role.value,
        approved_at=datetime.now(UTC),
    )
    existing = await audit.append_approval_if_absent(approval, store_path)

    if existing is not None:
        error = BusinessRejectionError(
            error="already_finalized",
            reason=f"Scorecard {ref} was already finalized. Nothing was changed.",
        )
        audit.write_entry(
            role=request.state.role,
            nct_id=record.nct_id,
            event_type=EventType.FINALIZE_REJECTED,
            detail=f"approve_finalize rejected: {error.error} for {_ref_for_log(ref)}",
        )
        return _render_panel(
            request,
            record,
            existing,
            focus_banner=True,
            finalize_notice=error.reason,
            status_code=409,
            headers=dict(_ALREADY_FINALIZED_HEADERS),
        )

    return _render_panel(request, record, approval, focus_banner=True)
