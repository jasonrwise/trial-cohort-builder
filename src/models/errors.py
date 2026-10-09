"""Shared error/degradation envelopes.

Field-for-field mirror of specs/001-trial-eligibility-screening/contracts/errors.json.
Every model is strict (no silent type coercion — the DEGR-04 enforcement mechanism)
and forbids undeclared extra fields (mirrors the contracts' additionalProperties: false).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ClientError(BaseModel):
    """A clear, handled error — malformed/unrecognized input, or an upstream payload
    that failed validation (FR-002, FR-021). Never a partial result presented as complete."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["ERROR"]
    reason: str


class DegradedEnvelope(BaseModel):
    """The one and only fallback shape for a failed dependency (FR-018, FR-019, FR-022).
    Reused verbatim across all three upstreams — never a cached/stale result."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["DEGRADED"]
    data: list = Field(default_factory=list, max_length=0)
    reason: str


class AccessDeniedError(BaseModel):
    """RBAC rejection per FR-015/FR-016. JSON-RPC error code -32003 (PRD RBAC table) —
    Site Admin/Auditor attempting query_patient_cohort or dispatch_screening_alert,
    regardless of input."""

    model_config = ConfigDict(strict=True, extra="forbid")

    code: Literal[-32003]
    message: str


# --- Web-app HTTP error bodies (06-02-PLAN.md Task 1, AD-20) -----------------
#
# Field-for-field mirror of specs/004-web-app-interface/contracts/web-errors.json.
# Each is named distinctly from, and never subclasses, the MCP-surface
# AccessDeniedError above (a different shape: {error, reason} vs. {code, message}).


class WebAccessDeniedError(BaseModel):
    """AD-18 allow-list denial (403); also reused by `login` for an
    unrecognized key (401), a Site Admin/Auditor refusal (403), and lockout
    threshold reached (429, spec.md FR-004) — `reason` text distinguishes the
    cases per FR-003, `error` stays "access_denied" across all of them."""

    model_config = ConfigDict(strict=True, extra="forbid")

    error: Literal["access_denied"]
    reason: str


class BusinessRejectionError(BaseModel):
    """spec.md FR-018/FR-019/FR-026: bad/fabricated reference, NCT-ID
    mismatch, double-finalization, rerun-on-finalized. HTTP 409."""

    model_config = ConfigDict(strict=True, extra="forbid")

    error: Literal[
        "reference_not_found",
        "reference_protocol_mismatch",
        "already_finalized",
        "rerun_on_finalized",
    ]
    reason: str


class LlmUnavailableError(BaseModel):
    """AD-25's `llm_proposed_status` proposal-call failure (FR-013/FR-014).
    HTTP 502. Distinct from AD-22's criteria-mapping ruling — the two never
    share a code path even though they reuse this HTTP shape."""

    model_config = ConfigDict(strict=True, extra="forbid")

    error: Literal["llm_unavailable"]
    reason: str


class NotFoundError(BaseModel):
    """spec.md Edge Cases: a requested scorecard reference doesn't exist or
    is malformed. HTTP 404 (distinct from the 409 business-rejection family —
    no conflicting write was attempted, this is a plain lookup miss)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    error: Literal["not_found"]
    reason: str


class PdfGenerationFailedError(BaseModel):
    """contracts/export_pdf.json 500, spec.md Edge Cases: rendering a scorecard
    that is already available failed. HTTP 500 with a fixed reason and no
    exception detail. Distinct from the 502 `llm_unavailable` reservation: no
    upstream service is involved, the stored record is rendered as-is."""

    model_config = ConfigDict(strict=True, extra="forbid")

    error: Literal["pdf_generation_failed"]
    reason: str
