"""
appctl_auth.py — Authentication & 3-Role RBAC Policy Enforcement (P4 Slice 3)

Implements the zero-trust authorization pipeline for homelab-appctl.service:
- SOPS-managed SHA-256 Bearer token store loader (mode 0400 check, constant-time compare)
- Unix domain socket SO_PEERCRED fallback to homelab-viewer for zero-friction local reads
- Mandatory Bearer tokens for all network (HTTP) operations and all mutations / sensitive reads
- 3-role RBAC enforcement (homelab-admin, homelab-operator, homelab-viewer)
- Workload scope gating against config/deployments.yaml for homelab-operator
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from appctl_contracts import (
    ErrorCode,
    OperationType,
    StrictModel,
)
from pydantic import BeforeValidator, ConfigDict, Field, StrictStr

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

DEFAULT_TOKENS_FILE = Path("/run/secrets/appctl/tokens.json")
DEFAULT_DEPLOYMENTS_FILE = Path(__file__).resolve().parent.parent / "config" / "deployments.yaml"

CORE_TARGETS: frozenset[str] = frozenset({
    "core",
    "traefik",
    "authelia",
    "lldap",
    "stalwart",
    "dozzle",
    "socket-proxy",
    "homepage",
    "diun",
    "watchtower",
})

SENSITIVE_OPERATIONS: frozenset[OperationType] = frozenset({
    OperationType.WORKLOAD_CONFIG,
    OperationType.WORKLOAD_LOGS,
})

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
})


class Role(StrEnum):
    """Hierarchical authorization roles for homelab-appctl.service."""

    ADMIN = "homelab-admin"
    OPERATOR = "homelab-operator"
    VIEWER = "homelab-viewer"


def _coerce_role(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return Role(v)
        except ValueError:
            return v
    return v


FlexibleRole = Annotated[
    Role,
    BeforeValidator(_coerce_role),
]


class Principal(StrictModel):
    """Authenticated caller identity and operational role."""

    model_config = ConfigDict(extra="forbid", strict=True)

    identity: StrictStr
    role: FlexibleRole
    token_id: StrictStr | None = None
    auth_method: Literal["bearer_token", "unix_peercred"]


class TokenDescriptor(StrictModel):
    """Internal model for token entries in SOPS tokens.json store."""

    model_config = ConfigDict(extra="forbid", strict=True)

    token_id: StrictStr
    identity: StrictStr
    role: FlexibleRole
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-fA-F]{64}$")]


class AuthenticationError(Exception):
    """Raised when authentication fails (401 Unauthorized)."""

    def __init__(self, message: str = "Authentication required", code: ErrorCode = ErrorCode.ERR_AUTH_REQUIRED) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = 401


class AuthorizationError(Exception):
    """Raised when an authenticated principal lacks required role or scope (403 Forbidden)."""

    def __init__(self, message: str = "Permission denied", code: ErrorCode = ErrorCode.ERR_PERMISSION_DENIED) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = 403


class TokenStore:
    """Loads and validates SHA-256 Bearer tokens from a mode 0400 store."""

    def __init__(self, tokens_path: Path | str | None = None) -> None:
        if tokens_path is not None:
            self.tokens_path = Path(tokens_path)
        elif "APPCTL_TOKENS_FILE" in os.environ:
            self.tokens_path = Path(os.environ["APPCTL_TOKENS_FILE"])
        else:
            self.tokens_path = DEFAULT_TOKENS_FILE

    def _verify_permissions(self) -> None:
        """Enforce strict 0400 file mode: readable only by owner, no write/exec, no group/world."""
        if not self.tokens_path.is_file():
            raise AuthenticationError(
                f"Token store '{self.tokens_path}' not found",
                code=ErrorCode.ERR_AUTH_REQUIRED,
            )
        # Check permissions: reject if group/world readable or writable (must be 0400)
        mode = stat.S_IMODE(self.tokens_path.stat().st_mode)
        if (mode & 0o077) != 0 or (mode & 0o200) != 0:
            raise AuthenticationError(
                f"Token store '{self.tokens_path}' has insecure mode {oct(mode)}; mode 0400 required",
                code=ErrorCode.ERR_AUTH_REQUIRED,
            )

    def load_tokens(self) -> list[TokenDescriptor]:
        """Load and parse token descriptors from the token store."""
        self._verify_permissions()
        try:
            content = self.tokens_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if not isinstance(data, list):
                raise TypeError("Tokens payload must be an array of token descriptors")
            return [TokenDescriptor.model_validate(item) for item in data]
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as err:
            raise AuthenticationError(
                f"Failed to load token store '{self.tokens_path}': {err}",
                code=ErrorCode.ERR_AUTH_REQUIRED,
            ) from err

    def authenticate_bearer(self, secret: str) -> Principal:
        """
        Hash secret with SHA-256 and look up against stored token descriptors using
        constant-time hmac.compare_digest.
        """
        if not secret:
            raise AuthenticationError("Empty Bearer token provided", code=ErrorCode.ERR_AUTH_REQUIRED)

        secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        tokens = self.load_tokens()

        for token in tokens:
            if hmac.compare_digest(secret_hash, token.sha256):
                return Principal(
                    identity=token.identity,
                    role=token.role,
                    token_id=token.token_id,
                    auth_method="bearer_token",
                )

        raise AuthenticationError("Invalid or unknown Bearer token", code=ErrorCode.ERR_AUTH_REQUIRED)


def load_admitted_workloads(deployments_path: Path | str | None = None) -> set[str]:
    """
    Load admitted repository names and aliases from config/deployments.yaml.
    Fails closed (empty set) if registry is missing or malformed.
    """
    if deployments_path is not None:
        reg_file = Path(deployments_path)
    elif "HOMELAB_DEPLOYMENTS_FILE" in os.environ:
        reg_file = Path(os.environ["HOMELAB_DEPLOYMENTS_FILE"])
    else:
        reg_file = DEFAULT_DEPLOYMENTS_FILE

    if not reg_file.is_file():
        return set()

    yaml_errors = (yaml.YAMLError,) if yaml is not None else ()
    try:
        content = reg_file.read_text(encoding="utf-8")
        if yaml is not None:
            data = yaml.safe_load(content)
        else:
            data = json.loads(content)
        if not isinstance(data, dict):
            return set()

        admitted: set[str] = set()
        repos = data.get("repositories", {})
        if isinstance(repos, dict):
            for name, entry in repos.items():
                admitted.add(name)
                if isinstance(entry, dict):
                    aliases = entry.get("aliases", [])
                    if isinstance(aliases, list):
                        for alias in aliases:
                            if isinstance(alias, str) and alias.strip():
                                admitted.add(alias.strip())
        return admitted
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError, *yaml_errors):
        return set()


def authenticate_request(
    transport: Literal["unix_socket", "http"],
    auth_header: str | None,
    peer_uid: int | None = None,
    token_store: TokenStore | None = None,
) -> Principal:
    """
    Authenticate an incoming request across transports:
    - If Authorization header present: parses Bearer secret and verifies against TokenStore.
    - If Authorization header missing over HTTP: fails closed with 401 ERR_AUTH_REQUIRED.
    - If Authorization header missing over Unix socket: falls back to homelab-viewer with peer_uid.
    """
    store = token_store or TokenStore()

    if auth_header is not None:
        header_val = auth_header.strip()
        if not header_val.startswith("Bearer "):
            raise AuthenticationError(
                "Malformed Authorization header: must start with 'Bearer '",
                code=ErrorCode.ERR_AUTH_REQUIRED,
            )
        secret = header_val[7:].strip()
        if not secret:
            raise AuthenticationError(
                "Missing token secret in Authorization header",
                code=ErrorCode.ERR_AUTH_REQUIRED,
            )
        return store.authenticate_bearer(secret)

    # Missing Authorization header
    if transport == "unix_socket":
        if peer_uid is not None:
            return Principal(
                identity=f"unix_uid_{peer_uid}",
                role=Role.VIEWER,
                token_id=None,
                auth_method="unix_peercred",
            )
        raise AuthenticationError(
            "Missing peer credentials over Unix socket",
            code=ErrorCode.ERR_AUTH_REQUIRED,
        )

    # HTTP transport requires Bearer token strictly
    raise AuthenticationError(
        "Bearer token required over network transport",
        code=ErrorCode.ERR_AUTH_REQUIRED,
    )


def enforce_rbac(
    principal: Principal,
    operation: OperationType,
    target: str | None = None,
    admitted_workloads: set[str] | None = None,
) -> None:
    """
    Enforce 3-role RBAC policy (homelab-admin, homelab-operator, homelab-viewer).
    Raises AuthorizationError (403) on violation.
    """
    # 1. homelab-admin has unrestricted access across all operations and targets
    if principal.role == Role.ADMIN:
        return

    # 2. homelab-operator:
    if principal.role == Role.OPERATOR:
        # Platform homepage sync allowed
        if operation == OperationType.PLATFORM_SYNC_HOMEPAGE:
            return

        # Target checks for operations targeting infrastructure
        if target is not None and (target in CORE_TARGETS or target == "core"):
            raise AuthorizationError(
                f"Role 'homelab-operator' is not permitted to target core service '{target}'",
                code=ErrorCode.ERR_PERMISSION_DENIED,
            )

        # Non-sensitive reads are permitted
        if operation in READ_ONLY_OPERATIONS:
            return

        # Sensitive reads and mutations must target an admitted workload from deployments.yaml
        if operation in SENSITIVE_OPERATIONS or operation in MUTATING_OPERATIONS:
            if not target:
                raise AuthorizationError(
                    f"Operation '{operation.value}' requires an explicit workload target",
                    code=ErrorCode.ERR_PERMISSION_DENIED,
                )
            registry = (
                admitted_workloads
                if admitted_workloads is not None
                else load_admitted_workloads()
            )
            if target not in registry:
                raise AuthorizationError(
                    f"Target '{target}' is not registered in deployments.yaml for role 'homelab-operator'",
                    code=ErrorCode.ERR_PERMISSION_DENIED,
                )
            return

    # 3. homelab-viewer:
    if principal.role == Role.VIEWER:
        if operation in MUTATING_OPERATIONS:
            raise AuthorizationError(
                f"Role 'homelab-viewer' is not permitted to execute mutating operation '{operation.value}'",
                code=ErrorCode.ERR_PERMISSION_DENIED,
            )
        if operation in SENSITIVE_OPERATIONS:
            raise AuthorizationError(
                f"Role 'homelab-viewer' is not permitted to access sensitive operation '{operation.value}'",
                code=ErrorCode.ERR_PERMISSION_DENIED,
            )
        if operation in READ_ONLY_OPERATIONS:
            return

    raise AuthorizationError(
        f"Operation '{operation.value}' denied for role '{principal.role.value}'",
        code=ErrorCode.ERR_PERMISSION_DENIED,
    )
