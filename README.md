# Computer-Use Automation System

A small, real implementation of interface.ai's take-home brief: an LLM discovers how to
operate a legacy back-office web app once, records what it learned as a typed, versioned
capability artifact, and a separate, deterministic engine replays that artifact in
production with no model in the loop — including real runtime-error handling and a
human-escalation path with genuine live-session handoff.

See **`/REPORT.md`** for the design write-up (architecture, schema, error handling,
heterogeneity/multi-tenant story, escalation model, safety model, and cuts).
See **`/evidence/`** for real run output (discovery, replay success, replay errors,
escalation) — including logs and the recorded artifact.

## Setup

Requires Python 3.11+ and an Anthropic API key.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install flask playwright anthropic pydantic python-dotenv requests pytest

# Chromium: if `playwright install chromium` works in your environment, use that.
# This project also auto-detects a pre-installed Chromium at
# /opt/pw-browsers/chromium-*/chrome-linux/chrome if present (set CUA_CHROMIUM_PATH
# to override the path explicitly).
playwright install chromium
```

Create a `.env` (never committed — see `.gitignore`) or export directly:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export MERIDIAN_TELLER_USERNAME=teller1     # mock app's own fixed demo login
export MERIDIAN_TELLER_PASSWORD=demo-pass
```

The mock target app (`app/server.py`, a Flask "Meridian Credit Union — Teller Console")
is started automatically by both driver scripts if nothing is already listening on
`:5055`. To run it yourself: `python app/server.py`.

Run tests (no browser or API key needed — these cover pure logic: schema validation,
the safety gate, template rendering/param validation):
```bash
python -m pytest tests/ -v
```

## Demo path

**1. Discovery** — a real, LLM-driven run that logs in, looks up a member, opens a
sub-account, and reaches the confirmation screen, then saves a capability artifact:

```bash
python run_discovery.py
```
Prints a summary and writes evidence + the artifact to
`artifacts/store/open_sub_account.latest.json`.

**2. Replay** — deterministic execution of the saved artifact, no LLM call:

```bash
# success
python run_replay.py open_sub_account \
  --params '{"member_id":"12345","account_type":"SAVINGS_SUB","opening_deposit":150}' \
  --allow-confirm

# a legitimate business outcome, not a crash
python run_replay.py open_sub_account \
  --params '{"member_id":"99999","account_type":"SAVINGS_SUB","opening_deposit":150}' \
  --allow-confirm
```
`--allow-confirm` authorizes this invocation to execute steps the artifact marked
`requires_confirmation` (see REPORT.md "Safety"). Omit it and that replay stops with a
`POLICY_DENIED` hard failure instead — the safe default.

**3. Escalation demo** — a discovery run that hits a screen the agent is explicitly not
authorized to click through on its own, escalates to a human operator attached to the
*same* live browser session over CDP, and resumes once the human acts:

```bash
python -m playwright install chromium   # if not already installed

CUA_GOAL_CASE=override_escalation \
CUA_EVIDENCE_DIR=evidence/discovery_run_escalation \
python run_discovery.py
```
This will print:
```
[ESCALATION] Operator should run:
    python -m escalation.operator_cli --state-dir evidence/discovery_run_escalation/escalation_state
```
Run that command **in a second terminal** to act as the human operator interactively
(`observe`, `click <key>`, `type <key> <text>`, `select <key> <value>`, `resume [notes]`).
For a non-interactive/reproducible run (what `/evidence/` was generated with, since this
sandbox has no second terminal), set `CUA_ESCALATION_AUTO=evidence/escalation_operator_commands.json`
instead — same code path, a scripted command list rather than a human typing.

## Repository layout

```
app/                mock target: legacy-flavored credit-union back office (Flask)
core/                surface.py: the perception/action seam (Playwright-backed today)
                     evidence.py: structured run logging
                     browser.py: Chromium launch helper
artifacts/           schema.py: the capability artifact contract
                     store.py: minimal JSON file-based artifact storage
agent/                loop.py: the LLM discovery agent (observe/decide/act)
                     tools.py: the tool schema exposed to the model
                     enrich.py: post-discovery outcome-probe authoring
replay/               engine.py: deterministic, LLM-free replay executor
safety/               policy.py: allowlist + risk gating + redaction
                     allowlist.json: this project's concrete policy config
escalation/           state.py, handoff.py: automation-side handoff
                     operator_cli.py: human/operator-side console (real CDP client)
evidence/            real run output (see evidence/README.md)
tests/               unit tests (schema, safety gate, replay templating)
run_discovery.py     discovery demo entrypoint
run_replay.py        replay demo entrypoint
```

## What's mocked, and why

- **The target app** is a purpose-built local Flask app, not a real bank system (the
  brief explicitly says not to try to obtain one) or a public site (avoids ToS/rate-limit
  risk and, more importantly, lets me *inject* the exact runtime error conditions the
  brief cares about — not-found, validation, permission denial, a confirmation
  interstitial — on demand, deterministically).
- **The operator console** is a CLI, not a graphical co-browsing UI (explicitly out of
  scope per Section 3.6). It is a real, separately-runnable program that attaches to the
  live browser over CDP — the mechanism is real, only the UI polish is stubbed.
- **The operator's specific actions** in `/evidence/` are scripted rather than typed live
  by a person, because this was generated in a non-interactive sandbox. The script talks
  to the exact same `operator_cli.py` a human runs — see `evidence/README.md`.

Full reasoning for these and other trade-offs is in `/REPORT.md`.
