"""
Shared on-disk state for the escalation handoff.

Why a file, not just an in-memory flag: the whole point of this module is
that control of the live browser session moves to a SEPARATE process (a
real human operator, running escalation/operator_cli.py in their own
terminal, attached to the same browser over the Chrome DevTools Protocol
-- not a fresh browser). Two processes need a shared, durable answer to
"who is in control right now," so it lives on disk, not in a variable
that only one of the two processes could see.

Layout, under a per-run directory (see evidence.EvidenceRun.run_dir):
  control_state.json      {"controller": "automation"|"human", "since": ts, "notes": str}
  intervention_request.json   context handed to the operator (why we stopped, where, screenshot)
  cdp_endpoint.txt         ws:// endpoint the operator_cli connects to (same browser, same page)
  human_steps.json         actions the operator performed, structured so the
                            automation process can fold them back into the
                            artifact being recorded (see agent/loop.py).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EscalationContext:
    capability_id: str
    goal: str
    current_step_index: int
    current_url: str
    reason: str
    screenshot_path: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class EscalationState:
    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.control_path = os.path.join(state_dir, "control_state.json")
        self.request_path = os.path.join(state_dir, "intervention_request.json")
        self.cdp_path = os.path.join(state_dir, "cdp_endpoint.txt")
        self.human_steps_path = os.path.join(state_dir, "human_steps.json")

    # -- control state --------------------------------------------------

    def set_controller(self, controller: str, notes: str = ""):
        with open(self.control_path, "w") as f:
            json.dump({"controller": controller, "since": time.time(), "notes": notes}, f, indent=2)

    def get_controller(self) -> Optional[dict]:
        if not os.path.exists(self.control_path):
            return None
        with open(self.control_path) as f:
            return json.load(f)

    def wait_for_controller(self, expected: str, timeout_s: float = 600, poll_s: float = 0.5) -> dict:
        start = time.time()
        while time.time() - start < timeout_s:
            state = self.get_controller()
            if state and state.get("controller") == expected:
                return state
            time.sleep(poll_s)
        raise TimeoutError(f"Timed out waiting for controller={expected!r} after {timeout_s}s")

    # -- intervention request --------------------------------------------

    def write_request(self, ctx: EscalationContext):
        with open(self.request_path, "w") as f:
            json.dump(asdict(ctx), f, indent=2)

    def read_request(self) -> Optional[dict]:
        if not os.path.exists(self.request_path):
            return None
        with open(self.request_path) as f:
            return json.load(f)

    # -- CDP endpoint (how the operator attaches to the SAME session) ----

    def write_cdp_endpoint(self, endpoint: str):
        with open(self.cdp_path, "w") as f:
            f.write(endpoint)

    def read_cdp_endpoint(self) -> Optional[str]:
        if not os.path.exists(self.cdp_path):
            return None
        with open(self.cdp_path) as f:
            return f.read().strip()

    # -- what the human did ----------------------------------------------

    def write_human_steps(self, steps: List[dict]):
        with open(self.human_steps_path, "w") as f:
            json.dump(steps, f, indent=2)

    def read_human_steps(self) -> List[dict]:
        if not os.path.exists(self.human_steps_path):
            return []
        with open(self.human_steps_path) as f:
            return json.load(f)
