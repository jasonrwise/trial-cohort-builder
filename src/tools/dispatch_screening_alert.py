"""The real DRAFT-mode (CRC, 04-01-PLAN.md Task 2) and DISPATCHED-mode (PI,
04-02-PLAN.md Task 2) bodies for `dispatch_screening_alert`, replacing the
Phase-1 not-implemented stub entirely.

Contract: specs/001-trial-eligibility-screening/contracts/dispatch_screening_alert.json
The signature below matches that contract's `inputSchema` field for field —
`nct_id` and `candidates` (required), `channel_id` (optional) — because
FastMCP infers the tool's input schema from the function signature; there is
no supported way to hand it a raw JSON schema for the input side. The added
`ctx: Context` parameter is FastMCP's own dependency-injection mechanism
(transformed out of the inferred schema entirely, never shown to a caller)
and does not change the contract's shape.

`DispatchCandidateInput` mirrors the contract's nested candidate object and
lives here, local to this tool module, rather than in `src/models/` — it is
this tool's input schema mirror, not a domain model; `src/models/scorecard.py`
holds the different, gateway-recomputed `Scorecard`/`CandidateResult` domain
shapes this module assembles below.

Role threading (RBAC-05): the caller's role is never taken from the request
and never re-resolved inside this tool — `src/server.py`'s
`RBACGateMiddleware` already resolved it once per call (the single point of
truth) and hands it down via FastMCP's request-scoped Context state
(`ctx.get_state("resolved_role")`), read here as the injected `ctx`
parameter. A CRC-resolved role reaches the DRAFT body; a PI-resolved role
reaches the DISPATCHED body below (SITE_ADMIN is already denied by the RBAC
gate before this function is ever called, so it is a defensive-only
fallback here).

Recomputation rule (research.md #8, FR-008/FR-009): this tool does not
re-run `scoring.determine_eligibility` — the caller already ran the real
scoring pass in `query_patient_cohort` and supplies each candidate's
resulting `matching_criteria`/`exclusion_flags`/`unmapped_criteria`
classification directly. This tool recomputes `eligibility_status` from
that classification alone, mirroring `scoring.py`'s own guard rule at the
presentation layer: an exclusion match forbids an unqualified `ELIGIBLE` by
forcing `INELIGIBLE`, and a non-empty `unmapped_criteria` (with no exclusion
match) forbids it by forcing `BORDERLINE` — the caller's own
`eligibility_status` proposal is trusted only when neither guard fires.
`llm_proposed_status` always carries the caller's original proposal through
unchanged; `recomputed_mismatch` is true exactly when the two disagree.

PI/DISPATCHED branch (04-02-PLAN.md Task 2, research.md §12 AD-4/AD-6/AD-7/
AD-9): `channel_id` gets format validation only (`CHANNEL_ID_PATTERN`, the
one named module-level constant AD-7 requires — no liveness check, a
well-formed but nonexistent channel is left to `slack.post_message`'s own
error) before any Slack call. `SLACK_BOT_TOKEN` is read directly from the
environment here, once per call, and handed to `slack.post_message` as an
explicit parameter — `slack.py` never reads it itself (AD-6's "no boot-time
fail-fast" pairs with this module being the only place a missing/empty token
surfaces, as the same actionable-error shape as a Slack-side failure). The
`slack.post_message` call is wrapped in its own catch-all so any exception
`slack.py` fails to translate into a `SlackPostError` still maps to
`DISPATCH_FAILED` rather than crashing the process (AD-9). Exactly one
`audit.write_entry` call happens per branch, placed so every return path
passes through it (AD-4's outcome -> EventType mapping)."""

import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from src.models.access import EventType, Role
from src.models.errors import ClientError
from src.models.scorecard import CandidateResult, Scorecard
from src.services import audit, slack
from src.services.slack import SlackPostError

_NOT_IMPLEMENTED_REASON = (
    "dispatch_screening_alert is not yet implemented "
    "(stub registered in Phase 1; real logic lands in Phase 4)."
)

# research.md §12 AD-7: unverified-until-implementation-time character
# class/length, confirmed here against Slack's own channel-ID documentation —
# "C" (public channel) followed by exactly 10 uppercase-alphanumeric
# characters, 11 total. The single named module-level constant this branch's
# validation is pinned to; never re-derived ad hoc elsewhere.
CHANNEL_ID_PATTERN = re.compile(r"^C[A-Z0-9]{10}\Z")

_INVALID_CHANNEL_ID_REASON = (
    "channel_id is missing or is not a valid Slack channel ID "
    "(expected 'C' followed by 10 uppercase alphanumeric characters)"
)
_DISPATCH_FAILED_REASON = (
    "Failed to dispatch the scorecard to Slack. The attempt was recorded to "
    "the audit trail; no message was posted."
)


class DispatchCandidateInput(BaseModel):
    """Mirror of the contract's nested candidate object. `strict=True` and
    `extra="forbid"` match the contract's `additionalProperties: false`,
    consistent with every model in this project."""

    model_config = ConfigDict(strict=True, extra="forbid")

    patient_pseudonym: str
    eligibility_status: Literal["ELIGIBLE", "INELIGIBLE", "BORDERLINE"]
    matching_criteria: list[str] = Field(default_factory=list)
    exclusion_flags: list[str] = Field(default_factory=list)
    unmapped_criteria: list[str] = Field(default_factory=list)


class DraftDispatchResult(BaseModel):
    """The CRC/DRAFT success envelope
    (`contracts/dispatch_screening_alert.json`'s `outputSchema` first
    `oneOf` branch)."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["OK"] = "OK"
    mode: Literal["DRAFT"] = "DRAFT"
    scorecard: Scorecard


class DispatchedResult(BaseModel):
    """The PI/DISPATCHED success envelope
    (`contracts/dispatch_screening_alert.json`'s `outputSchema` second
    `oneOf` branch) — `slack_message_ts` is Slack's own message timestamp,
    carried through unchanged for audit correlation."""

    model_config = ConfigDict(strict=True, extra="forbid")

    status: Literal["OK"] = "OK"
    mode: Literal["DISPATCHED"] = "DISPATCHED"
    scorecard: Scorecard
    slack_message_ts: str


# Ordinal restrictiveness, least to most: an exclusion match or non-empty
# unmapped_criteria imposes a FLOOR, never a fixed overwrite — a caller's
# own proposal that is already at least as restrictive as the floor (e.g. an
# honest INELIGIBLE proposal alongside a non-empty unmapped_criteria) must
# not be loosened to BORDERLINE. Only a proposal LESS restrictive than the
# floor (most commonly an unqualified ELIGIBLE) gets tightened up to it.
_RESTRICTIVENESS: dict[str, int] = {"ELIGIBLE": 0, "BORDERLINE": 1, "INELIGIBLE": 2}
_BY_RESTRICTIVENESS = {rank: status for status, rank in _RESTRICTIVENESS.items()}


def _recompute_candidate(candidate: DispatchCandidateInput) -> CandidateResult:
    """Applies the two ELIGIBLE-forbidding guards described in this module's
    docstring as a restrictiveness FLOOR, not an overwrite — an exclusion
    match outranks an unmapped criterion (mirrors
    `scoring.determine_eligibility`'s own exclusion-before-unmapped
    precedence), and neither guard ever loosens a proposal that is already
    at least as restrictive as the floor it would otherwise impose."""
    if candidate.exclusion_flags:
        floor = _RESTRICTIVENESS["INELIGIBLE"]
    elif candidate.unmapped_criteria:
        floor = _RESTRICTIVENESS["BORDERLINE"]
    else:
        floor = _RESTRICTIVENESS["ELIGIBLE"]

    proposed_rank = _RESTRICTIVENESS[candidate.eligibility_status]
    recomputed_status = _BY_RESTRICTIVENESS[max(proposed_rank, floor)]

    return CandidateResult(
        patient_pseudonym=candidate.patient_pseudonym,
        llm_proposed_status=candidate.eligibility_status,
        eligibility_status=recomputed_status,
        recomputed_mismatch=recomputed_status != candidate.eligibility_status,
        matching_criteria=candidate.matching_criteria,
        exclusion_flags=candidate.exclusion_flags,
        unmapped_criteria=candidate.unmapped_criteria,
    )


def _build_slack_blocks(scorecard: Scorecard) -> list[dict[str, Any]]:
    """One Block Kit section per candidate, summarizing the recomputed
    eligibility_status. Deliberately minimal — the message's self-describing
    guarantee (nct_id/screened_at/dispatching_role) is `slack.post_message`'s
    own job via its header block, not this function's."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{candidate.patient_pseudonym}*: {candidate.eligibility_status}",
            },
        }
        for candidate in scorecard.candidates
    ]


async def dispatch_screening_alert(
    nct_id: str,
    candidates: list[DispatchCandidateInput],
    ctx: Context,
    channel_id: str | None = None,
) -> DraftDispatchResult | DispatchedResult | ClientError:
    resolved_role = await ctx.get_state("resolved_role")

    if resolved_role is Role.CRC:
        scorecard = Scorecard(
            nct_id=nct_id,
            screened_at=datetime.now(UTC),
            candidates=[_recompute_candidate(candidate) for candidate in candidates],
            mode="DRAFT",
            dispatching_role="CRC",
        )

        audit.write_entry(
            role=Role.CRC,
            nct_id=nct_id,
            event_type=EventType.DISPATCH_ALLOWED,
            detail="draft scorecard rendered",
        )

        return DraftDispatchResult(scorecard=scorecard)

    if resolved_role is Role.PI:
        if not channel_id or not CHANNEL_ID_PATTERN.match(channel_id):
            audit.write_entry(
                role=Role.PI,
                nct_id=nct_id,
                event_type=EventType.DISPATCH_DENIED,
                detail="channel_id missing or invalid format",
            )
            return ClientError(status="ERROR", reason=_INVALID_CHANNEL_ID_REASON)

        scorecard = Scorecard(
            nct_id=nct_id,
            screened_at=datetime.now(UTC),
            candidates=[_recompute_candidate(candidate) for candidate in candidates],
            mode="DISPATCHED",
            dispatching_role="PI",
        )

        # AD-6: read once per call, never cached; a missing/empty token
        # surfaces as the same DISPATCH_FAILED shape as any other Slack
        # failure, only when a PI caller actually attempts a dispatch.
        bot_token = os.environ.get("SLACK_BOT_TOKEN")

        try:
            post_result = await slack.post_message(
                channel_id=channel_id,
                nct_id=nct_id,
                screened_at=scorecard.screened_at,
                dispatching_role="PI",
                blocks=_build_slack_blocks(scorecard),
                bot_token=bot_token,
            )
        except Exception:  # noqa: BLE001 - AD-9's own catch-all: any exception
            # slack.py fails to translate into a SlackPostError must still map
            # to DISPATCH_FAILED here, never crash the process.
            post_result = SlackPostError.TRANSPORT_ERROR

        if isinstance(post_result, SlackPostError):
            audit.write_entry(
                role=Role.PI,
                nct_id=nct_id,
                event_type=EventType.DISPATCH_FAILED,
                detail=f"Slack post failed: {post_result.value}",
            )
            return ClientError(status="ERROR", reason=_DISPATCH_FAILED_REASON)

        audit.write_entry(
            role=Role.PI,
            nct_id=nct_id,
            event_type=EventType.DISPATCH_ALLOWED,
            detail="scorecard dispatched to Slack",
        )

        return DispatchedResult(scorecard=scorecard, slack_message_ts=post_result)

    # Defensive-only: the RBAC gate already denies SITE_ADMIN (and any
    # unresolved role) before this tool body is ever reached.
    return ClientError(status="ERROR", reason=_NOT_IMPLEMENTED_REASON)
