"""`GET /web/scorecards/{ref}` (08-01-PLAN.md Task 1/Task 2, tasks.md
T005/T008/T010, contracts/get_scorecard.json, FR-004).

Gated centrally in `src/web/app.py` behind `require_web_endpoint(
"get_scorecard")` (AD-18). Reads the stored `ScreenedRecord` and never
recomputes it (EXPERIENCE.md: auto-recompute on open is rejected) -- derives
the finalized state fresh via `audit.get_finalization` (AD-17). Its render
helper (`render_scorecard_detail`) is also the render path re-run results use
(08-03), so get and re-run bodies cannot diverge.

Task 2 adds the saved/aged provenance banner (`saved_banner_for`,
`SAVED_SCORECARD_AGED_AFTER`, EXPERIENCE.md [ASSUMPTION: 7 days]) -- shown
only when a scorecard is opened directly (`saved_view=True`), never on the
fresh post-screening render `query_screening.create_screening` produces --
and the dedicated Scorecard-not-found panel (`render_scorecard_not_found`),
which 08-03's re-run also reuses for its own 404 branch.

08-03-PLAN.md Task 1 adds the `can_rerun` context key (`approval is None`,
FR-007 -- the Re-run control is hidden outright on a FINALIZED scorecard,
never merely disabled). Task 2 adds the optional `rejection` keyword so
`rerun_scorecard.py` can render its 409 `rerun_on_finalized` refusal through
this exact same detail-render path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.models.errors import BusinessRejectionError, NotFoundError
from src.models.scorecard import SCORECARD_REF_PATTERN, ScreenedRecord
from src.models.trial import CriterionKind
from src.services import audit
from src.services.anchor import derive_cohort_anchor
from src.web import rendering

router = APIRouter(prefix="/web")

SAVED_SCORECARD_AGED_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class SavedBanner:
    aged: bool
    age_days: int


def saved_banner_for(screened_at: datetime, now: datetime) -> SavedBanner:
    """`aged` is `True` only strictly past `SAVED_SCORECARD_AGED_AFTER`
    (exactly 7 days stays neutral) -- EXPERIENCE.md [ASSUMPTION: 7 days]."""
    age = now - screened_at
    return SavedBanner(aged=age > SAVED_SCORECARD_AGED_AFTER, age_days=age.days)


def render_scorecard_detail(
    request: Request,
    record: ScreenedRecord,
    *,
    saved_view: bool = False,
    rejection: BusinessRejectionError | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    anchor = derive_cohort_anchor(
        [c for c in record.criteria_snapshot if c.kind is CriterionKind.INCLUSION]
    )
    approval = audit.get_finalization(record.ref, request.app.state.settings.scorecard_store_path)
    saved_banner = saved_banner_for(record.screened_at, datetime.now(UTC)) if saved_view else None
    return rendering.render(
        request,
        request.app.state.templates,
        "_screening_step3.html",
        {
            "role": request.state.role.value,
            "record": record,
            "anchor": anchor,
            "approval": approval,
            "saved_banner": saved_banner,
            "can_rerun": approval is None,
            "rejection": rejection,
        },
        status_code=status_code,
        headers=headers,
    )


def render_scorecard_not_found(request: Request, error: NotFoundError) -> Response:
    """A dedicated Scorecard-not-found panel with a Back to Scorecards link
    (08-01-PLAN.md Task 2, tasks.md T005) -- 08-03's re-run reuses this for
    its own 404 branch."""
    return rendering.render(
        request,
        request.app.state.templates,
        "_scorecard_not_found.html",
        {"role": request.state.role.value, "error": error.error, "reason": error.reason},
        status_code=404,
    )


@router.get("/scorecards/{ref}")
async def get_scorecard(request: Request, ref: str) -> Response:
    store_path = request.app.state.settings.scorecard_store_path

    if not SCORECARD_REF_PATTERN.match(ref):
        error = NotFoundError(error="not_found", reason="Scorecard not found.")
        return render_scorecard_not_found(request, error)

    record = audit.get_screened_record(ref, store_path)
    if record is None:
        error = NotFoundError(error="not_found", reason=f"Scorecard {ref} not found.")
        return render_scorecard_not_found(request, error)

    return render_scorecard_detail(request, record, saved_view=True)
