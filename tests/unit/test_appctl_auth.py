"""
tests/unit/test_appctl_auth.py — Unit Tests for Authentication & 3-Role RBAC (P4 Slice 3)

Behavior-driven test naming convention:
test_<subject>_<accepts|rejects|enforces>_<condition>
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from appctl_auth import (
    AuthenticationError,
    AuthorizationError,
    Principal,
    Role,
    TokenStore,
    authenticate_request,
    enforce_rbac,
    load_admitted_workloads,
)
from appctl_contracts import ErrorCode, OperationType


@pytest.fixture
def secret_tokens() -> dict[str, str]:
    return {
        "admin_secret": "sec_admin_9876543210abcdef",
        "operator_secret": "sec_operator_1234567890abcdef",
    }


@pytest.fixture
def temp_tokens_file(tmp_path: Path, secret_tokens: dict[str, str]) -> Path:
    tokens_path = tmp_path / "tokens.json"
    admin_hash = hashlib.sha256(secret_tokens["admin_secret"].encode("utf-8")).hexdigest()
    op_hash = hashlib.sha256(secret_tokens["operator_secret"].encode("utf-8")).hexdigest()

    data = [
        {
            "token_id": "tok_admin_01",
            "identity": "operator",
            "role": "homelab-admin",
            "sha256": admin_hash,
        },
        {
            "token_id": "tok_gitops_01",
            "identity": "gitops-dispatcher",
            "role": "homelab-operator",
            "sha256": op_hash,
        },
    ]
    tokens_path.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tokens_path, 0o400)
    return tokens_path


@pytest.fixture
def admitted_workloads() -> set[str]:
    return {"snappymail", "mail", "webmail", "jellyfin", "media", "docs"}


# ---------------------------------------------------------------------------
# TokenStore & Strict Permissions (0400)
# ---------------------------------------------------------------------------


def test_token_store_accepts_valid_0400_store(temp_tokens_file: Path):
    store = TokenStore(temp_tokens_file)
    tokens = store.load_tokens()
    assert len(tokens) == 2
    assert tokens[0].role == Role.ADMIN
    assert tokens[1].role == Role.OPERATOR


def test_token_store_rejects_insecure_file_permissions(tmp_path: Path):
    bad_tokens_file = tmp_path / "insecure_tokens.json"
    bad_tokens_file.write_text("[]", encoding="utf-8")

    # Insecure modes: 0644, 0600, 0777
    for insecure_mode in [0o644, 0o600, 0o777, 0o444]:
        os.chmod(bad_tokens_file, insecure_mode)
        store = TokenStore(bad_tokens_file)
        with pytest.raises(AuthenticationError) as exc_info:
            store.load_tokens()
        assert "mode 0400 required" in str(exc_info.value)
        assert exc_info.value.code == ErrorCode.ERR_AUTH_REQUIRED


def test_token_store_rejects_missing_file(tmp_path: Path):
    missing_file = tmp_path / "nonexistent.json"
    store = TokenStore(missing_file)
    with pytest.raises(AuthenticationError) as exc_info:
        store.load_tokens()
    assert "not found" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bearer Token Authentication
# ---------------------------------------------------------------------------


def test_token_store_authenticates_valid_bearer_secrets(
    temp_tokens_file: Path, secret_tokens: dict[str, str]
):
    store = TokenStore(temp_tokens_file)

    p_admin = store.authenticate_bearer(secret_tokens["admin_secret"])
    assert p_admin.identity == "operator"
    assert p_admin.role == Role.ADMIN
    assert p_admin.token_id == "tok_admin_01"
    assert p_admin.auth_method == "bearer_token"

    p_op = store.authenticate_bearer(secret_tokens["operator_secret"])
    assert p_op.identity == "gitops-dispatcher"
    assert p_op.role == Role.OPERATOR
    assert p_op.token_id == "tok_gitops_01"


def test_token_store_rejects_invalid_bearer_secret(temp_tokens_file: Path):
    store = TokenStore(temp_tokens_file)

    with pytest.raises(AuthenticationError) as exc_info:
        store.authenticate_bearer("wrong_secret_token_123")
    assert "Invalid or unknown Bearer token" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.ERR_AUTH_REQUIRED


def test_token_store_rejects_empty_bearer_secret(temp_tokens_file: Path):
    store = TokenStore(temp_tokens_file)

    with pytest.raises(AuthenticationError) as exc_info:
        store.authenticate_bearer("")
    assert "Empty Bearer token provided" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Transport Authentication Rules (HTTP vs. Unix Domain Socket)
# ---------------------------------------------------------------------------


def test_authenticate_request_over_http_requires_valid_bearer(
    temp_tokens_file: Path, secret_tokens: dict[str, str]
):
    store = TokenStore(temp_tokens_file)

    # Valid Bearer token
    auth_header = f"Bearer {secret_tokens['admin_secret']}"
    p = authenticate_request("http", auth_header, token_store=store)
    assert p.role == Role.ADMIN

    # Missing Authorization header -> 401
    with pytest.raises(AuthenticationError) as exc_info:
        authenticate_request("http", None, token_store=store)
    assert "Bearer token required over network transport" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.ERR_AUTH_REQUIRED

    # Malformed Authorization header -> 401
    with pytest.raises(AuthenticationError):
        authenticate_request("http", "Basic dXNlcjpwYXNz", token_store=store)


def test_authenticate_request_over_uds_falls_back_to_viewer(
    temp_tokens_file: Path, secret_tokens: dict[str, str]
):
    store = TokenStore(temp_tokens_file)

    # Missing Authorization header over UDS with peer_uid=1000
    p = authenticate_request("unix_socket", None, peer_uid=1000, token_store=store)
    assert p.identity == "unix_uid_1000"
    assert p.role == Role.VIEWER
    assert p.token_id is None
    assert p.auth_method == "unix_peercred"

    # Explicit Bearer token over UDS confers full privileges
    auth_header = f"Bearer {secret_tokens['admin_secret']}"
    p_admin = authenticate_request("unix_socket", auth_header, peer_uid=1000, token_store=store)
    assert p_admin.role == Role.ADMIN
    assert p_admin.auth_method == "bearer_token"


def test_authenticate_request_over_uds_rejects_missing_peer_uid():
    with pytest.raises(AuthenticationError) as exc_info:
        authenticate_request("unix_socket", None, peer_uid=None)
    assert "Missing peer credentials" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Deployments Registry Scope Loader
# ---------------------------------------------------------------------------


def test_load_admitted_workloads_parses_names_and_aliases(tmp_path: Path):
    deploy_file = tmp_path / "deployments.yaml"
    deploy_file.write_text("""
schemaVersion: "1.0"
repositories:
  snappymail:
    path: "/home/operator/Sites/snappymail"
    aliases:
      - "mail"
      - "webmail"
  jellyfin:
    path: "/home/operator/Sites/homelab-jellyfin"
    aliases:
      - "media"
""", encoding="utf-8")

    admitted = load_admitted_workloads(deploy_file)
    assert "snappymail" in admitted
    assert "mail" in admitted
    assert "webmail" in admitted
    assert "jellyfin" in admitted
    assert "media" in admitted
    assert "unknown_app" not in admitted


def test_load_admitted_workloads_fails_closed_on_missing_file(tmp_path: Path):
    admitted = load_admitted_workloads(tmp_path / "missing.yaml")
    assert admitted == set()


# ---------------------------------------------------------------------------
# RBAC Policy Matrix Enforcement
# ---------------------------------------------------------------------------


def test_rbac_admin_permitted_across_all_operations_and_targets(
    admitted_workloads: set[str],
):
    admin = Principal(identity="admin_user", role=Role.ADMIN, auth_method="bearer_token")

    # Mutations across Core and Sites
    enforce_rbac(admin, OperationType.WORKLOAD_DEPLOY, "core", admitted_workloads)
    enforce_rbac(admin, OperationType.WORKLOAD_DEPLOY, "traefik", admitted_workloads)
    enforce_rbac(admin, OperationType.WORKLOAD_DEPLOY, "unregistered_site", admitted_workloads)
    enforce_rbac(admin, OperationType.WORKLOAD_CONFIG, "core", admitted_workloads)
    enforce_rbac(admin, OperationType.WORKLOAD_LOGS, "traefik", admitted_workloads)
    enforce_rbac(admin, OperationType.PLATFORM_SYNC_HOMEPAGE, None, admitted_workloads)
    enforce_rbac(admin, OperationType.WORKLOAD_LIST, None, admitted_workloads)


def test_rbac_operator_permitted_on_admitted_workloads(
    admitted_workloads: set[str],
):
    operator = Principal(identity="gitops_bot", role=Role.OPERATOR, auth_method="bearer_token")

    # Permitted on admitted workload
    enforce_rbac(operator, OperationType.WORKLOAD_DEPLOY, "snappymail", admitted_workloads)
    enforce_rbac(operator, OperationType.WORKLOAD_UPDATE, "mail", admitted_workloads)
    enforce_rbac(operator, OperationType.WORKLOAD_CONFIG, "jellyfin", admitted_workloads)
    enforce_rbac(operator, OperationType.WORKLOAD_LOGS, "docs", admitted_workloads)
    enforce_rbac(operator, OperationType.PLATFORM_SYNC_HOMEPAGE, None, admitted_workloads)
    enforce_rbac(operator, OperationType.WORKLOAD_LIST, None, admitted_workloads)


def test_rbac_operator_denied_on_core_targets(
    admitted_workloads: set[str],
):
    operator = Principal(identity="gitops_bot", role=Role.OPERATOR, auth_method="bearer_token")

    # Targeting "core" stack
    with pytest.raises(AuthorizationError) as exc_info:
        enforce_rbac(operator, OperationType.WORKLOAD_DEPLOY, "core", admitted_workloads)
    assert "not permitted to target core service 'core'" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.ERR_PERMISSION_DENIED

    # Targeting core services (traefik, stalwart, etc.)
    for core_svc in ["traefik", "authelia", "stalwart", "socket-proxy"]:
        with pytest.raises(AuthorizationError) as exc_info:
            enforce_rbac(operator, OperationType.WORKLOAD_RESTART, core_svc, admitted_workloads)
        assert f"not permitted to target core service '{core_svc}'" in str(exc_info.value)


def test_rbac_operator_denied_on_unregistered_workload(
    admitted_workloads: set[str],
):
    operator = Principal(identity="gitops_bot", role=Role.OPERATOR, auth_method="bearer_token")

    with pytest.raises(AuthorizationError) as exc_info:
        enforce_rbac(operator, OperationType.WORKLOAD_DEPLOY, "unregistered_app", admitted_workloads)
    assert "not registered in deployments.yaml" in str(exc_info.value)
    assert exc_info.value.code == ErrorCode.ERR_PERMISSION_DENIED


def test_rbac_viewer_permitted_only_on_non_sensitive_reads():
    viewer = Principal(identity="unix_uid_1000", role=Role.VIEWER, auth_method="unix_peercred")

    # Permitted reads
    enforce_rbac(viewer, OperationType.WORKLOAD_LIST, None)
    enforce_rbac(viewer, OperationType.WORKLOAD_GET, "snappymail")
    enforce_rbac(viewer, OperationType.WORKLOAD_RESOLVE, "snappymail")
    enforce_rbac(viewer, OperationType.PLATFORM_SSL, "snappymail")
    enforce_rbac(viewer, OperationType.PLATFORM_HEALTH, None)


def test_rbac_viewer_denied_on_mutations():
    viewer = Principal(identity="unix_uid_1000", role=Role.VIEWER, auth_method="unix_peercred")

    mutations = [
        OperationType.WORKLOAD_DEPLOY,
        OperationType.WORKLOAD_STOP,
        OperationType.WORKLOAD_RESTART,
        OperationType.WORKLOAD_UPDATE,
        OperationType.WORKLOAD_PULL,
        OperationType.PLATFORM_SYNC_HOMEPAGE,
    ]
    for mut in mutations:
        with pytest.raises(AuthorizationError) as exc_info:
            enforce_rbac(viewer, mut, "snappymail")
        assert "not permitted to execute mutating operation" in str(exc_info.value)
        assert exc_info.value.code == ErrorCode.ERR_PERMISSION_DENIED


def test_rbac_viewer_denied_on_sensitive_reads():
    viewer = Principal(identity="unix_uid_1000", role=Role.VIEWER, auth_method="unix_peercred")

    for sens_op in [OperationType.WORKLOAD_CONFIG, OperationType.WORKLOAD_LOGS]:
        with pytest.raises(AuthorizationError) as exc_info:
            enforce_rbac(viewer, sens_op, "snappymail")
        assert "not permitted to access sensitive operation" in str(exc_info.value)
        assert exc_info.value.code == ErrorCode.ERR_PERMISSION_DENIED
