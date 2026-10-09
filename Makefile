# Operator entry points for the demo stack (spec 007, FR-22). Kept to plain GNU Make 3.81
# features (macOS default): one command per recipe line, no .ONESHELL, no .RECIPEPREFIX.
# All logic lives in scripts/ (stack_up.sh, stack_reset.sh and stack_verify.sh) so it can be
# tested offline against a stub docker.
# Docker Compose reads the secret env file itself; this Makefile never includes or echoes it.

# Seconds `make up` waits for every service to be healthy. Override from the environment or
# the make command line (UP_TIMEOUT=300 make up). It may only be lengthened, never shortened.
UP_TIMEOUT ?= 180
# Seconds between health polls.
UP_POLL ?= 2

.PHONY: up down load reset verify

up: keys.json
	@UP_TIMEOUT=$(UP_TIMEOUT) UP_POLL=$(UP_POLL) sh scripts/stack_up.sh

# A clean checkout has only keys.example.json. No prerequisites, so make runs this only when
# keys.json is absent and never overwrites an existing one (D-01).
keys.json:
	cp keys.example.json keys.json

down:
	docker compose down

# The only target that deletes data (FHIR data, saved scorecards, the web audit log). It asks
# for the typed word reset, or runs unattended with FORCE=1. The script reads FORCE, UP_TIMEOUT
# and UP_POLL from the environment make exports (command-line and environment variables) and
# defaults UP_TIMEOUT and UP_POLL itself. Make never expands any of them into this recipe's
# shell text.
reset: keys.json
	@sh scripts/stack_reset.sh

# Seeds a labelled cohort for NCT=<id> (OFFLINE=1 adds --offline). The seeder validates the
# NCT ID authoritatively; the format check here only fails a typo before the stack is touched.
# NCT is read from the environment ("$$NCT"; make exports command-line variables), never
# spliced into shell text by make, and matched as a whole string. Runs as the host user so
# snapshot writes through the criteria bind mount land in the repository host-owned (D-07).
# OFFLINE=1 is the only value that adds --offline (`filter` yields "1" or nothing).
load:
	@test -n "$$NCT" || { echo "usage: make load NCT=<NCT ID> [OFFLINE=1]" >&2; exit 2; }
	@case "$$NCT" in NCT[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) printf 'make load: NCT must be NCT followed by 8 digits (got %s)\n' "$$NCT" >&2; exit 2;; esac
	docker compose --profile seed run --rm --build --user "$$(id -u):$$(id -g)" seed seed "$$NCT" $(if $(filter 1,$(OFFLINE)),--offline)

# Checks the seeded FHIR store and the newest scorecard for NCT against the manifest (SM-6).
# NCT defaults to NCT99999999. The script reads it from the environment make exports and
# validates it; make never expands it into this recipe's shell text. Writes nothing.
verify:
	@sh scripts/stack_verify.sh
