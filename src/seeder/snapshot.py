"""The committed criteria snapshot (Spec Kit T032/T033, SEED-02, SEED-03, AD-32, D-02).

A snapshot is `<criteria_dir>/<NCT_ID>.json`: four keys (`snapshot_schema_version`,
`captured_at`, `content_sha256`, `trial`) holding the strict `Trial` the gateway's
own criteria pipeline produced. It is a seeder-only reproducibility artifact the
operator commits (AD-32), not a cache: it is read at run time on every call, never at
import, and the directory is always the caller's `criteria_dir` argument.

Semantics (FR-015, FR-016):

* an existing valid snapshot is used with no network call at all;
* a missing snapshot is resolved once through `screening.resolve_trial` (the same
  pipeline the gateway runs, called as a module attribute so tests can replace it),
  checked with the injected `check_supported`, then written;
* a terminology-service outage (AD-34) aborts with `TERMINOLOGY_UNAVAILABLE` and writes
  nothing, on the create path and on `--refresh`, so an existing snapshot is left untouched;
* `content_sha256` is the SHA-256 of the canonical (sorted-key, compact) JSON of the
  trial body only, so `captured_at` never changes it.

Working decisions: WD-1 a trial the support check rejects leaves no snapshot, enforced
here behind the injected `check_supported`; WD-2 failures are returned as values
(`SnapshotFailure`, `SnapshotReadError`), and only `snapshot_path` raises, as defence in
depth against a path-traversal ID; WD-3 the file is written through a temporary sibling
and `os.replace`, so an interrupted write never leaves a truncated snapshot. The
fixture-coverage notice is skipped for reserved IDs and for `DRIFT_GUARDED_IDS` (ACC-03).

`captured_at` is passed in by the caller (D-02): this module never reads the clock. Every
JSON serialization uses `sort_keys=True` and no dict or set is iterated unsorted (AD-33).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from src.models import vocabulary
from src.models.errors import ClientError
from src.models.trial import EligibilityCriterion, Trial
from src.services import anchor, screening, terminology, trials

SNAPSHOT_SCHEMA_VERSION: int = 1

_SNAPSHOT_KEYS: tuple[str, ...] = (
    "captured_at",
    "content_sha256",
    "snapshot_schema_version",
    "trial",
)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")

# Real-trial NCT IDs whose committed snapshot the offline drift guard checks against
# recorded fixtures (FR-021, ACC-03). Add an ID only in the commit that adds its snapshot
# and its fixtures. tests/unit/test_snapshot.py ties this set to demo-data/criteria in
# both directions.
DRIFT_GUARDED_IDS: frozenset[str] = frozenset({"NCT00996294", "NCT04739241", "NCT05591391"})


class SnapshotSource(str, Enum):
    EXISTING = "existing"
    CREATED = "created"
    REFRESHED = "refreshed"


class SnapshotReadError(str, Enum):
    MISSING = "MISSING"
    INVALID = "INVALID"


class SnapshotFailureReason(str, Enum):
    INVALID = "INVALID"
    OFFLINE_MISSING = "OFFLINE_MISSING"
    OFFLINE_REFRESH = "OFFLINE_REFRESH"
    RESOLVE_FAILED = "RESOLVE_FAILED"
    TERMINOLOGY_UNAVAILABLE = "TERMINOLOGY_UNAVAILABLE"
    UNSUPPORTED = "UNSUPPORTED"
    WRITE_FAILED = "WRITE_FAILED"


@dataclass(frozen=True)
class SnapshotResolved:
    trial: Trial
    content_sha256: str
    source: SnapshotSource
    notices: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotFailure:
    reason: SnapshotFailureReason
    message: str
    details: tuple[str, ...] = ()


def canonical_json(value: object) -> str:
    """Sorted-key, compact, non-ASCII-preserving JSON: the one text the hash is over."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def trial_content_sha256(trial: Trial) -> str:
    """SHA-256 hex of the canonical JSON of the trial body (never `captured_at`)."""
    return hashlib.sha256(canonical_json(trial.model_dump(mode="json")).encode("utf-8")).hexdigest()


def snapshot_path(criteria_dir: Path, nct_id: str) -> Path:
    """`criteria_dir/<nct_id>.json`; raises ValueError unless `nct_id` is a well-formed
    NCT ID (the pattern is `\\Z`-anchored, so a trailing newline is refused too)."""
    if not trials.NCT_ID_PATTERN.match(nct_id):
        raise ValueError(f"not a well-formed NCT ID: {nct_id!r}")
    return criteria_dir / f"{nct_id}.json"


def write_snapshot(path: Path, trial: Trial, captured_at: str) -> str:
    """Write the four-key snapshot atomically and return its `content_sha256`.

    Raises OSError after removing its temporary file."""
    content_sha256 = trial_content_sha256(trial)
    body = {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "captured_at": captured_at,
        "content_sha256": content_sha256,
        "trial": trial.model_dump(mode="json"),
    }
    text = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return content_sha256


def read_snapshot(path: Path, nct_id: str) -> tuple[Trial, str] | SnapshotReadError:
    """Read `path` fresh and return `(trial, content_sha256)`, `MISSING` when there is no
    file, or `INVALID` for anything else: unparseable JSON, a key set other than the exact
    four, a schema version other than `SNAPSHOT_SCHEMA_VERSION`, a malformed hash, a trial
    the strict model rejects, a hash that does not match the trial body, or a trial for a
    different NCT ID."""
    if not path.is_file():
        return SnapshotReadError.MISSING
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or sorted(document) != list(_SNAPSHOT_KEYS):
            return SnapshotReadError.INVALID
        version = document["snapshot_schema_version"]
        if isinstance(version, bool) or version != SNAPSHOT_SCHEMA_VERSION:
            return SnapshotReadError.INVALID
        recorded_sha256 = document["content_sha256"]
        if not isinstance(document["captured_at"], str) or not isinstance(recorded_sha256, str):
            return SnapshotReadError.INVALID
        if not _SHA256_HEX.match(recorded_sha256) or not isinstance(document["trial"], dict):
            return SnapshotReadError.INVALID
        trial_text = canonical_json(document["trial"])
        content_sha256 = hashlib.sha256(trial_text.encode("utf-8")).hexdigest()
        if content_sha256 != recorded_sha256:
            return SnapshotReadError.INVALID
        trial = Trial.model_validate_json(trial_text)
    except (OSError, ValueError, KeyError, TypeError):
        return SnapshotReadError.INVALID
    if trial.nct_id != nct_id:
        return SnapshotReadError.INVALID
    return trial, content_sha256


def _mapping_fields(criterion: EligibilityCriterion) -> tuple[str, str, str, str]:
    """The code system, code, operator and threshold of one criterion, as report text."""
    system = criterion.code_system.value if criterion.code_system else "none"
    operator = criterion.operator.value if criterion.operator else "none"
    return system, str(criterion.code), operator, str(criterion.threshold)


def _describe(criterion: EligibilityCriterion) -> str:
    """The parenthesised mapping summary of one criterion in a diff line."""
    return f"({criterion.mapping_status.value}, {', '.join(_mapping_fields(criterion))})"


def _criteria_by_key(trial: Trial) -> dict[tuple[str, str], EligibilityCriterion]:
    return {
        (criterion.kind.value, criterion.raw_text): criterion
        for criterion in [*trial.inclusion_criteria, *trial.exclusion_criteria]
    }


def _changed_criteria(
    before_trial: Trial, after_trial: Trial
) -> Iterator[tuple[str, str, EligibilityCriterion | None, EligibilityCriterion | None]]:
    """Yield (kind, raw text, before, after) for each criterion that differs, in sorted key order.

    A criterion that one trial lacks is None on that side."""
    before = _criteria_by_key(before_trial)
    after = _criteria_by_key(after_trial)
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            yield key[0], key[1], before.get(key), after.get(key)


def diff_criteria(previous: Trial, current: Trial) -> tuple[str, ...]:
    """One line per criterion added, removed or changed, keyed by (kind, raw text) and
    emitted in sorted key order."""
    lines: list[str] = []
    for kind, text, before, after in _changed_criteria(previous, current):
        if before is None and after is not None:
            lines.append(f"added: {kind}: {text} {_describe(after)}")
        elif after is None and before is not None:
            lines.append(f"removed: {kind}: {text} {_describe(before)}")
        elif before is not None and after is not None:
            lines.append(f"changed: {kind}: {text} {_describe(before)} -> {_describe(after)}")
    return tuple(lines)


def _fields(criterion: EligibilityCriterion | None) -> str:
    """The mapping fields of one criterion in a drift line, or `missing` for no criterion.

    A drift line leaves out the mapping status. That is safe: the `EligibilityCriterion`
    validator requires a VERIFIED criterion to carry a code system and a code, and an UNMAPPED
    one to carry neither, so a status change always changes these fields."""
    if criterion is None:
        return "missing"
    return " ".join(_mapping_fields(criterion))


def drift_lines(snapshot_trial: Trial, live_trial: Trial) -> tuple[str, ...]:
    """The drift report between the committed snapshot and the live pipeline result.

    This is the production report `verify` prints. `tests/integration/test_snapshot_drift_guard.py`
    stays an independent offline guard. Three parts, in order: one line per differing criterion,
    keyed by (kind, raw text) in sorted order; one line per differing cohort-anchor key; and,
    only when neither produced a line, one line for a trial-content hash difference outside the
    criteria (WD-8). An empty tuple means no drift."""
    lines: list[str] = [
        f"{text}: snapshot {_fields(stored)} vs live {_fields(live)}"
        for _kind, text, stored, live in _changed_criteria(snapshot_trial, live_trial)
    ]

    stored_anchor = anchor.derive_cohort_anchor(snapshot_trial.inclusion_criteria)
    live_anchor = anchor.derive_cohort_anchor(live_trial.inclusion_criteria)
    anchor_keys = (
        (
            "condition code",
            stored_anchor and stored_anchor.condition_code,
            live_anchor and live_anchor.condition_code,
        ),
        (
            "observation LOINC",
            stored_anchor and stored_anchor.observation_loinc,
            live_anchor and live_anchor.observation_loinc,
        ),
    )
    for label, before, after in anchor_keys:
        if before != after:
            lines.append(
                f"cohort anchor {label}: snapshot {before or 'none'} vs live {after or 'none'}"
            )

    if not lines:
        stored_sha256 = trial_content_sha256(snapshot_trial)
        live_sha256 = trial_content_sha256(live_trial)
        if stored_sha256 != live_sha256:
            lines.append(
                "trial content outside the criteria differs: "
                f"snapshot sha256 {stored_sha256[:12]} vs live sha256 {live_sha256[:12]}"
            )
    return tuple(lines)


def _failure(
    reason: SnapshotFailureReason, message: str, details: Sequence[str] = ()
) -> SnapshotFailure:
    return SnapshotFailure(reason=reason, message=message, details=tuple(details))


def _fixture_coverage_notices(nct_id: str) -> tuple[str, ...]:
    if nct_id in vocabulary.RESERVED_DEMO_IDS or nct_id in DRIFT_GUARDED_IDS:
        return ()
    notice = (
        "No recorded terminology fixtures cover this snapshot, so the offline drift guard "
        "does not check it."
    )
    return (notice,)


async def resolve_criteria(
    nct_id: str,
    *,
    criteria_dir: Path,
    refresh: bool,
    offline: bool,
    captured_at: str,
    check_supported: Callable[[Trial], Sequence[str]],
) -> SnapshotResolved | SnapshotFailure:
    """Resolve a trial's criteria from its snapshot, or live when there is none.

    Order: `--offline --refresh` is refused first; an existing valid snapshot is used
    with no network (after `check_supported`, WD-4); an invalid one aborts with the
    refresh instruction unless refreshing; a missing one is a hard failure offline, else
    resolved live once. `check_supported` always runs before any write, so a rejected
    trial leaves no snapshot and a refresh of one leaves the existing file untouched.
    A write error is returned as `WRITE_FAILED`, never raised."""
    if offline and refresh:
        return _failure(
            SnapshotFailureReason.OFFLINE_REFRESH,
            "--refresh re-runs the criteria pipeline and needs network access; drop --offline.",
        )
    path = snapshot_path(criteria_dir, nct_id)
    existing = read_snapshot(path, nct_id)

    if not refresh:
        if existing is SnapshotReadError.INVALID:
            return _failure(
                SnapshotFailureReason.INVALID,
                f"Criteria snapshot {path} failed validation or its content hash does not "
                "match; re-run with --refresh.",
            )
        if not isinstance(existing, SnapshotReadError):
            trial, content_sha256 = existing
            reasons = tuple(check_supported(trial))
            if reasons:
                return _failure(
                    SnapshotFailureReason.UNSUPPORTED,
                    f"Criteria snapshot {path} holds a trial the seeder cannot use.",
                    reasons,
                )
            return SnapshotResolved(
                trial=trial,
                content_sha256=content_sha256,
                source=SnapshotSource.EXISTING,
                notices=_fixture_coverage_notices(nct_id),
            )
        if offline:
            return _failure(
                SnapshotFailureReason.OFFLINE_MISSING,
                f"No criteria snapshot at {path}; --offline forbids fetching criteria. "
                "Re-run without --offline (needs internet access) to create it.",
            )

    resolved = await screening.resolve_trial(nct_id)
    if resolved is terminology.TerminologyFetchError.UNAVAILABLE:
        return _failure(
            SnapshotFailureReason.TERMINOLOGY_UNAVAILABLE,
            f"Could not resolve criteria for {nct_id}: "
            f"{terminology.TERMINOLOGY_UNAVAILABLE_REASON} No criteria snapshot was written; "
            "re-run once the terminology service is reachable.",
        )
    if not isinstance(resolved, Trial):
        detail = resolved.reason if isinstance(resolved, ClientError) else resolved.value
        return _failure(
            SnapshotFailureReason.RESOLVE_FAILED,
            f"Could not resolve criteria for {nct_id}: {detail}.",
        )
    reasons = tuple(check_supported(resolved))
    if reasons:
        return _failure(
            SnapshotFailureReason.UNSUPPORTED,
            f"Trial {nct_id} cannot be seeded; no criteria snapshot was written.",
            reasons,
        )
    try:
        content_sha256 = write_snapshot(path, resolved, captured_at)
    except OSError as error:
        return _failure(
            SnapshotFailureReason.WRITE_FAILED,
            f"Could not write criteria snapshot {path}: {error.strerror or error}. "
            "To run the seeder from the host instead, see the README section "
            "'Seed the demo store'.",
        )

    if not refresh:
        source = SnapshotSource.CREATED
        notices: tuple[str, ...] = (
            f"Criteria snapshot created at {path}; commit it so later runs make no network call.",
        )
    else:
        source = SnapshotSource.REFRESHED
        if isinstance(existing, SnapshotReadError):
            changes: tuple[str, ...] = ("Previous snapshot missing or invalid; no criteria diff.",)
        else:
            changes = diff_criteria(existing[0], resolved) or (
                "No criteria added, removed or changed.",
            )
        notices = (f"Criteria snapshot refreshed at {path}.", *changes)
    return SnapshotResolved(
        trial=resolved,
        content_sha256=content_sha256,
        source=source,
        notices=(*notices, *_fixture_coverage_notices(nct_id)),
    )
