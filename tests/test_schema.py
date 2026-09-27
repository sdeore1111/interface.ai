from artifacts.schema import (
    ActionType, CapabilityArtifact, Locator, LocatorCandidate, LocatorStrategy,
    OutcomeCategory, OutcomeProbe, ParamType, ParamSpec, OutputSpec, Provenance,
    RiskLevel, Step, TargetSpec,
)


def _minimal_artifact() -> CapabilityArtifact:
    return CapabilityArtifact(
        capability_id="demo_cap",
        name="Demo Cap",
        description="A minimal artifact for testing.",
        goal_template="Do the thing.",
        target=TargetSpec(app_id="demo_app", base_url="http://127.0.0.1:5055", entry_path="/login"),
        input_params=[ParamSpec(name="member_id", type=ParamType.STRING)],
        outputs=[OutputSpec(name="ref", type=ParamType.STRING)],
        steps=[
            Step(
                id="s0", action=ActionType.CLICK, description="click something",
                locator=Locator(description="a button", candidates=[
                    LocatorCandidate(strategy=LocatorStrategy.TEXT, value="Search"),
                ]),
                risk_level=RiskLevel.SAFE,
            ),
        ],
        final_checkpoint=OutcomeProbe(code="DONE", category=OutcomeCategory.SUCCESS, match_text="Done"),
        provenance=Provenance(discovery_run_id="run1", model="test-model"),
    )


def test_round_trip_json():
    a = _minimal_artifact()
    js = a.to_json()
    b = CapabilityArtifact.model_validate_json(js)
    assert b.capability_id == a.capability_id
    assert b.steps[0].locator.candidates[0].strategy == LocatorStrategy.TEXT


def test_locator_requires_at_least_one_candidate():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Locator(description="empty", candidates=[])


def test_risk_level_default_is_safe():
    a = _minimal_artifact()
    assert a.steps[0].risk_level == RiskLevel.SAFE
