#!/usr/bin/env python3
"""
Demo entrypoint: deterministic replay of a saved capability artifact --
no LLM call anywhere in this path.

    python run_replay.py open_sub_account --params '{"member_id":"12345","account_type":"SAVINGS_SUB","opening_deposit":150}'
    python run_replay.py open_sub_account --params '{"member_id":"99999","account_type":"SAVINGS_SUB","opening_deposit":150}'   # not-found business outcome
    python run_replay.py open_sub_account --params '{"member_id":"12345","account_type":"SAVINGS_SUB","opening_deposit":6000}'  # recoverable override interstitial
    python run_replay.py open_sub_account --params '{"member_id":"12345","account_type":"SAVINGS_SUB","opening_deposit":-5}'    # validation business outcome

Add --allow-confirm to permit REQUIRES_CONFIRMATION steps to execute
(the production analogue of the calling agent/product having already
authorized this specific invocation).
"""
import argparse
import json
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

from artifacts.store import load as load_artifact
from core.browser import chromium_launch_kwargs
from core.evidence import EvidenceRun
from core.surface import PlaywrightSurface
from replay.engine import ReplayEngine
from safety.policy import AllowlistConfig, SafetyGate

BASE_URL = "http://127.0.0.1:5055"

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("capability_id")
    parser.add_argument("--params", required=True, help="JSON object of input parameters.")
    parser.add_argument("--allow-confirm", action="store_true")
    parser.add_argument("--evidence-subdir", default=None,
                         help="Name for the evidence run directory; auto-generated if omitted.")
    args = parser.parse_args()
    params = json.loads(args.params)

    mock_proc = ensure_mock_app_running()

    artifact = load_artifact(args.capability_id)

    run_id = args.evidence_subdir or f"replay_{args.capability_id}_{int(time.time())}"
    evidence = EvidenceRun(os.path.join("evidence", "replay_runs", run_id), "replay", run_id)

    cfg = AllowlistConfig.from_file("safety/allowlist.json")
    gate = SafetyGate(cfg, allow_confirmation_steps=args.allow_confirm)

    with sync_playwright() as p:
        browser = p.chromium.launch(**chromium_launch_kwargs())
        page = browser.new_page()
        surface = PlaywrightSurface(page, gate)
        surface.navigate(artifact.target.base_url + artifact.target.entry_path)

        engine = ReplayEngine(artifact=artifact, surface=surface, evidence=evidence, safety_gate=gate)
        result = engine.run(params)

        browser.close()

    print(f"\nOutcome : {result.outcome}")
    print(f"Code    : {result.code}")
    print(f"Message : {result.message}")
    print(f"Outputs : {result.outputs}")
    if result.debug:
        print(f"Debug   : {json.dumps(result.debug, indent=2)}")
    print(f"Evidence -> {evidence.run_dir}")

    if mock_proc:
        mock_proc.terminate()

    sys.exit(0 if result.outcome != "HARD_FAILURE" else 2)


if __name__ == "__main__":
    main()
