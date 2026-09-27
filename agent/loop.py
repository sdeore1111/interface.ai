"""
Goal-driven discovery agent: observe -> decide -> act, LLM in the loop.

This is the ONE place in the system where the model makes decisions.
Everything it produces that matters -- the sequence of actions, which
locator candidates back each one, what outcome probes apply -- is
converted into a CapabilityArtifact (artifacts/schema.py) at the end, so
that replay (replay/engine.py) never needs the model again. See
REPORT.md "Architecture" for the full split.

Stopping conditions (Section 3.1): max_steps, a wall-clock timeout, or the
model calling give_up (a self-assessed dead end). Any of these routes to
the escalation module rather than crashing.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from artifacts.schema import (
    ActionType, CapabilityArtifact, Locator, LocatorCandidate, LocatorStrategy,
    OutcomeCategory, OutcomeProbe, OutputSpec, ParamSpec, ParamType, Provenance,
    RiskLevel, Step, TargetSpec,
)
from agent.tools import TOOLS
from core.evidence import EvidenceRun
from core.surface import PlaywrightSurface
from safety.policy import redact_params

MODEL = os.environ.get("CUA_MODEL", "claude-sonnet-4-5-20250929")

SYSTEM_PROMPT = """You are an automation agent operating a real back-office web application \
on behalf of a bank teller, exactly the way a careful human operator would. You interact ONLY \
through the provided tools; you never receive or need raw HTML or coordinates.

Rules:
- Only click/type on element_keys that appear in the most recent observation. Keys are re-issued \
after every action -- always use the latest ones, never reuse a stale key from an earlier turn.
- Prefer type_param for any value that matches one of this run's named input parameters, so the \
recorded flow is reusable with different values later. Use type_literal only for fixed, \
non-parameter values. Use type_env for any credential/secret field.
- If you land on an unexpected page (an error, an interstitial, a permission message), read it \
carefully -- it may be a legitimate outcome for this goal (e.g. "member not found"), not a bug. \
If it's a known, reasonable business outcome, you may still call finish() with a checkpoint_text \
that reflects that actual outcome, OR call give_up() if you believe a human should decide. Use \
your judgment: routine, well-understood outcomes (validation message, not-found) can be finished; \
anything that looks unsafe, ambiguous, or irreversible should be escalated with give_up().
- Never attempt an action outside the stated goal's scope.
- Be efficient: don't re-observe unnecessarily, don't click things without a clear reason tied to \
the goal.
- When you believe the goal is reached, call finish() with a checkpoint_text that is a short, \
exact substring of the text currently visible on the page.
"""


@dataclass
class DiscoveryResult:
    success: bool
    artifact: Optional[CapabilityArtifact]
    steps_taken: int
    stop_reason: str
    outputs: Dict[str, Any] = field(default_factory=dict)


class DiscoveryAgent:
    def __init__(
        self,
        surface: PlaywrightSurface,
        evidence: EvidenceRun,
        goal: str,
        capability_id: str,
        target: TargetSpec,
        input_params: List[ParamSpec],
        param_values: Dict[str, Any],
        max_steps: int = 20,
        timeout_s: int = 180,
        risky_labels: Optional[List[str]] = None,
        escalation_state_dir: Optional[str] = None,
        cdp_endpoint: Optional[str] = None,
    ):
        self.surface = surface
        self.evidence = evidence
        self.goal = goal
        self.capability_id = capability_id
        self.target = target
        self.input_params = input_params
        self.param_values = param_values
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.risky_labels = set(risky_labels or [])
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        self.recorded_steps: List[Step] = []
        self.extracted_outputs: Dict[str, Any] = {}
        self._current_elements: Dict[str, Any] = {}
        # When set, give_up() triggers a real human handoff instead of just
        # ending the run -- see escalation/handoff.py.
        self.escalation_state_dir = escalation_state_dir
        self.cdp_endpoint = cdp_endpoint

    # ------------------------------------------------------------------

    def run(self) -> DiscoveryResult:
        started = time.time()
        obs = self.surface.observe()
        self._current_elements = self.surface.scan_actionable_elements()

        redacted_params = redact_params(
            self.param_values, [p.name for p in self.input_params if p.redact_in_logs]
        )
        self.evidence.event("discovery_start", {
            "goal": self.goal, "capability_id": self.capability_id,
            "param_values": redacted_params, "max_steps": self.max_steps,
        })

        user_intro = (
            f"Goal: {self.goal}\n\n"
            f"Input parameters available this run: {json.dumps({p.name: '<redacted>' if p.redact_in_logs else self.param_values.get(p.name) for p in self.input_params})}\n"
            f"(Use type_param with these param names -- never type their literal values yourself if you don't already know them.)\n\n"
            f"Current page:\n{obs.to_prompt_text()}"
        )
        messages: List[Dict[str, Any]] = [{"role": "user", "content": user_intro}]

        for step_num in range(1, self.max_steps + 1):
            if time.time() - started > self.timeout_s:
                self.evidence.event("discovery_stopped", {"reason": "timeout"})
                return self._finalize(False, step_num, "timeout")

            self.evidence.event("model_call", {"step": step_num})
            response = self.client.messages.create(
                model=MODEL, max_tokens=1024, system=SYSTEM_PROMPT,
                tools=TOOLS, messages=messages,
                # The agent must take exactly one action, observe, then decide again --
                # parallel tool calls would mean acting on stale observations for every
                # call after the first, and (mechanically) the Anthropic API requires a
                # tool_result for every tool_use in the same turn, which a single-action
                # loop can't produce for calls it intentionally didn't execute.
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            text_blocks = [b.text for b in response.content if b.type == "text"]
            if text_blocks:
                self.evidence.event("model_reasoning", {"text": " ".join(text_blocks)[:2000]})

            if not tool_use_blocks:
                # Model didn't call a tool -- nudge it back on track once.
                messages.append({"role": "user", "content": "Please call exactly one tool to proceed."})
                continue

            tool_use = tool_use_blocks[0]
            self.evidence.event("model_tool_call", {
                "step": step_num, "tool": tool_use.name, "input": tool_use.input,
            })

            try:
                tool_result_text, stop = self._execute_tool(tool_use.name, tool_use.input)
            except Exception as e:  # noqa: BLE001 - any action failure is reported back to the model, not fatal
                tool_result_text = f"Action failed with error: {e}"
                stop = None
                self.evidence.event("action_error", {"step": step_num, "error": str(e)})
                self.evidence.save_screenshot(self.surface, f"step{step_num}_error.png")

            if stop is not None:
                success, reason = stop
                return self._finalize(success, step_num, reason)

            obs = self.surface.observe()
            self._current_elements = self.surface.scan_actionable_elements()
            # Every tool_use in this turn needs a matching tool_result, even ones we
            # deliberately ignored because disable_parallel_tool_use should prevent
            # them from occurring -- defensive, in case the model still emits extras.
            result_blocks = [{
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": f"{tool_result_text}\n\nCurrent page:\n{obs.to_prompt_text()}",
            }]
            for extra in tool_use_blocks[1:]:
                result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": extra.id,
                    "content": "Not executed: only one tool call is processed per turn.",
                })
            messages.append({"role": "user", "content": result_blocks})

        self.evidence.event("discovery_stopped", {"reason": "max_steps_exhausted"})
        return self._finalize(False, self.max_steps, "max_steps_exhausted")

    # ------------------------------------------------------------------

    def _execute_tool(self, name: str, tool_input: dict):
        """Returns (result_text, stop) where stop is None to continue, or
        (success: bool, reason: str) to end the run."""

        if name == "navigate":
            url = tool_input["url"]
            self.surface.navigate(url)
            self.recorded_steps.append(Step(
                id=f"s{len(self.recorded_steps)}", action=ActionType.NAVIGATE,
                description=f"Navigate to {url}", value_template=url,
            ))
            return f"Navigated to {url}.", None

        if name in ("click", "type_literal", "type_param", "type_env", "select", "extract"):
            key = tool_input["element_key"]
            el = self._current_elements.get(key)
            if el is None:
                return f"Unknown element_key '{key}'. Use a key from the latest observation.", None

            risk = RiskLevel.REQUIRES_CONFIRMATION if el.label in self.risky_labels else RiskLevel.SAFE

            if name == "click":
                cand = self.surface.click(el.locator)
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.CLICK,
                    description=tool_input.get("reason", f"Click '{el.label}'"),
                    locator=el.locator, risk_level=risk,
                ))
                return f"Clicked '{el.label}'.", None

            if name == "type_literal":
                text = tool_input["text"]
                self.surface.type_text(el.locator, text)
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.TYPE,
                    description=tool_input.get("reason", f"Type literal into '{el.label}'"),
                    locator=el.locator, value_template=text, risk_level=risk,
                ))
                return f"Typed literal value into '{el.label}'.", None

            if name == "type_param":
                pname = tool_input["param_name"]
                if pname not in self.param_values:
                    return f"Unknown parameter '{pname}'. Available: {list(self.param_values.keys())}", None
                self.surface.type_text(el.locator, str(self.param_values[pname]))
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.TYPE,
                    description=tool_input.get("reason", f"Type param {pname} into '{el.label}'"),
                    locator=el.locator, value_template="{{" + pname + "}}", risk_level=risk,
                ))
                return f"Typed parameter '{pname}' into '{el.label}'.", None

            if name == "type_env":
                var = tool_input["env_var"]
                value = os.environ.get(var, "")
                if not value:
                    return f"Environment variable '{var}' is not set in this environment.", None
                self.surface.type_text(el.locator, value)
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.TYPE,
                    description=tool_input.get("reason", f"Type credential into '{el.label}'"),
                    locator=el.locator, value_template="{{env:" + var + "}}", risk_level=risk,
                ))
                return f"Typed credential from env:{var} into '{el.label}'.", None

            if name == "select":
                value = tool_input["value"]
                self.surface.select(el.locator, value)
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.SELECT,
                    description=tool_input.get("reason", f"Select '{value}' in '{el.label}'"),
                    locator=el.locator, value_template=value, risk_level=risk,
                ))
                return f"Selected '{value}' in '{el.label}'.", None

            if name == "extract":
                output_name = tool_input["output_name"]
                text = self.surface.extract_text(el.locator)
                self.extracted_outputs[output_name] = text.strip()
                self.recorded_steps.append(Step(
                    id=f"s{len(self.recorded_steps)}", action=ActionType.EXTRACT,
                    description=tool_input.get("reason", f"Extract '{el.label}' as {output_name}"),
                    locator=el.locator, extract_as=output_name, risk_level=risk,
                ))
                return f"Extracted '{el.label}' -> {output_name} = {text.strip()!r}.", None

        if name == "finish":
            checkpoint_text = tool_input["checkpoint_text"]
            page_text = self.surface.get_text_for_probe()
            if checkpoint_text.lower() not in page_text.lower():
                return (
                    f"checkpoint_text {checkpoint_text!r} was not found on the current page. "
                    f"Either it's premature to finish, or pick text that's actually visible now.",
                    None,
                )
            self.evidence.event("discovery_finish", {
                "checkpoint_text": checkpoint_text, "summary": tool_input.get("summary", ""),
                "outputs": self.extracted_outputs,
            })
            self._final_checkpoint_text = checkpoint_text
            return "", (True, "goal_reached")

        if name == "give_up":
            reason = tool_input.get("reason", "")
            self.evidence.event("discovery_give_up", {"reason": reason})
            self.evidence.save_screenshot(self.surface, "give_up.png")

            if self.escalation_state_dir and self.cdp_endpoint:
                from escalation.handoff import escalate
                from escalation.state import EscalationContext

                ctx = EscalationContext(
                    capability_id=self.capability_id, goal=self.goal,
                    current_step_index=len(self.recorded_steps),
                    current_url=self.surface.page.url, reason=reason,
                )
                result = escalate(
                    self.escalation_state_dir, self.cdp_endpoint,
                    self.surface, self.evidence, ctx,
                )
                if result.resolved:
                    self._merge_human_steps(result.human_steps)
                    return (
                        f"A human operator intervened ({result.notes or 'no notes'}) and performed "
                        f"{len(result.human_steps)} action(s) on this same session. Continue from the "
                        f"current state; re-assess whether the goal is now reached.",
                        None,
                    )
                return "", (False, f"escalation_unresolved: {reason}")

            return "", (False, f"give_up: {reason}")

        return f"Unknown tool '{name}'.", None

    def _merge_human_steps(self, human_steps: List[dict]):
        """Fold operator-performed actions (escalation/operator_cli.py) into the
        artifact under construction. Always recorded with risk_level
        REQUIRES_CONFIRMATION, regardless of what the control itself was,
        because a step a human had to do live is exactly the kind of step
        replay should pause on and re-confirm next time, not silently automate."""
        action_map = {"click": ActionType.CLICK, "type": ActionType.TYPE, "select": ActionType.SELECT}
        for hs in human_steps:
            loc = Locator.model_validate(hs["locator"]) if hs.get("locator") else None
            self.recorded_steps.append(Step(
                id=f"s{len(self.recorded_steps)}",
                action=action_map.get(hs["action"], ActionType.CLICK),
                description=hs.get("description", "Performed by human operator during escalation"),
                locator=loc,
                value_template=hs.get("value_template"),
                risk_level=RiskLevel.REQUIRES_CONFIRMATION,
            ))
        self.evidence.event("human_steps_merged", {"count": len(human_steps)})

    # ------------------------------------------------------------------

    def _finalize(self, success: bool, steps_taken: int, stop_reason: str) -> DiscoveryResult:
        artifact = None
        if success:
            artifact = self._build_artifact()
            self.evidence.save_json(artifact, "artifact.json")
        self.evidence.close({
            "success": success, "steps_taken": steps_taken, "stop_reason": stop_reason,
            "outputs": self.extracted_outputs,
        })
        return DiscoveryResult(
            success=success, artifact=artifact, steps_taken=steps_taken,
            stop_reason=stop_reason, outputs=self.extracted_outputs,
        )

    def _build_artifact(self) -> CapabilityArtifact:
        outputs = [
            OutputSpec(name=name, type=ParamType.STRING, description=f"Extracted during discovery: {name!r}")
            for name in self.extracted_outputs.keys()
        ]
        final_checkpoint = OutcomeProbe(
            code="GOAL_REACHED", category=OutcomeCategory.SUCCESS,
            match_text=getattr(self, "_final_checkpoint_text", ""),
            description="Text observed on the page when the LLM confirmed the goal was reached.",
        )
        return CapabilityArtifact(
            capability_id=self.capability_id,
            version=1,
            name=self.capability_id.replace("_", " ").title(),
            description=f"Auto-recorded capability for goal: {self.goal}",
            goal_template=self.goal,
            target=self.target,
            input_params=self.input_params,
            outputs=outputs,
            steps=self.recorded_steps,
            outcome_taxonomy=[],  # enriched post-hoc; see agent/enrich.py
            final_checkpoint=final_checkpoint,
            provenance=Provenance(discovery_run_id=self.evidence.run_id, model=MODEL, source="llm_discovery"),
        )
