import pytest

from artifacts.schema import RiskLevel
from safety.policy import AllowlistConfig, PolicyViolation, SafetyGate, redact_params, redact_text


def _gate(allow_confirmation=False):
    cfg = AllowlistConfig(
        allowed_domains=["127.0.0.1:5055"],
        allowed_path_prefixes=["/member", "/login"],
        allowed_actions=["navigate", "click", "type"],
    )
    return SafetyGate(cfg, allow_confirmation_steps=allow_confirmation)


def test_navigation_outside_domain_is_denied():
    gate = _gate()
    with pytest.raises(PolicyViolation):
        gate.check_navigation("http://evil.example.com/steal")


def test_navigation_inside_allowlist_is_allowed():
    gate = _gate()
    gate.check_navigation("http://127.0.0.1:5055/member/12345")  # should not raise


def test_navigation_outside_path_prefix_is_denied():
    gate = _gate()
    with pytest.raises(PolicyViolation):
        gate.check_navigation("http://127.0.0.1:5055/admin/danger")


def test_action_type_not_in_allowlist_is_denied():
    gate = _gate()
    with pytest.raises(PolicyViolation):
        gate.check_action_type("select")  # only navigate/click/type allowed in this config


def test_blocked_risk_never_permitted_even_with_confirmation():
    gate = _gate(allow_confirmation=True)
    with pytest.raises(PolicyViolation):
        gate.check_risk(RiskLevel.BLOCKED, "s0")


def test_requires_confirmation_denied_by_default():
    gate = _gate(allow_confirmation=False)
    with pytest.raises(PolicyViolation):
        gate.check_risk(RiskLevel.REQUIRES_CONFIRMATION, "s0")


def test_requires_confirmation_allowed_when_granted():
    gate = _gate(allow_confirmation=True)
    gate.check_risk(RiskLevel.REQUIRES_CONFIRMATION, "s0")  # should not raise


def test_redact_params_by_explicit_flag():
    out = redact_params({"member_id": "12345", "note": "hi"}, redact_names=["member_id"])
    assert out["member_id"] == "[REDACTED]"
    assert out["note"] == "hi"


def test_redact_params_by_sensitive_name_backstop():
    out = redact_params({"password": "hunter2"}, redact_names=[])
    assert out["password"] == "[REDACTED]"


def test_redact_text_scrubs_credential_like_lines():
    text = "login with password: hunter2 please"
    assert "hunter2" not in redact_text(text)


def test_redact_text_scrubs_long_digit_runs():
    text = "account number 1234567890123"
    assert "1234567890123" not in redact_text(text)
