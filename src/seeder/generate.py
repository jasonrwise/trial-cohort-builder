"""The deterministic cohort generator (Spec Kit T036 to T039, SEED-03, SEED-04, SEED-06,
SEED-07; Tier 2 T050 and T051, SEED-05; AD-29, AD-32, AD-33).

Everything here is a pure function of (snapshot trial, seed, group sizes): the support
predicate turns a trial into a ``SeedSpec`` (or a list of reasons it is outside the demo
limit), and ``build_cohort`` turns a ``SeedSpec`` into a FHIR transaction bundle of
eligible, near-miss and noise patients plus the ground-truth manifest.

Design rules this module holds:

* The anchor condition and observation come from ``derive_cohort_anchor``, the same rule
  the web path uses (AD-27); there is no second "first ICD-10 code" rule here.
* The generated value is built from the operator direction, never by comparing a value
  with the threshold, and this module never imports the scoring service (AD-32): the
  labels are an independent claim that the round-trip proof checks against scoring.
* A near-miss patient carries the anchor condition and an anchor-LOINC observation whose
  value is just outside the threshold, so it satisfies every retrieval key and is retrieved,
  but fails the threshold. Its value is the threshold plus a fixed per-operator number of
  0.1 steps from a table; no comparison is made here and the scoring service decides the
  outcome (the round-trip test proves it is INELIGIBLE).
* Which VERIFIED exclusions refuse a trial and which a seed lists comes from
  ``partition_exclusions``, the one rule shared with the manifest, ``verify`` and the web
  label (AD-37). This module creates no resource for a listed exclusion (FR-003). A partial
  seed's manifest gains ``partial`` and ``unevaluated_exclusions``, built from the same
  ``UnevaluatedExclusion`` values as the report lines.
* Group totals are checked against ``screening.COHORT_LIMIT_DEFAULT``, the single cohort
  limit constant; the retrievable groups (eligible plus near-miss) above it are a returned
  failure, never a warning.
* Protocol, group and data-revision tags go on Patient resources only (AD-29). Condition
  and Observation resources carry no tag of any kind.
* Every varying choice is a SHA-256 index over ``seed|nct|group|index|field`` (WD-4): no
  random module, no ``hash()``, no clock. All dates derive from ``ANCHOR_DATE``.
* Pools are tuples; no module-level dict, list or set exists (AD-1, AD-33).
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import math
import unicodedata
from dataclasses import dataclass
from typing import Any

from src.models import vocabulary
from src.models.trial import (
    CodeSystem,
    EligibilityCriterion,
    MappingStatus,
    Operator,
    Trial,
)
from src.seeder import revision, units
from src.services import screening, terminology
from src.services.anchor import derive_cohort_anchor, partition_exclusions

GROUP_ELIGIBLE: str = "eligible"
GROUP_NEAR_MISS: str = "near-miss"
GROUP_NOISE: str = "noise"

# The one fixed date every generated date derives from (FR-021).
ANCHOR_DATE: str = "2026-01-01"

# Operator -> direction of the satisfied side. +1 means the satisfied values lie above the
# threshold, -1 below. Built as pairs so no comparison with the threshold is ever needed.
_DIRECTIONS: tuple[tuple[Operator, int], ...] = (
    (Operator.GT, 1),
    (Operator.GTE, 1),
    (Operator.LT, -1),
    (Operator.LTE, -1),
)
# Operator -> signed 0.1 steps from the threshold to the nearest value that FAILS it. A strict
# comparison (GT, LT) already fails on the threshold itself, so no step is needed there; an
# inclusive one (GTE, LTE) fails one step beyond it. Pairs again: no comparison is ever made.
_NEAR_MISS_STEPS: tuple[tuple[Operator, int], ...] = (
    (Operator.GT, 0),
    (Operator.GTE, -1),
    (Operator.LT, 0),
    (Operator.LTE, 1),
)
# The operators a numeric threshold can be generated for (PRD FR-3, FR-36; WD-1: EQ is not one).
_NUMERIC_OPERATORS: tuple[Operator, ...] = (Operator.GT, Operator.GTE, Operator.LT, Operator.LTE)
_VALUE_STEP: float = 0.1
_VALUE_STEPS: int = 10
# Thresholds of this magnitude or larger are refused: 0.1 steps stay exact far below it (they fail
# near 4.5e14), and it is above clinical lab thresholds such as viral loads.
_MAX_THRESHOLD: float = 1_000_000_000.0

# Unrelated conditions for noise patients: (ICD-10-CM code, display).
_NOISE_CONDITIONS: tuple[tuple[str, str], ...] = (
    ("H10.9", "Unspecified conjunctivitis"),
    ("J45.909", "Unspecified asthma, uncomplicated"),
    ("K21.9", "Gastro-esophageal reflux disease without esophagitis"),
    ("L70.0", "Acne vulgaris"),
    ("M54.50", "Low back pain, unspecified"),
    ("N39.0", "Urinary tract infection, site not specified"),
)

# Unrelated observations for noise patients: (LOINC code, display, low, high).
_NOISE_OBSERVATIONS: tuple[tuple[str, str, float, float], ...] = (
    ("2085-9", "HDL cholesterol", 40.0, 80.0),
    ("2093-3", "Total cholesterol", 120.0, 199.0),
    ("39156-5", "Body mass index", 18.5, 29.9),
    ("8462-4", "Diastolic blood pressure", 60.0, 89.0),
    ("8480-6", "Systolic blood pressure", 90.0, 139.0),
)

# Committed fictional identity pools (T-12-08): invented names and places, never real identities.
FAMILY_NAMES: tuple[str, ...] = (
    "Aldermoor",
    "Brackwell",
    "Cindermere",
    "Dunhallow",
    "Eastwick",
    "Fennimore",
    "Gravenhurst",
    "Harrowgate",
    "Ilverton",
    "Juniperwood",
    "Kestrelby",
    "Lanthorne",
)
GIVEN_NAMES: tuple[str, ...] = (
    "Alvareth",
    "Brennoch",
    "Caldwyn",
    "Desmora",
    "Elowyn",
    "Fenrick",
    "Galdric",
    "Hestian",
    "Ilmarin",
    "Jorvane",
    "Kellivan",
    "Lorimar",
)
STREET_LINES: tuple[str, ...] = (
    "14 Quillfeather Lane",
    "27 Marrowbrook Court",
    "301 Thistledown Road",
    "58 Wickerfield Way",
    "9 Larkspur Terrace",
    "112 Copperleaf Drive",
    "73 Nettlebank Street",
    "240 Ashgrove Place",
)
# (city, state, postal code): fictional places in the reserved ZZ state.
LOCALITIES: tuple[tuple[str, str, str], ...] = (
    ("Marrowbrook", "ZZ", "00117"),
    ("Thistlemere", "ZZ", "00234"),
    ("Wickerfield", "ZZ", "00351"),
    ("Larkspur Falls", "ZZ", "00468"),
    ("Copperleaf", "ZZ", "00585"),
    ("Nettlebank", "ZZ", "00692"),
)
_GENDERS: tuple[str, ...] = ("female", "male")
_XHTML_NAMESPACE: str = "http://www.w3.org/1999/xhtml"
_MARKER_DISPLAY: str = "test health data"
# Birth date offsets before ANCHOR_DATE span ages of 25 to 84 years.
_BIRTH_MIN_DAYS: int = 9200
_BIRTH_SPAN_DAYS: int = 21800
_PHONE_PREFIX: str = "555-01"
_MRN_PREFIX: str = "MRN-"
_MRN_HEX_DIGITS: int = 8

_ONSET_MIN_DAYS: int = 30
_ONSET_SPAN_DAYS: int = 1500
_OBSERVATION_SPAN_DAYS: int = 60
_NOISE_VALUE_STEPS: int = 101


def single_line(text: str) -> str:
    """Protocol text made safe to print as one report line: control characters are removed
    and every run of whitespace, a newline included, becomes one space. The manifest keeps the
    original text, because `json.dumps` already escapes it."""
    printable = "".join(c for c in text if c.isspace() or unicodedata.category(c) != "Cc")
    return " ".join(printable.split())


@dataclass(frozen=True)
class UnevaluatedExclusion:
    """A VERIFIED exclusion the scorer cannot evaluate, kept for the partial-seed report."""

    text: str
    code_system: str
    code: str

    @classmethod
    def from_criterion(cls, criterion: EligibilityCriterion) -> UnevaluatedExclusion:
        assert criterion.code_system is not None and criterion.code is not None
        return cls(
            text=criterion.raw_text,
            code_system=criterion.code_system.value,
            code=criterion.code,
        )

    @property
    def report_line(self) -> str:
        return f"exclusion: {single_line(self.text)} ({self.code_system}:{self.code})"

    @property
    def manifest_item(self) -> dict[str, str]:
        """The manifest entry for this exclusion; the report line comes from the same values."""
        return {"text": self.text, "code_system": self.code_system, "code": self.code}


@dataclass(frozen=True)
class SeedSpec:
    nct_id: str
    condition_code: str
    observation_loinc: str
    operator: Operator
    threshold: float
    not_represented: tuple[str, ...]
    unevaluated: tuple[UnevaluatedExclusion, ...] = ()

    @property
    def unevaluated_exclusions(self) -> tuple[str, ...]:
        """The report line of each unevaluated exclusion, in protocol order."""
        return tuple(entry.report_line for entry in self.unevaluated)


@dataclass(frozen=True)
class UnsupportedTrial:
    reasons: tuple[str, ...]
    not_represented: tuple[str, ...]


@dataclass(frozen=True)
class GroupSizes:
    eligible: int = 15
    near_miss: int = 10
    noise: int = 10

    def as_pairs(self) -> tuple[tuple[str, int], ...]:
        """``(group, size)`` pairs sorted by group name; near-miss only when it is positive,
        so a size of 0 reproduces the data revision of a run without the group (FR-018)."""
        near_miss = ((GROUP_NEAR_MISS, self.near_miss),) if self.near_miss > 0 else ()
        return ((GROUP_ELIGIBLE, self.eligible), *near_miss, (GROUP_NOISE, self.noise))

    @property
    def retrievable(self) -> int:
        """The patients a cohort query can return: the eligible and near-miss groups."""
        return self.eligible + self.near_miss


@dataclass(frozen=True)
class CohortTooLarge:
    retrievable: int
    limit: int


@dataclass(frozen=True)
class GeneratedCohort:
    bundle: dict[str, Any]
    manifest: dict[str, Any]
    unit_fallbacks: tuple[str, ...]


@dataclass(frozen=True)
class _Inspection:
    reasons: tuple[str, ...]
    not_represented: tuple[str, ...]
    observation: EligibilityCriterion | None
    unevaluated: tuple[UnevaluatedExclusion, ...] = ()


def _is_verified(criterion: EligibilityCriterion, system: CodeSystem) -> bool:
    return criterion.mapping_status is MappingStatus.VERIFIED and criterion.code_system is system


def _observation_reasons(criterion: EligibilityCriterion) -> list[str]:
    """Why a VERIFIED LOINC inclusion cannot yield a numeric threshold, if it cannot."""
    if criterion.operator not in _NUMERIC_OPERATORS:
        if terminology.is_threshold_range(criterion.raw_text):
            return [f"LOINC criterion '{criterion.raw_text}' is a two-sided range (not supported)"]
        operator = criterion.operator.value if criterion.operator else "none"
        return [
            (
                f"LOINC criterion '{criterion.raw_text}' operator {operator} "
                "is not numeric (GT, GTE, LT or LTE required)"
            )
        ]
    threshold = criterion.threshold
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
    ):
        return [f"LOINC criterion '{criterion.raw_text}' threshold is not a number"]
    reasons: list[str] = []
    # A threshold with more decimals rounds onto the wrong side of a strict operator. This
    # checks the threshold's own shape and compares no generated value with it (AD-32).
    if round(threshold, 2) != threshold:
        reasons.append(
            f"LOINC criterion '{criterion.raw_text}' threshold {threshold!r} "
            "has more than 2 decimal places"
        )
    if abs(threshold) >= _MAX_THRESHOLD:
        reasons.append(
            f"LOINC criterion '{criterion.raw_text}' threshold {threshold!r} "
            f"is too large (the limit is {_MAX_THRESHOLD:.0f})"
        )
    return reasons


def _inspect(trial: Trial) -> _Inspection:
    """Walk both criteria lists in protocol order and sort every criterion into a bucket."""
    criteria = (*trial.inclusion_criteria, *trial.exclusion_criteria)
    not_represented = tuple(
        f"{criterion.kind.value}: {criterion.raw_text}"
        for criterion in criteria
        if criterion.mapping_status is MappingStatus.UNMAPPED
    )
    if not any(criterion.mapping_status is MappingStatus.VERIFIED for criterion in criteria):
        return _Inspection(
            reasons=("zero usable criteria: no VERIFIED criterion can be represented",),
            not_represented=not_represented,
            observation=None,
        )

    conditions = [c for c in trial.inclusion_criteria if _is_verified(c, CodeSystem.ICD10CM)]
    observations = [c for c in trial.inclusion_criteria if _is_verified(c, CodeSystem.LOINC)]
    partition = partition_exclusions(criteria)

    reasons: list[str] = []
    if not conditions:
        reasons.append("no VERIFIED ICD-10-CM inclusion criterion")
    reasons.extend(
        f"more than one VERIFIED ICD-10-CM inclusion criterion: '{extra.raw_text}'"
        for extra in conditions[1:]
    )
    if not observations:
        reasons.append("no VERIFIED LOINC inclusion criterion")
    reasons.extend(
        f"more than one VERIFIED LOINC inclusion criterion: '{extra.raw_text}'"
        for extra in observations[1:]
    )
    for observation in observations:
        reasons.extend(_observation_reasons(observation))
    reasons.extend(
        f"VERIFIED exclusion criterion '{exclusion.raw_text}' on a cohort key code "
        "cannot be represented"
        for exclusion in partition.key_code
    )
    return _Inspection(
        reasons=tuple(sorted(reasons)),
        not_represented=not_represented,
        observation=observations[0] if observations else None,
        unevaluated=tuple(UnevaluatedExclusion.from_criterion(c) for c in partition.unevaluated),
    )


def unsupported_reasons(trial: Trial) -> tuple[str, ...]:
    """Sorted reasons the trial is outside the demo limit; empty when it is supported."""
    return _inspect(trial).reasons


def derive_seed_spec(trial: Trial) -> SeedSpec | UnsupportedTrial:
    """The seed specification for a supported trial, else the reasons it is not.

    The anchor codes come from ``derive_cohort_anchor``, the one rule shared with the web
    path; a supported trial has exactly one ICD-10-CM inclusion, so the anchor is that
    criterion, and exactly one LOINC inclusion, so the observation is that criterion.
    """
    inspection = _inspect(trial)
    anchor = derive_cohort_anchor(trial.inclusion_criteria)
    observation = inspection.observation
    if (
        inspection.reasons
        or anchor is None
        or anchor.observation_loinc is None
        or observation is None
        or observation.operator is None
        or not isinstance(observation.threshold, (int, float))
    ):
        return UnsupportedTrial(
            reasons=inspection.reasons, not_represented=inspection.not_represented
        )
    return SeedSpec(
        nct_id=trial.nct_id,
        condition_code=anchor.condition_code,
        observation_loinc=anchor.observation_loinc,
        operator=observation.operator,
        threshold=float(observation.threshold),
        not_represented=inspection.not_represented,
        unevaluated=inspection.unevaluated,
    )


def check_group_sizes(sizes: GroupSizes) -> CohortTooLarge | None:
    """A failure when the retrievable group exceeds the single cohort limit, else ``None``."""
    limit = screening.COHORT_LIMIT_DEFAULT
    if sizes.retrievable > limit:
        return CohortTooLarge(retrievable=sizes.retrievable, limit=limit)
    return None


def serialize(document: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, compact separators."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _pick(seed: int, nct_id: str, group: str, index: int, field: str, modulus: int) -> int:
    """A deterministic index in ``range(modulus)`` for one varying field of one patient."""
    key = f"{seed}|{nct_id}|{group}|{index}|{field}"
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % modulus


def _direction(operator: Operator) -> int:
    for candidate, direction in _DIRECTIONS:
        if candidate is operator:
            return direction
    raise ValueError(f"operator {operator.value} has no satisfied direction")


def _near_miss_steps(operator: Operator) -> int:
    for candidate, steps in _NEAR_MISS_STEPS:
        if candidate is operator:
            return steps
    raise ValueError(f"operator {operator.value} has no near-miss offset")


def _date_before_anchor(days: int) -> str:
    return (datetime.date.fromisoformat(ANCHOR_DATE) - datetime.timedelta(days=days)).isoformat()


@dataclass(frozen=True)
class _Identity:
    """The identifier-shaped values of one patient, computed once and shared by the
    Patient, Condition and Observation builders so every narrative repeats the same text."""

    family: str
    given: str
    street: str
    city: str
    state: str
    postal_code: str
    birth_date: str
    phone: str
    mrn: str


def _identity(spec: SeedSpec, group: str, index: int, seed: int) -> _Identity:
    def pick(field: str, pool_size: int) -> int:
        return _pick(seed, spec.nct_id, group, index, field, pool_size)

    city, state, postal_code = LOCALITIES[pick("locality", len(LOCALITIES))]
    mrn_key = f"{seed}|{spec.nct_id}|{group}|{index}|mrn"
    return _Identity(
        family=FAMILY_NAMES[pick("family", len(FAMILY_NAMES))],
        given=GIVEN_NAMES[pick("given", len(GIVEN_NAMES))],
        street=STREET_LINES[pick("street", len(STREET_LINES))],
        city=city,
        state=state,
        postal_code=postal_code,
        birth_date=_date_before_anchor(_BIRTH_MIN_DAYS + pick("birth", _BIRTH_SPAN_DAYS)),
        phone=f"{_PHONE_PREFIX}{pick('phone', 100):02d}",
        mrn=_MRN_PREFIX + hashlib.sha256(mrn_key.encode("utf-8")).hexdigest()[:_MRN_HEX_DIGITS],
    )


def _identifier_paragraphs(identity: _Identity) -> str:
    """The paragraphs repeating the patient's identifiers, shared by all three narratives
    (research A4: Synthea repeats them in every resource narrative)."""
    return (
        f"<p>Name: {html.escape(identity.given)} {html.escape(identity.family)}</p>"
        f"<p>Birth date: {identity.birth_date}</p>"
        f"<p>Address: {html.escape(identity.street)}, {html.escape(identity.city)}, "
        f"{identity.state} {identity.postal_code}</p>"
        f"<p>Phone: {identity.phone}</p>"
        f"<p>MRN: {identity.mrn}</p>"
    )


def _narrative(fact: str, identity: _Identity) -> dict[str, str]:
    """A generated narrative stating one clinical fact and repeating the identifiers."""
    return {
        "status": "generated",
        "div": (
            f'<div xmlns="{_XHTML_NAMESPACE}">'
            f"<p>{html.escape(fact)}</p>"
            f"{_identifier_paragraphs(identity)}"
            "</div>"
        ),
    }


def _patient(
    spec: SeedSpec, group: str, index: int, identity: _Identity, seed: int, data_revision: str
) -> dict:
    """A Patient with the three tags, the synthetic marker and identifier-shaped fields."""
    narrative = _narrative("Synthetic patient: generated test data, not a real person.", identity)
    return {
        "resourceType": "Patient",
        "id": revision.resource_id(spec.nct_id, group, index),
        "meta": {
            "tag": [
                {"system": vocabulary.PROTOCOL_TAG_SYSTEM, "code": spec.nct_id},
                {"system": vocabulary.GROUP_TAG_SYSTEM, "code": group},
                {"system": vocabulary.DATA_REVISION_TAG_SYSTEM, "code": data_revision},
            ],
            "security": [
                {
                    "system": vocabulary.SYNTHETIC_MARKER_SYSTEM,
                    "code": vocabulary.SYNTHETIC_MARKER_CODE,
                    "display": _MARKER_DISPLAY,
                }
            ],
        },
        "identifier": [{"system": vocabulary.MRN_IDENTIFIER_SYSTEM, "value": identity.mrn}],
        "name": [{"family": identity.family, "given": [identity.given]}],
        "gender": _GENDERS[_pick(seed, spec.nct_id, group, index, "gender", len(_GENDERS))],
        "birthDate": identity.birth_date,
        "telecom": [{"system": "phone", "value": identity.phone, "use": "home"}],
        "address": [
            {
                "line": [identity.street],
                "city": identity.city,
                "state": identity.state,
                "postalCode": identity.postal_code,
            }
        ],
        "text": narrative,
    }


def _condition(
    spec: SeedSpec,
    group: str,
    index: int,
    seed: int,
    patient_id: str,
    identity: _Identity,
    code: str,
    display: str,
) -> dict:
    onset_days = _ONSET_MIN_DAYS + _pick(seed, spec.nct_id, group, index, "onset", _ONSET_SPAN_DAYS)
    coding: dict[str, str] = {"system": vocabulary.ICD10CM_SYSTEM, "code": code}
    if display:
        coding["display"] = display
    fact = " ".join(part for part in (f"Diagnosis: ICD-10-CM {code}", display) if part)
    return {
        "resourceType": "Condition",
        "id": revision.resource_id(spec.nct_id, group, index, "Condition"),
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": {"coding": [coding]},
        "onsetDateTime": _date_before_anchor(onset_days),
        "text": _narrative(fact, identity),
    }


def _observation(
    spec: SeedSpec,
    group: str,
    index: int,
    seed: int,
    patient_id: str,
    identity: _Identity,
    loinc: str,
    display: str,
    value: float,
) -> dict:
    unit, _ = units.unit_for(loinc)
    observed_days = _pick(seed, spec.nct_id, group, index, "observed", _OBSERVATION_SPAN_DAYS)
    coding: dict[str, str] = {"system": vocabulary.LOINC_SYSTEM, "code": loinc}
    if display:
        coding["display"] = display
    fact = " ".join(
        part for part in (f"Measurement: LOINC {loinc}", display, f"{value} {unit}") if part
    )
    return {
        "resourceType": "Observation",
        "id": revision.resource_id(spec.nct_id, group, index, "Observation"),
        "status": "final",
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": {"coding": [coding]},
        "valueQuantity": {
            "value": value,
            "unit": unit,
            "system": units.UCUM_SYSTEM,
            "code": unit,
        },
        "effectiveDateTime": _date_before_anchor(observed_days),
        "text": _narrative(fact, identity),
    }


def _eligible_value(spec: SeedSpec, index: int, seed: int) -> float:
    """A value on the satisfied side of the threshold, built from the operator direction."""
    step = 1 + _pick(seed, spec.nct_id, GROUP_ELIGIBLE, index, "value", _VALUE_STEPS)
    return round(spec.threshold + _direction(spec.operator) * _VALUE_STEP * step, 2)


def _near_miss_value(spec: SeedSpec) -> float:
    """The value just outside the threshold: the same for every near-miss patient, so the
    group tests the boundary rather than a spread."""
    return round(spec.threshold + _near_miss_steps(spec.operator) * _VALUE_STEP, 2)


def _noise_value(spec: SeedSpec, index: int, seed: int, low: float, high: float) -> float:
    fraction = _pick(seed, spec.nct_id, GROUP_NOISE, index, "value", _NOISE_VALUE_STEPS)
    return round(low + (high - low) * fraction / (_NOISE_VALUE_STEPS - 1), 2)


def _unit_fallbacks(spec: SeedSpec, sizes: GroupSizes) -> tuple[str, ...]:
    """One entry naming the anchor LOINC code when it has no committed unit mapping and some
    group (eligible or near-miss) carries it."""
    _, is_fallback = units.unit_for(spec.observation_loinc)
    if is_fallback and sizes.retrievable > 0:
        return (
            (
                f"{spec.observation_loinc}: no committed unit mapping; "
                f"used generic unit {units.FALLBACK_UNIT}"
            ),
        )
    return ()


def _entry(resource: dict) -> dict:
    return {
        "resource": resource,
        "request": {"method": "PUT", "url": f"{resource['resourceType']}/{resource['id']}"},
    }


def build_cohort(
    spec: SeedSpec, *, sizes: GroupSizes, seed: int, data_revision: str
) -> GeneratedCohort | CohortTooLarge:
    """The transaction bundle and manifest for ``spec``: eligible patients first, then
    near-miss, then noise.

    Retrievable groups above the cohort limit return ``CohortTooLarge`` before anything is
    generated.
    """
    too_large = check_group_sizes(sizes)
    if too_large is not None:
        return too_large
    noise_conditions = tuple(c for c in _NOISE_CONDITIONS if c[0] != spec.condition_code)
    noise_observations = tuple(o for o in _NOISE_OBSERVATIONS if o[0] != spec.observation_loinc)

    entries: list[dict] = []
    manifest_patients: list[dict] = []
    for group, size in sizes.as_pairs():
        for index in range(size):
            identity = _identity(spec, group, index, seed)
            patient = _patient(spec, group, index, identity, seed, data_revision)
            if group in (GROUP_ELIGIBLE, GROUP_NEAR_MISS):
                condition_code, condition_display = spec.condition_code, ""
                loinc, observation_display = spec.observation_loinc, ""
                value = (
                    _eligible_value(spec, index, seed)
                    if group == GROUP_ELIGIBLE
                    else _near_miss_value(spec)
                )
            else:
                condition_code, condition_display = noise_conditions[
                    _pick(seed, spec.nct_id, group, index, "condition", len(noise_conditions))
                ]
                loinc, observation_display, low, high = noise_observations[
                    _pick(seed, spec.nct_id, group, index, "observation", len(noise_observations))
                ]
                value = _noise_value(spec, index, seed, low, high)
            entries.append(_entry(patient))
            entries.append(
                _entry(
                    _condition(
                        spec,
                        group,
                        index,
                        seed,
                        patient["id"],
                        identity,
                        condition_code,
                        condition_display,
                    )
                )
            )
            entries.append(
                _entry(
                    _observation(
                        spec,
                        group,
                        index,
                        seed,
                        patient["id"],
                        identity,
                        loinc,
                        observation_display,
                        value,
                    )
                )
            )
            manifest_patients.append({"patient_id": patient["id"], "group": group})

    manifest: dict[str, Any] = {
        "protocol_id": spec.nct_id,
        "data_revision": data_revision,
        "counts": {group: size for group, size in sizes.as_pairs()},
        "patients": manifest_patients,
    }
    if spec.unevaluated:
        manifest["partial"] = True
        manifest["unevaluated_exclusions"] = [item.manifest_item for item in spec.unevaluated]

    return GeneratedCohort(
        bundle={"resourceType": "Bundle", "type": "transaction", "entry": entries},
        manifest=manifest,
        unit_fallbacks=_unit_fallbacks(spec, sizes),
    )
