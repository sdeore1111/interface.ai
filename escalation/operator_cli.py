"""
Operator console: the human side of the handoff.

Run this in a separate terminal (a separate OS process) while an
escalated discovery or replay run is blocked waiting in
escalation.handoff.escalate(). It connects to the SAME running browser
over the Chrome DevTools Protocol using the endpoint the automation
process wrote to <state-dir>/cdp_endpoint.txt -- this is the live session
the automation was using, not a fresh browser, which is the concrete
requirement in Section 3.6.

Usage (interactive, what a real operator runs):
    python -m escalation.operator_cli --state-dir <path>

Commands:
    observe                 show the current page and actionable elements
    click <key>              click an element by its key
    type <key> <text...>     fill a field
    select <key> <value>     choose a dropdown option
    screenshot               save a screenshot into the state dir
    resume [notes...]        hand control back to automation and exit

Non-interactive mode (used to produce /evidence/ deterministically,
without a live terminal in this environment -- see README for why):
    python -m escalation.operator_cli --state-dir <path> --auto commands.json
runs a fixed list of the same commands instead of reading stdin. The
connection, the actions performed, and everything written to state/evidence
are identical either way; only where the commands come from differs.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

from artifacts.schema import RiskLevel
from core.surface import PlaywrightSurface
from safety.policy import AllowlistConfig, SafetyGate
from escalation.state import EscalationState


def _print_context(state: EscalationState):
    req = state.read_request()
    if req:
        print("=" * 70)
        print(f"Capability : {req.get('capability_id')}")
        print(f"Goal       : {req.get('goal')}")
        print(f"Reason     : {req.get('reason')}")
        print(f"Stopped at : step index {req.get('current_step_index')}, url={req.get('current_url')}")
        if req.get("screenshot_path"):
            print(f"Screenshot : {req.get('screenshot_path')}")
        print("=" * 70)


def _print_observation(surface: PlaywrightSurface):
    obs = surface.observe()
    print(obs.to_prompt_text())


def run(state_dir: str, auto_commands_path: str = None, allowlist_path: str = "safety/allowlist.json"):
    state = EscalationState(state_dir)
    endpoint = state.read_cdp_endpoint()
    if not endpoint:
        print(f"No cdp_endpoint.txt found in {state_dir}; is a run currently escalated?")
        sys.exit(1)

    _print_context(state)

    cfg = AllowlistConfig.from_file(allowlist_path)
    # The human IS the confirmation; the console still enforces the domain/action
    # allowlist as defense in depth, but does not itself gate on risk_level.
    gate = SafetyGate(cfg, allow_confirmation_steps=True)

    human_steps = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        surface = PlaywrightSurface(page, gate)

        print("\nConnected to the live session (same browser, same page).")
        _print_observation(surface)

        def handle(line: str) -> bool:
            """Returns False when the operator has resumed (loop should stop)."""
            parts = line.strip().split(maxsplit=2)
            if not parts:
                return True
            cmd = parts[0].lower()

            if cmd == "observe":
                _print_observation(surface)
                return True

            if cmd in ("click", "type", "select") and len(parts) >= 2:
                key = parts[1]
                elements = surface.scan_actionable_elements()
                el = elements.get(key)
                if el is None:
                    print(f"Unknown key {key!r}. Run 'observe' to see current keys.")
                    return True
                if cmd == "click":
                    surface.click(el.locator)
                    human_steps.append({
                        "action": "click", "description": f"[human] Click '{el.label}'",
                        "locator": el.locator.model_dump(mode="json"), "value_template": None,
                        "risk_level": RiskLevel.REQUIRES_CONFIRMATION.value,
                    })
                    print(f"Clicked '{el.label}'.")
                elif cmd == "type":
                    text = parts[2] if len(parts) > 2 else ""
                    surface.type_text(el.locator, text)
                    human_steps.append({
                        "action": "type", "description": f"[human] Type into '{el.label}'",
                        "locator": el.locator.model_dump(mode="json"), "value_template": text,
                        "risk_level": RiskLevel.REQUIRES_CONFIRMATION.value,
                    })
                    print(f"Typed into '{el.label}'.")
                elif cmd == "select":
                    value = parts[2] if len(parts) > 2 else ""
                    surface.select(el.locator, value)
                    human_steps.append({
                        "action": "select", "description": f"[human] Select in '{el.label}'",
                        "locator": el.locator.model_dump(mode="json"), "value_template": value,
                        "risk_level": RiskLevel.REQUIRES_CONFIRMATION.value,
                    })
                    print(f"Selected in '{el.label}'.")
                _print_observation(surface)
                return True

            if cmd == "screenshot":
                path = f"{state_dir}/operator_screenshot_{int(time.time())}.png"
                surface.screenshot(path)
                print(f"Saved {path}")
                return True

            if cmd == "resume":
                notes = line[len("resume"):].strip()
                state.write_human_steps(human_steps)
                state.set_controller("automation", notes=notes or "resumed by operator")
                print(f"Control returned to automation. {len(human_steps)} human step(s) recorded.")
                return False

            print(f"Unrecognized command: {line!r}")
            return True

        if auto_commands_path:
            with open(auto_commands_path) as f:
                commands = json.load(f)
            for c in commands:
                line = c if isinstance(c, str) else " ".join(
                    [c["cmd"]] + ([c["key"]] if "key" in c else []) + ([c["value"]] if "value" in c else [])
                )
                print(f"(auto) > {line}")
                if not handle(line):
                    break
        else:
            print("\nType 'observe', 'click <key>', 'type <key> <text>', 'select <key> <value>', "
                  "'screenshot', or 'resume [notes]'.\n")
            for line in sys.stdin:
                if not handle(line):
                    break

        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--auto", default=None, help="Path to a JSON command list, for non-interactive runs.")
    parser.add_argument("--allowlist", default="safety/allowlist.json")
    args = parser.parse_args()
    run(args.state_dir, args.auto, args.allowlist)
