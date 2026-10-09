"""The cohort anchor rule — one derivation shared by every adapter (AD-27,
Spec Kit T006/T007).

Moved verbatim out of `src/web/query_screening.py` so the web screening path,
the seeder (Phase 12) and render-time query-key markers all derive the same
`CohortAnchor` from a trial's inclusion criteria. The query-key markers (the
web criteria list and the PDF criteria table, DEMO-03) read the same private
finder through `query_key_positions`, and the unhonored-criteria count and
coverage warning (WEBAPP-11) read it through `unhonored_criteria_count`. The exclusion
partition (`partition_exclusions`, AD-37) sorts each VERIFIED exclusion into key-code or
unevaluated. The seeder support check, the manifest and `verify` call it. The web label
(spec 008 FR-017) calls `unevaluated_exclusion_count`. This module imports only the trial
models, so it is safe for any adapter (and finalize's closed no-notification
import allow-list) to depend on it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.models.trial import CodeSystem, CriterionKind, EligibilityCriterion, MappingStatus


@dataclass(frozen=True)
class CohortAnchor:
    condition_code: str
    condition_criterion_text: str
    observation_loinc: str | None
    observation_criterion_text: str | None


@dataclass(frozen=True)
class ExclusionPartition:
    key_code: tuple[EligibilityCriterion, ...]
    unevaluated: tuple[EligibilityCriterion, ...]


def _anchor_indexes(inclusion: Sequence[EligibilityCriterion]) -> tuple[int | None, int | None]:
    """The one finder behind the anchor rule: the index of the first VERIFIED
    ICD10CM criterion and, only when that exists, the index of the first
    VERIFIED LOINC criterion. `derive_cohort_anchor` builds its anchor from
    these indexes and `query_key_positions` returns them, so the cohort query
    and the query-key markers cannot disagree (AD-27, WD-8)."""
    condition_index = next(
        (
            index
            for index, criterion in enumerate(inclusion)
            if criterion.mapping_status is MappingStatus.VERIFIED
            and criterion.code_system is CodeSystem.ICD10CM
        ),
        None,
    )
    if condition_index is None:
        return None, None

    observation_index = next(
        (
            index
            for index, criterion in enumerate(inclusion)
            if criterion.mapping_status is MappingStatus.VERIFIED
            and criterion.code_system is CodeSystem.LOINC
        ),
        None,
    )
    return condition_index, observation_index


def _key_pairs(
    criteria: Sequence[EligibilityCriterion],
) -> set[tuple[CodeSystem | None, str | None]]:
    """The `(code_system, code)` pairs of the anchor condition and the anchor
    observation, read from the inclusion criteria. Empty when there is no anchor.
    Callers test membership only and never iterate the set."""
    inclusion = [c for c in criteria if c.kind is CriterionKind.INCLUSION]
    return {
        (inclusion[index].code_system, inclusion[index].code)
        for index in _anchor_indexes(inclusion)
        if index is not None
    }


def _key_codes(criteria: Sequence[EligibilityCriterion]) -> set[str | None]:
    """The code strings of the anchor condition and the anchor observation, read
    from the inclusion criteria. Empty when there is no anchor. The partition
    uses code strings because the scorer looks up a candidate value by code string
    alone. Callers test membership only and never iterate the set."""
    inclusion = [c for c in criteria if c.kind is CriterionKind.INCLUSION]
    return {inclusion[index].code for index in _anchor_indexes(inclusion) if index is not None}


def derive_cohort_anchor(inclusion: list[EligibilityCriterion]) -> CohortAnchor | None:
    """The 06-01 option-a rule: `condition_code` is the first VERIFIED
    ICD10CM inclusion criterion in protocol order; `observation_loinc` is
    the first VERIFIED LOINC inclusion criterion, else `None`. Exclusion
    criteria never anchor — an exclusion code would select exactly the
    patients the protocol excludes."""
    condition_index, observation_index = _anchor_indexes(inclusion)
    if condition_index is None:
        return None

    condition_criterion = inclusion[condition_index]
    observation_criterion = inclusion[observation_index] if observation_index is not None else None

    return CohortAnchor(
        condition_code=condition_criterion.code,  # type: ignore[arg-type]
        condition_criterion_text=condition_criterion.raw_text,
        observation_loinc=observation_criterion.code if observation_criterion else None,
        observation_criterion_text=(
            observation_criterion.raw_text if observation_criterion else None
        ),
    )


def query_key_positions(inclusion: Sequence[EligibilityCriterion]) -> frozenset[int]:
    """Positions, in `inclusion`, of the criteria whose codes drove the cohort
    query: the condition criterion and the observation criterion
    `derive_cohort_anchor` selects. By position, never by code, so a second
    criterion on a key code is not a query key. Empty when there is no anchor."""
    return frozenset(index for index in _anchor_indexes(inclusion) if index is not None)


def unhonored_criteria_count(criteria: Sequence[EligibilityCriterion]) -> int:
    """How many VERIFIED criteria, inclusion or exclusion, the scorer cannot
    honor (WEBAPP-11, WD-9). The scorer sees only the two retrieval-key codes
    per candidate, so a VERIFIED criterion is honored only when its
    (code_system, code) pair is the anchor condition pair (ICD10CM) or the
    anchor observation pair (LOINC); an exclusion or a second criterion on a key
    code is honored. UNMAPPED criteria are never counted. With no anchor every
    VERIFIED criterion counts."""
    key_pairs = _key_pairs(criteria)
    return sum(
        1
        for criterion in criteria
        if criterion.mapping_status is MappingStatus.VERIFIED
        and (criterion.code_system, criterion.code) not in key_pairs
    )


def partition_exclusions(criteria: Sequence[EligibilityCriterion]) -> ExclusionPartition:
    """Sort every VERIFIED exclusion criterion by the anchor rule (AD-37, SEED-14).
    An exclusion whose code string equals the anchor condition code or the anchor
    observation code goes in `key_code`, whatever its code system (FR-004). This
    matches how the scorer reads patient data, so the scorer evaluates no exclusion
    that this function lists as unevaluated. Every other VERIFIED exclusion goes in
    `unevaluated`. Each tuple keeps protocol order. UNMAPPED criteria and inclusion
    criteria appear in neither tuple. With no anchor both tuples are empty. A trial
    is partial exactly when `unevaluated` is not empty."""
    key_codes = _key_codes(criteria)
    if not key_codes:
        return ExclusionPartition(key_code=(), unevaluated=())
    exclusions = [
        c
        for c in criteria
        if c.kind is CriterionKind.EXCLUSION and c.mapping_status is MappingStatus.VERIFIED
    ]
    return ExclusionPartition(
        key_code=tuple(c for c in exclusions if c.code in key_codes),
        unevaluated=tuple(c for c in exclusions if c.code not in key_codes),
    )


def unevaluated_exclusion_count(criteria: Sequence[EligibilityCriterion]) -> int:
    """How many VERIFIED exclusions the scorer cannot evaluate: the length of
    `partition_exclusions(criteria).unevaluated` (AD-37). Never above
    `unhonored_criteria_count`: an exclusion off the key codes is also off the key
    pairs. With no anchor it is 0."""
    return len(partition_exclusions(criteria).unevaluated)


def coverage_gap_warning(unhonored_count: int, candidate_statuses: Sequence[str]) -> bool:
    """True only when criteria went unhonored and every candidate, of at least
    one, is INELIGIBLE: the all-INELIGIBLE table may be a coverage gap rather
    than a clinical result (WD-11). An empty list never warns."""
    return (
        unhonored_count > 0
        and len(candidate_statuses) > 0
        and all(status == "INELIGIBLE" for status in candidate_statuses)
    )
