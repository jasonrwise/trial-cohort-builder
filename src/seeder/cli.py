"""The seeder command line (Spec Kit T045, T063 and T083; SEED-01, SEED-02, SEED-06, SEED-07,
SEED-08, SEED-09; AD-27, AD-31; contracts/seeder-cli.md).

The third inbound adapter: validate, resolve, delegate, report. It imports nothing from the
web or MCP adapters, never calls ``load_config()``, makes no LLM call, and reads the
``FHIR_*`` variables only through ``fhir.read_fhir_env`` and ``fhir.get_fhir_access``. It
knows nothing about how the stack is packaged (AD-31): the only environment value it ever
prints is the FHIR base URL.

Run it as ``python -m src.seeder.cli seed <NCT_ID> [--refresh] [--offline] [--seed N]
[--eligible N] [--near-miss N] [--noise N]`` or as ``python -m src.seeder.cli verify <NCT_ID>
[--seed N] [--eligible N] [--near-miss N] [--noise N]``. Exit codes: 0 on success, 1 on any
failure, 2 for an argparse usage error. Every failure message goes to stderr and starts with
``seed failed: `` or ``verify failed: ``, after the command that ran. Each failure line and
each notice prints as one line without control characters.

``verify`` writes nothing. On a pass it prints exactly one line on stdout: a JSON object with
sorted keys ``data_revision``, ``manifest_counts``, ``protocol_id``, ``protocol_patients``,
``revision_patients`` and ``store_counts`` (WD-7). The report of a partial seed adds
``partial`` and ``unevaluated_exclusions`` (spec 008 ``contracts/seeder-cli.md``). All progress
text and every failure go to stderr.

A partial seed prints the partial block after the not-represented block (spec 008
``contracts/seeder-cli.md``, AD-37).

Decisions this module implements:

* D-02: the capture timestamp of a new snapshot comes from the clock service and is passed
  down; this module never reads the wall clock itself.
* D-05: ``--offline`` means no ClinicalTrials.gov and no terminology request. FHIR access
  and writes still happen.
* WD-7: the ``verify`` output contract above. WD-2: ``verify`` takes the same seed and group-size
  flags as ``seed``, so an operator can verify a custom seed.
* WD-1: step order. Group sizes, then the FHIR environment and the printed target URL, then
  the criteria snapshot (the only place criteria network calls can happen), then the
  specification, revision and cohort, then FHIR access and readiness, then the write, then
  the store's per-group counts, then the report. The target URL is therefore printed before
  any snapshot or FHIR write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable

from src.models import vocabulary
from src.models.errors import ClientError
from src.models.trial import Trial
from src.seeder import generate, revision, snapshot
from src.services import anchor, clock, fhir, fhir_write, screening, terminology, trials


def _group_size(text: str) -> int:
    """An argparse type: a whole number of patients that is not negative."""
    try:
        size = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if size < 0:
        raise argparse.ArgumentTypeError(f"{size} is negative; a group size cannot be below 0")
    return size


def _sizes(args: argparse.Namespace) -> generate.GroupSizes:
    return generate.GroupSizes(eligible=args.eligible, near_miss=args.near_miss, noise=args.noise)


def build_parser() -> argparse.ArgumentParser:
    """The command parser: ``seed`` loads a cohort, ``verify`` checks the store and writes nothing."""
    defaults = generate.GroupSizes()
    parser = argparse.ArgumentParser(
        prog="python -m src.seeder.cli",
        description="Seed a FHIR store with a synthetic, labelled cohort for a trial.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser(
        "seed", help="Generate and load the labelled cohort for one trial."
    )
    seed.add_argument("nct_id", metavar="NCT_ID", help="ClinicalTrials.gov ID, NCT plus 8 digits.")
    seed.add_argument(
        "--refresh",
        action="store_true",
        help="Re-run the criteria pipeline and overwrite the committed snapshot.",
    )
    seed.add_argument(
        "--offline",
        action="store_true",
        help="Make no ClinicalTrials.gov or terminology request; FHIR is still used.",
    )
    seed.add_argument(
        "--seed",
        type=int,
        default=revision.DEFAULT_SEED,
        help="Seed for the deterministic generator.",
    )
    seed.add_argument(
        "--eligible", type=_group_size, default=defaults.eligible, help="Eligible patients to load."
    )
    seed.add_argument(
        "--near-miss",
        type=_group_size,
        default=defaults.near_miss,
        help="Near-miss patients to load: retrieved, but just outside the threshold.",
    )
    seed.add_argument(
        "--noise", type=_group_size, default=defaults.noise, help="Noise patients to load."
    )

    verify = subparsers.add_parser(
        "verify",
        help="Re-run the live criteria pipeline and check the store against the manifest; "
        "writes nothing.",
    )
    verify.add_argument(
        "nct_id", metavar="NCT_ID", help="ClinicalTrials.gov ID, NCT plus 8 digits."
    )
    verify.add_argument(
        "--seed",
        type=int,
        default=revision.DEFAULT_SEED,
        help="Seed the store was loaded with.",
    )
    verify.add_argument(
        "--eligible", type=_group_size, default=defaults.eligible, help="Eligible patients loaded."
    )
    verify.add_argument(
        "--near-miss",
        type=_group_size,
        default=defaults.near_miss,
        help="Near-miss patients loaded.",
    )
    verify.add_argument(
        "--noise", type=_group_size, default=defaults.noise, help="Noise patients loaded."
    )
    return parser


def _fail(message: str, *details: str, command: str = "seed") -> int:
    """Print a failure on stderr and return exit code 1.

    The message and each detail print as one line. A refusal reason or a drift line can quote
    protocol text. ``single_line`` removes control characters and joins whitespace.
    """
    print(f"{command} failed: {generate.single_line(message)}", file=sys.stderr)
    for detail in details:
        print(f"  - {generate.single_line(detail)}", file=sys.stderr)
    return 1


def _not_represented_lines(entries: tuple[str, ...]) -> list[str]:
    header = "Criteria not represented (never invented):"
    if not entries:
        return [f"{header} none"]
    return [header, *[f"  - {generate.single_line(entry)}" for entry in entries]]


def _partial_lines(entries: tuple[str, ...]) -> list[str]:
    if not entries:
        return []
    if len(entries) == 1:
        header = "Partial seed: 1 exclusion not evaluated (the scorer ignores it):"
    else:
        header = f"Partial seed: {len(entries)} exclusions not evaluated (the scorer ignores them):"
    return [header, *[f"  - {entry}" for entry in entries]]


def _partial_summary(partial: bool, items: list[dict[str, str]]) -> str:
    flag = "true" if partial else "false"
    return f"partial={flag}, unevaluated_exclusions={json.dumps(items, sort_keys=True)}"


def _too_large_message(too_large: generate.CohortTooLarge) -> str:
    return (
        f"{too_large.retrievable} retrievable patients exceed the cohort limit of "
        f"{too_large.limit}; lower --eligible or --near-miss."
    )


def _access_message(error: fhir.FhirAccessError) -> str:
    if error is fhir.FhirAccessError.NOT_CONFIGURED:
        return "FHIR access is not configured: set FHIR_BASE_URL and FHIR_TOKEN_URL."
    return "Could not get an access token from the FHIR token endpoint; check the FHIR credentials."


def _write_failure_message(failure: fhir_write.FhirWriteFailure) -> str:
    if failure.error is fhir_write.FhirWriteError.REJECTED:
        return f"The FHIR server rejected the write: {failure.detail}"
    return failure.detail


def _supported_probe(seen: list[Trial]) -> Callable[[Trial], tuple[str, ...]]:
    """The support check handed to the snapshot layer; it also keeps the trial it judged so a
    rejection can list the criteria that were not represented."""

    def check(trial: Trial) -> tuple[str, ...]:
        seen.append(trial)
        return generate.unsupported_reasons(trial)

    return check


def _reject_unsupported(
    nct_id: str,
    reasons: tuple[str, ...],
    not_represented: tuple[str, ...],
    *,
    command: str = "seed",
) -> int:
    code = _fail(
        f"Trial {nct_id} is outside the demo limit; nothing was written:",
        *reasons,
        command=command,
    )
    if not_represented:
        for line in _not_represented_lines(not_represented):
            print(line, file=sys.stderr)
    return code


def _protocol_tags(nct_id: str) -> tuple[tuple[str, str], ...]:
    return ((vocabulary.PROTOCOL_TAG_SYSTEM, nct_id),)


def _revision_tags(nct_id: str, data_revision: str) -> tuple[tuple[str, str], ...]:
    return (*_protocol_tags(nct_id), (vocabulary.DATA_REVISION_TAG_SYSTEM, data_revision))


def _group_tags(nct_id: str, group: str, data_revision: str) -> tuple[tuple[str, str], ...]:
    return (*_revision_tags(nct_id, data_revision), (vocabulary.GROUP_TAG_SYSTEM, group))


async def _seed(args: argparse.Namespace) -> int:
    sizes = _sizes(args)
    env = fhir.read_fhir_env()
    if isinstance(env, fhir.FhirAccessError):
        return _fail(_access_message(env))
    print(f"Target FHIR server: {env.base_url}")

    seen: list[Trial] = []
    resolved = await snapshot.resolve_criteria(
        args.nct_id,
        criteria_dir=vocabulary.DEMO_DATA_DIR / "criteria",
        refresh=args.refresh,
        offline=args.offline,
        captured_at=clock.utc_now_iso(),
        check_supported=_supported_probe(seen),
    )
    if isinstance(resolved, snapshot.SnapshotFailure):
        if resolved.reason is snapshot.SnapshotFailureReason.UNSUPPORTED:
            judged = generate.derive_seed_spec(seen[0]).not_represented if seen else ()
            return _reject_unsupported(args.nct_id, resolved.details, judged)
        return _fail(resolved.message, *resolved.details)
    print(f"Criteria snapshot: {resolved.source.value}")
    for notice in resolved.notices:
        # A refresh notice can quote criterion text from the criteria diff.
        print(f"Notice: {generate.single_line(notice)}")

    spec = generate.derive_seed_spec(resolved.trial)
    if isinstance(spec, generate.UnsupportedTrial):
        return _reject_unsupported(args.nct_id, spec.reasons, spec.not_represented)
    for line in _not_represented_lines(spec.not_represented):
        print(line)
    for line in _partial_lines(spec.unevaluated_exclusions):
        print(line)

    data_revision = revision.data_revision(resolved.content_sha256, args.seed, sizes.as_pairs())
    cohort = generate.build_cohort(spec, sizes=sizes, seed=args.seed, data_revision=data_revision)
    if isinstance(cohort, generate.CohortTooLarge):
        return _fail(_too_large_message(cohort))
    for fallback in cohort.unit_fallbacks:
        print(f"Unit fallback: {fallback}")
    print("Age and sex are not modelled; patients get deterministic plausible values.")
    print(f"Data revision: {data_revision}")

    access = await fhir.get_fhir_access()
    if isinstance(access, fhir.FhirAccessError):
        return _fail(_access_message(access))
    not_ready = await fhir_write.wait_until_ready(access)
    if not_ready is not None:
        return _fail(not_ready.detail)
    held = await fhir_write.count_patients(access, _protocol_tags(args.nct_id))
    if isinstance(held, fhir_write.FhirWriteFailure):
        return _fail(held.detail)
    current = await fhir_write.count_patients(access, _revision_tags(args.nct_id, data_revision))
    if isinstance(current, fhir_write.FhirWriteFailure):
        return _fail(current.detail)
    if held > current:
        return _fail(
            f"Store at {env.base_url} already holds {held - current} Patient(s) for "
            f"{args.nct_id} from another data revision (this run is {data_revision}). "
            "Reset the store, then re-seed. Nothing was written."
        )
    write_failure = await fhir_write.put_transaction(access, cohort.bundle)
    if write_failure is not None:
        return _fail(_write_failure_message(write_failure))

    loaded: list[str] = []
    for group, _ in sizes.as_pairs():
        count = await fhir_write.count_patients(
            access, _group_tags(args.nct_id, group, data_revision)
        )
        if isinstance(count, fhir_write.FhirWriteFailure):
            return _fail(count.detail)
        expected = cohort.manifest["counts"][group]
        if count != expected:
            return _fail(f"Store count for {group} is {count}, manifest expects {expected}.")
        loaded.append(f"{group}={count}")
    print(f"Loaded: {' '.join(loaded)}")
    print("Manifest:")
    print(json.dumps(cohort.manifest, sort_keys=True, indent=2))
    return 0


async def _verify(args: argparse.Namespace) -> int:
    """Check the store against the committed snapshot and the regenerated manifest.

    Reads only: the snapshot, the live criteria pipeline and the store's Patient counts. The
    expected data revision is recomputed from the snapshot hash, the seed and the sizes; this
    function never reads the ``DATA_REVISION`` override (T-15-02). It compares the live
    partition of the exclusions with the manifest after the drift check and before any FHIR
    access (AD-32, AD-37). The drift check compares the whole trial, so this comparison is
    defense in depth: it fails only if the drift check misses a change, and its message says so."""
    sizes = _sizes(args)
    env = fhir.read_fhir_env()
    if isinstance(env, fhir.FhirAccessError):
        return _fail(_access_message(env), command="verify")
    print(f"Target FHIR server: {env.base_url}", file=sys.stderr)

    path = snapshot.snapshot_path(vocabulary.DEMO_DATA_DIR / "criteria", args.nct_id)
    stored = snapshot.read_snapshot(path, args.nct_id)
    if stored is snapshot.SnapshotReadError.MISSING:
        return _fail(
            f"No criteria snapshot at {path}; run seed first (make load NCT={args.nct_id}) "
            "to create it.",
            command="verify",
        )
    if isinstance(stored, snapshot.SnapshotReadError):
        return _fail(
            f"Criteria snapshot {path} failed validation or its content hash does not "
            "match; re-run seed with --refresh.",
            command="verify",
        )
    stored_trial, stored_sha256 = stored

    live_trial = await screening.resolve_trial(args.nct_id)
    if live_trial is terminology.TerminologyFetchError.UNAVAILABLE:
        return _fail(
            f"Could not re-run the criteria pipeline for {args.nct_id}: "
            f"{terminology.TERMINOLOGY_UNAVAILABLE_REASON} Nothing was written.",
            command="verify",
        )
    if not isinstance(live_trial, Trial):
        detail = live_trial.reason if isinstance(live_trial, ClientError) else live_trial.value
        return _fail(
            f"Could not re-run the criteria pipeline for {args.nct_id}: {detail}.",
            command="verify",
        )

    drift = snapshot.drift_lines(stored_trial, live_trial)
    if drift:
        return _fail(
            f"Criteria drift between the committed snapshot {path} and the live pipeline:",
            *drift,
            "Review the change, then run seed with --refresh and commit the snapshot.",
            command="verify",
        )

    spec = generate.derive_seed_spec(stored_trial)
    if isinstance(spec, generate.UnsupportedTrial):
        return _reject_unsupported(
            args.nct_id, spec.reasons, spec.not_represented, command="verify"
        )
    data_revision = revision.data_revision(stored_sha256, args.seed, sizes.as_pairs())
    cohort = generate.build_cohort(spec, sizes=sizes, seed=args.seed, data_revision=data_revision)
    if isinstance(cohort, generate.CohortTooLarge):
        return _fail(_too_large_message(cohort), command="verify")
    print(f"Data revision: {data_revision}", file=sys.stderr)

    live_partition = anchor.partition_exclusions(
        [*live_trial.inclusion_criteria, *live_trial.exclusion_criteria]
    )
    live_items = [
        generate.UnevaluatedExclusion.from_criterion(criterion).manifest_item
        for criterion in live_partition.unevaluated
    ]
    manifest_partial = cohort.manifest.get("partial", False)
    manifest_items = cohort.manifest.get("unevaluated_exclusions", [])
    if manifest_partial != bool(live_items) or manifest_items != live_items:
        return _fail(
            "The partial flag or the unevaluated exclusions differ from the live pipeline, "
            "but the criteria drift check found no change:",
            f"manifest: {_partial_summary(manifest_partial, manifest_items)}",
            f"live: {_partial_summary(bool(live_items), live_items)}",
            "This is a defect in the drift check or the partition rule, not criteria drift. "
            "Report it before you run seed with --refresh.",
            command="verify",
        )

    access = await fhir.get_fhir_access()
    if isinstance(access, fhir.FhirAccessError):
        return _fail(_access_message(access), command="verify")
    not_ready = await fhir_write.wait_until_ready(access)
    if not_ready is not None:
        return _fail(not_ready.detail, command="verify")
    protocol_patients = await fhir_write.count_patients(access, _protocol_tags(args.nct_id))
    if isinstance(protocol_patients, fhir_write.FhirWriteFailure):
        return _fail(protocol_patients.detail, command="verify")
    revision_patients = await fhir_write.count_patients(
        access, _revision_tags(args.nct_id, data_revision)
    )
    if isinstance(revision_patients, fhir_write.FhirWriteFailure):
        return _fail(revision_patients.detail, command="verify")

    if protocol_patients != revision_patients:
        return _fail(
            f"Store at {env.base_url} holds {protocol_patients} Patient(s) for {args.nct_id}, "
            f"but {revision_patients} carry data revision {data_revision}: more than one data "
            "revision is stored. Reset the store, then re-seed. Nothing was written.",
            command="verify",
        )

    store_counts: dict[str, int] = {}
    manifest_counts: dict[str, int] = {}
    mismatches: list[str] = []
    for group, _ in sizes.as_pairs():
        count = await fhir_write.count_patients(
            access, _group_tags(args.nct_id, group, data_revision)
        )
        if isinstance(count, fhir_write.FhirWriteFailure):
            return _fail(count.detail, command="verify")
        expected = cohort.manifest["counts"][group]
        store_counts[group] = count
        manifest_counts[group] = expected
        if count != expected:
            mismatches.append(f"{group}: store {count}, manifest {expected}")
    if mismatches:
        if protocol_patients == 0:
            mismatches.append(
                f"The store holds no Patients for {args.nct_id}; seed it first "
                f"(make load NCT={args.nct_id})."
            )
        return _fail(
            f"Store counts differ from the manifest for data revision {data_revision}:",
            *mismatches,
            command="verify",
        )

    print("verify passed: no drift; store counts match the manifest.", file=sys.stderr)
    report = {
        "data_revision": data_revision,
        "manifest_counts": manifest_counts,
        "protocol_id": args.nct_id,
        "protocol_patients": protocol_patients,
        "revision_patients": revision_patients,
        "store_counts": store_counts,
    }
    if manifest_partial:
        report["partial"] = True
        report["unevaluated_exclusions"] = manifest_items
    print(json.dumps(report, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse ``argv`` and run the command; returns the process exit code."""
    args = build_parser().parse_args(argv)
    if not trials.NCT_ID_PATTERN.match(args.nct_id):
        return _fail(
            f"Malformed NCT ID {args.nct_id!r}: expected NCT followed by 8 digits.",
            command=args.command,
        )
    too_large = generate.check_group_sizes(_sizes(args))
    if too_large is not None:
        return _fail(_too_large_message(too_large), command=args.command)
    return asyncio.run(_seed(args) if args.command == "seed" else _verify(args))


if __name__ == "__main__":
    raise SystemExit(main())
