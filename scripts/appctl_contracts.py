"""
appctl_contracts.py — Authoritative Control Plane API Contracts & Schema Validation

Defines the wire protocol and Pydantic v2 domain schemas for homelab-appctl.service:
- Canonical 13 domain operations (workload.*, platform.*)
- Fail-closed strict typing (ConfigDict(extra="forbid", strict=True))
- Operation-specific parameter models (DeployParams, UpdateParams, LogsParams, ListParams, GetParams)
- Closed 6-code error taxonomy mapped to HTTP/1.1 semantics
- Information-disclosure-preventing error sanitizer (stripping input and url)
- Dual-transport request/response models
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    """Base model propagating strict validation across all control plane models — ADR-0003."""

    model_config = ConfigDict(extra="forbid", strict=True, ignored_types=(dict,))



class OperationType(StrEnum):
    """The 13 canonical domain operations exposed by homelab-appctl.service."""

    # Read-Only Operations
    WORKLOAD_LIST = "workload.list"
    WORKLOAD_GET = "workload.get"
    WORKLOAD_RESOLVE = "workload.resolve"
    PLATFORM_SSL = "platform.ssl"
    PLATFORM_HEALTH = "platform.health"
    WORKLOAD_CONFIG = "workload.config"
    WORKLOAD_LOGS = "workload.logs"

    # Mutating Operations
    WORKLOAD_DEPLOY = "workload.deploy"
    WORKLOAD_STOP = "workload.stop"
    WORKLOAD_RESTART = "workload.restart"
    WORKLOAD_UPDATE = "workload.update"
    WORKLOAD_PULL = "workload.pull"
    PLATFORM_SYNC_HOMEPAGE = "platform.sync_homepage"


# Backward-compatibility alias
WorkloadAction = OperationType
CanonicalOperation = OperationType

MUTATING_OPERATIONS: frozenset[OperationType] = frozenset({
    OperationType.WORKLOAD_DEPLOY,
    OperationType.WORKLOAD_STOP,
    OperationType.WORKLOAD_RESTART,
    OperationType.WORKLOAD_UPDATE,
    OperationType.WORKLOAD_PULL,
    OperationType.PLATFORM_SYNC_HOMEPAGE,
})

READ_ONLY_OPERATIONS: frozenset[OperationType] = frozenset({
    OperationType.WORKLOAD_LIST,
    OperationType.WORKLOAD_GET,
    OperationType.WORKLOAD_RESOLVE,
    OperationType.PLATFORM_SSL,
    OperationType.PLATFORM_HEALTH,
    OperationType.WORKLOAD_CONFIG,
    OperationType.WORKLOAD_LOGS,
})

SENSITIVE_READ_OPERATIONS: frozenset[OperationType] = frozenset({
    OperationType.WORKLOAD_CONFIG,
    OperationType.WORKLOAD_LOGS,
})

TARGET_REQUIRED_OPERATIONS: frozenset[OperationType] = frozenset({
    OperationType.WORKLOAD_GET,
    OperationType.WORKLOAD_RESOLVE,
    OperationType.WORKLOAD_CONFIG,
    OperationType.WORKLOAD_LOGS,
    OperationType.WORKLOAD_DEPLOY,
    OperationType.WORKLOAD_STOP,
    OperationType.WORKLOAD_RESTART,
    OperationType.WORKLOAD_UPDATE,
    OperationType.WORKLOAD_PULL,
})


def _coerce_operation(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return OperationType(v)
        except ValueError:
            return v
    return v


FlexibleOperationType = Annotated[
    OperationType,
    BeforeValidator(_coerce_operation),
]
FlexibleWorkloadAction = FlexibleOperationType


class ErrorCode(StrEnum):
    """Canonical 6-code error taxonomy for homelab-appctl.service."""

    ERR_VALIDATION_FAILED = "ERR_VALIDATION_FAILED"
    ERR_AUTH_REQUIRED = "ERR_AUTH_REQUIRED"
    ERR_PERMISSION_DENIED = "ERR_PERMISSION_DENIED"
    ERR_TARGET_NOT_FOUND = "ERR_TARGET_NOT_FOUND"
    ERR_CONCURRENT_MUTATION = "ERR_CONCURRENT_MUTATION"
    ERR_EXECUTION_FAILED = "ERR_EXECUTION_FAILED"

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS[self]


ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.ERR_VALIDATION_FAILED: 400,
    ErrorCode.ERR_AUTH_REQUIRED: 401,
    ErrorCode.ERR_PERMISSION_DENIED: 403,
    ErrorCode.ERR_TARGET_NOT_FOUND: 404,
    ErrorCode.ERR_CONCURRENT_MUTATION: 409,
    ErrorCode.ERR_EXECUTION_FAILED: 500,
}


def _coerce_error_code(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return ErrorCode(v)
        except ValueError:
            return v
    return v


FlexibleErrorCode = Annotated[
    ErrorCode,
    BeforeValidator(_coerce_error_code),
]


class DeployParams(StrictModel):
    """Parameters for workload.deploy."""

    recreate: StrictBool = True
    remove_orphans: StrictBool = True
    build: StrictBool = False


class UpdateParams(StrictModel):
    """Parameters for workload.update."""

    expected_branch: Annotated[StrictStr, Field(min_length=1)] | None = None
    build: StrictBool = False


class LogsParams(StrictModel):
    """Parameters for workload.logs."""

    tail: StrictInt = Field(default=100, ge=1, le=1000)


class ListParams(StrictModel):
    """Parameters for workload.list."""

    fetch: StrictBool = False
    core: StrictBool = False
    ssl: StrictBool = False


class GetParams(StrictModel):
    """Parameters for workload.get."""

    ssl: StrictBool = False


class EmptyParams(StrictModel):
    """Parameters model for operations that accept no arguments."""


OPERATION_PARAMS_MAP: dict[OperationType, type[StrictModel]] = {
    OperationType.WORKLOAD_DEPLOY: DeployParams,
    OperationType.WORKLOAD_UPDATE: UpdateParams,
    OperationType.WORKLOAD_LOGS: LogsParams,
    OperationType.WORKLOAD_LIST: ListParams,
    OperationType.WORKLOAD_GET: GetParams,
    OperationType.WORKLOAD_STOP: EmptyParams,
    OperationType.WORKLOAD_RESTART: EmptyParams,
    OperationType.WORKLOAD_PULL: EmptyParams,
    OperationType.WORKLOAD_RESOLVE: EmptyParams,
    OperationType.PLATFORM_SSL: EmptyParams,
    OperationType.PLATFORM_HEALTH: EmptyParams,
    OperationType.WORKLOAD_CONFIG: EmptyParams,
    OperationType.PLATFORM_SYNC_HOMEPAGE: EmptyParams,
}


class OperationError(StrictModel):
    """Standardized error payload returned in failed OperationResponse payloads."""

    code: FlexibleErrorCode
    message: StrictStr
    details: list[dict[str, Any]] | None = None


def sanitize_validation_error(exc: ValidationError) -> OperationError:
    """
    Format and sanitize a Pydantic ValidationError into an OperationError with ERR_VALIDATION_FAILED,
    stripping raw user input and documentation URLs to prevent information disclosure.
    """
    raw_details = exc.errors(include_input=False, include_url=False)
    clean_details: list[dict[str, Any]] = []
    for d in raw_details:
        clean_details.append({
            "type": d.get("type"),
            "loc": list(d.get("loc", ())),
            "msg": d.get("msg"),
        })
    return OperationError(
        code=ErrorCode.ERR_VALIDATION_FAILED,
        message="Request payload validation failed",
        details=clean_details,
    )


class OperationRequest(StrictModel):
    """Defines the body requirements of an HTTP/1.1 operation request."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
        ignored_types=(dict,),
    )


    version: Literal["1.0"] = "1.0"
    operation: FlexibleOperationType = Field(
        validation_alias=AliasChoices("operation", "action")
    )
    target: Annotated[StrictStr, Field(min_length=1)] | None = None
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("parameters", "params"),
    )

    @property
    def action(self) -> OperationType:
        return self.operation

    @property
    def params(self) -> dict[str, Any]:
        return self.parameters

    @field_validator("parameters", mode="before")
    @classmethod
    def _coerce_parameters(cls, v: Any) -> Any:
        if isinstance(v, BaseModel):
            return v.model_dump()
        return v

    @model_validator(mode="after")
    def validate_request(self) -> OperationRequest:
        if self.operation in TARGET_REQUIRED_OPERATIONS and not self.target:
            raise ValueError(f"Operation '{self.operation.value}' requires a non-empty target")
        param_model = OPERATION_PARAMS_MAP[self.operation]
        param_model.model_validate(self.parameters)
        return self

    def parse_parameters(self) -> StrictModel:
        """Parse and return the typed parameter model for this operation."""
        param_model = OPERATION_PARAMS_MAP[self.operation]
        return param_model.model_validate(self.parameters)



class OperationResponse(StrictModel):
    """Defines the standard response payload for control plane operations."""

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    version: Literal["1.0"] = "1.0"
    success: StrictBool | None = None
    status: Literal["success", "error"] | None = None
    operation_id: StrictStr = Field(min_length=1)
    operation: FlexibleOperationType | StrictStr | None = Field(
        default=None,
        validation_alias=AliasChoices("operation", "action"),
    )
    target: StrictStr | None = None
    output: StrictStr = ""
    result: dict[str, Any] | None = None
    error: OperationError | None = None
    timestamp: StrictStr | None = None
    duration_ms: StrictInt | None = None

    @property
    def action(self) -> OperationType | str | None:
        return self.operation

    @model_validator(mode="after")
    def check_consistency(self) -> OperationResponse:
        if self.success is None and self.status is None:
            raise ValueError("Either 'success' or 'status' must be provided")
        if self.success is None and self.status is not None:
            self.success = self.status == "success"
        elif self.status is None and self.success is not None:
            self.status = "success" if self.success else "error"
        elif (
            self.success is not None
            and self.status is not None
            and (self.status == "success") != self.success
        ):
            raise ValueError("'status' and 'success' must agree")

        if self.success and self.error is not None:
            raise ValueError("Successful operation cannot contain an error")
        if not self.success and self.error is None:
            raise ValueError("Failed operation must contain an error")
        return self
