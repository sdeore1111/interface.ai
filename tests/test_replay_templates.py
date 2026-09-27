import os

import pytest

from artifacts.schema import CapabilityArtifact, OutcomeCategory, OutcomeProbe, OutputSpec, ParamSpec, ParamType, Provenance, TargetSpec
from replay.engine import ParamValidationError, render_template, validate_params


def test_render_template_substitutes_param():
    assert render_template("hello {{name}}", {"name": "world"}) == "hello world"


def test_render_template_missing_param_raises():
    with pytest.raises(ParamValidationError):
        render_template("{{missing}}", {})


def test_render_template_substitutes_env_var(monkeypatch):
    monkeypatch.setenv("MY_SECRET", "s3cr3t")
    assert render_template("{{env:MY_SECRET}}", {}) == "s3cr3t"


def test_render_template_missing_env_var_is_empty_not_error(monkeypatch):
    monkeypatch.delenv("NOT_SET_VAR", raising=False)
    assert render_template("{{env:NOT_SET_VAR}}", {}) == ""


def _artifact_with_params():
    return CapabilityArtifact(
        capability_id="x", name="X", description="d", goal_template="g",
        target=TargetSpec(app_id="a", base_url="http://x", entry_path="/"),
        input_params=[
            ParamSpec(name="member_id", type=ParamType.STRING, required=True),
            ParamSpec(name="opening_deposit", type=ParamType.NUMBER, required=True),
            ParamSpec(name="nickname", type=ParamType.STRING, required=False),
        ],
        outputs=[OutputSpec(name="ref", type=ParamType.STRING)],
        steps=[],
        final_checkpoint=OutcomeProbe(code="DONE", category=OutcomeCategory.SUCCESS, match_text="Done"),
        provenance=Provenance(discovery_run_id="r", model="m"),
    )


def test_validate_params_coerces_number():
    out = validate_params(_artifact_with_params(), {"member_id": "1", "opening_deposit": "150"})
    assert out["opening_deposit"] == 150.0
    assert isinstance(out["opening_deposit"], float)


def test_validate_params_missing_required_raises():
    with pytest.raises(ParamValidationError):
        validate_params(_artifact_with_params(), {"member_id": "1"})


def test_validate_params_optional_param_omitted_is_fine():
    out = validate_params(_artifact_with_params(), {"member_id": "1", "opening_deposit": "1"})
    assert "nickname" not in out


def test_validate_params_bad_number_raises():
    with pytest.raises(ParamValidationError):
        validate_params(_artifact_with_params(), {"member_id": "1", "opening_deposit": "not-a-number"})
