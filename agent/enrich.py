"""
Post-discovery enrichment: attach known runtime-error outcome probes to a
freshly recorded artifact.

Design note (see REPORT.md "Determinism & error handling"): the LLM's
discovery run only ever walks ONE concrete path (the happy path we asked
for). It cannot discover "member not found" or "session expired" handling
just by completing one successful run -- those are runtime conditions that
occur at REPLAY time, for inputs/situations the discovery run never saw.

So this project treats outcome-probe authoring as a distinct, declarative
step: a human reviewer (here, encoded as this function, standing in for a
reviewer using domain knowledge of the target app) adds the known
error/business-outcome signatures before the artifact is approved for
replay. This mirrors how the brief frames it -- "the artifact should be
versioned and reviewable" -- reviewing is exactly the moment error
handling gets added, it isn't something replay invents unsupervised.

Stretch-goal note: Section 8 lists "assisted fallback" (a bounded LLM pass
recovering a single replay step) as optional. A natural non-stretch
extension of THIS function, if more time were available, would be a
second bounded discovery run that deliberately visits known error routes
(bad member id, over-limit deposit, restricted member) so these probes are
themselves LLM-discovered rather than hand-authored. Documented as a cut
in REPORT.md.
"""
from __future__ import annotations

from artifacts.schema import CapabilityArtifact, OutcomeCategory, OutcomeProbe


def attach_known_outcome_probes(artifact: CapabilityArtifact) -> CapabilityArtifact:
    artifact.outcome_taxonomy = [
        OutcomeProbe(
            code="MEMBER_NOT_FOUND",
            category=OutcomeCategory.BUSINESS_OUTCOME,
            match_text="No member found matching",
            description="Search returned no match for the given member_id. Legitimate answer, not a failure.",
        ),
        OutcomeProbe(
            code="PERMISSION_DENIED_RESTRICTED",
            category=OutcomeCategory.BUSINESS_OUTCOME,
            match_text="flagged RESTRICTED",
            description="Member account is restricted; sub-account actions are blocked for this teller role.",
        ),
        OutcomeProbe(
            code="VALIDATION_ERROR_DEPOSIT",
            category=OutcomeCategory.BUSINESS_OUTCOME,
            match_text="Opening deposit must be",
            description="Opening deposit failed form validation (non-numeric, zero, or negative).",
        ),
        OutcomeProbe(
            code="SESSION_TIMEOUT",
            category=OutcomeCategory.RECOVERABLE,
            match_text="session has expired",
            http_status_in=[440],
            description="Idle session expired mid-flow.",
            recovery_action="reauthenticate_and_retry_step",
            retry_limit=1,
        ),
        OutcomeProbe(
            code="SUPERVISOR_OVERRIDE_INTERSTITIAL",
            category=OutcomeCategory.RECOVERABLE,
            match_text="Supervisor Override Required",
            description="Deposit exceeds the auto-approval threshold; a known confirmation interstitial appears.",
            recovery_action="confirm_override",
            retry_limit=1,
        ),
        OutcomeProbe(
            code="TRANSIENT_SERVER_ERROR",
            category=OutcomeCategory.RECOVERABLE,
            http_status_in=[500, 502, 503, 504],
            description="Transient server-side error; safe to retry the current step a bounded number of times.",
            recovery_action="wait_and_retry_step",
            retry_limit=2,
        ),
    ]
    return artifact
