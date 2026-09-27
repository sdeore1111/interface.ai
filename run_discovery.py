#!/usr/bin/env python3
"""
Demo entrypoint: run a real, LLM-driven discovery pass against the mock
target app, and save the resulting capability artifact.

    python run_discovery.py

Requires ANTHROPIC_API_KEY in the environment (see README.md). Starts the
mock app itself if it isn't already listening on :5055.

Env overrides:
    CUA_GOAL_CASE=open_sub_account (default) | restricted_member
        restricted_member drives a deliberate escalation demo: it asks for
        a sub-account on a RESTRICTED member, which the agent cannot
        complete on its own and should route to human escalation.
    CUA_ESCALATION_AUTO=<path to a JSON command list>
        If set, the escalation operator console (a separate process) is
        launched automatically in --auto mode using that command file
        instead of requiring a person at a second terminal. See
        escalation/operator_cli.py and evidence/discovery_run/README for
        why this project scripts the operator for reproducible evidence.
"""
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from agent.enrich import attach_known_outcome_probes
from agent.loop import DiscoveryAgent
from artifacts.schema import ParamSpec, ParamType, TargetSpec
from artifacts.store import save as save_artifact
from core.browser import chromium_launch_kwargs
from core.evidence import EvidenceRun
from core.surface import PlaywrightSurface
from safety.policy import AllowlistConfig, SafetyGate

BASE_URL = "http://127.0.0.1:5055"
CDP_PORT = 9333

os.environ.setdefault("MERIDIAN_TELLER_USERNAME", "teller1")
os.environ.setdefault("MERIDIAN_TELLER_PASSWORD", "demo-pass")


def ensure_mock_app_running():
    import http.client
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 5055, timeout=1)
        conn.request("GET", "/login")
        conn.getresponse()
        return None
    except Exception:
        proc = subprocess.Popen(
            [sys.executable, "app/server.py"],
            stdout=open("/tmp/mockapp.log", "a"), stderr=subprocess.STDOUT,
        )
        time.sleep(1.2)
        return proc


CREDENTIAL_NOTE = (
    "If you land on a sign-in form, use type_env with env_var='MERIDIAN_TELLER_USERNAME' for the "
    "username field and env_var='MERIDIAN_TELLER_PASSWORD' for the password field, then click Sign In."
)


def build_case(case: str):
    if case == "restricted_member":
        return dict(
            capability_id="open_sub_account_restricted_demo",
            goal=(
                "Open a new Savings Sub-Account for member 10003 with an opening deposit of $200, "
                f"and reach the confirmation screen. {CREDENTIAL_NOTE}"
            ),
            member_id="10003", deposit=200,
        )
    if case == "override_escalation":
        return dict(
            capability_id="open_sub_account_override_escalation_demo",
            goal=(
                "Open a new Savings Sub-Account for member 12345 with an opening deposit of $6000, "
                "and reach the confirmation screen. IMPORTANT POLICY: if a supervisor-override "
                "confirmation screen appears, you are NOT authorized to confirm it yourself under any "
                "circumstances -- call give_up() immediately with a clear reason instead of clicking "
                f"anything on that screen. {CREDENTIAL_NOTE}"
            ),
            member_id="12345", deposit=6000,
        )
    return dict(
        capability_id="open_sub_account",
        goal=(
            "Look up member 12345, open a new Savings Sub-Account for them with an opening "
            f"deposit of $150, and reach the confirmation screen. {CREDENTIAL_NOTE}"
        ),
        member_id="12345", deposit=150,
    )


def main():
    case = os.environ.get("CUA_GOAL_CASE", "open_sub_account")
    spec = build_case(case)

    mock_proc = ensure_mock_app_running()

    run_id = f"discovery_{int(time.time())}"
    evidence_dir = os.environ.get("CUA_EVIDENCE_DIR", os.path.join("evidence", "discovery_run"))
    evidence = EvidenceRun(evidence_dir, "discovery", run_id)

    input_params = [
        ParamSpec(name="member_id", type=ParamType.STRING, required=True,
                   description="Credit union member ID to open the sub-account for.",
                   redact_in_logs=True, example="12345"),
        ParamSpec(name="account_type", type=ParamType.STRING, required=True,
                   description="Sub-account type code, e.g. SAVINGS_SUB or CHRISTMAS_CLUB.",
                   example="SAVINGS_SUB"),
        ParamSpec(name="opening_deposit", type=ParamType.NUMBER, required=True,
                   description="Opening deposit amount in USD.", example=150),
    ]
    param_values = {
        "member_id": spec["member_id"],
        "account_type": "SAVINGS_SUB",
        "opening_deposit": spec["deposit"],
    }

    target = TargetSpec(app_id="meridian_teller_console", surface_type="legacy_web",
                         base_url=BASE_URL, entry_path="/login")

    escalation_state_dir = os.path.join(evidence.run_dir, "escalation_state")
    cfg = AllowlistConfig.from_file("safety/allowlist.json")
    gate = SafetyGate(cfg, allow_confirmation_steps=False)

    auto_cmds_path = os.environ.get("CUA_ESCALATION_AUTO")

    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs(cdp_port=CDP_PORT))
        page = browser.new_page()
        surface = PlaywrightSurface(page, gate)
        surface.navigate(f"{BASE_URL}/login")

        cdp_endpoint = f"http://127.0.0.1:{CDP_PORT}"

        agent = DiscoveryAgent(
            surface=surface, evidence=evidence, goal=spec["goal"],
            capability_id=spec["capability_id"], target=target,
            input_params=input_params, param_values=param_values,
            max_steps=20, timeout_s=240,
            risky_labels={"Submit", "Confirm Override & Continue"},
            escalation_state_dir=escalation_state_dir, cdp_endpoint=cdp_endpoint,
        )

        operator_proc = None
        if auto_cmds_path:
            # Launch the SAME operator_cli a human would run, in --auto mode, as a
            # real separate process, only once escalation actually starts (it polls
            # for the state files agent.loop writes when give_up() fires).
            def launch_operator_when_ready():
                from escalation.state import EscalationState
                state = EscalationState(escalation_state_dir)
                for _ in range(600):
                    if state.get_controller() and state.get_controller().get("controller") == "human":
                        break
                    time.sleep(0.5)
                return subprocess.Popen([
                    sys.executable, "-m", "escalation.operator_cli",
                    "--state-dir", escalation_state_dir, "--auto", auto_cmds_path,
                ])
            import threading
            holder = {}
            t = threading.Thread(target=lambda: holder.update(proc=launch_operator_when_ready()))
            t.start()

        result = agent.run()

        if auto_cmds_path:
            t.join(timeout=30)

        browser.close()

    print(f"\nDiscovery result: success={result.success} stop_reason={result.stop_reason} "
          f"steps={result.steps_taken}")
    print(f"Outputs: {result.outputs}")

    if result.success and result.artifact:
        result.artifact = attach_known_outcome_probes(result.artifact)
        path = save_artifact(result.artifact)
        evidence.save_json(result.artifact, "artifact.json")
        print(f"Saved capability artifact -> {path}")
        print(f"Evidence -> {evidence.run_dir}")
    else:
        print(f"No artifact produced (run did not complete the goal). Evidence -> {evidence.run_dir}")

    if mock_proc:
        mock_proc.terminate()


if __name__ == "__main__":
    main()
