"""`GET /web/screenings/new` and `POST /web/screenings` — the step 1 NCT ID
form and the single-endpoint screening action (WEBAPP-02/WEBAPP-03,
06-03-PLAN.md Task 1 + 06-04-PLAN.md Task 1, T028/T029).

Collapses what the wizard UI shows as two steps (fetch criteria, then query
cohort) into one backend call. The criteria pipeline (AD-22 — fully
deterministic, no LLM mapping call) and the cohort core (fetch, de-identify,
score; AD-14) live once in `src/services/screening.py` (AD-27), shared with
`src/tools/get_protocol_criteria.py` and `src/tools/query_patient_cohort.py`,
so the web and MCP surfaces behave identically by construction. This module
keeps the AD-25 fan-out and the presentation mapping (reason tables, 422 vs
DEGRADED, `ScreenedRecord` construction).
The one addition with no MCP-surface analog is the per-candidate
`llm_proposal.propose_status` fan-out (AD-25): if any candidate's proposal
call fails, the whole request aborts with 502 `llm_unavailable` and no
`ScreenedRecord` is ever constructed — the store lock is never taken during
this fan-out (Pitfall 3).

The two 06-01 option-a criteria-only outcomes (every criterion UNMAPPED; no
verified ICD-10-CM inclusion criterion to anchor a cohort query) render the
criteria and stop — no fhir/llm_proposal/audit call, no ScreenedRecord
(06-04-PLAN.md Task 1, spec.md Edge Case).

Trial-fetch/segmentation/cohort-fetch upstream-error mapping (06-07-PLAN.md
Task 1, 06-01 option-a): a malformed or unknown NCT ID, or unsegmentable
eligibility text, is a 422 rendered on step 1. Every other `TrialFetchError`/
`FhirFetchError` member (rate-limit exhaustion, upstream error, invalid
response, FHIR circuit-open) returns HTTP 200 with a `DegradedEnvelope`
whose `reason` is identical to the MCP surface's own reason text for that
same member (`get_protocol_criteria.py`/`query_patient_cohort.py`) — never a
silent reuse of their `ClientError` mapping over HTTP 200. The LLM proposal
502 (Task 2, AD-25) stays the only true HTTP error this endpoint produces;
it is never conflated with the DegradedEnvelope branches above (Pitfall 2).

08-03-PLAN.md Task 1 extracts the trial-fetch -> criteria-verify ->
cohort-fetch -> AD-25 proposal-fan-out -> `ScreenedRecord`-construction body
into `run_screening_pipeline(nct_id, role, settings)` (a pure, compute-only
`PipelineOutcome` producer -- it never appends, renders, or touches the
request, and never takes the scorecard-store lock), plus
`render_pipeline_stop` for its non-scored outcomes. `create_screening`
itself is unchanged in behavior: it still validates the NCT ID format, then
delegates to the extracted pipeline and renders exactly as before. This
extraction lets `src/web/rerun_scorecard.py` run the identical live pipeline
for a saved scorecard's `nct_id` without duplicating any of this logic.

A terminology-service outage (14-04-PLAN.md, TERM-01, AD-34) is the step-1 DEGRADED stop
with the shared `terminology.TERMINOLOGY_UNAVAILABLE_REASON`: no `ScreenedRecord`, no audit
line, no criteria rows, and the banner is identical for every role. Re-run reaches it
through the same `run_screening_pipeline`, so a Re-run during an outage appends nothing
and leaves the source scorecard untouched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

from src.config import Config
from src.models.access import Role
from src.models.errors import ClientError, DegradedEnvelope, LlmUnavailableError
from src.models.scorecard import CandidateResult, ScreenedRecord
from src.models.trial import EligibilityCriterion, MappingStatus
from src.services import audit, fhir, llm_proposal, screening, terminology, trials
from src.services.anchor import CohortAnchor, derive_cohort_anchor
from src.web import rendering

router = APIRouter(prefix="/web")

_MALFORMED_NCT_ID_REASON = "Malformed NCT ID format: expected NCT followed by 8 digits."

# Parity reason strings (06-07-PLAN.md Task 1, 06-01 option-a): must equal
# src/tools/get_protocol_criteria.py's / src/tools/query_patient_cohort.py's
# own ClientError/DegradedEnvelope `reason` text for the same failure member.
# tests/contract/test_web_scorecard_endpoints_contract.py proves parity by
# awaiting those MCP functions under the identical monkeypatch rather than
# trusting these literals stay in sync by inspection alone.
#
# Tuples-of-pairs, not a module-level dict literal: tests/unit/
# test_no_caching_layer.py's AST guard flags any module-level dict/list
# binding as a disguised response-cache risk unless it is one of the two
# named role-allow-list exceptions (AD-3/AD-18) — a frozen tuple constant is
# the guard's own "isn't a frozen constant" carve-out, so no guard change is
# needed for a plain static lookup table (see `_lookup_reason` below).
_TRIAL_FETCH_DEGRADED_REASONS: tuple[tuple[trials.TrialFetchError, str], ...] = (
    (
        trials.TrialFetchError.RATE_LIMIT_EXHAUSTED,
        "ClinicalTrials.gov rate limit exceeded after 4 attempts; try again later",
    ),
    (
        trials.TrialFetchError.UPSTREAM_ERROR,
        "ClinicalTrials.gov upstream error while fetching the trial",
    ),
    (
        trials.TrialFetchError.INVALID_RESPONSE,
        "ClinicalTrials.gov returned a response that failed validation",
    ),
)

_COHORT_FETCH_DEGRADED_REASONS: tuple[tuple[fhir.FhirFetchError, str], ...] = (
    (
        fhir.FhirFetchError.CIRCUIT_OPEN,
        (
            "The FHIR circuit breaker is open after repeated upstream failures; "
            "try again after the cooldown window elapses"
        ),
    ),
    (
        fhir.FhirFetchError.UPSTREAM_ERROR,
        "FHIR upstream error while fetching the patient cohort",
    ),
    (
        fhir.FhirFetchError.INVALID_RESPONSE,
        "The FHIR store returned a response that failed strict validation",
    ),
)


def _lookup_reason(table: tuple[tuple[object, str], ...], member: object) -> str:
    return next(reason for candidate, reason in table if candidate is member)


class ScreeningForm(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    nct_id: str


@dataclass(frozen=True)
class PipelineStep1Stop:
    """`run_screening_pipeline` stopped before any cohort work — mirrors
    `_render_step1`'s own parameters (08-03-PLAN.md Task 1)."""

    field_error: str | None
    degraded_reason: str | None
    status_code: int


@dataclass(frozen=True)
class PipelineStep2Stop:
    """`run_screening_pipeline` stopped after criteria verification but
    before (or during) the cohort/LLM fan-out — mirrors `_render_step2`'s/
    `_render_criteria_only`'s own parameters (08-03-PLAN.md Task 1)."""

    trial_title: str
    inclusion: list[EligibilityCriterion]
    exclusion: list[EligibilityCriterion]
    anchor: CohortAnchor | None
    note: str | None
    degraded_reason: str | None
    error: LlmUnavailableError | None
    status_code: int


@dataclass(frozen=True)
class PipelineScored:
    """`run_screening_pipeline` completed successfully: a constructed, not
    yet appended, `ScreenedRecord` plus the `CohortAnchor` used to produce
    it (08-03-PLAN.md Task 1)."""

    record: ScreenedRecord
    anchor: CohortAnchor


@dataclass(frozen=True)
class PipelineNoCandidates:
    """FR-028 / PRD FR-17(7): the cohort query succeeded and retrieved
    nobody; nothing is stored and no ref is minted (12-05-PLAN.md D-04).
    Distinct from `PipelineStep2Stop`'s criteria-only note, which says no
    cohort query ran -- false here."""

    trial_title: str
    inclusion: list[EligibilityCriterion]
    exclusion: list[EligibilityCriterion]
    anchor: CohortAnchor


PipelineOutcome = PipelineScored | PipelineStep1Stop | PipelineStep2Stop | PipelineNoCandidates


def _render_step1(
    request: Request,
    templates: object,
    nct_id: str,
    *,
    field_error: str | None = None,
    degraded_reason: str | None = None,
    status_code: int = 200,
) -> Response:
    return rendering.render(
        request,
        templates,
        "_screening_step1.html",
        {
            "role": request.state.role.value,
            "nct_id": nct_id,
            "field_error": field_error,
            "degraded_reason": degraded_reason,
        },
        status_code=status_code,
    )


def _render_step2(
    request: Request,
    templates: object,
    nct_id: str,
    trial_title: str,
    inclusion: list[EligibilityCriterion],
    exclusion: list[EligibilityCriterion],
    anchor: CohortAnchor | None,
    *,
    note: str | None = None,
    degraded_reason: str | None = None,
    error: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return rendering.render(
        request,
        templates,
        "_screening_step2.html",
        {
            "role": request.state.role.value,
            "nct_id": nct_id,
            "trial_title": trial_title,
            "inclusion": inclusion,
            "exclusion": exclusion,
            "anchor": anchor,
            "note": note,
            "degraded_reason": degraded_reason,
            "error": error,
            "embedded": False,
        },
        status_code=status_code,
    )


@router.get("/screenings/new")
async def new_screening(request: Request, nct_id: str = "") -> Response:
    """Step 1 NCT ID form (06-04-PLAN.md Task 1, WEBAPP-02). Covered by the
    router-level `require_web_endpoint("query_screening")` gate already
    applied in `app.py` -- no second gate here."""
    templates = request.app.state.templates
    return rendering.render(
        request,
        templates,
        "_screening_step1.html",
        {
            "role": request.state.role.value,
            "nct_id": nct_id.strip(),
            "field_error": None,
            "degraded_reason": None,
        },
    )


def _render_criteria_only(
    request: Request,
    templates: object,
    nct_id: str,
    trial_title: str,
    inclusion: list[EligibilityCriterion],
    exclusion: list[EligibilityCriterion],
    note: str,
) -> Response:
    """The two 06-01 option-a criteria-only outcomes (100% UNMAPPED; no
    verified ICD-10-CM inclusion criterion to anchor a cohort query): no
    cohort query, no LLM call, no ScreenedRecord (spec.md Edge Case)."""
    return _render_step2(
        request, templates, nct_id, trial_title, inclusion, exclusion, None, note=note
    )


async def run_screening_pipeline(nct_id: str, role: Role, settings: Config) -> PipelineOutcome:
    """The trial-fetch -> criteria-verify -> cohort-fetch -> AD-25
    proposal-fan-out -> `ScreenedRecord`-construction body extracted
    verbatim from `create_screening` (08-03-PLAN.md Task 1, tasks.md T014).

    Precondition: `nct_id` already matches `trials.NCT_ID_PATTERN` — the
    caller (`create_screening`, or `rerun_scorecard.py`'s re-fetch of an
    already-validated stored `ScreenedRecord.nct_id`) is responsible for
    that check. Compute-only: this function never appends a record, never
    renders a response, never touches `request`, and never takes the
    scorecard-store lock — its only side effects are the upstream calls
    (the trials/terminology/fhir/deidentify steps run inside
    `src/services/screening.py`, then the single breaker-free
    `fhir.fetch_data_revision` read, AD-29, right after a non-empty cohort
    fetch, then `llm_proposal` here), each kept as a
    `module.function(...)` attribute call so existing test monkeypatches
    keep working. Shared by `create_screening` (fresh screening) and
    `rerun_scorecard` (08-03) so the two callers can never diverge.
    """
    resolved = await screening.resolve_trial(nct_id)
    if resolved is terminology.TerminologyFetchError.UNAVAILABLE:
        # AD-34 / WD-3: the whole request is DEGRADED with the one shared reason, so the
        # step-1 banner reads the same for every role and the same as the MCP surface.
        # Deliberately not routed through `_lookup_reason`: this member is not in either
        # reason table and its no-default `next()` would raise.
        return PipelineStep1Stop(
            field_error=None,
            degraded_reason=terminology.TERMINOLOGY_UNAVAILABLE_REASON,
            status_code=200,
        )
    if isinstance(resolved, trials.TrialFetchError):
        if resolved is trials.TrialFetchError.NOT_FOUND:
            # A semantic 422 field error, not a 404 — the field itself is
            # the problem (contracts/query_screening.json), distinct wording
            # from get_protocol_criteria's own NOT_FOUND reason on purpose.
            return PipelineStep1Stop(
                field_error=f"No study found for {nct_id}.",
                degraded_reason=None,
                status_code=422,
            )
        envelope = DegradedEnvelope(
            status="DEGRADED",
            data=[],
            reason=_lookup_reason(_TRIAL_FETCH_DEGRADED_REASONS, resolved),
        )
        return PipelineStep1Stop(field_error=None, degraded_reason=envelope.reason, status_code=200)
    if isinstance(resolved, ClientError):
        return PipelineStep1Stop(field_error=resolved.reason, degraded_reason=None, status_code=422)

    inclusion = resolved.inclusion_criteria
    exclusion = resolved.exclusion_criteria
    all_criteria_ordered = inclusion + exclusion
    trial_title = resolved.title

    # The two 06-01 option-a criteria-only outcomes (spec.md Edge Case):
    # neither branch calls fhir, llm_proposal or audit, and no ScreenedRecord
    # is ever constructed for either.
    if all(
        criterion.mapping_status is MappingStatus.UNMAPPED for criterion in all_criteria_ordered
    ):
        return PipelineStep2Stop(
            trial_title=trial_title,
            inclusion=inclusion,
            exclusion=exclusion,
            anchor=None,
            note="No verified criteria to query. Every criterion requires manual coding.",
            degraded_reason=None,
            error=None,
            status_code=200,
        )

    anchor = derive_cohort_anchor(inclusion)
    if anchor is None:
        return PipelineStep2Stop(
            trial_title=trial_title,
            inclusion=inclusion,
            exclusion=exclusion,
            anchor=None,
            note="No verified ICD-10-CM inclusion criterion to anchor a cohort query.",
            degraded_reason=None,
            error=None,
            status_code=200,
        )

    de_identified_candidates = await screening.fetch_candidates(
        anchor.condition_code, anchor.observation_loinc, screening.COHORT_LIMIT_DEFAULT
    )
    if isinstance(de_identified_candidates, fhir.FhirFetchError):
        envelope = DegradedEnvelope(
            status="DEGRADED",
            data=[],
            reason=_lookup_reason(_COHORT_FETCH_DEGRADED_REASONS, de_identified_candidates),
        )
        return PipelineStep2Stop(
            trial_title=trial_title,
            inclusion=inclusion,
            exclusion=exclusion,
            anchor=anchor,
            note=None,
            degraded_reason=envelope.reason,
            error=None,
            status_code=200,
        )

    if not de_identified_candidates:
        return PipelineNoCandidates(
            trial_title=trial_title, inclusion=inclusion, exclusion=exclusion, anchor=anchor
        )

    # AD-29: read only after a successful, non-empty cohort fetch. It runs before the LLM
    # fan-out, so the stamp names the revision the candidates came from, not whatever the
    # data became while the model calls ran (WR-03). Best-effort and cosmetic: any Exception
    # becomes None, never a DEGRADED envelope (IN-04), while CancelledError is a BaseException
    # and still propagates. The value is never passed to llm_proposal (T-13-11).
    try:
        revision = await fhir.fetch_data_revision(nct_id)
    except Exception:  # noqa: BLE001 - cosmetic stamp, must never fail a screening
        revision = None

    criteria_text = [
        f"{criterion.kind.value}: {criterion.raw_text}" for criterion in all_criteria_ordered
    ]

    pending: list[tuple[asyncio.Task[str], object]] = []
    llm_error: LlmUnavailableError | None = None
    try:
        async with asyncio.TaskGroup() as task_group:
            for de_identified in de_identified_candidates:
                facts = {
                    "age": de_identified.age,
                    "condition_values": de_identified.condition_values,
                    "observation_values": de_identified.observation_values,
                }
                task = task_group.create_task(
                    llm_proposal.propose_status(
                        facts,
                        criteria_text,
                        api_key=settings.anthropic_api_key,
                        model=settings.anthropic_model,
                    )
                )
                pending.append((task, de_identified))
    except* llm_proposal.LlmProposalError:
        llm_error = LlmUnavailableError(
            error="llm_unavailable",
            reason="Eligibility status proposal unavailable; no scorecard was saved.",
        )

    if llm_error is not None:
        return PipelineStep2Stop(
            trial_title=trial_title,
            inclusion=inclusion,
            exclusion=exclusion,
            anchor=anchor,
            note=None,
            degraded_reason=None,
            error=llm_error,
            status_code=502,
        )

    candidates: list[CandidateResult] = []
    for task, de_identified in pending:
        llm_proposed_status = task.result()
        determination = screening.score_candidate(
            de_identified,
            anchor.condition_code,
            anchor.observation_loinc,
            inclusion,
            exclusion,
        )
        candidates.append(
            CandidateResult(
                patient_pseudonym=de_identified.patient_pseudonym,
                llm_proposed_status=llm_proposed_status,
                eligibility_status=determination.status.value,
                recomputed_mismatch=determination.status.value != llm_proposed_status,
                matching_criteria=determination.matching_criteria,
                exclusion_flags=determination.exclusion_flags,
                unmapped_criteria=determination.unmapped_criteria,
            )
        )

    record = ScreenedRecord(
        record_type="screened",
        ref=audit.new_scorecard_ref(),
        nct_id=nct_id,
        trial_title=trial_title,
        screened_at=datetime.now(UTC),
        screened_by_role=role,
        criteria_snapshot=all_criteria_ordered,
        candidates=candidates,
        data_revision=revision,
    )

    return PipelineScored(record=record, anchor=anchor)


def render_pipeline_stop(
    request: Request,
    nct_id: str,
    outcome: PipelineStep1Stop | PipelineStep2Stop | PipelineNoCandidates,
) -> Response:
    """Renders a non-scored `run_screening_pipeline` outcome through the
    same step-1/step-2 render helpers `create_screening` always used
    (08-03-PLAN.md Task 1) — shared by `create_screening` and
    `rerun_scorecard` so the two callers render identically."""
    templates = request.app.state.templates
    if isinstance(outcome, PipelineNoCandidates):
        return rendering.render(
            request,
            templates,
            "_screening_zero_candidates.html",
            {
                "role": request.state.role.value,
                "nct_id": nct_id,
                "trial_title": outcome.trial_title,
                "inclusion": outcome.inclusion,
                "exclusion": outcome.exclusion,
            },
            status_code=200,
        )
    if isinstance(outcome, PipelineStep1Stop):
        return _render_step1(
            request,
            templates,
            nct_id,
            field_error=outcome.field_error,
            degraded_reason=outcome.degraded_reason,
            status_code=outcome.status_code,
        )
    if outcome.note is not None:
        return _render_criteria_only(
            request,
            templates,
            nct_id,
            outcome.trial_title,
            outcome.inclusion,
            outcome.exclusion,
            outcome.note,
        )
    error = (
        {"error": outcome.error.error, "reason": outcome.error.reason}
        if outcome.error is not None
        else None
    )
    return _render_step2(
        request,
        templates,
        nct_id,
        outcome.trial_title,
        outcome.inclusion,
        outcome.exclusion,
        outcome.anchor,
        degraded_reason=outcome.degraded_reason,
        error=error,
        status_code=outcome.status_code,
    )


@router.post("/screenings")
async def create_screening(request: Request, form: Annotated[ScreeningForm, Form()]) -> Response:
    settings: Config = request.app.state.settings
    templates = request.app.state.templates

    nct_id = form.nct_id.strip()
    if not trials.NCT_ID_PATTERN.match(nct_id):
        return _render_step1(
            request, templates, nct_id, field_error=_MALFORMED_NCT_ID_REASON, status_code=422
        )

    outcome = await run_screening_pipeline(nct_id, request.state.role, settings)
    if not isinstance(outcome, PipelineScored):
        return render_pipeline_stop(request, nct_id, outcome)

    await audit.append_screened_record(outcome.record, settings.scorecard_store_path)

    return rendering.render(
        request,
        templates,
        "_screening_step3.html",
        {"role": request.state.role.value, "record": outcome.record, "anchor": outcome.anchor},
    )
