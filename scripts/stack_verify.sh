#!/bin/sh
# Two-step check of the seeded and screened demo stack, run by `make verify` (spec 007 FR-37,
# SM-6, T083). It writes nothing: no load, no reset, no volume change.
#
# 1. Seeder step: first builds the seed image and sends the build output to standard error.
#    Compose writes build progress to standard output when that is not a terminal, and that
#    text would corrupt the captured JSON. Then runs `seed verify <NCT>` in the seed container
#    without a build flag. It re-runs the criteria pipeline, compares the FHIR store with the
#    manifest and prints one JSON object on standard output. The container runs as the image's
#    own user (no --user override), because verify only reads.
# 2. Scorecard step: pipes that JSON into scripts/verify_counts.py inside the running web
#    container. The script finds the newest screened scorecard for the protocol and compares
#    its candidate counts with the seeded counts.
#
# NCT comes from the environment and defaults to NCT99999999. It is matched as a whole string
# before any docker call and is always double-quoted afterwards. It is data and is never
# evaluated. POSIX sh has no pipefail, so the seeder output is captured and its status is
# tested before the pipe.
#
# Exit 0 when both steps pass; 2 for a malformed NCT; 1 for a missing Compose plugin, a web
# service that is not running, a seed image build failure, or a seeder failure; otherwise the exit status of the scorecard
# step. POSIX sh only (runs under dash).
set -eu

nct=${NCT:-NCT99999999}

case "$nct" in
  NCT[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *)
    printf 'make verify: NCT must be NCT followed by 8 digits (got %s)\n' "$nct" >&2
    exit 2
    ;;
esac

# --- Preflights: nothing runs until both pass ------------------------------------------------

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 (the 'docker compose' plugin) is required. On Ubuntu: sudo apt install docker-compose-v2" >&2
  exit 1
fi

web=$(docker compose ps -q web 2>/dev/null) || web=""
if [ -z "$web" ]; then
  echo "make verify: the web service is not running; start the stack with make up first." >&2
  exit 1
fi

# --- Step 1: the seeder verify step ----------------------------------------------------------

if ! docker compose --profile seed build seed >&2; then
  echo "make verify: the seed image build failed; the seeder and scorecard checks were not run. Read the build output above." >&2
  exit 1
fi

if ! out=$(docker compose --profile seed run --rm -T seed verify "$nct"); then
  printf "make verify: seeder verify failed for %s; the scorecard check was not run. Read the 'verify failed' lines above.\n" "$nct" >&2
  exit 1
fi

# --- Step 2: the scorecard check, fed with the seeder's output -------------------------------

status=0
printf '%s\n' "$out" | docker compose exec -T web python scripts/verify_counts.py --protocol "$nct" --counts - || status=$?
if [ "$status" -ne 0 ]; then
  printf 'make verify: the scorecard check failed (exit %s).\n' "$status" >&2
  if [ "$status" -eq 2 ]; then
    echo "make verify: exit 2 means unreadable input, or a web container built before scripts/verify_counts.py existed; run make up to rebuild it, then run make verify again." >&2
  fi
  exit "$status"
fi

echo "make verify: passed for $nct."
