"""`POST /web/scorecards/{ref}/rerun` (08-03-PLAN.md Task 1/Task 2, tasks.md
T015, contracts/rerun_scorecard.json, spec.md US2, FR-006/FR-007/FR-008).

Gated centrally in `src/web/app.py` behind `require_web_endpoint(
"rerun_scorecard")` (AD-18, CRC/PI). No request body — the "Re-run
screening" control posts with no named input (contract `requestBody: null`).
Re-runs the live screening pipeline for the source `ScreenedRecord`'s
`nct_id` only — never its stored `criteria_snapshot`, `trial_title`, or
`candidates` (spec.md Edge Cases: criteria are re-verified at re-run time,
never copied from the snapshot). On success, appends exactly one brand-new
`ScreenedRecord` under a fresh `audit.new_scorecard_ref()` and renders it
through `get_scorecard.render_scorecard_detail` — the same detail-render
path `GET /web/scorecards/{ref}` uses, so a re-run result and a directly
opened scorecard can never diverge (contracts/rerun_scorecard.json's `$ref`
to `get_scorecard.json`'s 200 body). The source record is never modified by
any branch of this handler.

Lock discipline (08-RESEARCH.md Pattern 3, correcting plan.md's own
wording): `audit.is_finalized` is a pure, synchronous, lock-free read — it
never takes `_SCORECARD_STORE_LOCK`. The only place a lock is taken at all
is inside `audit.append_screened_record` (via `_append_line`), which
acquires and releases it around its own single write. This module therefore
acquires no lock itself; the live pipeline (trial fetch, criteria verify,
cohort fetch, AD-25 LLM-proposal fan-out) always runs fully unlocked, and
the append that follows a successful outcome is a single, already-lock-
scoped call — never a second explicit acquire/release around it.

A source already finalized (`audit.is_finalized`, a lock-free fresh read
checked before any upstream call or write) is rejected 409
`rerun_on_finalized` with nothing changed — it renders the source's own
FINALIZED view (original `finalized_at`, no Re-run control) plus the
refusal, through the identical `render_scorecard_detail` path a 200 result
uses. A source finalized during this handler's own unlocked pipeline window
is an accepted, benign race (specs/005 research.md §2): the new record still
lands under its own independent ref, and the source stays untouched either
way.

This module sends no notification of any kind, ever — the only outbound
calls a re-run makes are the screening pipeline's own ClinicalTrials.gov,
NLM, FHIR, and Anthropic requests (the web app has no notification path;
FR-19 excludes new channels). Enforced structurally by tests/unit/
test_web_no_external_notification.py's import allow-list, same as every
other `src/web/*.py` module.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from src.models.errors import BusinessRejectionError, NotFoundError
from src.models.scorecard import SCORECARD_REF_PATTERN
from src.services import audit
from src.web import query_screening
from src.web.get_scorecard import render_scorecard_detail, render_scorecard_not_found

router = APIRouter(prefix="/web")


@router.post("/scorecards/{ref}/rerun")
async def rerun_scorecard(request: Request, ref: str) -> Response:
    settings = request.app.state.settings
    store_path = settings.scorecard_store_path

    if not SCORECARD_REF_PATTERN.match(ref):
        error = NotFoundError(error="not_found", reason="Scorecard not found.")
        return render_scorecard_not_found(request, error)

    source = audit.get_screened_record(ref, store_path)
    if source is None:
        error = NotFoundError(error="not_found", reason=f"Scorecard {ref} not found.")
        return render_scorecard_not_found(request, error)

    if audit.is_finalized(ref, store_path):
        rejection = BusinessRejectionError(
            error="rerun_on_finalized",
            reason=f"Scorecard {ref} is finalized and cannot be re-run. Nothing was changed.",
        )
        return render_scorecard_detail(
            request, source, saved_view=True, rejection=rejection, status_code=409
        )

    outcome = await query_screening.run_screening_pipeline(
        source.nct_id, request.state.role, settings
    )
    if not isinstance(outcome, query_screening.PipelineScored):
        return query_screening.render_pipeline_stop(request, source.nct_id, outcome)

    await audit.append_screened_record(outcome.record, store_path)

    return render_scorecard_detail(request, outcome.record)
