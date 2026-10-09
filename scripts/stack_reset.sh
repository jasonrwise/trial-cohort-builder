#!/bin/sh
# Confirmed, preflighted wipe of the demo state, run by `make reset` (spec 007 FR-23, AD-31,
# R-9, T064). The only command in the repository that deletes data.
#
# DESTROYED: the FHIR database volume (all FHIR data) and the web-state volume (every saved
#            scorecard and the web audit log), both through Compose's own `down --volumes`.
# KEPT:      .env, keys.json, demo-data/criteria and the host logs/ directory.
#
# 1. Preflights everything `make up` needs, BEFORE anything is destroyed: .env (and that it holds
#    every name from .env.example), keys.json, UP_TIMEOUT, UP_POLL and the Compose v2 plugin.
# 2. Confirms: proceeds without asking only when FORCE is exactly 1; otherwise the operator must
#    type the word reset. End of input (closed stdin, CI) refuses. FORCE and the typed answer
#    are compared as data and never evaluated.
# 3. Builds the images and pulls the pinned ones, still before anything is destroyed. If either
#    fails (no network, a registry or sign-in error, a broken build) it aborts with nothing
#    changed.
# 4. Stops web (nothing holds the store open), drops both volumes in one call, then hands over
#    to scripts/stack_up.sh so the health-gated start path is reused, not copied. stack_up.sh
#    repeats the build and pull from cache; a failed pull there is only a warning.
# Exit 1 on refusal, a failed file/plugin preflight or a failed build/pull; exit 2 on a bad
# UP_TIMEOUT or UP_POLL; otherwise the exit status of stack_up.sh. POSIX sh only (runs under
# dash).
set -eu

UP_TIMEOUT=${UP_TIMEOUT:-180}
UP_POLL=${UP_POLL:-2}

# --- Preflights: the same checks as stack_up.sh, before anything destructive ----------
# Only the key names of the secret env file are checked against .env.example; no value is
# printed or sourced.

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Run: cp .env.example .env" >&2
  exit 1
fi

# Every name on a NAME= line of .env.example must be a key in .env, or Compose would fill a blank
# secret. Only key names are checked and printed; a quiet grep tests each key, and no .env value
# is stored, printed or sourced.
if [ ! -f .env.example ]; then
  echo "ERROR: .env.example not found; restore it with git checkout -- .env.example" >&2
  exit 1
fi

missing=""
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    [A-Z]*=*)
      name=${line%%=*}
      case "$name" in
        *[!A-Z0-9_]*) continue ;;
      esac
      if ! grep -q "^${name}=" .env; then
        missing="$missing $name"
      fi
      ;;
  esac
done < .env.example
if [ -n "$missing" ]; then
  echo "ERROR: .env is missing:$missing. Copy each line from .env.example and set its value." >&2
  exit 1
fi

if [ ! -f keys.json ]; then
  echo "ERROR: keys.json is not a regular file. Docker creates a directory when a bind-mount source is missing; remove it (rm -rf keys.json) and re-run make reset." >&2
  exit 1
fi

case "$UP_TIMEOUT" in
  '' | *[!0-9]*)
    echo "ERROR: UP_TIMEOUT must be a whole number of seconds, got '$UP_TIMEOUT'" >&2
    exit 2
    ;;
esac

# UP_POLL is a positive number of seconds: digits with at most one dot, and at least one
# non-zero digit (a zero poll would be a busy loop).
case "$UP_POLL" in
  '' | *[!0-9.]* | *.*.* | .)
    echo "ERROR: UP_POLL must be a positive number of seconds, got '$UP_POLL'" >&2
    exit 2
    ;;
esac
case "$UP_POLL" in
  *[1-9]*) ;;
  *)
    echo "ERROR: UP_POLL must be a positive number of seconds, got '$UP_POLL'" >&2
    exit 2
    ;;
esac

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: Docker Compose v2 (the 'docker compose' plugin) is required. On Ubuntu: sudo apt install docker-compose-v2" >&2
  exit 1
fi

# --- Confirmation ---------------------------------------------------------------------------

if [ "${FORCE:-}" != 1 ]; then
  {
    echo "make reset destroys all FHIR data (the database volume), every saved scorecard"
    echo "and the web audit log (the web-state volume)."
    echo "It keeps .env, keys.json, demo-data/criteria and the host logs/ directory."
    printf 'Type "reset" to continue: '
  } >&2
  answer=""
  IFS= read -r answer || true
  if [ "$answer" != reset ]; then
    echo >&2
    echo "reset refused: nothing was changed. Run 'make reset' and type reset, or 'make reset FORCE=1'." >&2
    exit 1
  fi
fi

# --- Build and pull, before anything is destroyed -------------------------------------------
# The fallible, non-destructive half of `make up`. A failure here leaves the stack untouched.

if ! docker compose build; then
  echo "ERROR: 'docker compose build' failed before the wipe: nothing was changed. Fix the build error and re-run make reset." >&2
  exit 1
fi
if ! docker compose pull db fhir; then
  echo "ERROR: 'docker compose pull db fhir' failed before the wipe: nothing was changed. make reset needs network access to the image registry; check the connection or registry sign-in and re-run make reset." >&2
  exit 1
fi

# --- Wipe and restart -----------------------------------------------------------------------

# Nothing may hold the scorecard store or the audit log open while the volume is removed (AD-31).
docker compose stop web
# Both named volumes, pgdata and webstate, in one call (R-9).
docker compose down --volumes
exec sh scripts/stack_up.sh
