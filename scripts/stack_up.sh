#!/bin/sh
# Health-gated, time-bounded start of the demo stack, run by `make up` (spec 007, FR-22, T026).
#
# 1. Preflights: secret env file, keys.json, UP_TIMEOUT, UP_POLL and the Compose v2 plugin. Only
#    the key names of the env file are checked against .env.example; no value is printed or
#    sourced.
# 2. Untimed: build the images and pull the pinned third-party ones (first-run downloads are
#    about 1.5 GB and must not count against UP_TIMEOUT). A failed pull is a warning, so a
#    second start works offline from cached images; a missing image fails the timed start.
# 3. Timed: start the stack in the background and poll each service's health until all four
#    report healthy or UP_TIMEOUT elapses. Compose itself waits on service_healthy
#    dependencies with no bound, so the deadline is enforced here.
# On failure it names every service that is not healthy and exits non-zero without printing
# a URL. POSIX sh only (runs under dash).
set -eu

SERVICES="db fhir token web"
UP_TIMEOUT=${UP_TIMEOUT:-180}
UP_POLL=${UP_POLL:-2}

# Print one service's status: its health status when it has a healthcheck, else its state.
# An empty container id means the service was never created.
service_status() {
  cid=$(docker compose ps -a -q "$1" 2>/dev/null || true)
  if [ -z "$cid" ]; then
    echo "not created"
    return 0
  fi
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "unknown"
}

all_healthy() {
  for svc in $SERVICES; do
    if [ "$(service_status "$svc")" != healthy ]; then
      return 1
    fi
  done
  return 0
}

# $1 is the reason, e.g. "demo stack not healthy within 180s". Prints the reason, one line
# per service that is not healthy (dependency order), and where to look next, all to stderr.
report_unhealthy() {
  echo "ERROR: $1; not healthy:" >&2
  for svc in $SERVICES; do
    status=$(service_status "$svc")
    if [ "$status" != healthy ]; then
      echo "  $svc: $status" >&2
    fi
  done
  echo "Check: docker compose ps; docker compose logs <service>" >&2
}

# --- Preflights: nothing here may touch Docker until the checkout is known to be usable ----

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
  echo "ERROR: keys.json is not a regular file. Docker creates a directory when a bind-mount source is missing; remove it (rm -rf keys.json) and re-run make up." >&2
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

# --- Untimed: downloads and image builds -------------------------------------------------

docker compose build
if ! docker compose pull db fhir; then
  echo "WARN: could not pull the db and fhir images; continuing with any cached copies. If an image is missing, the start step fails and names it." >&2
fi

# --- Timed: start and poll ---------------------------------------------------------------

start=$(date +%s)
docker compose up -d &
up_pid=$!
up_status=""

# Collect the status of the background `docker compose up -d` once it has ended.
reap_up() {
  if [ -z "$up_status" ] && ! kill -0 "$up_pid" 2>/dev/null; then
    up_status=0
    wait "$up_pid" || up_status=$?
  fi
}

while :; do
  reap_up
  if [ -n "$up_status" ] && [ "$up_status" -ne 0 ]; then
    report_unhealthy "docker compose up -d failed"
    exit 1
  fi

  if all_healthy; then
    # A non-zero `up -d` is a failure even when every service reads healthy.
    if [ -z "$up_status" ]; then
      up_status=0
      wait "$up_pid" || up_status=$?
    fi
    if [ "$up_status" -ne 0 ]; then
      report_unhealthy "docker compose up -d failed"
      exit 1
    fi
    echo "web:   http://127.0.0.1:8000"
    echo "FHIR:  http://127.0.0.1:8080/fhir"
    echo "token: http://127.0.0.1:8081"
    exit 0
  fi

  now=$(date +%s)
  if [ $((now - start)) -ge "$UP_TIMEOUT" ]; then
    # Stop the background job so no Compose output spills after make returns. Containers that
    # already started keep running; `make down` stops them.
    kill "$up_pid" 2>/dev/null || true
    wait "$up_pid" 2>/dev/null || true
    report_unhealthy "demo stack not healthy within ${UP_TIMEOUT}s"
    exit 1
  fi
  sleep "$UP_POLL"
done
