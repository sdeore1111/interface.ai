"""
Automation-side half of the handoff (see escalation/state.py for the
shared state format and escalation/operator_cli.py for the human/operator
side).

Call `escalate(...)` from wherever the system decides it's stuck:
  - agent/loop.py, when the model calls give_up()
  - a replay driver, when ReplayEngine returns HARD_FAILURE and the caller
    has opted into human escalation on failure

What it does, in order:
  1. Write the intervention request (why we stopped, current URL/step,
     a screenshot) so the operator has real context, not just "help."
  2. Flip control_state to "human" and print/log where the operator_cli
     should connect (the CDP endpoint of the browser this run is using --
     the actual live session, not a new tab).
  3. Block, polling control_state, until the operator sets it back to
     "automation" (or the wait times out -- a real deployment would also
     page/alert here; out of scope for this project).
  4. Read back whatever Steps the operator performed (human_steps.json)
     so the caller can fold them into the artifact being built, and
     return a HandoffResult the caller uses to decide whether/how to
     resume.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.evidence import EvidenceRun
from core.surface import PlaywrightSurface
from escalation.state import EscalationContext, EscalationState


@dataclass
class HandoffResult:
    resolved: bool
    notes: str
    human_steps: List[dict] = field(default_factory=list)
    wait_seconds: float = 0.0


def escalate(
    state_dir: str,
    cdp_endpoint: str,
    surface: PlaywrightSurface,
    evidence: EvidenceRun,
    ctx: EscalationContext,
    timeout_s: float = 600,
) -> HandoffResult:
    state = EscalationState(state_dir)
    state.write_cdp_endpoint(cdp_endpoint)
    state.write_request(ctx)

    screenshot_path = evidence.save_screenshot(surface, "escalation_trigger.png")
    ctx.screenshot_path = screenshot_path
    state.write_request(ctx)  # rewrite with the screenshot path now known

    evidence.event("escalation_requested", {
        "capability_id": ctx.capability_id, "reason": ctx.reason,
        "current_url": ctx.current_url, "current_step_index": ctx.current_step_index,
        "cdp_endpoint": cdp_endpoint, "state_dir": state_dir,
    })
    state.set_controller("human", notes="awaiting operator")

    print(f"\n[ESCALATION] Human intervention requested: {ctx.reason}")
    print(f"[ESCALATION] Operator should run:\n"
          f"    python -m escalation.operator_cli --state-dir {state_dir}\n"
          f"[ESCALATION] Waiting for operator to resume (timeout={timeout_s}s)...\n")

    start = time.time()
    try:
        final_state = state.wait_for_controller("automation", timeout_s=timeout_s)
    except TimeoutError:
        evidence.event("escalation_timed_out", {"waited_s": time.time() - start})
        return HandoffResult(resolved=False, notes="Escalation timed out waiting for a human operator.",
                              wait_seconds=time.time() - start)

    human_steps = state.read_human_steps()
    evidence.event("escalation_resolved", {
        "notes": final_state.get("notes", ""), "human_step_count": len(human_steps),
        "waited_s": time.time() - start,
    })
    return HandoffResult(
        resolved=True, notes=final_state.get("notes", ""),
        human_steps=human_steps, wait_seconds=time.time() - start,
    )
