"""Scorecard and CandidateResult models (04-01-PLAN.md Task 1).

Field-for-field mirror of `specs/001-trial-eligibility-screening/data-model.md`'s
`Scorecard`/`CandidateResult` tables and `contracts/scorecard.json`'s
required/enum/`additionalProperties: false` constraints. Both models are
strict (`ConfigDict(strict=True, extra="forbid")`), matching every other
model in `src/models/` (DEGR-04's no-silent-coercion guarantee).

`recomputed_mismatch` is deliberately NOT derived inside `CandidateResult` —
`src/tools/dispatch_screening_alert.py` (Task 2) computes it, mirroring
`EligibilityDetermination`'s existing separation of concerns (the pure
scoring engine computes; the model only validates). What this model DOES
enforce, as `model_validator`s, are the two structural invariants
data-model.md defines between fields:

  1. `recomputed_mismatch` must equal `eligibility_status != llm_proposed_status`
     — a caller cannot construct an internally-inconsistent CandidateResult
     even though the flag itself isn't derived here.
  2. `eligibility_status` may never be an unqualified `ELIGIBLE` when
     `unmapped_criteria` is non-empty (FR-010) — mirrors
     `src/models/candidate.py`'s `Candidate._unmapped_criteria_implies_not_eligible`.

`ScreenedRecord` and `ApprovalRecord` (06-02-PLAN.md Task 1, AD-17) are the
two line types of the new web-app scorecard store: an append-only,
never-rotated JSON-Lines file, discriminated by each record's `record_type`
literal. Both are strict, extra-forbid, and frozen (immutable) — neither
carries a `state` field, since "finalized" is always a derived fact (an
`ApprovalRecord` with a matching `screened_ref` exists) rather than
something stored on the `ScreenedRecord` itself.
"""

import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from src.models.access import Role
from src.models.trial import EligibilityCriterion
from src.services.trials import NCT_ID_PATTERN

EligibilityStatusLiteral = Literal["ELIGIBLE", "INELIGIBLE", "BORDERLINE"]

# Opaque `sc_` + 16 lowercase-hex-digit format (06-02-PLAN.md Task 1) — the 16
# hex digits carry 64 bits from `secrets.token_hex(8)`, generated in plan
# 06-03. `\Z` (not `$`), mirroring `trials.NCT_ID_PATTERN`'s own reasoning:
# `$` matches immediately before a trailing "\n", which would let a
# caller-supplied "sc_...\n" slip past this check. Enough entropy that
# AD-17/FR-018's fabricated-reference rejection is meaningful.
SCORECARD_REF_PATTERN = re.compile(r"^sc_[0-9a-f]{16}\Z")


def _require_zero_offset_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be a timezone-aware UTC datetime (zero offset)")
    return value


class CandidateResult(BaseModel):
    """One candidate's entry in a `Scorecard` (data-model.md `CandidateResult`)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    patient_pseudonym: str
    llm_proposed_status: EligibilityStatusLiteral
    eligibility_status: EligibilityStatusLiteral
    recomputed_mismatch: bool
    matching_criteria: list[str]
    exclusion_flags: list[str]
    unmapped_criteria: list[str]

    @model_validator(mode="after")
    def _recomputed_mismatch_matches_status_comparison(self) -> "CandidateResult":
        expected = self.eligibility_status != self.llm_proposed_status
        if self.recomputed_mismatch != expected:
            raise ValueError(
                "recomputed_mismatch must equal (eligibility_status != llm_proposed_status); "
                f"got recomputed_mismatch={self.recomputed_mismatch} with "
                f"eligibility_status={self.eligibility_status!r}, "
                f"llm_proposed_status={self.llm_proposed_status!r}"
            )
        return self

    @model_validator(mode="after")
    def _unmapped_criteria_implies_not_eligible(self) -> "CandidateResult":
        if self.unmapped_criteria and self.eligibility_status == "ELIGIBLE":
            raise ValueError(
                "eligibility_status must not be ELIGIBLE when unmapped_criteria is non-empty"
            )
        return self


class Scorecard(BaseModel):
    """The per-run screening output (data-model.md `Scorecard`) —
    `dispatch_screening_alert`'s draft preview or dispatched message body is
    built from this shape."""

    model_config = ConfigDict(strict=True, extra="forbid")

    nct_id: str
    screened_at: datetime
    candidates: list[CandidateResult]
    mode: Literal["DRAFT", "DISPATCHED"]
    dispatching_role: Literal["CRC", "PI"]


class ScreenedRecord(BaseModel):
    """One line of the AD-17 scorecard store — written once by
    `query_screening.py`/`rerun_scorecard.py`, never updated in place.
    `record_type` discriminates it from `ApprovalRecord` in the shared,
    append-only, never-rotated JSON-Lines file. No `state` field: "finalized"
    is always a derived fact (whether a matching `ApprovalRecord` exists),
    never stored here.

    `data_revision` (AD-30) is the seeded data revision the screening was
    computed against; `None` means revision unknown. The field has no content
    validator, so any string loads, however odd. A non-string value is still
    rejected by strict mode and, like any invalid line, makes the append-only
    store unreadable. Sanitizing happens only where the value is read
    (`fhir._clean_revision`)."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    record_type: Literal["screened"]
    ref: str
    nct_id: str
    trial_title: str
    screened_at: datetime
    screened_by_role: Role
    criteria_snapshot: list[EligibilityCriterion]
    candidates: list[CandidateResult]
    data_revision: str | None = None

    @field_validator("ref")
    @classmethod
    def _ref_matches_scorecard_ref_pattern(cls, value: str) -> str:
        if not SCORECARD_REF_PATTERN.match(value):
            raise ValueError(f"ref must match {SCORECARD_REF_PATTERN.pattern!r}")
        return value

    @field_validator("nct_id")
    @classmethod
    def _nct_id_matches_trials_pattern(cls, value: str) -> str:
        if not NCT_ID_PATTERN.match(value):
            raise ValueError(f"nct_id must match {NCT_ID_PATTERN.pattern!r}")
        return value

    @field_validator("screened_at")
    @classmethod
    def _screened_at_is_utc(cls, value: datetime) -> datetime:
        return _require_zero_offset_utc(value, "screened_at")

    @field_validator("screened_by_role")
    @classmethod
    def _screened_by_role_is_not_site_admin(cls, value: Role) -> Role:
        if value is Role.SITE_ADMIN:
            raise ValueError("screened_by_role must not be SITE_ADMIN")
        return value


class ApprovalRecord(BaseModel):
    """The other line of the AD-17 scorecard store — written once by
    `approve_finalize.py`, never updated in place. A `ScreenedRecord` is
    FINALIZED if and only if an `ApprovalRecord` with a matching
    `screened_ref` exists; this model never stores that fact itself."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    record_type: Literal["approval"]
    screened_ref: str
    approved_by_role: Literal["PI"]
    approved_at: datetime

    @field_validator("screened_ref")
    @classmethod
    def _screened_ref_matches_scorecard_ref_pattern(cls, value: str) -> str:
        if not SCORECARD_REF_PATTERN.match(value):
            raise ValueError(f"screened_ref must match {SCORECARD_REF_PATTERN.pattern!r}")
        return value

    @field_validator("approved_at")
    @classmethod
    def _approved_at_is_utc(cls, value: datetime) -> datetime:
        return _require_zero_offset_utc(value, "approved_at")
