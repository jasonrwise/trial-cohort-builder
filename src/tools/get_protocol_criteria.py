"""The real `get_protocol_criteria` body (D-06), replacing the Phase 1 stub.

Contract: specs/001-trial-eligibility-screening/contracts/get_protocol_criteria.json
The signature below matches that contract's `inputSchema` field for field —
one required string property, `nct_id` — because FastMCP infers the tool's
input schema from the function signature; there is no supported way to hand
it a raw JSON schema for the input side. The signature is unchanged from the
Phase 1 stub; only the return annotation and body are replaced.

Pipeline: validate NCT ID format (short-circuits before any upstream call,
per T-02-06) -> `screening.resolve_trial` (src/services/screening.py, AD-27:
`trials.fetch_trial` -> segment eligibility text -> verify every criterion
concurrently via `terminology.verify_criterion`, bounded by
`terminology.TERMINOLOGY_CONCURRENCY_LIMIT` -> assemble `Trial`) -> map its
`TrialFetchError` members to this tool's `ClientError`/`DegradedEnvelope`.
`TrialFetchError.RATE_LIMIT_EXHAUSTED` (DEGR-01: `fetch_trial` exhausting its
429-scoped retry/backoff) is the one `TrialFetchError` member mapped to
`DegradedEnvelope` rather than `ClientError` — the three others
(`NOT_FOUND`/`UPSTREAM_ERROR`/`INVALID_RESPONSE`) keep their existing
`ClientError` mapping unchanged. `terminology.TerminologyFetchError.UNAVAILABLE` (AD-34:
the terminology service stayed unreachable through `verify_criterion`'s 4 attempts, for any
one criterion) is mapped to the same `DegradedEnvelope` with the shared
`terminology.TERMINOLOGY_UNAVAILABLE_REASON` — the whole response degrades, never a
partial or UNMAPPED criteria list.
"""

from src.models.errors import ClientError, DegradedEnvelope
from src.models.trial import Trial
from src.services import screening, terminology, trials


async def get_protocol_criteria(nct_id: str) -> Trial | ClientError | DegradedEnvelope:
    if not trials.NCT_ID_PATTERN.match(nct_id):
        return ClientError(
            status="ERROR",
            reason=f"Malformed NCT ID format: {nct_id!r} (expected 'NCT' followed by 8 digits)",
        )

    result = await screening.resolve_trial(nct_id)

    if result is trials.TrialFetchError.NOT_FOUND:
        return ClientError(
            status="ERROR",
            reason=f"NCT ID not found in registry: {nct_id}",
        )
    if result is trials.TrialFetchError.UPSTREAM_ERROR:
        return ClientError(
            status="ERROR",
            reason="ClinicalTrials.gov upstream error while fetching the trial",
        )
    if result is trials.TrialFetchError.INVALID_RESPONSE:
        return ClientError(
            status="ERROR",
            reason="ClinicalTrials.gov returned a response that failed validation",
        )
    if result is trials.TrialFetchError.RATE_LIMIT_EXHAUSTED:
        return DegradedEnvelope(
            status="DEGRADED",
            data=[],
            reason="ClinicalTrials.gov rate limit exceeded after 4 attempts; try again later",
        )

    if result is terminology.TerminologyFetchError.UNAVAILABLE:
        return DegradedEnvelope(
            status="DEGRADED",
            data=[],
            reason=terminology.TERMINOLOGY_UNAVAILABLE_REASON,
        )

    # A segmentation ClientError (D-02) or the assembled Trial passes through unchanged.
    return result
