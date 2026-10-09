"""Trial and EligibilityCriterion models.

Field-for-field mirror of specs/001-trial-eligibility-screening/data-model.md's
`Trial` and `EligibilityCriterion` tables. The non-LOINC `code_system` enum
member is `ICD10CM`, per D-19 (02-CONTEXT.md) — 02-RESEARCH.md's Critical
Correction live-verified that the NLM `conditions/v3/search` endpoint returns
only ICD-9-CM/ICD-10-CM codes, not the vocabulary this field originally named,
so no artifact in this repo may claim that other vocabulary for a code this
endpoint returns.

Every model here is strict (`ConfigDict(strict=True, extra="forbid")`), matching
the DEGR-04 enforcement mechanism already established in `src/models/errors.py`
and `src/models/access.py`. Strict mode does not coerce a plain string into an
Enum member (02-RESEARCH.md Pitfall 3) — callers must convert explicitly, the
same way `src/services/access_control.py`'s `resolve_role` already does for
`Role` (`Role(raw_role)` inside a `try/except ValueError`).
"""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class MappingStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNMAPPED = "UNMAPPED"


class CodeSystem(str, Enum):
    ICD10CM = "ICD10CM"
    LOINC = "LOINC"


class CriterionKind(str, Enum):
    INCLUSION = "INCLUSION"
    EXCLUSION = "EXCLUSION"


class Operator(str, Enum):
    EQ = "EQ"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    PRESENT = "PRESENT"


class EligibilityCriterion(BaseModel):
    """One inclusion or exclusion rule, produced by the mapping step and
    consumed by both `query_patient_cohort` and the scoring step.

    All seven fields are required with no default — the contract lists all
    seven in `required`, so a caller states a null explicitly rather than
    letting it default in. The `allOf` conditional in
    `contracts/eligibility_criterion.json` is enforced below as a model
    validator: when `mapping_status` is `UNMAPPED`, all four nullable fields
    must be `None`. `operator`/`threshold` are NOT required non-null when
    `VERIFIED` — a `VERIFIED` criterion with both `None` is legal (D-11): a
    presence/absence criterion whose comparison phrasing could not be
    confidently parsed. Only `code_system` and `code` are required non-null
    when `VERIFIED`, and that direction is enforced below too, so both halves
    of the invariant this docstring states are structurally guaranteed, not
    just the UNMAPPED-implies-null half.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    raw_text: str
    kind: CriterionKind
    mapping_status: MappingStatus
    code_system: CodeSystem | None
    code: str | None
    operator: Operator | None
    threshold: float | str | None

    @model_validator(mode="after")
    def _mapping_status_implies_expected_nullness(self) -> "EligibilityCriterion":
        if self.mapping_status is MappingStatus.UNMAPPED and (
            self.code_system is not None
            or self.code is not None
            or self.operator is not None
            or self.threshold is not None
        ):
            raise ValueError(
                "mapping_status=UNMAPPED requires code_system, code, operator "
                "and threshold to all be null"
            )
        if self.mapping_status is MappingStatus.VERIFIED and (
            self.code_system is None or self.code is None
        ):
            raise ValueError("mapping_status=VERIFIED requires code_system and code to be non-null")
        return self


class Trial(BaseModel):
    """Fetched fresh on every `get_protocol_criteria` call — never persisted.

    `status` is a fixed `"OK"` discriminator, added in plan 02-02 to match the
    success branch of `contracts/get_protocol_criteria.json`'s `outputSchema`
    (`status: {"const": "OK"}` alongside `nct_id`/`title`/`inclusion_criteria`/
    `exclusion_criteria`) and to mirror the same convention `ClientError`
    (`status: Literal["ERROR"]`) and `DegradedEnvelope`
    (`status: Literal["DEGRADED"]`) already use in `src/models/errors.py` — a
    caller distinguishes the three `get_protocol_criteria` response shapes by
    this one discriminator field, so its absence here (the shape plan 02-01
    originally built) would leave a successful response with no status field
    at all."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["OK"] = "OK"
    nct_id: str
    title: str
    inclusion_criteria: list[EligibilityCriterion]
    exclusion_criteria: list[EligibilityCriterion]
