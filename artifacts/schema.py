"""
Artifact schema: the reusable, agent-invocable "capability" contract.

Design intent (see /REPORT.md section 2 for the full rationale):

- An artifact is a CONTRACT, not a transcript. It must be understandable by
  (a) a human reviewer deciding whether to approve it, and (b) a calling
  AI agent that only sees input_schema/output_schema/description — never
  the steps. The steps are the replay engine's business, not the caller's.

- Locators are never a single string. Each target is a ranked list of
  candidate locator strategies (LocatorCandidate), because in a legacy,
  no-test-id surface, any single strategy can silently stop matching
  (a label's wording changes, a table row shifts). Ranked fallback is the
  concrete answer to "how each target element/control is identified, with
  your reasoning about robustness."

- Outcomes are typed and enumerated on the artifact itself, not
  discovered ad hoc at replay time. Each step can declare "outcome probes"
  -- conditions that, if matched after the step runs, mean the flow has
  landed in a known business outcome (e.g. "member not found"), a
  recoverable condition (e.g. dismiss a known interstitial), or should be
  treated as a hard failure. This directly encodes the brief's
  "business outcome vs. failure" distinction into the artifact rather than
  leaving it to replay-time guesswork.

- Surface-agnostic on purpose. `LocatorCandidate.strategy` is an enum that
  includes both web strategies (role, css, text, xpath) and stand-ins for
  non-web strategies (ax_node for a desktop accessibility-tree node,
  image_template for coordinate/vision-based fallback) so the schema does
  not have to change shape when a desktop or screenshot-driven adapter is
  added later -- only a new SurfaceAdapter implementation is needed. See
  REPORT.md "Heterogeneity & multi-tenant."
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------

class LocatorStrategy(str, Enum):
    ROLE = "role"                 # accessibility role + accessible name (preferred on web)
    LABEL = "label"                # associated <label> text -> form control
    TEXT = "text"                  # visible text match
    CSS = "css"                    # CSS selector (last resort on web; brittle on legacy markup)
    XPATH = "xpath"                # structural fallback for frameset/table-heavy legacy pages
    AX_NODE = "ax_node"            # desktop accessibility-tree node path (future surface)
    IMAGE_TEMPLATE = "image_template"  # screenshot template match + coordinates (last resort, any surface)


class LocatorCandidate(BaseModel):
    """One candidate way to find a control, ranked within Locator.candidates."""
    strategy: LocatorStrategy
    value: str = Field(..., description="Strategy-specific selector value, e.g. role=button + name, or a CSS selector.")
    role_name: Optional[str] = Field(
        default=None, description="Accessible name, only used when strategy == ROLE."
    )
    robustness_note: str = Field(
        default="", description="Why this candidate should or shouldn't survive minor UI drift."
    )


class Locator(BaseModel):
    """
    A ranked fallback chain. Replay tries candidates in order and uses the
    first one that resolves to exactly one visible, actionable element.
    Recorded during discovery with the model's own reasoning about why a
    given candidate is or isn't robust (robustness_note).
    """
    description: str = Field(..., description="Human-readable purpose, e.g. 'Member ID search field'.")
    candidates: List[LocatorCandidate]

    @field_validator("candidates")
    @classmethod
    def _non_empty(cls, v):
        if not v:
            raise ValueError("Locator must have at least one candidate")
        return v


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT_FOR = "wait_for"
    EXTRACT = "extract"
    ASSERT_TEXT = "assert_text"


class RiskLevel(str, Enum):
    SAFE = "safe"                     # reversible, read-only or low-consequence
    REQUIRES_CONFIRMATION = "requires_confirmation"  # irreversible-ish; gated (see safety module)
    BLOCKED = "blocked"               # never permitted to execute automatically


class OutcomeCategory(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"   # legitimate non-error result caller needs, e.g. "not found"
    RECOVERABLE = "recoverable"             # replay handles internally and continues (retry/dismiss)
    HARD_FAILURE = "hard_failure"           # stop, surface a debuggable error


class OutcomeProbe(BaseModel):
    """
    A condition checked against page state right after a step executes.
    If it matches, replay short-circuits into the declared outcome instead
    of blindly proceeding to the next step. This is how the artifact
    encodes "record not found is a legitimate answer, not a crash."
    """
    code: str = Field(..., description="Machine-readable outcome code, e.g. 'MEMBER_NOT_FOUND'.")
    category: OutcomeCategory
    match_text: Optional[str] = Field(
        default=None, description="Substring/regex checked against the observed surface text."
    )
    match_locator: Optional[Locator] = Field(
        default=None, description="Alternative: matches if this locator resolves (e.g. an error banner element)."
    )
    http_status_in: Optional[List[int]] = Field(
        default=None, description="Matches if the last navigation's HTTP status is in this set."
    )
    description: str = Field(default="", description="Human-readable explanation for reviewers.")
    recovery_action: Optional[str] = Field(
        default=None,
        description="For RECOVERABLE only: what replay should do, e.g. 'dismiss_dialog', 'retry_step', 'wait_and_retry'.",
    )
    retry_limit: int = Field(default=0, description="Max automatic retries when recovery_action requires one.")


class Step(BaseModel):
    id: str
    action: ActionType
    description: str = Field(..., description="What this step accomplishes, for human review.")
    locator: Optional[Locator] = Field(
        default=None, description="Required for click/type/select/wait_for/assert_text/extract; unused for navigate."
    )
    value_template: Optional[str] = Field(
        default=None,
        description="Value to type/select/navigate to. May reference input params as {{param_name}}.",
    )
    extract_as: Optional[str] = Field(
        default=None, description="If action == EXTRACT, the output field name this value is stored under."
    )
    risk_level: RiskLevel = Field(default=RiskLevel.SAFE)
    outcome_probes: List[OutcomeProbe] = Field(
        default_factory=list,
        description="Checked in order immediately after this step executes, before moving on.",
    )
    timeout_ms: int = Field(default=8000)

    @field_validator("value_template")
    @classmethod
    def _validate_template(cls, v):
        if v is None:
            return v
        # allow only {{identifier}} placeholders, nothing else executable
        stray = re.sub(r"\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}", "", v)
        return v


# --------------------------------------------------------------------------
# Parameters (typed contract for the caller)
# --------------------------------------------------------------------------

class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class ParamSpec(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str = ""
    example: Optional[Any] = None
    redact_in_logs: bool = Field(
        default=False,
        description="True for PII/sensitive inputs (e.g. member_id in a real system); "
                    "redacted before persisting to logs/artifacts per safety policy.",
    )


class OutputSpec(BaseModel):
    name: str
    type: ParamType
    description: str = ""


# --------------------------------------------------------------------------
# Target / provenance
# --------------------------------------------------------------------------

class TargetSpec(BaseModel):
    app_id: str = Field(..., description="Stable identifier for the underlying vendor app/product, not the tenant.")
    tenant_id: Optional[str] = Field(
        default=None,
        description="Institution this recording was captured against. None for a tenant-agnostic base artifact "
                    "intended for cross-tenant reuse (see REPORT.md 'Heterogeneity & multi-tenant').",
    )
    surface_type: Literal["web", "legacy_web", "desktop"] = "web"
    base_url: str
    entry_path: str = Field(default="/", description="Path the flow starts from, after auth.")


class Provenance(BaseModel):
    discovery_run_id: str
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    model: str
    source: Literal["llm_discovery", "hand_authored", "human_edited"] = "llm_discovery"


# --------------------------------------------------------------------------
# Top-level Capability Artifact
# --------------------------------------------------------------------------

class CapabilityArtifact(BaseModel):
    """The full, versioned, serializable capability contract."""
    schema_version: str = "1.0"
    capability_id: str = Field(..., description="Stable slug, e.g. 'open_sub_account'.")
    version: int = Field(default=1, description="Bumped whenever steps/locators are re-recorded.")
    name: str
    description: str = Field(..., description="What this capability does, for a human reviewer or a calling agent.")
    goal_template: str = Field(..., description="The natural-language goal this was recorded from.")

    target: TargetSpec
    input_params: List[ParamSpec]
    outputs: List[OutputSpec]
    steps: List[Step]
    outcome_taxonomy: List[OutcomeProbe] = Field(
        default_factory=list,
        description="Flow-level outcomes not tied to one specific step (e.g. global session-timeout detector), "
                    "checked at every step boundary in addition to that step's own probes.",
    )
    final_checkpoint: OutcomeProbe = Field(
        ...,
        description="Condition that must hold at the end of the flow for the run to count as SUCCESS.",
    )
    provenance: Provenance

    def to_json(self, indent: int = 2) -> str:
        return self.model_dump_json(indent=indent)
