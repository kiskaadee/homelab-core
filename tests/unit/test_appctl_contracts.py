"""
tests/unit/test_appctl_contracts.py — Unit Tests for Control Plane API Contracts (P4 Slice 1)

Behavior-driven test naming convention:
test_<subject>_<accepts|rejects>_<condition>
"""

from __future__ import annotations

import pytest
from appctl_contracts import (
    MUTATING_OPERATIONS,
    READ_ONLY_OPERATIONS,
    TARGET_REQUIRED_OPERATIONS,
    DeployParams,
    EmptyParams,
    ErrorCode,
    GetParams,
    ListParams,
    LogsParams,
    OperationError,
    OperationRequest,
    OperationResponse,
    OperationType,
    UpdateParams,
    sanitize_validation_error,
)
from core_manifest import ALLOWED_ACTIONS, Manifest
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_validation_error_locs(data: dict) -> list[str | int]:
    with pytest.raises(ValidationError) as exc_info:
        OperationRequest.model_validate(data)
    return [err["loc"][0] for err in exc_info.value.errors()]


# ---------------------------------------------------------------------------
# Canonical 13 Operations
# ---------------------------------------------------------------------------


def test_operation_type_contains_exactly_13_canonical_operations():
    assert len(OperationType) == 13
    expected = {
        "workload.list",
        "workload.get",
        "workload.resolve",
        "platform.ssl",
        "platform.health",
        "workload.config",
        "workload.logs",
        "workload.deploy",
        "workload.stop",
        "workload.restart",
        "workload.update",
        "workload.pull",
        "platform.sync_homepage",
    }
    assert {op.value for op in OperationType} == expected


def test_operation_sets_partition_canonical_operations():
    assert MUTATING_OPERATIONS | READ_ONLY_OPERATIONS == set(OperationType)
    assert MUTATING_OPERATIONS.isdisjoint(READ_ONLY_OPERATIONS)


@pytest.mark.parametrize("op", list(OperationType))
def test_operation_request_accepts_all_canonical_operations(op: OperationType):
    target = "jellyfin" if op in TARGET_REQUIRED_OPERATIONS else None
    request = {
        "version": "1.0",
        "operation": op.value,
        "target": target,
        "parameters": {},
    }
    validated = OperationRequest.model_validate(request)
    assert validated.operation == op
    assert validated.action == op


def test_operation_request_rejects_unknown_operation():
    request = {
        "version": "1.0",
        "operation": "workload.unknown_action",
        "target": "jellyfin",
        "parameters": {},
    }
    locs = get_validation_error_locs(request)
    assert "operation" in locs


def test_operation_request_rejects_empty_operation():
    request = {
        "version": "1.0",
        "operation": "",
        "target": "jellyfin",
        "parameters": {},
    }
    locs = get_validation_error_locs(request)
    assert "operation" in locs


def test_operation_request_rejects_non_string_operation():
    request = {
        "version": "1.0",
        "operation": 12345,
        "target": "jellyfin",
        "parameters": {},
    }
    locs = get_validation_error_locs(request)
    assert "operation" in locs


# ---------------------------------------------------------------------------
# Version "1.0"
# ---------------------------------------------------------------------------


def test_operation_request_accepts_default_and_explicit_version_1_0():
    # Explicit "1.0"
    r1 = OperationRequest.model_validate({
        "version": "1.0",
        "operation": "platform.health",
    })
    assert r1.version == "1.0"

    # Default "1.0"
    r2 = OperationRequest.model_validate({
        "operation": "platform.health",
    })
    assert r2.version == "1.0"


def test_operation_request_rejects_non_1_0_version():
    for bad_ver in ["2.0", "0.9", "1.1", ""]:
        request = {
            "version": bad_ver,
            "operation": "platform.health",
        }
        locs = get_validation_error_locs(request)
        assert "version" in locs


def test_operation_request_rejects_numeric_version():
    request = {
        "version": 1.0,
        "operation": "platform.health",
    }
    locs = get_validation_error_locs(request)
    assert "version" in locs


# ---------------------------------------------------------------------------
# Target Validation
# ---------------------------------------------------------------------------


def test_operation_request_rejects_empty_target():
    request = {
        "version": "1.0",
        "operation": "workload.deploy",
        "target": "",
        "parameters": {},
    }
    locs = get_validation_error_locs(request)
    assert "target" in locs


@pytest.mark.parametrize("op", list(TARGET_REQUIRED_OPERATIONS))
def test_operation_request_rejects_missing_target_when_required(op: OperationType):
    request = {
        "version": "1.0",
        "operation": op.value,
        "target": None,
        "parameters": {},
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationRequest.model_validate(request)
    assert "requires a non-empty target" in str(exc_info.value)


@pytest.mark.parametrize("op", [
    OperationType.WORKLOAD_LIST,
    OperationType.PLATFORM_HEALTH,
    OperationType.PLATFORM_SYNC_HOMEPAGE,
])
def test_operation_request_accepts_missing_target_when_optional(op: OperationType):
    request = {
        "version": "1.0",
        "operation": op.value,
        "parameters": {},
    }
    validated = OperationRequest.model_validate(request)
    assert validated.target is None


# ---------------------------------------------------------------------------
# Parameter Models & Bounds Validation
# ---------------------------------------------------------------------------


def test_deploy_params_defaults_and_explicit_bounds():
    # Defaults
    p_default = DeployParams()
    assert p_default.recreate is True
    assert p_default.remove_orphans is True
    assert p_default.build is False

    # Explicit
    p_custom = DeployParams(recreate=False, remove_orphans=False, build=True)
    assert p_custom.recreate is False
    assert p_custom.remove_orphans is False
    assert p_custom.build is True


def test_deploy_params_rejects_non_strict_types():
    for bad_bool in ["true", "false", 1, 0, None, [True]]:
        with pytest.raises(ValidationError):
            DeployParams(recreate=bad_bool)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DeployParams(remove_orphans=bad_bool)  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            DeployParams(build=bad_bool)  # type: ignore[arg-type]


def test_deploy_params_rejects_extra_fields():
    with pytest.raises(ValidationError):
        DeployParams.model_validate({"recreate": True, "unexpected": "extra"})


def test_update_params_defaults_and_explicit():
    # Defaults
    p_default = UpdateParams()
    assert p_default.expected_branch is None
    assert p_default.build is False

    # Explicit
    p_custom = UpdateParams(expected_branch="main", build=True)
    assert p_custom.expected_branch == "main"
    assert p_custom.build is True


def test_update_params_rejects_empty_branch_and_non_strict_types():
    with pytest.raises(ValidationError):
        UpdateParams(expected_branch="")  # min_length=1

    with pytest.raises(ValidationError):
        UpdateParams(expected_branch=123)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        UpdateParams(build="true")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        UpdateParams.model_validate({"build": False, "extra": "prohibited"})


def test_logs_params_defaults_and_bounds():
    # Default
    p_default = LogsParams()
    assert p_default.tail == 100

    # Bounds
    assert LogsParams(tail=1).tail == 1
    assert LogsParams(tail=1000).tail == 1000
    assert LogsParams(tail=500).tail == 500

    # Below minimum
    with pytest.raises(ValidationError):
        LogsParams(tail=0)

    # Above maximum
    with pytest.raises(ValidationError):
        LogsParams(tail=1001)

    # Non-strict integer (string / float)
    with pytest.raises(ValidationError):
        LogsParams(tail="100")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LogsParams(tail=100.5)  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        LogsParams.model_validate({"tail": 50, "follow": True})


def test_list_params_defaults_and_explicit():
    p_default = ListParams()
    assert p_default.fetch is False
    assert p_default.core is False
    assert p_default.ssl is False

    p_custom = ListParams(fetch=True, core=True, ssl=True)
    assert p_custom.fetch is True
    assert p_custom.core is True
    assert p_custom.ssl is True

    with pytest.raises(ValidationError):
        ListParams(fetch="yes")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ListParams.model_validate({"fetch": False, "extra": 1})


def test_get_params_defaults_and_explicit():
    p_default = GetParams()
    assert p_default.ssl is False

    p_custom = GetParams(ssl=True)
    assert p_custom.ssl is True

    with pytest.raises(ValidationError):
        GetParams(ssl="true")  # type: ignore[arg-type]


def test_empty_params_rejects_extra_fields():
    p = EmptyParams()
    assert p.model_dump() == {}

    with pytest.raises(ValidationError):
        EmptyParams.model_validate({"any_field": "disallowed"})


def test_operation_request_validates_and_parses_operation_specific_params():
    # Deploy
    req_deploy = OperationRequest.model_validate({
        "version": "1.0",
        "operation": "workload.deploy",
        "target": "snappymail",
        "parameters": {"recreate": True, "build": True},
    })
    params = req_deploy.parse_parameters()
    assert isinstance(params, DeployParams)
    assert params.build is True

    # Logs
    req_logs = OperationRequest.model_validate({
        "version": "1.0",
        "operation": "workload.logs",
        "target": "snappymail",
        "parameters": {"tail": 250},
    })
    log_params = req_logs.parse_parameters()
    assert isinstance(log_params, LogsParams)
    assert log_params.tail == 250

    # Stop (accepts EmptyParams)
    req_stop = OperationRequest.model_validate({
        "version": "1.0",
        "operation": "workload.stop",
        "target": "snappymail",
        "parameters": {},
    })
    assert isinstance(req_stop.parse_parameters(), EmptyParams)


def test_operation_request_rejects_invalid_params_for_operation():
    # Deploy with bad tail
    with pytest.raises(ValidationError):
        OperationRequest.model_validate({
            "version": "1.0",
            "operation": "workload.deploy",
            "target": "snappymail",
            "parameters": {"tail": 50},  # Deploy does not accept tail
        })

    # Logs with out-of-bounds tail
    with pytest.raises(ValidationError):
        OperationRequest.model_validate({
            "version": "1.0",
            "operation": "workload.logs",
            "target": "snappymail",
            "parameters": {"tail": 9999},
        })

    # Stop with extra params
    with pytest.raises(ValidationError):
        OperationRequest.model_validate({
            "version": "1.0",
            "operation": "workload.stop",
            "target": "snappymail",
            "parameters": {"force": True},
        })


def test_operation_request_rejects_extra_top_level_fields():
    request = {
        "version": "1.0",
        "operation": "platform.health",
        "prohibited_extra": "attack",
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationRequest.model_validate(request)
    assert "extra_forbidden" in [e["type"] for e in exc_info.value.errors()]


def test_operation_request_backward_compatibility_aliases():
    # Accepts 'action' alias for 'operation', and 'params' alias for 'parameters'
    request = {
        "action": "workload.deploy",
        "target": "jellyfin",
        "params": {"recreate": True},
    }
    validated = OperationRequest.model_validate(request)
    assert validated.operation == OperationType.WORKLOAD_DEPLOY
    assert validated.action == OperationType.WORKLOAD_DEPLOY
    assert validated.parameters == {"recreate": True}
    assert validated.params == {"recreate": True}


# ---------------------------------------------------------------------------
# Closed 6-Code Error Taxonomy
# ---------------------------------------------------------------------------


def test_error_code_taxonomy_contains_exactly_6_canonical_codes():
    assert len(ErrorCode) == 6
    expected_codes = {
        "ERR_VALIDATION_FAILED",
        "ERR_AUTH_REQUIRED",
        "ERR_PERMISSION_DENIED",
        "ERR_TARGET_NOT_FOUND",
        "ERR_CONCURRENT_MUTATION",
        "ERR_EXECUTION_FAILED",
    }
    assert {code.value for code in ErrorCode} == expected_codes


def test_error_code_http_status_mapping():
    assert ErrorCode.ERR_VALIDATION_FAILED.http_status == 400
    assert ErrorCode.ERR_AUTH_REQUIRED.http_status == 401
    assert ErrorCode.ERR_PERMISSION_DENIED.http_status == 403
    assert ErrorCode.ERR_TARGET_NOT_FOUND.http_status == 404
    assert ErrorCode.ERR_CONCURRENT_MUTATION.http_status == 409
    assert ErrorCode.ERR_EXECUTION_FAILED.http_status == 500


def test_operation_error_accepts_valid_error_code():
    err = OperationError(
        code=ErrorCode.ERR_AUTH_REQUIRED,
        message="Bearer token required",
    )
    assert err.code == ErrorCode.ERR_AUTH_REQUIRED
    assert err.message == "Bearer token required"
    assert err.details is None


def test_operation_error_accepts_string_error_code_coercion():
    err = OperationError.model_validate({
        "code": "ERR_CONCURRENT_MUTATION",
        "message": "Operation locked by concurrent task",
    })
    assert err.code == ErrorCode.ERR_CONCURRENT_MUTATION


def test_operation_error_rejects_invalid_error_code():
    with pytest.raises(ValidationError):
        OperationError.model_validate({
            "code": "ERR_CUSTOM_CODE",
            "message": "Invalid code",
        })


def test_operation_error_rejects_extra_fields():
    with pytest.raises(ValidationError):
        OperationError.model_validate({
            "code": "ERR_VALIDATION_FAILED",
            "message": "Failed",
            "secret_leak": "value",
        })


# ---------------------------------------------------------------------------
# Error Sanitizer (Information Disclosure Prevention)
# ---------------------------------------------------------------------------


def test_sanitize_validation_error_strips_input_and_url():
    # Create a model with secret value that fails validation
    class SecretPayload(BaseModel):
        token: str = "default"

    try:
        LogsParams.model_validate({
            "tail": "SUPER_SECRET_TOKEN_DO_NOT_LEAK",
        })
    except ValidationError as exc:
        op_err = sanitize_validation_error(exc)

    assert op_err.code == ErrorCode.ERR_VALIDATION_FAILED
    assert op_err.message == "Request payload validation failed"
    assert op_err.details is not None
    assert len(op_err.details) == 1
    detail = op_err.details[0]

    # Ensure neither input nor url appears in any details dictionary
    assert "input" not in detail
    assert "url" not in detail
    assert "SUPER_SECRET_TOKEN_DO_NOT_LEAK" not in str(detail)
    assert detail["loc"] == ["tail"]
    assert detail["type"] == "int_type"
    assert "Input should be a valid integer" in detail["msg"]

    # Verify defense-in-depth: exc.errors called with include_input=False and include_url=False
    from unittest.mock import MagicMock
    mock_exc = MagicMock(spec=ValidationError)
    mock_exc.errors.return_value = [{"loc": ("field",), "type": "value_error", "msg": "Invalid"}]
    sanitize_validation_error(mock_exc)
    mock_exc.errors.assert_called_once_with(include_input=False, include_url=False)


# ---------------------------------------------------------------------------
# OperationResponse Consistency & Edge Cases
# ---------------------------------------------------------------------------


def test_operation_response_accepts_success_without_error():
    response_data = {
        "version": "1.0",
        "success": True,
        "operation_id": "op_123",
        "operation": "workload.deploy",
        "target": "snappymail",
        "output": "Snappymail deployment complete",
    }
    validated = OperationResponse.model_validate(response_data)
    assert validated.success is True
    assert validated.status == "success"
    assert validated.error is None
    assert validated.operation == OperationType.WORKLOAD_DEPLOY
    assert validated.action == OperationType.WORKLOAD_DEPLOY
    assert validated.operation_id == "op_123"


def test_operation_response_accepts_status_field_reconciliation():
    # Pass 'status': 'success' without 'success'
    r1 = OperationResponse.model_validate({
        "version": "1.0",
        "status": "success",
        "operation_id": "op_123",
        "operation": "platform.health",
    })
    assert r1.success is True
    assert r1.status == "success"

    # Pass 'status': 'error' without 'success'
    r2 = OperationResponse.model_validate({
        "version": "1.0",
        "status": "error",
        "operation_id": "op_124",
        "operation": "platform.health",
        "error": {
            "code": "ERR_EXECUTION_FAILED",
            "message": "Health probe failed",
        },
    })
    assert r2.success is False
    assert r2.status == "error"


def test_operation_response_accepts_failure_with_nested_error():
    response_data = {
        "version": "1.0",
        "success": False,
        "operation_id": "op_456",
        "operation": "workload.deploy",
        "target": "capybara",
        "error": {
            "code": "ERR_TARGET_NOT_FOUND",
            "message": "Workload `capybara` does not exist",
        },
    }
    validated = OperationResponse.model_validate(response_data)
    assert validated.success is False
    assert validated.target == "capybara"
    assert validated.operation_id == "op_456"
    assert validated.operation == OperationType.WORKLOAD_DEPLOY
    assert isinstance(validated.error, OperationError)
    assert validated.error.code == ErrorCode.ERR_TARGET_NOT_FOUND
    assert validated.error.message == "Workload `capybara` does not exist"


def test_operation_response_rejects_success_with_error():
    response_data = {
        "version": "1.0",
        "success": True,
        "operation": "workload.deploy",
        "target": "microservice1",
        "operation_id": "op_124",
        "error": {
            "code": "ERR_VALIDATION_FAILED",
            "message": "This error should not appear on success response",
        },
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationResponse.model_validate(response_data)
    assert "cannot contain an error" in str(exc_info.value)


def test_operation_response_rejects_failure_without_error():
    response_data = {
        "version": "1.0",
        "success": False,
        "operation": "workload.deploy",
        "target": "microservice2",
        "operation_id": "op_125",
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationResponse.model_validate(response_data)
    assert "must contain an error" in str(exc_info.value)


def test_operation_response_rejects_inconsistent_status_and_success():
    response_data = {
        "version": "1.0",
        "success": True,
        "status": "error",
        "operation": "platform.health",
        "operation_id": "op_126",
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationResponse.model_validate(response_data)
    assert "'status' and 'success' must agree" in str(exc_info.value)


def test_operation_response_rejects_extra_fields():
    response_data = {
        "version": "1.0",
        "success": True,
        "operation": "platform.health",
        "operation_id": "op_127",
        "unauthorized_field": "data",
    }
    with pytest.raises(ValidationError) as exc_info:
        OperationResponse.model_validate(response_data)
    assert "extra_forbidden" in [e["type"] for e in exc_info.value.errors()]


# ---------------------------------------------------------------------------
# Core Manifest compose_pull Action
# ---------------------------------------------------------------------------


def test_core_manifest_allows_compose_pull_action():
    assert "compose_pull" in ALLOWED_ACTIONS

    valid_manifest = {
        "schema_version": "1.0",
        "name": "snappymail",
        "source": {
            "type": "git",
            "url": "https://github.com/example/snappymail.git",
            "ref": "main",
        },
        "deployment": {
            "strategy": "compose",
            "actions": ["git_pull", "compose_pull", "compose_up"],
        },
    }
    parsed = Manifest.model_validate(valid_manifest)
    assert parsed.deployment.actions == ["git_pull", "compose_pull", "compose_up"]