"""Candidate, EligibilityStatus, EligibilityDetermination, and
CohortQueryResult models (03-01-PLAN.md Task 1).

Field-for-field mirror of `specs/002-cohort-deid-scoring/data-model.md`'s
`Candidate` and `EligibilityDetermination` tables. Every model here is strict
(`ConfigDict(strict=True, extra="forbid")`), matching the convention already
established in `src/models/trial.py` and `src/models/errors.py` (DEGR-04's
no-silent-coercion guarantee).

`EligibilityDetermination` is deliberately kept distinct from `Candidate` (not
directly serialized over the wire) so `src/services/scoring.py`'s
`determine_eligibility()` stays a pure, independently-unit-testable function
with no dependency on the `Candidate` wire model (data-model.md).

`CohortQueryResult` is this phase's addition, mirroring `src/models/trial.py`'s
`Trial` convention of carrying its own `status: Literal["OK"] = "OK"`
discriminator directly on the success-envelope model — the shape
`specs/001-trial-eligibility-screening/contracts/query_patient_cohort.json`'s
`outputSchema` success branch declares (`{"status": {"const": "OK"},
"candidates": [...]}`) but that data-model.md, written before this task's own
implementation pass, did not need to separately name as its own entity.
"""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

# Safe Harbor's age-90 aggregation rule: an integer age is only ever reported
# up to this value; anything older is the literal "90 or older" (never a
# specific integer). Shared with `src/services/deidentify.py`'s
# `_compute_capped_age`, the only other place this threshold is applied, so
# the two can't drift out of sync.
MAX_INTEGER_AGE = 89


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    BORDERLINE = "BORDERLINE"


class Candidate(BaseModel):
    """The record returned by `query_patient_cohort`, one per synthetic
    patient matched. Every field is guaranteed Safe-Harbor-stripped
    (COHT-02) before a `Candidate` is ever constructed — this model does not
    itself perform the stripping, `src/services/deidentify.py` does, before
    `src/tools/query_patient_cohort.py` assembles this shape.

    `unmapped_criteria`-implies-not-`ELIGIBLE` is enforced below as a
    `model_validator`, mirroring `EligibilityCriterion`'s own
    `mapping_status`-implies-nullness validator in `src/models/trial.py` —
    structurally guaranteed, not left to caller discipline (SCORE-01)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    patient_pseudonym: str
    age: int | Literal["90 or older"] | None
    condition_values: dict[str, Any]
    observation_values: dict[str, Any]
    eligibility_status: EligibilityStatus
    matching_criteria: list[str]
    exclusion_flags: list[str]
    unmapped_criteria: list[str]

    @model_validator(mode="after")
    def _unmapped_criteria_implies_not_eligible(self) -> "Candidate":
        if self.unmapped_criteria and self.eligibility_status is EligibilityStatus.ELIGIBLE:
            raise ValueError(
                "eligibility_status must not be ELIGIBLE when unmapped_criteria is non-empty"
            )
        return self

    @model_validator(mode="after")
    def _integer_age_capped_at_89(self) -> "Candidate":
        if isinstance(self.age, int) and self.age > MAX_INTEGER_AGE:
            raise ValueError(
                f"age as an integer must be <= {MAX_INTEGER_AGE}; ages above it use the "
                'literal "90 or older"'
            )
        return self


class EligibilityDetermination(BaseModel):
    """The pure return value of `src/services/scoring.py`'s
    `determine_eligibility()`, before its fields are merged onto a
    `Candidate`. Kept distinct so the scoring engine stays independently
    unit-testable with no dependency on the `Candidate` wire model.

    `proposed_status`/`recomputed_mismatch` exist for Phase 4's
    `dispatch_screening_alert` to call this same function with a real
    assistant-proposed status (research.md #5) — this phase's own
    `query_patient_cohort` always calls `determine_eligibility` with
    `proposed_status=None`, so every determination it produces carries
    `recomputed_mismatch=False`."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: EligibilityStatus
    matching_criteria: list[str]
    exclusion_flags: list[str]
    unmapped_criteria: list[str]
    proposed_status: EligibilityStatus | None = None
    recomputed_mismatch: bool = False


class CohortQueryResult(BaseModel):
    """`query_patient_cohort`'s success envelope
    (`specs/001-trial-eligibility-screening/contracts/query_patient_cohort.json`'s
    `outputSchema` success branch)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["OK"] = "OK"
    candidates: list[Candidate]
