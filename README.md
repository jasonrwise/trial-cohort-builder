# TrialBridge MCP

Screening patients against a clinical trial's eligibility criteria is slow, manual work. TrialBridge MCP turns a trial's NCT ID into a scored list of candidate patients. It reads the real protocol from ClinicalTrials.gov and screens the patients against a HAPI FHIR store.

The demo stack includes a self-hosted HAPI FHIR server with synthetic patients. It is for demonstration only. No real patient data is involved. To screen your own patients, point the gateway at your HAPI FHIR server. See [Point the gateway at another FHIR server](#point-the-gateway-at-another-fhir-server).

A Clinical Research Coordinator (CRC) or Principal Investigator (PI) can screen in two ways:

- **In Claude.** Ask Claude Code or Claude Desktop to screen a trial. Claude calls the TrialBridge MCP server. A CRC gets a draft alert preview. A PI posts the scorecard to a Slack channel.
- **In the web app.** Sign in with your role key and screen in the browser. A PI finalizes the scorecard. See [Web application (v2)](#web-application-v2).

## How screening works

**In Claude**

1. Name a trial by its NCT ID.
2. Claude fetches the eligibility criteria and checks each one against a terminology lookup.
3. Claude queries the patient cohort. The gateway removes identifiers and sets each candidate's status.
4. Claude drafts a screening alert. A CRC sees a preview. A PI's call posts a scorecard to Slack.

**In the web app**

1. Sign in with a CRC or PI key.
2. Enter an NCT ID. The app shows the verified inclusion and exclusion criteria.
3. The app queries the FHIR store and shows each matching candidate with its evidence.
4. The app saves a scorecard. A CRC or PI can browse it, re-run it or export it as a PDF.
5. A PI finalizes the scorecard. Finalizing is irreversible. The web app sends no Slack message.

## What you can rely on

- **The gateway decides eligibility.** It computes each `ELIGIBLE`, `INELIGIBLE` or `BORDERLINE` status. It overrides any status that an LLM proposes.
- **Patient data is de-identified.** The gateway removes all 18 HIPAA Safe Harbor identifiers before a candidate leaves the FHIR store.
- **Actions follow the caller's role.** A key maps to a CRC or PI role. Only a PI can post to Slack or finalize a scorecard.
- **Failures are bounded.** The gateway never hangs or crashes when an upstream fails. A ClinicalTrials.gov or FHIR failure returns a structured `DEGRADED` response. A terminology failure leaves criteria `UNMAPPED`. A Slack failure returns an actionable error. See [Graceful degradation](#graceful-degradation).

## MCP tools

The MCP server provides three tools:

- **`get_protocol_criteria`** fetches a trial from ClinicalTrials.gov and splits its eligibility text into criteria. It verifies each criterion against a terminology lookup. See [Live-trial demo (MCP)](#live-trial-demo-mcp).
- **`query_patient_cohort`** re-verifies the caller's codes and queries the FHIR store. It removes the HIPAA Safe Harbor identifiers and computes each candidate's status.
- **`dispatch_screening_alert`** acts on the caller's role. A CRC always gets a draft preview. A PI's call posts a Block Kit scorecard to Slack.

To try both surfaces on your own machine, start at the [Quickstart](#quickstart).

## Quickstart

This path needs Docker and Make. It does not need a Python install. It takes a fresh clone to
a screened scorecard on your own machine.

### What you need

- Docker Engine with the Compose v2 plugin (`docker compose version` must work).
- GNU Make.
- About 4 GB of free RAM.
- Host ports 8000, 8080 and 8081 free. An old container left holding port 8080 (for example
  from a manual HAPI recipe) blocks the stack.
- Internet access.
- A real Anthropic API key for the screening step.

The table shows which steps need the internet and which need the real key.

| Activity | Needs internet | Needs a real API key |
|---|---|---|
| First `make up` | Yes: it pulls about 1.5 GB and builds the image | No |
| `make load NCT=NCT99999999` | No: it uses the committed snapshot (`OFFLINE=1` also works) | No |
| Screening in the web app | Yes: the web app calls the terminology service on every criteria fetch, even for the committed demo protocol | Yes: `ANTHROPIC_API_KEY` for the per-candidate proposal |
| `make reset` | Yes: it builds, then pulls the pinned images before it deletes anything | No |
| `make verify` | Yes: the live pipeline calls the terminology service | No |
| MCP tools | Yes: the terminology service only | No |
| Seeding a trial without a committed snapshot | Yes: ClinicalTrials.gov and the terminology service | No |

### Steps

1. Clone the repository.

   ```bash
   git clone https://github.com/jasonrwise/trial-cohort-builder.git
   cd trial-cohort-builder
   ```

2. Copy the env example, then put a real key in `ANTHROPIC_API_KEY`.

   ```bash
   cp .env.example .env
   ```

   The placeholder key only lets the web app boot. Git ignores `.env`. Never commit it. After
   any later change to `.env`, run `make up` again so the web container picks it up.

3. Start the stack.

   ```bash
   make up
   ```

   The first run builds the image and pulls about 1.5 GB. Then `make up` prints the three
   URLs. If `make up` reports a service that is not healthy on a slow machine, see
   [Start and stop](#start-and-stop) for `UP_TIMEOUT`.

4. Seed the demo protocol.

   ```bash
   make load NCT=NCT99999999
   ```

   This writes 35 synthetic Patients from the committed snapshot. It needs no internet.

5. Screen the protocol. Open `http://127.0.0.1:8000` and sign in with the PI key
   `tb_demo_fake_pi_0000000000000000` from `keys.json`. Screen `NCT99999999`. Expect 25
   candidates: 15 not INELIGIBLE and 10 INELIGIBLE. The page carries the SYNTHETIC tag.

6. Verify the demo data.

   ```bash
   make verify
   ```

   The last line reads `make verify: passed for NCT99999999.` Screen first: the command
   compares the newest saved scorecard, so it fails when none exists. See
   [Verify the demo data](#verify-the-demo-data) for what it checks.

7. Reset and teardown.

   ```bash
   make down
   make reset
   ```

   `make down` stops the stack and keeps all data. `make reset` deletes all data and asks you
   to type `reset`. See [Start and stop](#start-and-stop).

### Demo notes

- `NCT99999999` is a reserved synthetic protocol. Its payload is committed under
  `demo-data/synthetic`. All patient data in the demo is synthetic.
- The criterion "Diagnosis of type 2 diabetes mellitus" verifies to ICD-10-CM `E11.9`, "Type 2
  diabetes mellitus without complications". That code is narrower than the criterion text. A
  real cohort of patients with complications would be missed.
- The HbA1c criterion uses the LOINC short name `HbA1c MFr Bld >= 7.5 %`. The plainer wording
  "Hemoglobin A1c" verifies to the panel code `112870-1`.
- Age and sex are not modeled. The seeder prints: "Age and sex are not modelled; patients
  get deterministic plausible values."
- The static demo keys and the mock token server are not a production identity provider. The
  keys in `keys.example.json` are public placeholders. The mock token server issues a fixed
  token to anyone.
- For the `NCT01370005` to `I10` caveat on a real trial, see
  [Observed counts](#observed-counts-run-date-2026-09-14).

## Local install

Create a virtual environment and install the project with its dev extra:

```bash
uv venv
uv pip install -e ".[dev]"
```

If you do not have `uv`, run `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`. It
works the same way.

## Setting up a new deployment — checklist

To run the demo, follow the [Quickstart](#quickstart) instead. This checklist covers running
the gateway outside the demo stack.

Do these five steps on a fresh clone before you use the three tools:

1. **Install dependencies.** See [Local install](#local-install).
2. **Seed the key→role file.** See [Seeding the key→role file](#seeding-the-keyrole-file). The
   server will not start without it.
3. **Stand up a FHIR store.** See [FHIR store setup](#fhir-store-setup). Without it,
   `query_patient_cohort` returns an upstream connection error until the circuit breaker
   trips. After that, it returns `DEGRADED`.
4. **Create a Slack app and bot token.** See [Slack setup](#slack-setup). Only a PI-role
   `dispatch_screening_alert` call needs it. A CRC-role call never touches Slack.
5. **Configure your MCP client.** See
   [Configuring an MCP client](#configuring-an-mcp-client-claude-desktopcode).

`get_protocol_criteria` and the CRC draft-only path of `dispatch_screening_alert` need only
steps 1, 2 and 5. They need no FHIR store or Slack app. ClinicalTrials.gov and the NLM
terminology service are free, unauthenticated public APIs. They need outbound network access
and no credentials.

## Environment variables

| Variable | Required at boot? | Default | Purpose |
|---|---|---|---|
| `TRIALBRIDGE_KEYS_FILE` | Yes | none | Path to the local key→role JSON file. See [Seeding the key→role file](#seeding-the-keyrole-file). The server refuses to start if this path is unset, missing or unreadable. The error names the path and this variable. |
| `TRIALBRIDGE_AUDIT_LOG_PATH` | No | `./logs/audit.log` | Path to the append-only audit log. The parent directory must exist and be writable at boot. The server creates the file on first write. If the directory is missing or read-only, the server refuses to start and names the path and this variable. |
| `TRIALBRIDGE_API_KEY` | Yes | none | The caller's own credential for the life of the process. See [How the caller's role is resolved](#access-control-and-role-resolution). |
| `FHIR_BASE_URL` | Only for `query_patient_cohort` | none | Base URL of the self-hosted FHIR server. The gateway reads it on every request. See [FHIR store setup](#fhir-store-setup). |
| `FHIR_TOKEN_URL` | Only for `query_patient_cohort` | none | OAuth2 client-credentials token endpoint for the FHIR server. |
| `FHIR_CLIENT_ID` | Only for `query_patient_cohort` | none | OAuth2 client ID for the FHIR server. |
| `FHIR_CLIENT_SECRET` | Only for `query_patient_cohort` | none | OAuth2 client secret for the FHIR server. |
| `SLACK_BOT_TOKEN` | Only for PI-role `dispatch_screening_alert` | none | Slack Web API bot token (`xoxb-...`). See [Slack setup](#slack-setup). |
| `TRIALBRIDGE_SESSION_SECRET` | Web process only | none | HMAC key that signs the web app's session cookie. The web process refuses to start without it. See [Web application (v2)](#web-application-v2). |
| `TRIALBRIDGE_SCORECARD_STORE_PATH` | Web process only | none | Path to the append-only JSON-Lines scorecard store. Its parent directory must be writable at boot. Use `./logs/scorecards.jsonl`, which Git ignores (`logs/*.jsonl`). The app never rotates this file. |
| `ANTHROPIC_API_KEY` | Web process only | none | Key for the web app's per-candidate eligibility proposal call (`llm_proposed_status`). The app uses it for nothing else. |
| `ANTHROPIC_MODEL` | Web process only | none | Anthropic model ID for the same call. You supply it in config; the code never hardcodes it. Use `claude-opus-5`. |
| `SESSION_COOKIE_SECURE` | No (web process only) | `true` | Sets the `Secure` flag on the web session cookie. Set `false` only for the plain-HTTP loopback stack. See [Session cookie over plain HTTP](#session-cookie-over-plain-http). |
| `DATA_REVISION` | No (web process only) | none | When non-blank, overrides the data revision stored on each new scorecard. When unset (the normal case), the web app reads the revision from the seeded Patients' `urn:trialbridge:data-revision` tag after a successful cohort fetch. A missing or unreadable tag stores `unknown`. `src/services/fhir.py` reads it on each call. It is not a `Config` field. The Compose stack does not pass it to the web container. |

The gateway does not check `FHIR_BASE_URL`, `FHIR_TOKEN_URL`, `FHIR_CLIENT_ID`,
`FHIR_CLIENT_SECRET` or `SLACK_BOT_TOKEN` at boot, and the code has no default for any of
them. A CRC-only deployment that never queries the cohort or dispatches to Slack boots without
them. The gateway reads each one at the point of use, on every request
(`src/services/fhir.py`, `src/tools/dispatch_screening_alert.py`). It never caches them in the
process. A rotated credential takes effect on the next call, with no restart.

The shared `load_config()` also does not validate the six web-only variables. An MCP-only
deployment boots without them. The separate `src/web` process checks the four "Web process
only" variables at startup and fails fast if one is missing. See
[Web application (v2)](#web-application-v2).

## Seeding the key→role file

The runtime key→role file is **never committed**. Git ignores it (`keys.json` at the repo
root, or wherever `TRIALBRIDGE_KEYS_FILE` points). The checked-in template
[`keys.example.json`](./keys.example.json) shows the shape with obviously fake placeholder
keys:

```json
{
  "tb_demo_fake_crc_0000000000000000": "CRC",
  "tb_demo_fake_pi_0000000000000000": "PI",
  "tb_demo_fake_site_admin_00000000": "SITE_ADMIN"
}
```

To seed your own runtime file, copy the template:

```bash
cp keys.example.json keys.json
```

Then replace each placeholder key with your own generated value. Any sufficiently random string
works. This is a single-tenant demo mechanism, not a production credential store. **Never
commit `keys.json`.** `.gitignore` already covers it, but check `git status` before your first
commit if you rename or move it.

## Demo stack

One command, `make up`, starts a Postgres database, a HAPI FHIR R4 server, a demo-only mock
token server and the web app, all published on `127.0.0.1` only.

For the prerequisites, see [What you need](#what-you-need) in the Quickstart.

### Start and stop

```bash
cp .env.example .env
make up
```

`make up` copies `keys.example.json` to `keys.json` when that file is absent. It never
overwrites an existing `keys.json`. It mounts the file read-only into the web container. The
first run builds the image and pulls about 1.5 GB. Both happen before the timed wait. Then
`make up` starts every service and waits for all four to report healthy. When they do, it
prints the web, FHIR and token URLs.

`UP_TIMEOUT` is the number of seconds `make up` waits for health. The default is 180. The cold
first start can take longer on a slow machine. Lengthen the wait, for example
`UP_TIMEOUT=300 make up`. Never lower the committed default.

`make down` stops the stack and keeps all data. The database, the scorecard store and the web
audit log live in named volumes.

`make reset` is the only command that deletes data.

- It asks you to type `reset`. `make reset FORCE=1` runs it unattended.
- Before it deletes anything, it builds the images and pulls the pinned ones, as `make up`
  does. If either step fails, it stops and changes nothing. Failures include no network, a
  registry or sign-in error and a broken build.
- It stops the web app. It removes the FHIR database volume and the web-state volume together.
  The web-state volume holds the saved scorecards and the web audit log.
- It then starts an empty, healthy stack. This restart repeats the build and pull from cache.
  The pull still contacts the registry, so keep network access until the reset finishes.
- It keeps `.env`, `keys.json`, `demo-data/criteria` and the host `logs/` directory.

A reset followed by the same `make load` reproduces the same data revision.

### Seeding synthetic patient data

#### Option 1: Seed the demo store

With the stack running, seed a trial and its labeled synthetic cohort:

```bash
make load NCT=NCT99999999
```

The seeder prints the target FHIR URL before any write. It reads the committed criteria
snapshot under `demo-data/criteria`, so the demo protocol needs no internet to seed.
`OFFLINE=1 make load NCT=NCT99999999` forbids ClinicalTrials.gov and terminology calls while
still writing to FHIR.

- A trial without a committed snapshot resolves live once. The seeder writes its snapshot into
  `demo-data/criteria` as your own user. The seed service bind-mounts that directory
  read-write, and `make load` runs it with your uid and gid. Commit the new file.
- Re-running `make load` for the same trial is idempotent.
- A store that already holds a different data revision for the protocol is refused. Run
  `make reset`, then `make load` again.
- If the container cannot write the snapshot directory, run the seeder from the host instead:

  ```bash
  export FHIR_BASE_URL=http://127.0.0.1:8080/fhir
  export FHIR_TOKEN_URL=http://127.0.0.1:8081/token
  export FHIR_CLIENT_ID=...      # the values from your .env file
  export FHIR_CLIENT_SECRET=...
  .venv/bin/python -m src.seeder.cli seed NCT99999999
  ```

#### Option 2: Seed a real trial

`make load NCT=<NCT ID>` also seeds a real ClinicalTrials.gov trial, if the trial has the same
attributes as the demo: one diagnosis, one lab value and, at most, exclusions on other codes. A
real-trial seed is a **partial seed**. The seeder represents the diagnosis and the lab value
only. The synthetic protocol `NCT99999999` is the fully represented demo.

The run report lists everything the seed leaves out:

- `Criteria not represented (never invented):` lists criteria that have no verified code.
- `Partial seed: N exclusions not evaluated` lists verified exclusions on other codes. The
  scorer ignores them, and the seeder creates no patient data for them.

The seeder refuses a trial it cannot represent. It exits with a non-zero status, writes
nothing and states the reason. These trials are refused:

- a trial with an exclusion on the same diagnosis or lab code as the cohort query,
- a trial with a second diagnosis or lab criterion, including a two-sided range such as
  "between 6.5 and 9.0",
- a trial with no verified diagnosis inclusion or no verified lab inclusion,
- a trial whose lab criterion has no numeric threshold.

A screening reads every seeded patient that carries the
[diagnosis and lab codes](#cohort-screening). It does not filter by trial. To count one trial's
cohort, keep one trial in the store at a time. Run `make reset` before you run `make load` for
another trial.

### Verify the demo data

`make verify` checks that the seeded store and the newest screened scorecard match the
committed manifest. It runs two steps and passes only when both pass.

1. The seeder step runs in the seed container. It re-runs the criteria pipeline and compares
   the result with the committed snapshot. It checks that the store holds one data revision
   for the protocol. It then compares the store's Patient counts with the manifest.
   For a partial seed, this step also compares the manifest's partial flag and its list of
   unevaluated exclusions with the live pipeline.
2. The scorecard step runs in the web container through `scripts/verify_counts.py`. It reads
   the newest screened scorecard for the protocol. The candidates not scored INELIGIBLE must
   equal the seeded eligible group. The INELIGIBLE candidates must equal the seeded near-miss
   group. When the scorecard records a data revision, it must equal the seeded revision. A scorecard
   with an unknown revision skips this check.

`make verify NCT=<NCT ID>` checks another protocol. The default is `NCT99999999`.

What a failure means:

- A drift message lists each changed criterion with its snapshot code and its live code. The
  live pipeline no longer agrees with the committed snapshot. Review the change, then run
  the seeder with `--refresh` and commit the snapshot.
- A partial-seed message prints two lists, the one in the manifest and the live one. The
  exclusions the pipeline finds no longer match the exclusions the seed recorded. Review the
  change, then refresh the snapshot.
- A missing scorecard means nobody has screened the protocol yet. Screen it in the web app,
  then run `make verify` again.
- A data revision difference means the newest scorecard was screened against other seeded
  data, or under a `DATA_REVISION` override. Screen the protocol again, then run
  `make verify` again.
- A message about more than one data revision means the store holds data from two seeds. Run
  `make reset`, then `make load`.
- Exit status 2 after an upgrade means the web container predates the check script. Run
  `make up` to rebuild it.

`make verify` needs internet access, because the seeder step calls the terminology service.
It checks the default seed and group sizes only. `make load` and `make verify` take no
variable for group sizes or the seed.

To check a custom seed, run the seeder `verify` command on the host with the same flags you
seeded with, and pipe its output into the scorecard step. Export the four `FHIR_*` variables
first, as in [Seed the demo store](#seed-the-demo-store):

```bash
.venv/bin/python -m src.seeder.cli verify NCT99999999 --eligible 20 --near-miss 5 --noise 5 --seed 7 \
  | docker compose exec -T web python scripts/verify_counts.py --protocol NCT99999999 --counts -
```

Screen the protocol first, as for the default check.

### Services

| Service | Address | Notes |
|---|---|---|
| `db` | not published | Postgres 16; reachable only from the other containers |
| `fhir` | `http://127.0.0.1:8080/fhir` | HAPI FHIR R4 on Postgres |
| `token` | `http://127.0.0.1:8081` | A demo-only mock OAuth token server. It is not an identity provider and issues a fixed token to anyone |
| `web` | `http://127.0.0.1:8000` | The web app, run with one worker |

Every port is bound to `127.0.0.1`. Nothing is reachable from another machine. The demo keys in
`keys.example.json`, the session secret in `.env.example` and the mock token server are public.
If you publish a port beyond loopback, anyone who can reach it can sign in with the example
keys. Keep every port on `127.0.0.1`. For the session cookie setting, see
[Session cookie over plain HTTP](#session-cookie-over-plain-http).

### Session cookie over plain HTTP

The stack serves plain HTTP on `127.0.0.1`. A browser drops a `Secure` cookie that arrives
over plain HTTP, so sign-in would not persist with the Secure flag on. For that reason only
`.env.example` sets `SESSION_COOKIE_SECURE=false`. `docker-compose.yml` defaults the variable
to `true`. Any other deployment keeps the Secure flag and terminates TLS in front of the web
app.

### Troubleshooting

- A failed `make up` names each service that is not healthy. Read its log with
  `docker compose logs <service>`.
- Changing `POSTGRES_PASSWORD` after the first start leaves `db` healthy but `fhir` unable to
  sign in, because Postgres reads that password only when the data volume is first created.
  Run `make reset`, which also deletes the saved scorecards.
- Variables exported in your shell override the values in the env file.
- `keys.json` must be readable by uid 10001 inside the web container.
- An old container holding port 8080 blocks the stack. Stop or remove it first.
- Before you roll back to an older build, run `make reset` on the current checkout. A build
  from before the data revision column rejects every saved scorecard that carries
  `data_revision`, so the whole scorecard store becomes unreadable. That build also predates
  `make reset`. The reset deletes the saved scorecards. Then switch back.

### Pinned versions

Every image is pinned to an exact tag. `postgres:16-alpine` is a rolling minor tag, so it is
also pinned by digest. `tests/unit/test_stack_guards.py` refuses `latest`, untagged and
`-tomcat` images. It also fails if a pin in the table below disagrees with the committed
`docker-compose.yml` or `Dockerfile`.

| Image | Digest at pin time | Used by |
|---|---|---|
| `postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea` | in the image reference | `db` |
| `hapiproject/hapi:v8.12.0-1` | `sha256:d7f38d3676900b07e8a7d3500426eaf8d90ea9ced9a6726416f080f68e230fb0` | `fhir` |
| `python:3.12.14-slim-trixie` | `sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f` | the web image (`web`, `token`, `seed`) |

Upgrade procedure:

1. Change the tag in `docker-compose.yml` or `Dockerfile` and in the table above in the same
   commit. The guard fails otherwise. A Postgres upgrade changes the digest in both places.
2. Run `make down`, then `make up`, and confirm all four services report healthy.
3. Run the start-and-seed smoke test: `make load NCT=NCT99999999`, then screen that trial in
   the web app.
4. On a HAPI bump, re-check the Postgres wiring names (`SPRING_DATASOURCE_*`,
   `HIBERNATE_DIALECT`) and the `HapiFhirPostgresDialect` class against the new starter. A tag
   change may rename them. Never switch to a `-tomcat` variant.
5. A Postgres major-version bump cannot reuse the old data volume.

Only direct dependencies are pinned in `pyproject.toml`, so transitive Python packages resolve
at image build time. This drift is an accepted limitation.

## FHIR store setup

`query_patient_cohort` needs a FHIR R4 server. For local demos, the repo ships one. Follow the
[Quickstart](#quickstart) or see [Demo stack](#demo-stack). For every other deployment, the
four `FHIR_*` variables make the gateway host-agnostic. Fly.io, Render and a small VPS all work
the same way from the gateway's side. Hosting is an open, non-blocking operator decision.

Use synthetic data only. Never load real patient data. Generate a cohort with
[Synthea](https://github.com/synthetichealth/synthea) or with the demo seeder. See
[Seed the demo store](#seed-the-demo-store). The gateway's Safe Harbor stripping
(`src/services/deidentify.py`) is a second, mandatory line of defense. It does not replace
starting from synthetic records.

To refresh the recorded test fixtures against your own live instance, run
`python scripts/capture_fixtures.py fhir --condition-code <ICD-10-CM> --loinc <LOINC>`. This
step is optional. The automated test suite always runs against the recorded fixtures, never
live FHIR.

Without a reachable FHIR server, `query_patient_cohort` returns the `DEGRADED` envelope once
the circuit breaker trips, after 3 consecutive failures. It does not hang or crash.

### Point the gateway at another FHIR server

The gateway reads `FHIR_BASE_URL` on every request and holds no host name of its own.

- An open HAPI R4 server needs only `FHIR_BASE_URL`. The gateway still asks `FHIR_TOKEN_URL`
  for a token. An open server ignores the token, so the demo stack's token settings can stay
  as they are.
- A server that enforces OAuth2 also needs its own `FHIR_TOKEN_URL`, `FHIR_CLIENT_ID` and
  `FHIR_CLIENT_SECRET`.

Three routes read these variables directly. Each example uses a server on port 8090 of your
own machine.

1. **The host-run seeder.** Export the four variables, as in
   [Seed the demo store](#seed-the-demo-store). Set `FHIR_BASE_URL` to the new server, for
   example `http://127.0.0.1:8090/fhir`. Then run `.venv/bin/python -m src.seeder.cli seed
   NCT99999999`.

2. **The host-run web app.** Export the variables from
   [Web application (v2)](#web-application-v2) and the four `FHIR_*` variables above. Stop the
   stack's `web` container first, because it holds port 8000. Then start `uvicorn`:

   ```bash
   FHIR_BASE_URL=http://127.0.0.1:8090/fhir \
     .venv/bin/python -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000 --workers 1
   ```

3. **The MCP server.** Set the `FHIR_*` values in the `env` block of your client entry, as in
   [Configuring an MCP client](#configuring-an-mcp-client-claude-desktopcode).

The Compose containers differ. `docker-compose.yml` sets `FHIR_BASE_URL` to a literal value
on the `web` service and on the `seed` service. A variable that you export in your shell or
write in `.env` does not reach them. To point a container at another server, edit
`FHIR_BASE_URL` on the `web` service and on the `seed` service in `docker-compose.yml`. For a
protected server, also edit `FHIR_TOKEN_URL` on both services and set the client ID and secret
in `.env`.

The offline test `tests/integration/test_fhir_server_agnostic.py` runs the same screening
against two base URLs. It checks that only `FHIR_BASE_URL` changes between the runs.

## Slack setup

Only the PI-role path of `dispatch_screening_alert` calls Slack. A CRC-role call always
returns a draft preview and never touches Slack, with or without this setup.

1. Create a Slack app at [api.slack.com/apps](https://api.slack.com/apps) in your workspace
   and install it. Grant the bot the `chat:write` scope.
2. Copy the Bot User OAuth Token (`xoxb-...`) into `SLACK_BOT_TOKEN`.
3. Invite the bot to the channel you will dispatch to. Note the channel's ID. It starts with
   `C` and has 11 characters in total, for example `C0123456789`. The tool checks the
   `channel_id` argument against the exact pattern `^C[A-Z0-9]{10}$` before any Slack call. It
   rejects a malformed ID as a `ClientError` and makes no live request.

The gateway catches a missing or invalid `SLACK_BOT_TOKEN` and a Slack 401. It returns an
actionable error to the caller and logs the event to the audit trail. The process keeps running
and serves the next call.

## Configuring an MCP client (Claude Desktop/Code)

Add an entry under `mcpServers` in your client's config. Point `command` and `args` at this
project's virtualenv Python. Point `env` at your seeded key file, your chosen API key and, once
you have set them up, your FHIR and Slack credentials:

```json
{
  "mcpServers": {
    "trialbridge-mcp": {
      "command": "/absolute/path/to/trial-cohort-builder/.venv/bin/python",
      "args": ["-m", "src.server"],
      "env": {
        "TRIALBRIDGE_KEYS_FILE": "/absolute/path/to/trial-cohort-builder/keys.json",
        "TRIALBRIDGE_AUDIT_LOG_PATH": "/absolute/path/to/trial-cohort-builder/logs/audit.log",
        "TRIALBRIDGE_API_KEY": "tb_demo_fake_crc_0000000000000000",
        "FHIR_BASE_URL": "https://your-fhir-host.example.com",
        "FHIR_TOKEN_URL": "https://your-fhir-host.example.com/oauth2/token",
        "FHIR_CLIENT_ID": "your-client-id",
        "FHIR_CLIENT_SECRET": "your-client-secret",
        "SLACK_BOT_TOKEN": "xoxb-your-bot-token"
      }
    }
  }
}
```

Restart the client after saving. All three tools should appear in its tool list:
`get_protocol_criteria`, `query_patient_cohort` and `dispatch_screening_alert`.

`TRIALBRIDGE_API_KEY` is fixed for the life of the launched process. It identifies which
credential the process runs as. You do not re-type it per call. To act as a different role, for
example to see an access denial, change this value to another key in your seeded file and
restart the client.

## Web application (v2)

The web app is a second, separate inbound surface. It is a server-rendered FastAPI, Jinja2
and htmx app. It gives a CRC or PI a browser view of the same gateway logic that the three MCP
tools use: sign-in, protocol criteria and cohort screening with an in-app scorecard.

The web app runs as its own OS process, separate from the MCP stdio process. The two share no
state. Each has its own FHIR circuit-breaker instance.

Run the web app as a single ASGI worker. Never use more than one worker. The login-lockout
counter and the scorecard store's check-then-append lock both live in process memory, not in a
shared store.

```bash
.venv/bin/python -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000 --workers 1
```

The web process needs the same shared config as the MCP process: `TRIALBRIDGE_KEYS_FILE`,
`TRIALBRIDGE_AUDIT_LOG_PATH` and `TRIALBRIDGE_API_KEY`. It uses the same key→role file and
audit log. It also needs four web-only variables: `TRIALBRIDGE_SESSION_SECRET`,
`TRIALBRIDGE_SCORECARD_STORE_PATH`, `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL`. At startup, the
web process checks those four. If one is unset or unwritable, it refuses to start and names the
variable on stderr. See the [environment variables table](#environment-variables).

```bash
export TRIALBRIDGE_KEYS_FILE=/absolute/path/to/trial-cohort-builder/keys.json
export TRIALBRIDGE_AUDIT_LOG_PATH=/absolute/path/to/trial-cohort-builder/logs/audit.log
export TRIALBRIDGE_API_KEY=tb_demo_fake_crc_0000000000000000
export TRIALBRIDGE_SESSION_SECRET=tb_demo_fake_session_secret_0000
export TRIALBRIDGE_SCORECARD_STORE_PATH=/absolute/path/to/trial-cohort-builder/logs/scorecards.jsonl
export ANTHROPIC_API_KEY=tb_demo_fake_anthropic_key
export ANTHROPIC_MODEL=claude-opus-5
```

### Sign-in and sessions

Sign-in uses the same per-role static API keys as the MCP surface. Enter a CRC or PI key from
your seeded `keys.json`. See [Seeding the key→role file](#seeding-the-keyrole-file). The web
app has no separate credential. The gateway rate-limits repeated failed sign-ins.

The session cookie is `HttpOnly` and `SameSite=Lax`. It is `Secure` by default. A browser drops
a `Secure` cookie that arrives over plain HTTP, so sign-in does not persist. Browsers and
`curl` treat `http://localhost` as a secure origin, so `localhost` works over plain HTTP. Any
other hostname must terminate TLS in front of the app. For the demo stack's one setting, see
[Session cookie over plain HTTP](#session-cookie-over-plain-http).

The app re-derives the role from the key file on every request and never caches it. If you
rotate a key or remove it from `keys.json`, that session ends on its next request. The user
returns to the sign-in page. No server restart is needed.

Signing in opens the Scorecards list, which is the home screen. Sign out is on the right of the
role banner. It ends that browser's session and returns to the sign-in page with the notice
"Signed out."

### Cohort screening

Screening follows a fixed anchor rule. You do not choose the code.

- The condition code that queries the cohort is the first `VERIFIED` ICD-10-CM inclusion
  criterion, in protocol order.
- If the protocol has one, the first `VERIFIED` LOINC inclusion criterion supplies the
  observation value.

A trial with no `VERIFIED` ICD-10-CM inclusion criterion gives a criteria-only result. The
criteria list still shows each `VERIFIED` or `UNMAPPED` status. The app runs no cohort query,
makes no LLM proposal call and saves no scorecard.

**Partial label.** When the saved criteria hold N verified exclusions that the scorer does not
evaluate, the scorecard, the saved scorecard page and the PDF show
`Partial: N exclusions not evaluated` (`1 exclusion` for one). A sentence follows: "ELIGIBLE
means no evaluated criterion failed. It does not mean no exclusion applies."

The label shows whatever the candidate statuses are, and it appears next to the coverage
warning when both apply. The app counts the exclusions when it renders the page, so an older
saved scorecard shows the label too. The synthetic protocol `NCT99999999` shows no label.

### Finalizing a scorecard

A PI finalizes a screened scorecard with Approve / Finalize on the scorecard page. A
confirmation step restates the scorecard first. The request carries only the scorecard
reference and its NCT ID, and both must match the saved scorecard.

- Finalizing is irreversible. The app rejects a second finalize of the same reference.
- Finalizing appends one approval record to the scorecard store. The record links to the
  scorecard by reference. The store is never rotated.
- Finalizing sends no Slack message and no other notification. The MCP surface's PI Slack
  dispatch is a separate surface and is unchanged.
- A CRC session sees a handoff note instead of the control. The app refuses a direct finalize
  request from a CRC with 403.

Every web response forbids framing.

### Browsing scorecards

Every signed-in page links to Scorecards (`/web/scorecards`) from its role banner. Any CRC or PI
session sees every saved scorecard there, newest first. It does not matter which role or
session screened it.

Each row shows:

- The NCT ID and trial title.
- When the scorecard was screened and by which role.
- Its state: SCREENED, or FINALIZED with the finalizing PI and time.

The list filters to All, Awaiting approval (SCREENED) or Finalized. A search box narrows the
list by NCT ID or trial title as you type. Both run on the server against a fresh read of the
scorecard store on every request. Neither uses a cache.

Each scorecard opens at its own stable link, `/web/scorecards/<ref>`. The page shows exactly
what the app saved. It never recomputes a scorecard on open, and a banner says so. The banner
turns amber when the scorecard is more than 7 days old.

### Re-running screening

A scorecard that is not yet finalized offers Re-run screening to CRCs and PIs alike. Open it
from Scorecards or from the scorecard's link.

A re-run repeats the whole screening live for the same NCT ID. The app re-fetches and
re-verifies the criteria, re-queries the cohort and re-requests the eligibility proposals. It
saves the result as a new scorecard with its own reference. The role that ran the re-run is the
role that screened the new scorecard.

- The original scorecard is never modified.
- The app never merges repeated re-runs.
- If the live run stops before scoring, the app saves no new scorecard. This happens for a
  criteria-only result, a degraded upstream or an unavailable proposal. A fresh screening
  behaves the same way.
- Once a scorecard is finalized, the control is gone. The app refuses a direct re-run request
  with 409 and changes nothing.
- A re-run sends no notification.

### PDF export

Download PDF is on every scorecard page, whether fresh, saved or finalized. CRCs and PIs can
both use it.

- **Content.** The server generates the PDF from the saved scorecard, with no live re-query.
  It carries the same content as the page: the heading, the FINALIZED stamp (when finalized),
  the criteria grouped with their status and code, and every candidate with its evidence. A
  scorecard with a [partial label](#cohort-screening) prints the same label and sentence under
  the heading. The PDF adds the synthetic-data disclaimer on every page.
- **Read-only.** An export never changes a scorecard's SCREENED or FINALIZED state. It writes
  nothing to the scorecard store or the audit log, so an export leaves no audit line.
- **Caching.** The response carries `Cache-Control: no-store`.
- **States.** While the app makes the PDF, the control reads "Preparing PDF…". If the app cannot
  make it, the page shows "PDF could not be generated." with a Retry.
- **No JavaScript needed.** The control is a plain link to `/web/scorecards/<ref>/pdf`.
- **Symbols.** Clinical symbols (β, ≥, →, dashes, curly quotes) print as stored. The PDF uses
  the vendored DejaVu Sans 2.37. Its license is in `src/services/fonts/LICENSE`.

### UI conformance check

The conformance check renders every mapped mockup state and its built page in headless
Chromium. It compares layout (region order, alignment and spacing), not text. It is opt-in.
The default test run never launches a browser.

Prerequisites:

- The dev extra, which installs Playwright: `uv pip install -e ".[dev]"`.
- The browser build: `.venv/bin/python -m playwright install chromium`.
- On Ubuntu 23.10 and later, Chromium's sandbox also needs
  `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. The setting lasts until
  reboot.

Run it with:

```bash
.venv/bin/python -m pytest -m ui_conformance
```

The check needs no keys, cookies or `.env` values. It writes fixture keys to a temp file and
signs in through the real login form. A failing state writes side-by-side images to
`tests/ui_conformance/reports/`. Git ignores that directory.

## Access control and role resolution

**How the caller's role is resolved.** The gateway looks up `TRIALBRIDGE_API_KEY` in the
key→role file named by `TRIALBRIDGE_KEYS_FILE`. It re-reads that file from disk on every tool
call. No code under `src/services/` caches it, and `tests/unit/test_no_caching_layer.py`
enforces this statically. An edit to the key file takes effect on that key's next call, with
no restart.

The caller's role never comes from the client or the request. No tool input schema has a role
field, and no MCP tool signature accepts one.

A key that resolves to `SITE_ADMIN`, or a key that is absent from the file, is denied on every
tool (JSON-RPC error code `-32003`). A denial is a response, not a shutdown. The gateway keeps
running. `SITE_ADMIN` has no MCP tool that touches cohort data or dispatch. Audit review is
direct filesystem access, outside the MCP interface.

| Tool | CRC | PI | SITE_ADMIN |
|---|---|---|---|
| `get_protocol_criteria` | ✅ | ✅ | ❌ |
| `query_patient_cohort` | ✅ | ✅ | ❌ |
| `dispatch_screening_alert` | ✅ (draft preview only, never posts to Slack) | ✅ (posts to Slack) | ❌ |

## Audit log

The gateway writes every access denial as one append-only JSON line to the audit log
(`TRIALBRIDGE_AUDIT_LOG_PATH`, default `./logs/audit.log`). Each line holds a timestamp, the
resolved role (`null` if none resolved), the event type and a human-readable detail. It never
holds the raw API key. The log rotates weekly and keeps the last 4 rotations.

**No MCP tool exposes the audit log's contents to any role.** You review it by direct
filesystem access only. This is a deliberate scope boundary, not a missing feature.

## Graceful degradation

Every upstream dependency fails loudly and predictably. The gateway never hangs, caches or
crashes.

| Upstream | Failure handling | Result on exhaustion |
|---|---|---|
| ClinicalTrials.gov | Retries only on HTTP 429: 4 attempts in total (1 initial, 3 retries), with waits of 1, 2 and 4 seconds | `DEGRADED` envelope |
| NLM terminology service | Retries transport and status failures: 4 attempts in total, with waits of 1, 2 and 4 seconds | Affected criteria fall through to `UNMAPPED` |
| Self-hosted FHIR store | Circuit breaker: trips after 3 consecutive failures, waits 30 s, then allows a single half-open recovery probe. A failed probe restarts the 30 s wait | `DEGRADED` envelope, returned immediately with zero live requests while the breaker is open |
| Slack | No retry. One outbound call, by design | The gateway writes a 401 or other failure to the audit log and returns an actionable error |

The gateway validates every upstream payload through strict Pydantic v2 models before it
reaches the LLM or the caller. It rejects a malformed response with a handled error, never an
unhandled exception.

**The gateway has no caching layer.** `DEGRADED` is the only fallback under failure.
`tests/unit/test_no_caching_layer.py` enforces this statically.

**Known limitation:** the FHIR circuit breaker's "single live half-open probe" guarantee is a
plain check with no lock. Concurrent callers in the same post-cooldown window can each pass
through as a probe. This causes no hang, crash or stale result. The worst case is more than one
live probe instead of one. The gap matters only if this deployment's MCP client issues
concurrent tool calls.

## Live-trial demo (MCP)

`get_protocol_criteria` fetches a trial's inclusion and exclusion criteria from
ClinicalTrials.gov and splits them into criteria. It verifies each criterion against a real
NLM Clinical Table Search Service lookup, never an LLM guess. Two real, registered trials
demonstrate it:

- **`NCT01370005`**: "12 Week Efficacy and Safety Study of Empagliflozin (BI 10773) in
  Hypertensive Patients With Type 2 Diabetes Mellitus." It has clean, single-concept diagnosis
  and lab criteria, so it shows the VERIFIED and UNMAPPED mechanism honestly. Most of its
  criteria do **not** verify. Live data gives 1 VERIFIED and 5 UNMAPPED. See
  [Observed counts](#observed-counts-run-date-2026-09-14), which also explains the one VERIFIED
  criterion. The five UNMAPPED criteria include two LOINC-shaped lab criteria. They fail the
  locked 0.80 similarity threshold or are compound bullets. Both are honest UNMAPPED outcomes,
  not defects.
- **`NCT01779336`**: "Clinical Study of Oral IGF-1R Inhibitor in Subjects With Advanced
  Refractory Solid Tumors." It is an oncology-style protocol. Its compound bullets, for
  example four conditions in one exclusion criterion, genuinely produce UNMAPPED flags. It also
  exercises the nested sub-list edge case: a parent header with five LOINC-shaped lab-value
  sub-items.

### Reproduce the live run

The automated test suite never makes a live call. Every test routes through recorded fixtures
(`mock_upstreams` in `tests/conftest.py`). To reproduce the demo against the real upstreams,
you need outbound network access to `clinicaltrials.gov` and `clinicaltables.nlm.nih.gov`:

```bash
.venv/bin/python -c "
import asyncio

from src.tools.get_protocol_criteria import get_protocol_criteria


async def main():
    for nct_id in ('NCT01370005', 'NCT01779336'):
        result = await get_protocol_criteria(nct_id)
        inclusion = result.inclusion_criteria
        exclusion = result.exclusion_criteria
        all_criteria = inclusion + exclusion
        verified = [c for c in all_criteria if c.mapping_status.value == 'VERIFIED']
        unmapped = [c for c in all_criteria if c.mapping_status.value == 'UNMAPPED']
        print(f'{nct_id}: inclusion={len(inclusion)} exclusion={len(exclusion)} VERIFIED={len(verified)} UNMAPPED={len(unmapped)}')
        for c in verified:
            print(f'  VERIFIED [{c.kind.value}] {c.raw_text!r} -> {c.code_system.value} {c.code}')


asyncio.run(main())
"
```

To run `query_patient_cohort` and `dispatch_screening_alert` end to end, finish
[FHIR store setup](#fhir-store-setup). For PI dispatch, also finish
[Slack setup](#slack-setup). Then connect an MCP client. See
[Configuring an MCP client](#configuring-an-mcp-client-claude-desktopcode). Then:

1. Call `get_protocol_criteria` with `NCT01370005`. It is the only demo trial with a `VERIFIED`
   condition code. The call returns `inclusion_criteria` and `exclusion_criteria`.
2. Call `query_patient_cohort` with that `VERIFIED` code as `condition_code`, and with the
   `inclusion_criteria` and `exclusion_criteria` lists from step 1. `observation_loinc` is
   optional. Add a `VERIFIED` LOINC code if the trial has one.
3. Call `dispatch_screening_alert` with the resulting `nct_id` and candidates. Use a CRC key
   for a draft preview. Use a PI key with a valid `channel_id` to post to Slack.

### Observed counts (run date: 2026-09-14)

Registry data can change. These counts are what the run observed on the date above. They are
not a permanent guarantee. `scripts/capture_fixtures.py` refreshes every recorded test fixture
from the live APIs on demand. If a count differs, re-run the command above and
`scripts/capture_fixtures.py` to re-establish the numbers.

| NCT ID | Inclusion | Exclusion | VERIFIED | UNMAPPED |
|---|---|---|---|---|
| `NCT01370005` | 3 | 3 | 1 | 5 |
| `NCT01779336` | 16 | 15 | 0 | 31 |

The one VERIFIED criterion in `NCT01370005` is an exclusion criterion: "Known or suspected
secondary hypertension" maps to `ICD10CM` `I10`.

**Caveat:** `I10` is "Essential (primary) hypertension". It is the ICD-10-CM category for
hypertension *without* an identifiable secondary cause. The criterion names the `I15.x`
"Secondary hypertension" category, not `I10`. The code exists, and a real terminology lookup
returned it. That satisfies the requirement that every code comes from a real lookup. But the
0.80 token-set similarity check passed on a superset match: "secondary hypertension" contains
every word of the returned display name "Hypertension". Token-set matching cannot tell sibling
clinical sub-categories apart. The project locks the threshold at 0.80.

### Which vocabulary the non-LOINC codes belong to

The non-LOINC `code_system` value is **ICD-10-CM**, not SNOMED-CT. The gateway uses the NLM
Clinical Table Search Service. It is free, needs no credentials and is the endpoint the PRD
mandates. Its `conditions` table has no SNOMED-CT data. Its API documentation and table listing
offer only ICD-10-CM and ICD-9-CM condition codes. The field name matches what the API returns.
You can check a returned code against a vocabulary browser such as [icd10data.com](https://www.icd10data.com/).

### A non-zero UNMAPPED count is the designed outcome

A trial with zero UNMAPPED criteria would be suspicious, not impressive. The project never
optimizes toward it. Compound criteria, which name several conditions in one bullet, are the
main source of UNMAPPED flags. Decomposing them is out of scope for v1. The 0.80 similarity
threshold is locked. The project never lowers it, bypasses it or makes it configurable to reduce this count.

## Project status

Five milestones have shipped:

- **v1.0 MVP** shipped on 2026-09-19. It covers all five roadmap phases and all 15 v1
  requirements (`CRIT-01`/`02`, `COHT-01`/`02`, `SCORE-01`, `RBAC-01`–`05`, `DEGR-01`–`05`).
  Each requirement is complete and verified.
- **v2.0 Web Application Interface** shipped on 2026-09-29. It added the web app foundation
  and screening, PI approval and finalization, scorecard browsing and re-run, page chrome, UI
  conformance to the UX spec and scorecard PDF export.
- **v2.2 Demo Stack** shipped on 2026-10-04. It added the one-command demo stack, the
  Quickstart, `make verify`, honest labeling and terminology-outage conformance.
- **v2.3 Limited demo support for real NCT trials** shipped on 2026-10-06. It added a
  reviewed HbA1c lab-code table, mapper fixes for real-trial wording, partial seeds with a `make verify` check, and the "Partial: N exclusions not evaluated" label on the scorecard, detail view and PDF. Three real trials (NCT00996294, NCT04739241, NCT05591391) seed, verify and replay offline. A real-trial seed is partial. The synthetic protocol is the fully represented demo. A store holds one trial at a time, because the cohort query does not filter by protocol. Run `make reset` between trials.

The project deliberately leaves these items out of scope: compound-criteria decomposition, conversational audit-log access, Slack inbound triggers, any caching layer and enterprise IdP auth.


