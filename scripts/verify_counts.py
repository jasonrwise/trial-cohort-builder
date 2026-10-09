"""Check the newest screened scorecard against the seeded store counts (SM-6).

This is the scorecard-side half of `make verify`. The seeder's `verify` command
reports how many patients of each group the FHIR store holds. This script finds
the newest screened scorecard for the protocol and checks two numbers:

- the candidates not scored INELIGIBLE (ELIGIBLE plus BORDERLINE) equal the
  seeded eligible count;
- the INELIGIBLE candidates equal the seeded near-miss count (zero when the
  store was seeded without a near-miss group);
- the scorecard's data revision equals the seeder's `data_revision`. A scorecard
  with no recorded revision (null, AD-30 "unknown") skips this check.

It runs in the web container through `make verify`. It uses the standard library
only, because `src` is not importable there (AD-31). It never writes to the store.

Arguments:
  --protocol NCT   the protocol to check (required)
  --counts PATH    the seeder's JSON object; "-" reads standard input (required)
  --store PATH     the scorecard store; defaults to TRIALBRIDGE_SCORECARD_STORE_PATH,
                   then /data/scorecards.jsonl

Exit codes: 0 pass; 1 count mismatch or no screened scorecard; 2 unreadable input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

DEFAULT_STORE = "/data/scorecards.jsonl"
STORE_ENV = "TRIALBRIDGE_SCORECARD_STORE_PATH"
STATUSES = {"ELIGIBLE", "INELIGIBLE", "BORDERLINE"}


class _Input(Exception):
    """Input that cannot be read or parsed. The message is the full failure text."""


def _fail(message: str, code: int = 1) -> int:
    print(f"verify failed: {message}", file=sys.stderr)
    return code


def _read_counts(source: str, protocol: str) -> tuple[dict[str, int], str]:
    """The seeder's store_counts and data_revision, checked strictly (T-15-04)."""
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            with open(source, encoding="utf-8") as handle:
                raw = handle.read()
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise _Input(f"unreadable counts input: {type(error).__name__}") from error
    if not isinstance(data, dict):
        raise _Input("unreadable counts input: expected the seeder JSON object")
    if data.get("protocol_id") != protocol:
        raise _Input(
            f"unreadable counts input: protocol_id {data.get('protocol_id')!r} "
            f"is not {protocol}"
        )
    counts = data.get("store_counts")
    if not isinstance(counts, dict) or "eligible" not in counts:
        raise _Input("unreadable counts input: store_counts with an eligible count is missing")
    for group, value in counts.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _Input(
                f"unreadable counts input: store_counts {group!r} is not a non-negative integer"
            )
    revision = data.get("data_revision")
    if not isinstance(revision, str) or not revision:
        raise _Input("unreadable counts input: data_revision is not a non-empty string")
    return counts, revision


def _newest_screened(path: str, protocol: str) -> tuple[dict, int] | None:
    """The screened record with the greatest screened_at and its line number.

    Ties go to the later line. Approval lines and other protocols are skipped.
    Returns None when the store file does not exist.
    """
    newest: tuple[dict, int] | None = None
    newest_at: datetime | None = None
    try:
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                invalid = _Input(f"scorecard store {path} line {number} is not a valid record")
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise invalid
                    if record.get("record_type") != "screened":
                        continue
                    screened_at = datetime.fromisoformat(record["screened_at"])
                    if screened_at.tzinfo is None or not isinstance(record["candidates"], list):
                        raise invalid
                    if not isinstance(record["ref"], str):
                        raise invalid
                    if not isinstance(record.get("data_revision"), (str, type(None))):
                        raise invalid
                except (ValueError, KeyError, TypeError) as error:
                    raise invalid from error
                if record.get("nct_id") != protocol:
                    continue
                if newest_at is None or screened_at >= newest_at:
                    newest, newest_at = (record, number), screened_at
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise _Input(f"cannot read the scorecard store {path}: {type(error).__name__}") from error
    return newest


def _count_statuses(record: dict, path: str, number: int) -> tuple[int, int]:
    """(non-INELIGIBLE, INELIGIBLE) candidate counts; BORDERLINE is non-INELIGIBLE (SM-6)."""
    statuses = []
    for candidate in record["candidates"]:
        status = candidate.get("eligibility_status") if isinstance(candidate, dict) else None
        if status not in STATUSES:
            raise _Input(f"scorecard store {path} line {number} is not a valid record")
        statuses.append(status)
    ineligible = statuses.count("INELIGIBLE")
    return len(statuses) - ineligible, ineligible


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the newest screened scorecard against the seeded store counts."
    )
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--counts", required=True)
    parser.add_argument("--store", default=os.environ.get(STORE_ENV, DEFAULT_STORE))
    args = parser.parse_args(argv)

    try:
        store_counts, seeded_revision = _read_counts(args.counts, args.protocol)
        found = _newest_screened(args.store, args.protocol)
        counts = _count_statuses(found[0], args.store, found[1]) if found else None
    except _Input as error:
        return _fail(str(error), 2)

    seeded_eligible = store_counts["eligible"]
    seeded_near_miss = store_counts.get("near-miss", 0)
    if found is None or counts is None:
        return _fail(
            f"expected {seeded_eligible} non-INELIGIBLE and {seeded_near_miss} INELIGIBLE "
            f"candidates, but found no screened scorecard for {args.protocol} in {args.store}. "
            f"Screen {args.protocol} in the web app, then run make verify again."
        )

    record = found[0]
    non_ineligible, ineligible = counts
    differences = []
    if non_ineligible != seeded_eligible:
        differences.append(
            f"  - non-INELIGIBLE: scorecard {non_ineligible}, seeded eligible {seeded_eligible}"
        )
    if ineligible != seeded_near_miss:
        differences.append(
            f"  - INELIGIBLE: scorecard {ineligible}, seeded near-miss {seeded_near_miss}"
        )
    scorecard_revision = record.get("data_revision")
    if scorecard_revision and scorecard_revision != seeded_revision:
        differences.append(
            f"  - data revision: scorecard {scorecard_revision}, seeded {seeded_revision}"
        )
    if differences:
        return _fail(
            f"scorecard {record['ref']} for {args.protocol} does not match the seeded store:\n"
            + "\n".join(differences)
        )

    print(
        f"verify passed: {args.protocol} scorecard {record['ref']} "
        f"(screened {record['screened_at']}, data revision "
        f"{record.get('data_revision') or 'unknown'}): "
        f"non-INELIGIBLE {non_ineligible} = seeded eligible {seeded_eligible}, "
        f"INELIGIBLE {ineligible} = seeded near-miss {seeded_near_miss}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
