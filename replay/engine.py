"""
Deterministic replay engine: the production execution path (Section 3.3).

No LLM call happens anywhere in this file. Given an artifact and a params
dict, it walks `artifact.steps` in order, resolves each locator through the
SAME ranked-fallback chain recorded at discovery time (core.surface),
and reports one of exactly three terminal outcomes back to the caller:

    SUCCESS          -- final_checkpoint matched; declared outputs returned.
    BUSINESS_OUTCOME  -- a known, legitimate non-error result (e.g. "member
                         not found"). Not a crash. Caller gets a `code` and
                         whatever outputs were captured before the branch.
    HARD_FAILURE      -- something the system could not safely resolve on
                         its own. Includes exactly which step, what was
                         expected, and what was actually observed, so a
                         human (or the escalation module) can act on it.

RECOVERABLE conditions are handled internally and never surface as a
terminal outcome by themselves -- they either resolve (and replay
continues) or, once retries are exhausted, escalate to HARD_FAILURE with
the probe's code suffixed _RETRY_EXHAUSTED so it's still clear what was
attempted.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from artifacts.schema import (
    ActionType, CapabilityArtifact, Locator, LocatorCandidate, LocatorStrategy,
    OutcomeCategory, OutcomeProbe, ParamType, Step,
)
from core.evidence import EvidenceRun
from core.surface import LocatorResolutionError, PlaywrightSurface
from safety.policy import PolicyViolation, SafetyGate, redact_params

_PARAM_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_ENV_RE = re.compile(r"\{\{\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Recovery actions whose semantics are "the original action never actually
# succeeded, redo it" (transient failure, session re-auth). Any recovery
# action NOT in this set is assumed to be "dismiss/advance past" semantics
# (e.g. confirm_override): the recovery itself performed the step that was
# needed, so the original step should not be blindly repeated afterward.
_RETRY_ORIGINAL_STEP_ACTIONS = {"wait_and_retry_step", "reauthenticate_and_retry_step"}


class ReplayTerminalOutcome(str):
    SUCCESS = "SUCCESS"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"
    HARD_FAILURE = "HARD_FAILURE"


@dataclass
class ReplayResult:
    outcome: str  # one of ReplayTerminalOutcome
    code: str
    message: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    debug: Optional[Dict[str, Any]] = None  # populated for HARD_FAILURE: step, expected, observed


class ParamValidationError(Exception):
    pass


def render_template(value: str, params: Dict[str, Any]) -> str:
    import os

    def env_sub(m):
        return os.environ.get(m.group(1), "")

    def param_sub(m):
        name = m.group(1)
        if name not in params:
            raise ParamValidationError(f"Template references unknown/missing param '{name}'")
        return str(params[name])

    value = _ENV_RE.sub(env_sub, value)
    value = _PARAM_RE.sub(param_sub, value)
    return value


def validate_params(artifact: CapabilityArtifact, params: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for spec in artifact.input_params:
        if spec.name not in params:
            if spec.required:
                raise ParamValidationError(f"Missing required parameter: {spec.name}")
            continue
        v = params[spec.name]
        if spec.type == ParamType.NUMBER:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ParamValidationError(f"Parameter '{spec.name}' must be a number, got {v!r}")
        elif spec.type == ParamType.BOOLEAN:
            v = bool(v)
        else:
            v = str(v)
        out[spec.name] = v
    return out


class ReplayEngine:
    def __init__(
        self,
        artifact: CapabilityArtifact,
        surface: PlaywrightSurface,
        evidence: EvidenceRun,
        safety_gate: SafetyGate,
    ):
        self.artifact = artifact
        self.surface = surface
        self.evidence = evidence
        self.safety_gate = safety_gate

    def run(self, params: Dict[str, Any]) -> ReplayResult:
        try:
            validated = validate_params(self.artifact, params)
        except ParamValidationError as e:
            self.evidence.event("replay_param_error", {"error": str(e)})
            return ReplayResult(
                outcome=ReplayTerminalOutcome.HARD_FAILURE, code="INVALID_PARAMS",
                message=str(e),
            )

        redacted = redact_params(validated, [p.name for p in self.artifact.input_params if p.redact_in_logs])
        self.evidence.event("replay_start", {
            "capability_id": self.artifact.capability_id, "version": self.artifact.version,
            "params": redacted,
        })

        outputs: Dict[str, Any] = {}

        for step in self.artifact.steps:
            result = self._execute_step_with_recovery(step, validated, outputs)
            if result is not None:  # a terminal outcome short-circuited the flow
                self.evidence.close({"outcome": result.outcome, "code": result.code})
                return result

        # All steps completed without a short-circuit; verify the final checkpoint.
        final_result = self._check_final_checkpoint(outputs)
        self.evidence.close({"outcome": final_result.outcome, "code": final_result.code})
        return final_result

    # ------------------------------------------------------------------

    def _execute_step_with_recovery(self, step: Step, params: Dict[str, Any], outputs: Dict[str, Any]) -> Optional[ReplayResult]:
        attempts = 0
        while True:
            attempts += 1
            self.evidence.event("replay_step_start", {"step": step.id, "action": step.action.value})

            action_error: Optional[Exception] = None
            try:
                self._run_action(step, params, outputs)
            except PolicyViolation as e:
                self.evidence.event("replay_policy_denied", {"step": step.id, "error": str(e)})
                self.evidence.save_screenshot(self.surface, f"{step.id}_policy_denied.png")
                return ReplayResult(
                    outcome=ReplayTerminalOutcome.HARD_FAILURE, code="POLICY_DENIED",
                    message=str(e),
                    debug={"step": step.id, "expected": "action permitted by policy", "observed": "denied by SafetyGate"},
                )
            except (LocatorResolutionError, Exception) as e:  # noqa: BLE001
                action_error = e

            # Probe check happens whether or not the action raised: the *page state*
            # is the source of truth for outcome classification, not the exception.
            probe_result = self._check_probes(step)
            if probe_result is not None:
                category, probe = probe_result
                if category == OutcomeCategory.BUSINESS_OUTCOME:
                    self.evidence.event("replay_business_outcome", {"step": step.id, "code": probe.code})
                    return ReplayResult(
                        outcome=ReplayTerminalOutcome.BUSINESS_OUTCOME, code=probe.code,
                        message=probe.description, outputs=outputs,
                    )
                if category == OutcomeCategory.RECOVERABLE:
                    if attempts > max(probe.retry_limit, 1):
                        self.evidence.event("replay_recovery_exhausted", {"step": step.id, "code": probe.code})
                        self.evidence.save_screenshot(self.surface, f"{step.id}_recovery_exhausted.png")
                        return ReplayResult(
                            outcome=ReplayTerminalOutcome.HARD_FAILURE,
                            code=f"{probe.code}_RETRY_EXHAUSTED",
                            message=f"Recoverable condition '{probe.code}' did not resolve after {attempts} attempt(s).",
                            debug={"step": step.id, "expected": "recovery to succeed", "observed": probe.description},
                        )
                    self.evidence.event("replay_recovery_attempt", {
                        "step": step.id, "code": probe.code, "action": probe.recovery_action, "attempt": attempts,
                    })
                    self._attempt_recovery(probe)
                    if probe.recovery_action in _RETRY_ORIGINAL_STEP_ACTIONS:
                        # e.g. a transient error or a session re-auth: the ORIGINAL
                        # action never actually happened, so redo it.
                        continue
                    # e.g. confirm_override: the recovery action itself performed the
                    # UI step that was blocking us (clicked past the interstitial).
                    # Re-checking the original step's own probes/locator would be
                    # wrong -- that control may no longer even be on screen. Re-run
                    # the probe scan once more (to catch a chained interstitial) and
                    # otherwise treat this step as satisfied.
                    probe_result = self._check_probes(step)
                    if probe_result is not None and probe_result[0] == OutcomeCategory.RECOVERABLE:
                        continue
                    self.evidence.event("replay_step_ok", {"step": step.id, "via": "recovery"})
                    return None

            if action_error is not None:
                self.evidence.event("replay_hard_failure", {"step": step.id, "error": str(action_error)})
                self.evidence.save_screenshot(self.surface, f"{step.id}_failure.png")
                self.evidence.save_dom_snapshot(self.surface, f"{step.id}_failure_dom.html")
                return ReplayResult(
                    outcome=ReplayTerminalOutcome.HARD_FAILURE, code="STEP_EXECUTION_FAILED",
                    message=f"Step {step.id} ({step.description}) failed: {action_error}",
                    debug={
                        "step": step.id,
                        "expected": step.locator.description if step.locator else step.action.value,
                        "observed": str(action_error),
                    },
                )

            self.evidence.event("replay_step_ok", {"step": step.id})
            return None  # step succeeded, no short-circuit -> proceed to next step

    def _run_action(self, step: Step, params: Dict[str, Any], outputs: Dict[str, Any]):
        self.safety_gate.check_risk(step.risk_level, step.id)

        if step.action == ActionType.NAVIGATE:
            url = render_template(step.value_template, params)
            self.surface.navigate(url)
        elif step.action == ActionType.CLICK:
            self.surface.click(step.locator, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.TYPE:
            text = render_template(step.value_template, params)
            self.surface.type_text(step.locator, text, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.SELECT:
            value = render_template(step.value_template, params)
            self.surface.select(step.locator, value, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.WAIT_FOR:
            self.surface.wait_for(step.locator, timeout_ms=step.timeout_ms)
        elif step.action == ActionType.EXTRACT:
            text = self.surface.extract_text(step.locator, timeout_ms=step.timeout_ms)
            outputs[step.extract_as] = text.strip()
        elif step.action == ActionType.ASSERT_TEXT:
            page_text = self.surface.get_text_for_probe()
            expected = render_template(step.value_template or "", params)
            if expected.lower() not in page_text.lower():
                raise AssertionError(f"Expected text {expected!r} not found on page.")
        else:
            raise ValueError(f"Unsupported action type: {step.action}")

    def _check_probes(self, step: Step):
        """Check step-level probes first, then artifact-level (flow-wide) probes.
        Returns (category, probe) for the first BUSINESS_OUTCOME or RECOVERABLE
        match, or None if nothing matched (i.e. the step is presumed fine)."""
        page_text = self.surface.get_text_for_probe()
        http_status = self.surface.last_http_status
        for probe in list(step.outcome_probes) + list(self.artifact.outcome_taxonomy):
            if probe.category not in (OutcomeCategory.BUSINESS_OUTCOME, OutcomeCategory.RECOVERABLE):
                continue
            if probe.match_text and probe.match_text.lower() in page_text.lower():
                return probe.category, probe
            if probe.http_status_in and http_status in probe.http_status_in:
                return probe.category, probe
            if probe.match_locator:
                try:
                    self.surface.resolve(probe.match_locator, timeout_ms=1000)
                    return probe.category, probe
                except LocatorResolutionError:
                    pass
        return None

    def _attempt_recovery(self, probe: OutcomeProbe):
        if probe.recovery_action == "wait_and_retry_step":
            time.sleep(1.5)
        elif probe.recovery_action == "confirm_override":
            loc = Locator(
                description="Supervisor override confirmation control",
                candidates=[
                    LocatorCandidate(strategy=LocatorStrategy.TEXT, value="Confirm Override & Continue"),
                ],
            )
            try:
                self.surface.click(loc, timeout_ms=4000)
            except LocatorResolutionError:
                pass  # will be caught by retry_limit exhaustion if this keeps failing
        elif probe.recovery_action == "reauthenticate_and_retry_step":
            # Best-effort: navigate back to the login entry point; a production
            # implementation would replay the artifact's own recorded login
            # sub-sequence here rather than hardcoding a path.
            base = self.artifact.target.base_url
            self.surface.navigate(base.rstrip("/") + "/login")
        # unknown recovery_action: no-op, caller's retry_limit will bound the loop

    def _check_final_checkpoint(self, outputs: Dict[str, Any]) -> ReplayResult:
        probe = self.artifact.final_checkpoint
        page_text = self.surface.get_text_for_probe()
        matched = bool(probe.match_text) and probe.match_text.lower() in page_text.lower()
        if matched:
            self.evidence.event("replay_success", {"outputs": outputs})
            return ReplayResult(
                outcome=ReplayTerminalOutcome.SUCCESS, code=probe.code,
                message="Final checkpoint matched.", outputs=outputs,
            )
        self.evidence.event("replay_checkpoint_failed", {"expected": probe.match_text})
        self.evidence.save_screenshot(self.surface, "final_checkpoint_failed.png")
        return ReplayResult(
            outcome=ReplayTerminalOutcome.HARD_FAILURE, code="CHECKPOINT_NOT_MET",
            message="All steps executed but the final checkpoint was not observed.",
            debug={"step": "final_checkpoint", "expected": probe.match_text, "observed": page_text[:500]},
        )
