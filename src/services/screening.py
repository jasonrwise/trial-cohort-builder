"""The one criteria pipeline and the one cohort core shared by the MCP tools, the web
screening path and the seeder (AD-27, Spec Kit T008-T010): "none keeps a copy".

`resolve_trial` owns fetch -> segment -> concurrent criterion verification -> `Trial`
assembly. Candidate-value flattening (`_build_candidate_values` and its helpers) was moved
verbatim out of `src/tools/query_patient_cohort.py` so no adapter has to import another
adapter, and flattens a de-identified candidate's related resources into the same
`code -> value` mapping by construction — MCP/web parity.

Every upstream step is called as a module attribute (`trials.fetch_trial`,
`terminology.verify_criterion`, ...), never from-imported, so the test suite's
monkeypatches keep intercepting the calls. Errors are returned as values; each adapter
keeps its own presentation mapping (reason strings, 422 vs DEGRADED).
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from src.models.candidate import EligibilityDetermination
from src.models.errors import ClientError
from src.models.trial import CriterionKind, EligibilityCriterion, Trial
from src.services import deidentify, fhir, scoring, terminology, trials
from src.services.deidentify import DeidentifiedCandidate

# AD-27: the one cohort-limit constant. The MCP tool's `limit` input schema (ge=1,
# le=COHORT_LIMIT_MAX, default COHORT_LIMIT_DEFAULT) and the web path's fixed cohort size
# both read these.
COHORT_LIMIT_DEFAULT = 25
COHORT_LIMIT_MAX = 200


async def resolve_trial(
    nct_id: str,
) -> Trial | trials.TrialFetchError | ClientError | terminology.TerminologyFetchError:
    """Fetches `nct_id`, segments its eligibility text, verifies every criterion
    concurrently and assembles the `Trial`. A `TrialFetchError` member from
    `trials.fetch_trial` and the `ClientError` from `trials.segment_criteria` are
    returned unchanged for the calling adapter to present. AD-34: every criterion is
    verified (the whole fan-out is collected, never cancelled early), and if any came
    back `terminology.TerminologyFetchError.UNAVAILABLE` that member is returned — never
    a partially verified `Trial`."""
    payload = await trials.fetch_trial(nct_id)
    if isinstance(payload, trials.TrialFetchError):
        return payload

    segmentation_result = trials.segment_criteria(
        payload.protocolSection.eligibilityModule.eligibilityCriteria
    )
    # Plan 02-03 extended segment_criteria to return a ClientError (D-02:
    # neither section marker found) instead of a tuple. Neither real demo
    # trial exercises this branch, so it went unwired until this plan's
    # contract-conformance pass caught it: unconditionally unpacking the
    # return value here silently destructured a ClientError's own pydantic
    # fields into two bogus "criteria" strings instead of propagating the
    # error, fabricating a garbage "successful" Trial response instead of
    # the clean ClientError this branch is supposed to produce (Rule 1 bug,
    # found and fixed in plan 02-05).
    if isinstance(segmentation_result, ClientError):
        return segmentation_result
    inclusion_raw, exclusion_raw = segmentation_result

    # One flat asyncio.gather over both sections, tagged by kind — order is
    # preserved (asyncio.gather returns results in call order regardless of
    # completion order), and each call is independent/stateless so concurrent
    # execution changes nothing about correctness. Fan-out is bounded inside
    # verify_criterion by TERMINOLOGY_CONCURRENCY_LIMIT.
    all_criteria = [(text, CriterionKind.INCLUSION) for text in inclusion_raw] + [
        (text, CriterionKind.EXCLUSION) for text in exclusion_raw
    ]
    results = await asyncio.gather(
        *[terminology.verify_criterion(text, kind) for text, kind in all_criteria]
    )
    if any(result is terminology.TerminologyFetchError.UNAVAILABLE for result in results):
        return terminology.TerminologyFetchError.UNAVAILABLE
    inclusion_criteria = list(results[: len(inclusion_raw)])
    exclusion_criteria = list(results[len(inclusion_raw) :])

    return Trial(
        nct_id=nct_id,
        title=payload.protocolSection.identificationModule.briefTitle,
        inclusion_criteria=inclusion_criteria,
        exclusion_criteria=exclusion_criteria,
    )


@dataclass(frozen=True)
class ScoredCandidate:
    """One de-identified candidate and its recomputed eligibility determination."""

    candidate: DeidentifiedCandidate
    determination: EligibilityDetermination


async def fetch_candidates(
    condition_code: str, observation_loinc: str | None, limit: int
) -> list[DeidentifiedCandidate] | fhir.FhirFetchError:
    """Fetches the cohort bundle and de-identifies it (COHT-02). A `FhirFetchError`
    member is returned unchanged for the calling adapter to present; callers only ever
    receive `DeidentifiedCandidate` values, never raw FHIR resources."""
    bundle = await fhir.fetch_cohort(condition_code, observation_loinc, limit)
    if isinstance(bundle, fhir.FhirFetchError):
        return bundle
    return deidentify.strip_bundle(bundle, condition_code, observation_loinc)


def score_candidate(
    candidate: DeidentifiedCandidate,
    condition_code: str,
    observation_loinc: str | None,
    inclusion: list[EligibilityCriterion],
    exclusion: list[EligibilityCriterion],
) -> EligibilityDetermination:
    """Scores one candidate against the criteria with `proposed_status=None` — no
    assistant-proposed status exists at screening time (SCORE-01), so the determination
    never carries a recompute mismatch."""
    candidate_values = _build_candidate_values(candidate, condition_code, observation_loinc)
    return scoring.determine_eligibility(
        candidate_values, inclusion, exclusion, proposed_status=None
    )


async def run_screening(
    condition_code: str,
    observation_loinc: str | None,
    limit: int,
    inclusion: list[EligibilityCriterion],
    exclusion: list[EligibilityCriterion],
) -> list[ScoredCandidate] | fhir.FhirFetchError:
    """The composed cohort core the MCP tool calls: fetch and de-identify, then score
    every candidate in order. The web path composes `fetch_candidates` and
    `score_candidate` itself because its AD-25 LLM fan-out sits between the two."""
    candidates = await fetch_candidates(condition_code, observation_loinc, limit)
    if isinstance(candidates, fhir.FhirFetchError):
        return candidates
    return [
        ScoredCandidate(
            candidate=candidate,
            determination=score_candidate(
                candidate, condition_code, observation_loinc, inclusion, exclusion
            ),
        )
        for candidate in candidates
    ]


def _first_coding_code(values: dict[str, Any], expected_code: str | None = None) -> str | None:
    """Returns the `values["code"]["coding"][]` entry whose own `code`
    equals `expected_code`, tolerating any non-conforming shape (a
    `code`/`coding` that isn't the expected dict/list) by returning `None`
    rather than raising — this data is sourced from the untyped,
    externally-controlled FHIR store (`fhir.py`'s
    `Bundle.entry.resource: dict[str, Any]`), so a malformed shape must
    fail closed, not crash the call.

    `expected_code` should be the SAME code `deidentify._matches_code`
    already selected this resource for (`condition_code`/
    `observation_loinc`) — a resource can legally carry more than one
    `coding` entry (e.g. dual ICD-10-CM + SNOMED-CT on a Condition), and
    `_matches_code` accepts a match anywhere in the array, not just at
    index 0. Trusting `coding[0]` unconditionally could silently extract a
    *different* code than the one the resource was actually selected for,
    causing `scoring.determine_eligibility` to score against the wrong
    code (WR-03, gsd-code-review 03-REVIEW.md). When `expected_code` is
    `None` (observation extraction when the caller didn't supply
    `observation_loinc` — `deidentify.strip_bundle` keeps every related
    Observation unfiltered in that case, so there is no code to
    disambiguate against), this falls back to `coding[0]`, the pre-existing
    behavior."""
    code_field = values.get("code")
    codings = code_field.get("coding") if isinstance(code_field, dict) else None
    if not codings:
        return None
    if expected_code is not None:
        for coding in codings:
            if isinstance(coding, dict) and coding.get("code") == expected_code:
                return expected_code
        return None
    if not isinstance(codings[0], dict):
        return None
    return codings[0].get("code")


def _extract_condition_code(condition_values: dict[str, Any], condition_code: str) -> str | None:
    return _first_coding_code(condition_values, condition_code)


def _extract_observation_code_and_value(
    observation_values: dict[str, Any], observation_loinc: str | None
) -> tuple[str | None, Any]:
    code = _first_coding_code(observation_values, observation_loinc)
    value_quantity = observation_values.get("valueQuantity")
    value = value_quantity.get("value") if isinstance(value_quantity, dict) else None
    return code, value


def _build_candidate_values(
    de_identified: DeidentifiedCandidate,
    condition_code: str,
    observation_loinc: str | None,
) -> dict[str, Any]:
    """Flattens a de-identified candidate's `condition_values`/
    `observation_values` into the `code -> value` mapping
    `scoring.determine_eligibility` scores against: the matched
    `Condition`'s code maps to `True` (presence — a diagnosis either applies
    or it doesn't), and the matched `Observation`'s code maps to its numeric
    `valueQuantity.value` when present, else `True`. `condition_code`/
    `observation_loinc` are the same codes `strip_bundle` selected the
    related resources with, threaded through so the code extracted here
    matches the code the resource was actually selected for (WR-03)."""
    values: dict[str, Any] = {}

    matched_condition_code = _extract_condition_code(de_identified.condition_values, condition_code)
    if matched_condition_code is not None:
        values[matched_condition_code] = True

    observation_code, observation_value = _extract_observation_code_and_value(
        de_identified.observation_values, observation_loinc
    )
    if observation_code is not None:
        values[observation_code] = observation_value if observation_value is not None else True

    return values
