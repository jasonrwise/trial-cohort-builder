"""Append-only JSON-lines audit writer, weekly rotation, 4 retained (D-03, D-04).

`write_entry` never raises past its caller: data-model.md requires the write to
happen synchronously before the response is returned AND requires that the
write itself never be what kills the process (Pitfall 4). Those two
constraints are only satisfiable together with an explicit exception boundary,
so the entire body — construction, serialization, and write — sits inside one
`try`/`except Exception`.

A second, unrelated store lives below (06-03-PLAN.md Task 1, AD-17): the
scorecard store, a plain append-only JSON-Lines file at
`Config.scorecard_store_path`, holding `ScreenedRecord`/`ApprovalRecord`
lines. Unlike the rotating audit log above, it is NEVER rotated — no
`TimedRotatingFileHandler` is involved anywhere in this second half of the
module. `_SCORECARD_STORE_LOCK` is the second named, scoped exception to
this codebase's "no module-level mutable binding" convention (alongside
`fhir.py`'s `_breaker`) — it is held only around the append itself, never
across any awaited external I/O (research.md §9, Pitfall 3): the LLM-
proposal fan-out in `src/web/query_screening.py` always completes, or
aborts, entirely before this lock is ever acquired.

The store's seven public functions (06-05-PLAN.md Task 2, T012/T018;
07-02-PLAN.md Task 1 adds the sixth; 08-01-PLAN.md Task 1 adds the
seventh): `append_screened_record`, `append_approval_record`,
`append_approval_if_absent`, `get_screened_record`, `list_screened_records`,
`is_finalized`, `get_finalization`. The four reads scan the file fresh on
every call and hold no cached state and no
module-level collection (AD-17's derived fact, AD-1) — "finalized" is never
stored, only computed: a `ScreenedRecord` is FINALIZED if and only if an
`ApprovalRecord` with a matching `screened_ref` exists in the store right
now. Reads never take the lock (single-writer, multi-reader is safe for an
append-only file); appends are single write calls in a single process
(AD-21). A line that fails JSON parsing, strict model validation, or names
an unrecognized `record_type` raises `ScorecardStoreCorruptError` naming
its 1-based line number — never silently skipped (T-06-30): every read
function fully consumes the underlying generator before returning, so a
corrupt line anywhere in the file surfaces on every call that scans the
file, not only one that happens to reach it first.

`append_approval_if_absent` is the check-and-append `approve_finalize` uses
(07-02-PLAN.md Task 1, research.md §9): the already-finalized check and the
append share one hold of `_SCORECARD_STORE_LOCK`, closing the concurrent
double-submit race `append_approval_record` alone left open. The unguarded
`append_approval_record` stays exported for seeding and tests only — the
web finalize path never calls it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler

from pydantic import ValidationError

from src.models.access import AuditTrailEntry, EventType, Role
from src.models.scorecard import ApprovalRecord, ScreenedRecord

_LOGGER_NAME = "trialbridge.audit"

_SCORECARD_STORE_LOCK = asyncio.Lock()


class ScorecardStoreCorruptError(Exception):
    """Raised by any scorecard-store read when a line fails JSON parsing,
    strict model validation, or names a `record_type` other than
    `"screened"`/`"approval"`. `line_number` is the 1-based line the failure
    was found on (T-06-30) — a corrupt line is never silently skipped."""

    def __init__(self, line_number: int) -> None:
        super().__init__(f"Scorecard store corrupt at line {line_number}")
        self.line_number = line_number


class _RaisingTimedRotatingFileHandler(TimedRotatingFileHandler):
    """Stdlib logging swallows a handler's write failures by default (Handler
    .handleError prints to stderr and returns, never re-raising) — so a plain
    TimedRotatingFileHandler makes an unwritable path invisible to the caller.
    Overriding handleError to re-raise the in-flight exception surfaces it back
    through `emit`/`handle`/`Logger.info`, where `write_entry`'s own
    try/except catches it, reports it, and returns falsy. The write failure
    still never crashes the process — it is caught one frame up, deliberately,
    rather than silently discarded here."""

    def handleError(self, record: logging.LogRecord) -> None:
        raise  # noqa: PLE0704 - re-raises the exception `emit()` is already handling


def build_audit_logger(log_path: str) -> logging.Logger:
    """Returns the `trialbridge.audit` logger, configured with a single
    rotating file handler pointed at `log_path`. Idempotent: repeated calls
    replace any existing handler on this logger rather than stacking a
    duplicate one, so the logger always writes to the most recently requested
    path."""
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception as exc:  # noqa: BLE001 - discarding a stale handler; a broken
            # underlying stream (e.g. one a prior failure already closed it) must
            # never block installing the new one — the never-crash-the-process
            # guarantee this module's docstring states, applied to
            # re-initialization rather than to a write.
            print(f"AUDIT HANDLER CLEANUP FAILED: {exc}", file=sys.stderr)

    handler = _RaisingTimedRotatingFileHandler(log_path, when="W0", backupCount=4, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))  # message is pre-serialized JSON
    logger.addHandler(handler)

    return logger


def write_entry(
    role: Role | None,
    nct_id: str | None,
    event_type: EventType,
    detail: str,
) -> bool:
    """Builds an `AuditTrailEntry`, serializes it through the model
    (`model_dump_json()` — JSON encoding escapes newlines/control characters,
    so a hostile `detail` cannot forge a second log line), and emits it as one
    line. Never raises past the caller: any failure is reported to stderr and
    this returns a falsy value instead."""
    try:
        entry = AuditTrailEntry(
            timestamp=datetime.now(UTC),
            role=role,
            nct_id=nct_id,
            event_type=event_type,
            detail=detail,
        )
        line = entry.model_dump_json()
        logging.getLogger(_LOGGER_NAME).info(line)
        return True
    except Exception as exc:  # noqa: BLE001 - deliberate: audit failures must never propagate
        print(f"AUDIT WRITE FAILED: {exc}", file=sys.stderr)
        return False


def new_scorecard_ref() -> str:
    """Returns a fresh, unguessable scorecard reference: ``"sc_"`` plus 16
    lowercase hex digits (64 bits of entropy from `secrets.token_hex(8)`) —
    matching `SCORECARD_REF_PATTERN` (`src/models/scorecard.py`)."""
    return "sc_" + secrets.token_hex(8)


def _write_line(line: str, store_path: str) -> None:
    """The synchronous append body: open in append mode, write the line plus
    a newline, flush, then fsync. Holds no lock itself — callers take
    `_SCORECARD_STORE_LOCK` around this (or, for `append_approval_if_absent`,
    around this plus a preceding read, in one hold)."""
    with open(store_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


async def _append_line(line: str, store_path: str) -> None:
    """Serializes before taking the lock; the lock is held only around the
    append itself, never across any awaited external I/O (research.md §9,
    Pitfall 3)."""
    async with _SCORECARD_STORE_LOCK:
        # A blocking open()/fsync() under the lock, deliberately — this repo
        # has no aiofiles dependency (not part of the ratified web stack,
        # 06-RESEARCH.md Standard Stack), and the write is a single short
        # line held only for the append itself, never across awaited
        # external I/O (research.md §9, Pitfall 3).
        _write_line(line, store_path)


async def append_screened_record(record: ScreenedRecord, store_path: str) -> None:
    """Appends one line to the AD-17 scorecard store. Unlike `write_entry`
    above, I/O errors are left to propagate: a lost scorecard must fail
    loudly, never silently."""
    await _append_line(record.model_dump_json(), store_path)


async def append_approval_record(record: ApprovalRecord, store_path: str) -> None:
    """Appends one `ApprovalRecord` line, unconditionally, with no repeat
    check. The unguarded primitive — kept for seeding and tests. The web
    finalize path never calls this; it calls `append_approval_if_absent`,
    whose repeat check and append share one lock hold."""
    await _append_line(record.model_dump_json(), store_path)


async def append_approval_if_absent(
    record: ApprovalRecord, store_path: str
) -> ApprovalRecord | None:
    """The lock-held check-and-append `approve_finalize` uses (07-02-PLAN.md
    Task 1, research.md §9, AD-17): serializes `record` before taking the
    lock, then — inside one hold of `_SCORECARD_STORE_LOCK` — scans the
    store for an existing `ApprovalRecord` whose `screened_ref` matches. If
    one is already there, writes nothing and returns the earliest such
    record (append order — the first successful writer wins). Otherwise
    appends `record` and returns `None`. The scan is bounded local file I/O,
    never awaited external I/O, so it is safe to run under the lock
    (Pitfall 3). `asyncio.Lock` is not re-entrant — this never calls
    `_append_line` (which itself takes the lock) from inside this hold; it
    calls `_write_line` directly. A corrupt line anywhere in the store
    raises `ScorecardStoreCorruptError` from the scan, before any write."""
    line = record.model_dump_json()
    async with _SCORECARD_STORE_LOCK:
        existing = [
            r
            for r in _iter_store_records(store_path)
            if isinstance(r, ApprovalRecord) and r.screened_ref == record.screened_ref
        ]
        if existing:
            return existing[0]
        _write_line(line, store_path)
        return None


def _iter_store_records(store_path: str) -> Iterator[ScreenedRecord | ApprovalRecord]:
    """Yields nothing when the file does not exist. For every other
    non-blank line, dispatches on the `record_type` discriminator to
    `ScreenedRecord.model_validate_json`/`ApprovalRecord.model_validate_json`
    (strict JSON mode — accepts the ISO datetimes and enum values these
    models were serialized with). Any JSON, validation, or unknown-
    record_type failure raises `ScorecardStoreCorruptError(line_number)`."""
    if not os.path.exists(store_path):
        return

    with open(store_path, encoding="utf-8") as f:
        for line_number, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ScorecardStoreCorruptError(line_number) from exc

            record_type = parsed.get("record_type") if isinstance(parsed, dict) else None
            try:
                if record_type == "screened":
                    yield ScreenedRecord.model_validate_json(line)
                elif record_type == "approval":
                    yield ApprovalRecord.model_validate_json(line)
                else:
                    raise ScorecardStoreCorruptError(line_number)
            except ValidationError as exc:
                raise ScorecardStoreCorruptError(line_number) from exc


def get_screened_record(ref: str, store_path: str) -> ScreenedRecord | None:
    """Returns the stored `ScreenedRecord` for `ref`, or `None` for an
    unknown ref or a missing store file. Fully consumes the underlying
    generator (never short-circuits on an early match) so a corrupt line
    anywhere in the file is never silently skipped, regardless of where the
    matching ref happens to sit (T-06-30)."""
    matches = [
        record
        for record in _iter_store_records(store_path)
        if isinstance(record, ScreenedRecord) and record.ref == ref
    ]
    return matches[-1] if matches else None


def list_screened_records(store_path: str) -> list[ScreenedRecord]:
    """Returns every `ScreenedRecord` in the store, in append order. `[]` for
    a missing store file."""
    return [
        record for record in _iter_store_records(store_path) if isinstance(record, ScreenedRecord)
    ]


def is_finalized(ref: str, store_path: str) -> bool:
    """AD-17 derived fact: `True` if and only if an `ApprovalRecord` whose
    `screened_ref` equals `ref` exists in the store, re-read fresh on every
    call — never a stored boolean/enum."""
    approvals = [
        record for record in _iter_store_records(store_path) if isinstance(record, ApprovalRecord)
    ]
    return any(approval.screened_ref == ref for approval in approvals)


def get_finalization(ref: str, store_path: str) -> ApprovalRecord | None:
    """The single AD-17 derivation source `list_scorecards`, `get_scorecard`
    and `rerun_scorecard` all use for `state`/`finalized_by_role`/
    `finalized_at` (`can_rerun` is its negation, 08-01-PLAN.md Task 1) —
    returns the earliest `ApprovalRecord` whose `screened_ref` equals `ref`,
    or `None` for no match, an unknown ref, a malformed ref, or a missing
    store file. Lock-free read like every other read above — re-scans the
    store fresh on every call, never cached. The earliest match mirrors
    `append_approval_if_absent`'s "first successful writer wins". Fully
    consumes the underlying generator via a list comprehension before
    indexing, so a corrupt line anywhere in the store raises
    `ScorecardStoreCorruptError` on every call, not only one that happens to
    reach it first (T-06-30). Does not change `is_finalized`, which
    `approve_finalize.py` still calls unchanged."""
    matches = [
        record
        for record in _iter_store_records(store_path)
        if isinstance(record, ApprovalRecord) and record.screened_ref == ref
    ]
    return matches[0] if matches else None
