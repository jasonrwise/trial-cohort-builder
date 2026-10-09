"""Deterministic eligibility scoring engine (03-01-PLAN.md Task 2,
research.md #5/#6, reused mechanism from
`specs/001-trial-eligibility-screening/research.md` §8).

`determine_eligibility` is a pure function of its four arguments only — no
randomness, no wall-clock read, no I/O, no delegation to an LLM. Given the
same `candidate_values`/`inclusion_criteria`/`exclusion_criteria`/
`proposed_status`, it always returns a byte-identical
`EligibilityDetermination` (SCORE-01).

`candidate_values` is a flat `code -> value` mapping (the caller assembles
this from a `DeidentifiedCandidate`'s `condition_values`/`observation_values`
— see `src/tools/query_patient_cohort.py`): a matched `Condition`'s code maps
to `True` (presence), and a matched `Observation`'s code maps to its numeric
`valueQuantity.value` when present, else `True`.

This phase's own `query_patient_cohort` always calls this function with
`proposed_status=None` — no assistant-proposed status exists at query time
(research.md #5). The `proposed_status`/`recomputed_mismatch` comparison
branch is nonetheless built and directly unit-tested here (`tests/unit/
test_scoring.py`), ready for Phase 4's `dispatch_screening_alert` to call
with a real proposal.
"""

from __future__ import annotations

from typing import Any

from src.models.candidate import EligibilityDetermination, EligibilityStatus
from src.models.trial import EligibilityCriterion, MappingStatus, Operator


def _criterion_satisfied(candidate_values: dict[str, Any], criterion: EligibilityCriterion) -> bool:
    """Evaluates one `VERIFIED` criterion's `(operator, threshold)` against
    the matching de-identified candidate value, looked up by `criterion.code`.

    `operator is None` covers D-11's legal VERIFIED-with-unparseable-
    comparison shape — treated the same as `Operator.PRESENT`: satisfied
    when the code appears in the candidate's de-identified values at all,
    regardless of its value. A missing value (the code never appeared for
    this candidate) is never satisfied, for every operator."""
    value = candidate_values.get(criterion.code)

    if criterion.operator is None or criterion.operator is Operator.PRESENT:
        return value is not None

    if value is None or criterion.threshold is None:
        return False

    try:
        if criterion.operator is Operator.EQ:
            return bool(value == criterion.threshold)
        if criterion.operator is Operator.GT:
            return bool(value > criterion.threshold)
        if criterion.operator is Operator.GTE:
            return bool(value >= criterion.threshold)
        if criterion.operator is Operator.LT:
            return bool(value < criterion.threshold)
        if criterion.operator is Operator.LTE:
            return bool(value <= criterion.threshold)
    except TypeError:
        return False

    return False


def determine_eligibility(
    candidate_values: dict[str, Any],
    inclusion_criteria: list[EligibilityCriterion],
    exclusion_criteria: list[EligibilityCriterion],
    proposed_status: EligibilityStatus | None = None,
) -> EligibilityDetermination:
    """Assigns `INELIGIBLE` on any matched exclusion or unmet verified
    inclusion, `BORDERLINE` when nothing failed outright but at least one
    `UNMAPPED` criterion was involved (never `ELIGIBLE` in that case —
    SCORE-01/research.md #6), and `ELIGIBLE` only when every verified
    criterion is satisfied and no criterion is `UNMAPPED`. An `UNMAPPED`
    criterion is always collected into `unmapped_criteria`, regardless of
    the final status."""
    matching_criteria: list[str] = []
    exclusion_flags: list[str] = []
    unmapped_criteria: list[str] = []
    any_inclusion_unmet = False
    any_exclusion_matched = False

    for criterion in inclusion_criteria:
        if criterion.mapping_status is MappingStatus.UNMAPPED:
            unmapped_criteria.append(criterion.raw_text)
            continue
        if _criterion_satisfied(candidate_values, criterion):
            matching_criteria.append(criterion.raw_text)
        else:
            any_inclusion_unmet = True

    for criterion in exclusion_criteria:
        if criterion.mapping_status is MappingStatus.UNMAPPED:
            unmapped_criteria.append(criterion.raw_text)
            continue
        if _criterion_satisfied(candidate_values, criterion):
            exclusion_flags.append(criterion.raw_text)
            any_exclusion_matched = True

    if any_exclusion_matched or any_inclusion_unmet:
        status = EligibilityStatus.INELIGIBLE
    elif unmapped_criteria:
        status = EligibilityStatus.BORDERLINE
    else:
        status = EligibilityStatus.ELIGIBLE

    recomputed_mismatch = proposed_status is not None and proposed_status != status

    return EligibilityDetermination(
        status=status,
        matching_criteria=matching_criteria,
        exclusion_flags=exclusion_flags,
        unmapped_criteria=unmapped_criteria,
        proposed_status=proposed_status,
        recomputed_mismatch=recomputed_mismatch,
    )
