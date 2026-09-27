"""
Safety & policy guardrails.

Three independent controls, deliberately kept simple and enforced at a
single choke point (SurfaceAdapter, see agent/surface.py) rather than
scattered through the agent loop or replay engine, so there is exactly one
place that can be audited:

1. Allowlist: which domains/routes the agent may navigate to, and which
   action types are permitted at all. Enforced BEFORE every navigate/click/
   type, both during discovery (LLM-chosen actions) and replay
   (artifact-declared actions) -- an artifact recorded against an allowed
   target does not get a permanent pass; every execution re-checks.

2. Risk classification: actions are SAFE, REQUIRES_CONFIRMATION, or
   BLOCKED (see artifacts.schema.RiskLevel). This mock treats "opening a
   sub-account" (money movement, hard to cleanly reverse in a real core
   system) as REQUIRES_CONFIRMATION -- during discovery the LLM must not
   auto-submit that step without it being explicitly allowed by policy;
   during replay, REQUIRES_CONFIRMATION steps execute only if the caller
   passed `allow_confirmation_steps=True` (the production analogue: the
   calling AI agent/product decided this invocation is authorized, e.g.
   because a human already approved the underlying request upstream).
   BLOCKED actions never execute, period -- there is no override.

3. Redaction: never persist secrets or raw sensitive values into logs or
   artifacts. Any ParamSpec marked redact_in_logs=True has its value
   replaced with a stable placeholder before it is written anywhere
   durable. Passwords are never logged. This mock uses synthetic data, but
   the redaction path is real and exercised (see evidence/ logs).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from artifacts.schema import RiskLevel


class PolicyViolation(Exception):
    """Raised when an action would violate the allowlist. Never silently swallowed."""


@dataclass
class AllowlistConfig:
    allowed_domains: List[str] = field(default_factory=list)   # e.g. ["127.0.0.1:5055"]
    allowed_path_prefixes: List[str] = field(default_factory=lambda: ["/"])
    allowed_actions: List[str] = field(
        default_factory=lambda: ["navigate", "click", "type", "select", "wait_for", "extract", "assert_text"]
    )
    # Action types never permitted regardless of anything else (defense in depth;
    # this project's action set has no destructive verbs, but a real deployment
    # driving a core banking UI would list things like "delete", "wire_transfer" here).
    hard_blocked_actions: List[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str) -> "AllowlistConfig":
        with open(path) as f:
            data = json.load(f)
        return cls(**data)


class SafetyGate:
    def __init__(self, config: AllowlistConfig, allow_confirmation_steps: bool = False):
        self.config = config
        self.allow_confirmation_steps = allow_confirmation_steps
        self.decisions: List[dict] = []  # audit trail of every gate decision made this run

    def check_navigation(self, url: str) -> None:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if self.config.allowed_domains and netloc not in self.config.allowed_domains:
            self._deny(f"navigate to disallowed domain: {netloc}")
            raise PolicyViolation(f"Domain not in allowlist: {netloc}")
        if self.config.allowed_path_prefixes:
            if not any(parsed.path.startswith(p) for p in self.config.allowed_path_prefixes):
                self._deny(f"navigate to disallowed path: {parsed.path}")
                raise PolicyViolation(f"Path not in allowlist: {parsed.path}")
        self._allow(f"navigate -> {url}")

    def check_action_type(self, action_type: str) -> None:
        if action_type in self.config.hard_blocked_actions:
            self._deny(f"hard-blocked action type: {action_type}")
            raise PolicyViolation(f"Action type is hard-blocked: {action_type}")
        if self.config.allowed_actions and action_type not in self.config.allowed_actions:
            self._deny(f"action type not in allowlist: {action_type}")
            raise PolicyViolation(f"Action type not permitted: {action_type}")
        self._allow(f"action type ok: {action_type}")

    def check_risk(self, risk_level: RiskLevel, step_id: str) -> None:
        if risk_level == RiskLevel.BLOCKED:
            self._deny(f"step {step_id} is BLOCKED by policy")
            raise PolicyViolation(f"Step {step_id} is blocked and cannot execute.")
        if risk_level == RiskLevel.REQUIRES_CONFIRMATION and not self.allow_confirmation_steps:
            self._deny(f"step {step_id} requires confirmation, not granted for this run")
            raise PolicyViolation(
                f"Step {step_id} requires confirmation (allow_confirmation_steps=False)."
            )
        self._allow(f"risk ok for step {step_id}: {risk_level.value}")

    def _allow(self, note: str):
        self.decisions.append({"decision": "allow", "note": note})

    def _deny(self, note: str):
        self.decisions.append({"decision": "deny", "note": note})


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

_REDACTED = "[REDACTED]"

# Field-name patterns treated as sensitive even if not explicitly flagged,
# as a defense-in-depth backstop against a capability author forgetting to
# set redact_in_logs on a param.
_SENSITIVE_NAME_RE = re.compile(
    r"(password|passwd|secret|token|ssn|ss_num|social_security|credit_card|card_number|cvv|pin)",
    re.IGNORECASE,
)


def redact_params(params: dict, redact_names: Iterable[str]) -> dict:
    """Return a copy of `params` safe to write to logs/artifacts."""
    redact_names = set(redact_names)
    out = {}
    for k, v in params.items():
        if k in redact_names or _SENSITIVE_NAME_RE.search(k):
            out[k] = _REDACTED
        else:
            out[k] = v
    return out


def redact_text(text: Optional[str]) -> Optional[str]:
    """Best-effort scrub of common secret-shaped substrings in free text
    (e.g. a password accidentally echoed in a page snapshot) before it is
    persisted to a log line or evidence file."""
    if text is None:
        return None
    # crude patterns: long digit runs that look like account/card numbers,
    # and key=value pairs where the key looks like a credential.
    text = re.sub(r"\b\d{9,16}\b", _REDACTED, text)
    text = re.sub(
        r"(?i)\b(password|passwd|secret|token|pin)\b\s*[:=]\s*\S+",
        lambda m: f"{m.group(1)}: {_REDACTED}",
        text,
    )
    return text
