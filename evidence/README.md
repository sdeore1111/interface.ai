# Evidence

All runs here are real: real Claude API calls, a real headless Chromium browser,
against the mock app in `/app`. No output in this directory is hand-authored or simulated
data — every `log.jsonl` is the literal event stream a run produced.

## `discovery_run/`
The required real, LLM-driven discovery run (Section 4: "the discovery run has to be
real"). Goal: log in, look up member 12345, open a $150 Savings Sub-Account, reach the
confirmation screen. 10 steps, fully autonomous, no human intervention. Produced
`artifact.json`, the capability later replayed below.

Reproduce: `python run_discovery.py`

## `discovery_run_escalation/`
A second discovery run demonstrating the human-in-the-loop escalation path end to end
(Section 3.6). Goal: open a $6,000 sub-account, which trips the app's own "supervisor
override required" interstitial. The agent is explicitly instructed it is not authorized
to confirm that screen itself, so it calls `give_up()`. The harness then:

1. Writes `escalation_state/intervention_request.json` (why it stopped, current URL, a
   screenshot) and flips `escalation_state/control_state.json` to `"human"`.
2. A **separate OS process** (`escalation/operator_cli.py`) connects to the *same*
   Chromium instance over the Chrome DevTools Protocol (the CDP endpoint is in
   `escalation_state/cdp_endpoint.txt`) — not a fresh browser.
3. The operator clicks "Confirm Override & Continue" on the live page, then `resume`s.
4. Control flips back to `"automation"`; the discovery loop re-observes and finishes the
   goal. The human's click is folded into `artifact.json` as step `s9`, recorded with
   `risk_level: requires_confirmation` — the same gating replay uses for the same class of
   action, so a future unattended replay of this capability still pauses for confirmation.

Because this sandboxed environment has no interactive terminal for a live human, the
operator's actions were scripted (`evidence/escalation_operator_commands.json`) and run
through `escalation/operator_cli.py --auto ...` — the exact same code path, tools, and CDP
connection a real person's interactive session would use (see README.md "Escalation demo"
for how to run it interactively yourself).

Reproduce: `CUA_GOAL_CASE=override_escalation CUA_EVIDENCE_DIR=evidence/discovery_run_escalation CUA_ESCALATION_AUTO=evidence/escalation_operator_commands.json python run_discovery.py`

## `replay_runs/`
Deterministic replays of the `open_sub_account` artifact recorded above. **No LLM call
occurs in any of these.**

| Run | Params | Outcome | What it demonstrates |
|---|---|---|---|
| `success/` | member 12345, $150 | `SUCCESS` | Baseline: replay reproduces the discovery run's result exactly. |
| `not_found/` | member 99999, $150 | `BUSINESS_OUTCOME` / `MEMBER_NOT_FOUND` | A legitimate non-error result, not a crash — the "bad input" demonstration Section 6 asks for. |
| `validation_error/` | member 12345, -$5 | `BUSINESS_OUTCOME` / `VALIDATION_ERROR_DEPOSIT` | Form-level validation surfaced as a typed outcome, not an exception. |
| `recoverable_override/` | member 12345, $6000 | `SUCCESS` (after internal recovery) | The RECOVERABLE bucket: replay hits the same interstitial the discovery agent refused to click through, but here it's a pre-declared, policy-reviewed recovery (`confirm_override`), so it dismisses it and continues automatically, no human needed. |

Reproduce any of them, e.g.:
```
python run_replay.py open_sub_account --params '{"member_id":"99999","account_type":"SAVINGS_SUB","opening_deposit":150}' --allow-confirm
```
