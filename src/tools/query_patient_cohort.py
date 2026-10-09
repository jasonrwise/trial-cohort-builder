"""The real `query_patient_cohort` body (03-01-PLAN.md Task 3), replacing
the Phase 1 stub.

Contract: `specs/001-trial-eligibility-screening/contracts/query_patient_cohort.json`
(migrated from `specs/002-cohort-deid-scoring/contracts/` in this same
commit, alongside this file's real body — research.md #9's sequencing
requirement, so `tests/contract/test_tool_schemas.py` never observes a
mismatched intermediate state). The signature below matches that contract's
`inputSchema` field for field — because FastMCP infers the tool's input
schema from the function signature, there is no supported way to hand it a
raw JSON schema for the input side.

An outage of the terminology service during any of these re-verifications (AD-34:
`terminology.verify_code` returned `terminology.TerminologyFetchError.UNAVAILABLE` after its
4 attempts) is the existing `DegradedEnvelope` with the shared
`terminology.TERMINOLOGY_UNAVAILABLE_REASON`, returned before any FHIR request — never a
"not recognized" `ClientError`. Every `verify_code` result is tested by identity against
`UNAVAILABLE` first (the enum member is truthy) and accepted only when `is True`; an outage
anywhere in the concurrent criteria group wins over a genuine rejection in the same group.

Pipeline: re-verify `condition_code` (and `observation_loinc`, when
supplied) via `terminology.verify_code` — short-circuiting to `ClientError`
before any FHIR request on failure (COHT-01, research.md #4) -> re-verify
every VERIFIED `inclusion_criteria`/`exclusion_criteria` entry's own
`(code, code_system)` the same way, also short-circuiting before any FHIR
request (03-SECURITY.md T5 — a caller's own `mapping_status: VERIFIED`
claim on a criterion is never trusted without this server-side re-check,
same principle as COHT-01, just not applied to this input surface until the
Phase 3 security audit) -> `screening.run_screening` (src/services/screening.py,
AD-27: `fhir.fetch_cohort` -> `deidentify.strip_bundle` (COHT-02, research.md #7)
-> `scoring.determine_eligibility` once per de-identified candidate against the
caller-supplied `inclusion_criteria`/`exclusion_criteria`, always with
`proposed_status=None` — no assistant-proposed status exists at query time
(SCORE-01, research.md #5)) -> assembled `Candidate` list. `fhir.FhirFetchError.CIRCUIT_OPEN` (the
FHIR circuit breaker, DEGR-02, ARCHITECTURE-SPINE.md AD-11) is the one
`FhirFetchError` member mapped to `DegradedEnvelope` rather than
`ClientError` — the other two (`UPSTREAM_ERROR`/`INVALID_RESPONSE`) keep
their existing `ClientError` mapping unchanged.
"""

import asyncio
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.models.candidate import Candidate, CohortQueryResult
from src.models.errors import ClientError, DegradedEnvelope
from src.models.trial import (
    CodeSystem,
    CriterionKind,
    EligibilityCriterion,
    MappingStatus,
    Operator,
)
from src.services import fhir, screening, terminology


class EligibilityCriterionInput(BaseModel):
    """Mirror of `contracts/eligibility_criterion.json`'s nested criterion
    object, local to this tool module rather than reused directly from
    `src.models.trial.EligibilityCriterion` — pydantic v2 strict mode does
    not coerce a plain wire-format JSON string into an `Enum` member (the
    same Pitfall 3 discipline `src/models/trial.py` documents), so accepting
    these fields' enum values over the wire requires `Literal[...]` here,
    converted to the real domain model via `to_domain()` just after FastMCP's
    own validation. Mirrors `src/tools/dispatch_screening_alert.py`'s
    `DispatchCandidateInput`'s own precedent for this exact problem."""

    model_config = ConfigDict(strict=True, extra="forbid")

    raw_text: str
    kind: Literal["INCLUSION", "EXCLUSION"]
    mapping_status: Literal["VERIFIED", "UNMAPPED"]
    code_system: Literal["ICD10CM", "LOINC"] | None
    code: str | None
    operator: Literal["EQ", "GT", "GTE", "LT", "LTE", "PRESENT"] | None
    threshold: float | str | None

    def to_domain(self) -> EligibilityCriterion:
        """Converts to the real domain model, re-triggering
        `EligibilityCriterion`'s own `mapping_status`-implies-nullness
        `model_validator` as a defense-in-depth check on caller-supplied
        input (data-model.md's `CohortQuery.inclusion_criteria`/
        `exclusion_criteria`)."""
        return EligibilityCriterion(
            raw_text=self.raw_text,
            kind=CriterionKind(self.kind),
            mapping_status=MappingStatus(self.mapping_status),
            code_system=CodeSystem(self.code_system) if self.code_system is not None else None,
            code=self.code,
            operator=Operator(self.operator) if self.operator is not None else None,
            threshold=self.threshold,
        )


async def _first_unverified_criterion(
    criteria: list[EligibilityCriterion],
) -> EligibilityCriterion | terminology.TerminologyFetchError | None:
    """Re-verifies every VERIFIED criterion's `(code, code_system)` against
    the terminology service, exactly mirroring the top-level
    `condition_code`/`observation_loinc` re-verification (COHT-01,
    research.md #4) — extended here to `inclusion_criteria`/
    `exclusion_criteria` (03-SECURITY.md T5). Without this, a caller's own
    `mapping_status: VERIFIED` claim on a criterion is trusted outright,
    letting a compromised or manipulated caller fabricate a criterion that
    silently forces any `eligibility_status`. Returns
    `terminology.TerminologyFetchError.UNAVAILABLE` if any lookup hit a terminology
    outage (AD-34; it wins over a rejection in the same group), else the first
    criterion that fails re-verification, or `None` if every VERIFIED criterion
    checks out (an `UNMAPPED` criterion has no code to verify and is always
    skipped). `terminology.verify_code` already bounds its own concurrency
    via its module-level semaphore, so these run concurrently."""
    verified = [
        criterion for criterion in criteria if criterion.mapping_status is MappingStatus.VERIFIED
    ]
    if not verified:
        return None

    # code/code_system are guaranteed non-null for VERIFIED criteria by
    # EligibilityCriterion's own model_validator.
    results = await asyncio.gather(
        *(terminology.verify_code(criterion.code, criterion.code_system) for criterion in verified)  # type: ignore[arg-type]
    )
    if any(result is terminology.TerminologyFetchError.UNAVAILABLE for result in results):
        return terminology.TerminologyFetchError.UNAVAILABLE
    for criterion, result in zip(verified, results, strict=True):
        if result is not True:
            return criterion
    return None


def _terminology_unavailable() -> DegradedEnvelope:
    return DegradedEnvelope(
        status="DEGRADED",
        data=[],
        reason=terminology.TERMINOLOGY_UNAVAILABLE_REASON,
    )


async def query_patient_cohort(
    condition_code: str,
    inclusion_criteria: Annotated[list[EligibilityCriterionInput], Field(max_length=100)],
    exclusion_criteria: Annotated[list[EligibilityCriterionInput], Field(max_length=100)],
    observation_loinc: str | None = None,
    limit: Annotated[
        int, Field(ge=1, le=screening.COHORT_LIMIT_MAX)
    ] = screening.COHORT_LIMIT_DEFAULT,
) -> CohortQueryResult | ClientError | DegradedEnvelope:
    try:
        inclusion_criteria_domain = [criterion.to_domain() for criterion in inclusion_criteria]
        exclusion_criteria_domain = [criterion.to_domain() for criterion in exclusion_criteria]
    except ValidationError as exc:
        return ClientError(
            status="ERROR",
            reason=f"A criterion failed validation: {exc}",
        )

    condition_result = await terminology.verify_code(condition_code, CodeSystem.ICD10CM)
    if condition_result is terminology.TerminologyFetchError.UNAVAILABLE:
        return _terminology_unavailable()
    if condition_result is not True:
        return ClientError(
            status="ERROR",
            reason=(
                f"condition_code failed re-verification: {condition_code!r} is not a "
                "recognized, currently-verifiable ICD-10-CM code"
            ),
        )

    if observation_loinc is not None:
        observation_result = await terminology.verify_code(observation_loinc, CodeSystem.LOINC)
        if observation_result is terminology.TerminologyFetchError.UNAVAILABLE:
            return _terminology_unavailable()
        if observation_result is not True:
            return ClientError(
                status="ERROR",
                reason=(
                    f"observation_loinc failed re-verification: {observation_loinc!r} is not "
                    "a recognized, currently-verifiable LOINC code"
                ),
            )

    unverified_criterion = await _first_unverified_criterion(
        inclusion_criteria_domain + exclusion_criteria_domain
    )
    if unverified_criterion is terminology.TerminologyFetchError.UNAVAILABLE:
        return _terminology_unavailable()
    if isinstance(unverified_criterion, EligibilityCriterion):
        return ClientError(
            status="ERROR",
            reason=(
                f"{unverified_criterion.kind.value.lower()} criterion "
                f"{unverified_criterion.raw_text!r} failed re-verification: "
                f"{unverified_criterion.code!r} is not a recognized, "
                "currently-verifiable code"
            ),
        )

    scored = await screening.run_screening(
        condition_code,
        observation_loinc,
        limit,
        inclusion_criteria_domain,
        exclusion_criteria_domain,
    )
    if scored is fhir.FhirFetchError.UPSTREAM_ERROR:
        return ClientError(
            status="ERROR", reason="FHIR upstream error while fetching the patient cohort"
        )
    if scored is fhir.FhirFetchError.INVALID_RESPONSE:
        return ClientError(
            status="ERROR",
            reason="The FHIR store returned a response that failed strict validation",
        )
    if scored is fhir.FhirFetchError.CIRCUIT_OPEN:
        return DegradedEnvelope(
            status="DEGRADED",
            data=[],
            reason=(
                "The FHIR circuit breaker is open after repeated upstream failures; "
                "try again after the cooldown window elapses"
            ),
        )

    candidates: list[Candidate] = []
    for item in scored:
        de_identified = item.candidate
        determination = item.determination
        candidates.append(
            Candidate(
                patient_pseudonym=de_identified.patient_pseudonym,
                age=de_identified.age,
                condition_values=de_identified.condition_values,
                observation_values=de_identified.observation_values,
                eligibility_status=determination.status,
                matching_criteria=determination.matching_criteria,
                exclusion_flags=determination.exclusion_flags,
                unmapped_criteria=determination.unmapped_criteria,
            )
        )

    return CohortQueryResult(candidates=candidates)
