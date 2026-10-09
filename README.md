# TrialBridge

TrialBridge turns a clinical trial's NCT ID into a scored list of candidate patients. It reads the trial's eligibility criteria from ClinicalTrials.gov. It then screens patients in a FHIR store against those criteria.

This repository runs a synthetic demo. The stack holds a HAPI FHIR server filled with synthetic patients. You can screen the demo trial in a web app on your own machine.

## What you need

- Docker Engine with Compose v2. Run `docker compose version` to check.
- GNU Make.
- Free host ports 8000, 8080 and 8081.
- About 4 GB of free RAM.
- Internet access. The first start pulls about 1.5 GB of images.

## Run the demo

1. Clone the repository.

   ```bash
   git clone https://github.com/jasonrwise/trial-cohort-builder
   cd trial-cohort-builder
   ```

2. Copy the example environment file.

   ```bash
   cp .env.example .env
   ```

3. Start the stack.

   ```bash
   make up
   ```

   The first start builds the web image and pulls the other images. When all four services are healthy, `make up` prints their URLs.

4. Load the demo trial.

   ```bash
   make load NCT=NCT99999999
   ```

   This writes 35 synthetic patients to the FHIR store. It does not need the internet.

5. Open `http://127.0.0.1:8000`. Sign in with the demo key `tb_demo_fake_pi_0000000000000000`. Enter `NCT99999999` to screen the trial.

6. Stop the stack when you finish.

   ```bash
   make down
   ```

   `make down` keeps all data. To delete the data and start again from an empty stack, run `make reset`. The command asks you to type `reset` before it deletes anything.

## What the demo does not do

The demo keys in `keys.example.json` are public placeholders. They are not an identity system. Anyone who reads this repository knows them.

No real patient data is involved. Every patient in the demo is synthetic.

## Screening needs an Anthropic key

The placeholder `ANTHROPIC_API_KEY` in `.env` lets the web app start. It does not let you screen. To run a screening, put your own Anthropic API key in `.env`. Then run `make up` again so the web app reads the new value. Git ignores `.env`, so the key stays on your machine.

## Check the demo data

Run `make verify` after you screen `NCT99999999`. The command compares the seeded patients and the newest saved scorecard with the expected counts. It fails when no scorecard exists, so screen first.

## Use the MCP server from Claude

The repository also holds an MCP server for Claude Desktop and Claude Code. The file [.mcp.json.example](.mcp.json.example) shows the client entry. Copy it into your client's configuration. Replace `/path/to/trial-cohort-builder` with the path of your clone. The server needs Python 3.11 or later. Create the virtual environment the entry points to:

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

Restart the client after you save the configuration.

## License

TrialBridge uses the MIT license. See [LICENSE](LICENSE).
