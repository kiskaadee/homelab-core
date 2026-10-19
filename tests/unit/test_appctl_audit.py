"""
tests/unit/test_appctl_audit.py — Unit Tests for Audit Logger & Concurrency Lock (P4 Slice 2)

Behavior-driven test naming convention:
test_<subject>_<accepts|rejects|enforces>_<condition>
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from appctl_audit import (
    AuditEvent,
    AuditLogger,
    AuditPersistenceError,
    AuditPhase,
    ConcurrentMutationError,
    PrincipalAudit,
    TargetLockManager,
    redact_sensitive_data,
)


@pytest.fixture
def temp_audit_file(tmp_path: Path) -> Path:
    return tmp_path / "audit" / "audit.jsonl"


@pytest.fixture
def temp_lock_dir(tmp_path: Path) -> Path:
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


@pytest.fixture
def sample_principal() -> PrincipalAudit:
    return PrincipalAudit(
        identity="operator",
        role="homelab-admin",
        token_id="tok_admin_01",
        auth_method="bearer_token",
    )


# ---------------------------------------------------------------------------
# File Permissions & Initialization
# ---------------------------------------------------------------------------


def test_audit_logger_enforces_0600_permissions_on_creation(
    temp_audit_file: Path, sample_principal: PrincipalAudit
):
    logger = AuditLogger(temp_audit_file)
    logger.log_dispatched(
        operation_id="op_100",
        principal=sample_principal,
        action="workload.deploy",
        target="snappymail",
        transport="unix_socket",
        origin="peer_uid_1000",
        client="appctl-cli/2.0",
        parameters={"recreate": True},
    )

    assert temp_audit_file.exists()
    mode = stat.S_IMODE(temp_audit_file.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# Audit Phases & RFC JSONL Schema
# ---------------------------------------------------------------------------


def test_audit_logger_writes_valid_jsonl_lifecycle(
    temp_audit_file: Path, sample_principal: PrincipalAudit
):
    logger = AuditLogger(temp_audit_file)

    # 1. DISPATCHED
    evt_disp = logger.log_dispatched(
        operation_id="op_101",
        principal=sample_principal,
        action="workload.update",
        target="jellyfin",
        transport="http",
        origin="127.0.0.1:48291",
        client="gitops-dispatcher/1.0",
        parameters={"expected_branch": "main", "build": False},
    )
    assert evt_disp.phase == AuditPhase.DISPATCHED
    assert evt_disp.result is not None
    assert evt_disp.result.status == "IN_PROGRESS"

    # 2. COMPLETED
    evt_comp = logger.log_completed(
        operation_id="op_101",
        principal=sample_principal,
        action="workload.update",
        target="jellyfin",
        transport="http",
        origin="127.0.0.1:48291",
        client="gitops-dispatcher/1.0",
        parameters={"expected_branch": "main", "build": False},
        duration_ms=1450,
        commit_sha="a1b2c3d",
        message="Workload updated and reconciled successfully",
    )
    assert evt_comp.phase == AuditPhase.COMPLETED
    assert evt_comp.result is not None
    assert evt_comp.result.status == "SUCCESS"
    assert evt_comp.result.exit_code == 0
    assert evt_comp.result.commit_sha == "a1b2c3d"

    # Read and validate JSONL file contents
    lines = temp_audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    parsed_disp = AuditEvent.model_validate(json.loads(lines[0]))
    assert parsed_disp.phase == AuditPhase.DISPATCHED
    assert parsed_disp.operation_id == "op_101"

    parsed_comp = AuditEvent.model_validate(json.loads(lines[1]))
    assert parsed_comp.phase == AuditPhase.COMPLETED
    assert parsed_comp.operation_id == "op_101"


def test_audit_logger_writes_failed_event(
    temp_audit_file: Path, sample_principal: PrincipalAudit
):
    logger = AuditLogger(temp_audit_file)
    logger.log_failed(
        operation_id="op_102",
        principal=sample_principal,
        action="workload.deploy",
        target="jellyfin",
        transport="unix_socket",
        origin="peer_uid_1000",
        client="appctl-cli/2.0",
        parameters={"recreate": True},
        duration_ms=320,
        exit_code=1,
        error_code="ERR_EXECUTION_FAILED",
        message="Docker compose failed with exit code 1",
    )

    lines = temp_audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = AuditEvent.model_validate(json.loads(lines[0]))
    assert event.phase == AuditPhase.FAILED
    assert event.result is not None
    assert event.result.status == "FAILED"
    assert event.result.exit_code == 1
    assert event.result.error_code == "ERR_EXECUTION_FAILED"


def test_audit_logger_writes_denied_event(
    temp_audit_file: Path, sample_principal: PrincipalAudit
):
    logger = AuditLogger(temp_audit_file)
    logger.log_denied(
        operation_id="op_103",
        principal=sample_principal,
        action="workload.config",
        target="core",
        transport="http",
        origin="127.0.0.1:49000",
        client="curl/8.1",
        parameters={},
        error_code="ERR_PERMISSION_DENIED",
        message="Role homelab-operator cannot target core stack",
    )

    lines = temp_audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    event = AuditEvent.model_validate(json.loads(lines[0]))
    assert event.phase == AuditPhase.DENIED
    assert event.result is not None
    assert event.result.status == "DENIED"
    assert event.result.error_code == "ERR_PERMISSION_DENIED"


# ---------------------------------------------------------------------------
# Secret Redaction
# ---------------------------------------------------------------------------


def test_redact_sensitive_data_masks_secret_keys():
    raw_params = {
        "API_KEY": "supersecretkey123",
        "WEBHOOK_SECRET": "whsec_9999",
        "db_password": "mypassword",
        "AUTH_TOKEN": "tok_12345",
        "SSH_KEY": "ssh-ed25519 AAA...",
        "PASSWORD_HASH": "$2b$12$...",
        "recreate": True,
        "nested": {
            "SECRET_PASS": "pass123",
            "normal_config": "enabled",
            "list_items": [
                {"ITEM_SECRET": "leak", "id": 1},
                "safe_string",
            ],
        },
    }

    redacted = redact_sensitive_data(raw_params)
    assert redacted["API_KEY"] == "[REDACTED]"
    assert redacted["WEBHOOK_SECRET"] == "[REDACTED]"
    assert redacted["db_password"] == "[REDACTED]"
    assert redacted["AUTH_TOKEN"] == "[REDACTED]"
    assert redacted["SSH_KEY"] == "[REDACTED]"
    assert redacted["PASSWORD_HASH"] == "[REDACTED]"
    assert redacted["recreate"] is True
    assert redacted["nested"]["SECRET_PASS"] == "[REDACTED]"
    assert redacted["nested"]["normal_config"] == "enabled"
    assert redacted["nested"]["list_items"][0]["ITEM_SECRET"] == "[REDACTED]"
    assert redacted["nested"]["list_items"][0]["id"] == 1
    assert redacted["nested"]["list_items"][1] == "safe_string"


def test_audit_logger_automatically_redacts_sensitive_parameters(
    temp_audit_file: Path, sample_principal: PrincipalAudit
):
    logger = AuditLogger(temp_audit_file)
    logger.log_dispatched(
        operation_id="op_104",
        principal=sample_principal,
        action="workload.deploy",
        target="vaultwarden",
        transport="unix_socket",
        origin="peer_uid_1000",
        client="appctl-cli/2.0",
        parameters={"ADMIN_TOKEN": "super_token_leak", "recreate": True},
    )

    content = temp_audit_file.read_text(encoding="utf-8")
    assert "super_token_leak" not in content
    assert "[REDACTED]" in content


# ---------------------------------------------------------------------------
# Pre-Execution Fail-Closed Disk Guard
# ---------------------------------------------------------------------------


def test_audit_logger_fails_closed_when_audit_log_unwritable(
    tmp_path: Path, sample_principal: PrincipalAudit
):
    read_only_dir = tmp_path / "readonly_audit"
    read_only_dir.mkdir(parents=True, exist_ok=True)
    unwritable_file = read_only_dir / "audit.jsonl"
    unwritable_file.touch(mode=0o400)
    os.chmod(unwritable_file, 0o400)

    # In addition, make directory read-only
    os.chmod(read_only_dir, 0o500)

    try:
        logger = AuditLogger(unwritable_file)
        with pytest.raises(AuditPersistenceError) as exc_info:
            logger.log_dispatched(
                operation_id="op_105",
                principal=sample_principal,
                action="workload.deploy",
                target="jellyfin",
                transport="unix_socket",
                origin="peer_uid_1000",
                client="appctl-cli/2.0",
                parameters={"recreate": True},
            )
        assert "Failed to persist audit event" in str(exc_info.value)
    finally:
        os.chmod(read_only_dir, 0o700)
        os.chmod(unwritable_file, 0o600)


# ---------------------------------------------------------------------------
# Concurrency & Non-Blocking Flock Manager
# ---------------------------------------------------------------------------


def test_target_lock_manager_acquires_and_releases_lock(temp_lock_dir: Path):
    manager = TargetLockManager(temp_lock_dir)

    with manager.lock_target("snappymail") as lock:
        assert lock.target == "snappymail"
        assert lock.lock_path.exists()
        # Verify mode 0600 on lock file
        assert stat.S_IMODE(lock.lock_path.stat().st_mode) == 0o600

    # Lock can be re-acquired immediately after release
    with manager.lock_target("snappymail"):
        pass


def test_target_lock_manager_rejects_concurrent_mutation(temp_lock_dir: Path):
    manager = TargetLockManager(temp_lock_dir)

    with (
        manager.lock_target("jellyfin"),
        pytest.raises(ConcurrentMutationError) as exc_info,
        manager.lock_target("jellyfin"),
    ):
        pass

    assert exc_info.value.target == "jellyfin"
    assert "locked by a concurrent task" in str(exc_info.value)


def test_target_lock_manager_permits_independent_targets_concurrently(
    temp_lock_dir: Path,
):
    manager = TargetLockManager(temp_lock_dir)

    with manager.lock_target("app1"), manager.lock_target("app2"):
        pass


def test_homepage_sync_lock_acquires_designated_file(temp_lock_dir: Path):
    manager = TargetLockManager(temp_lock_dir)

    with (
        manager.homepage_sync_lock() as lock,
        pytest.raises(ConcurrentMutationError) as exc_info,
        manager.homepage_sync_lock(),
    ):
        pass

    assert lock.lock_path.name == "_homepage_sync.lock"
    assert exc_info.value.target == "_homepage_sync"
