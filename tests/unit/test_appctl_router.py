"""
tests/unit/test_appctl_router.py — Unit Tests for Operational Router & Execution Engine (P4 Slice 4)

Behavior-driven test naming convention:
test_<subject>_<accepts|rejects|enforces>_<condition>
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from appctl_audit import AuditLogger, TargetLockManager
from appctl_auth import Principal, Role
from appctl_contracts import ErrorCode, OperationRequest
from appctl_router import (
    MAX_OUTPUT_BYTES,
    OperationalRouter,
    RouterExecutionError,
    RouterValidationError,
    assemble_workload_env,
    parse_dotenv,
    resolve_canonical_domain,
    run_bounded_subprocess,
    validate_workload_preflight,
)
from core_manifest import parse_manifest


@pytest.fixture(autouse=True)
def set_test_domain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOMELAB_DOMAIN", "example.test")


@pytest.fixture
def mock_sites_dir(tmp_path: Path) -> Path:
    sites = tmp_path / "Sites"
    sites.mkdir(parents=True, exist_ok=True)
    return sites


@pytest.fixture
def mock_core_dir(tmp_path: Path) -> Path:
    core = tmp_path / "Core"
    core.mkdir(parents=True, exist_ok=True)
    return core


@pytest.fixture
def mock_workload_dir(mock_sites_dir: Path) -> Path:
    app_dir = mock_sites_dir / "snappymail"
    app_dir.mkdir(parents=True, exist_ok=True)

    manifest_content = """
schema_version: "1.0"
name: "snappymail"
source:
  type: "git"
  url: "https://github.com/example/snappymail.git"
  ref: "main"
deployment:
  strategy: "compose"
  actions:
    - "git_pull"
    - "compose_pull"
    - "compose_up"
env:
  WORKLOAD_CUSTOM_VAR: "custom_value"
"""
    (app_dir / "app.yaml").write_text(manifest_content.strip(), encoding="utf-8")

    compose_content = """
services:
  webmail:
    image: "snappymail:latest"
    container_name: "snappymail"
    restart: "unless-stopped"
"""
    (app_dir / "docker-compose.yml").write_text(compose_content.strip(), encoding="utf-8")
    return app_dir


@pytest.fixture
def admin_principal() -> Principal:
    return Principal(identity="operator", role=Role.ADMIN, auth_method="bearer_token")


@pytest.fixture
def operator_principal() -> Principal:
    return Principal(identity="gitops_bot", role=Role.OPERATOR, auth_method="bearer_token")


@pytest.fixture
def viewer_principal() -> Principal:
    return Principal(identity="unix_uid_1000", role=Role.VIEWER, auth_method="unix_peercred")


# ---------------------------------------------------------------------------
# Pure-Python Inert Dotenv Parser
# ---------------------------------------------------------------------------


def test_parse_dotenv_parses_valid_key_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("""
# Comment line
APP_PORT=8080
APP_HOST="127.0.0.1"
SINGLE_QUOTE='single_val'
EMPTY_VAL=
WITH_EQUALS=key=value=more
""", encoding="utf-8")

    parsed = parse_dotenv(env_file)
    assert parsed["APP_PORT"] == "8080"
    assert parsed["APP_HOST"] == "127.0.0.1"
    assert parsed["SINGLE_QUOTE"] == "single_val"
    assert parsed["EMPTY_VAL"] == ""
    assert parsed["WITH_EQUALS"] == "key=value=more"


def test_parse_dotenv_preserves_shell_injection_as_inert_literals(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("""
INJECTION_ONE=$(touch /tmp/pwned)
INJECTION_TWO=`cat /etc/passwd`
VARIABLE_REF=$HOMELAB_DOMAIN
""", encoding="utf-8")

    parsed = parse_dotenv(env_file)
    assert parsed["INJECTION_ONE"] == "$(touch /tmp/pwned)"
    assert parsed["INJECTION_TWO"] == "`cat /etc/passwd`"
    assert parsed["VARIABLE_REF"] == "$HOMELAB_DOMAIN"
    assert not Path("/tmp/pwned").exists()


def test_parse_dotenv_rejects_invalid_key_syntax(tmp_path: Path):
    bad_keys = ["APP-PORT=80", "123NUM=val", "KEY WITH SPACES=val", "@INVALID=1"]
    for bad in bad_keys:
        env_file = tmp_path / ".env"
        env_file.write_text(bad, encoding="utf-8")
        with pytest.raises(RouterValidationError) as exc_info:
            parse_dotenv(env_file)
        assert "Prohibited environment key syntax" in str(exc_info.value)


def test_parse_dotenv_rejects_reserved_environment_keys(tmp_path: Path):
    for reserved in ["PATH=foo", "LD_PRELOAD=bar", "SHELL=/bin/bash", "USER=root"]:
        env_file = tmp_path / ".env"
        env_file.write_text(reserved, encoding="utf-8")
        with pytest.raises(RouterValidationError) as exc_info:
            parse_dotenv(env_file)
        assert "Prohibited reserved environment key" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Canonical Domain Resolution & Environment Assembly
# ---------------------------------------------------------------------------


def test_resolve_canonical_domain_precedence(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOMELAB_DOMAIN", "domain-a.test")
    monkeypatch.setenv("DOMAIN_SUFFIX", "domain-b.test")
    monkeypatch.setenv("DOMAIN", "domain-c.test")
    assert resolve_canonical_domain() == "domain-a.test"

    monkeypatch.delenv("HOMELAB_DOMAIN")
    assert resolve_canonical_domain() == "domain-b.test"

    monkeypatch.delenv("DOMAIN_SUFFIX")
    assert resolve_canonical_domain() == "domain-c.test"


def test_assemble_workload_env_constructs_6_layer_environment(
    mock_workload_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOMELAB_DOMAIN", "example.test")
    manifest = parse_manifest(mock_workload_dir / "app.yaml")

    # Add local .env
    (mock_workload_dir / ".env").write_text("LOCAL_ENV_VAR=local_val\n", encoding="utf-8")

    env = assemble_workload_env(mock_workload_dir, manifest, "snappymail")

    # Layer 1
    assert "PATH" in env
    # Layer 3 & 4
    assert env["HOMELAB_DOMAIN"] == "example.test"
    assert env["SERVICE_NAME"] == "snappymail"
    assert env["SERVICE_DOMAIN"] == "snappymail.example.test"
    assert env["PROXY_NETWORK"] == "proxy-net"
    # Layer 5 (manifest env)
    assert env["WORKLOAD_CUSTOM_VAR"] == "custom_value"
    # Layer 6 (.env)
    assert env["LOCAL_ENV_VAR"] == "local_val"


# ---------------------------------------------------------------------------
# Bounded Subprocess Execution Engine
# ---------------------------------------------------------------------------


def test_run_bounded_subprocess_executes_valid_command():
    code, out, err = run_bounded_subprocess(["echo", "hello world"])
    assert code == 0
    assert out.strip() == "hello world"
    assert err == ""


def test_run_bounded_subprocess_enforces_256_kib_cap():
    # Produce > 256 KiB of output
    python_cmd = [
        "python3",
        "-c",
        f"import sys; sys.stdout.write('A' * ({MAX_OUTPUT_BYTES} + 1024))",
    ]
    code, out, _ = run_bounded_subprocess(python_cmd)
    assert code == 0
    assert len(out.encode("utf-8")) <= MAX_OUTPUT_BYTES + 200
    assert "[TRUNCATED: Output exceeded 256 KiB limit]" in out


def test_run_bounded_subprocess_times_out():
    cmd = ["sleep", "2"]
    with pytest.raises(RouterExecutionError) as exc_info:
        run_bounded_subprocess(cmd, timeout=0.1)
    assert "timed out after 0.1 seconds" in str(exc_info.value)
    assert exc_info.value.exit_code == 124


# ---------------------------------------------------------------------------
# Workload Pre-Flight Gate
# ---------------------------------------------------------------------------


def test_validate_workload_preflight_accepts_valid_workload(
    mock_workload_dir: Path, mock_sites_dir: Path
):
    manifest, compose_file = validate_workload_preflight(
        mock_workload_dir, mock_sites_dir, "snappymail"
    )
    assert manifest.name == "snappymail"
    assert compose_file.exists()


def test_validate_workload_preflight_rejects_path_traversal(
    mock_sites_dir: Path, tmp_path: Path
):
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RouterValidationError) as exc_info:
        validate_workload_preflight(outside_dir, mock_sites_dir)
    assert "Path traversal rejected" in str(exc_info.value)


def test_validate_workload_preflight_rejects_prohibited_compose_security(
    mock_workload_dir: Path, mock_sites_dir: Path
):
    # Inject privileged: true into docker-compose.yml
    bad_compose = """
services:
  webmail:
    image: "snappymail:latest"
    privileged: true
"""
    (mock_workload_dir / "docker-compose.yml").write_text(bad_compose, encoding="utf-8")

    with pytest.raises(RouterValidationError) as exc_info:
        validate_workload_preflight(mock_workload_dir, mock_sites_dir, "snappymail")
    assert "Compose AST security validation failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Operational Router Dispatch Pipeline
# ---------------------------------------------------------------------------


def test_operational_router_dispatches_platform_health(tmp_path: Path, admin_principal: Principal):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
    )
    req = OperationRequest.model_validate({"operation": "platform.health"})
    resp = router.dispatch(req, admin_principal)
    assert resp.success is True
    assert resp.result is not None
    assert resp.result["status"] == "healthy"


def test_operational_router_dispatches_platform_sync_homepage(
    tmp_path: Path, admin_principal: Principal
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
    )
    req = OperationRequest.model_validate({"operation": "platform.sync_homepage"})
    with patch("appctl_router.cmd_sync_homepage") as mock_sync:
        resp = router.dispatch(req, admin_principal)
        assert resp.success is True
        mock_sync.assert_called_once()


def test_operational_router_enforces_rbac_denial(
    tmp_path: Path, viewer_principal: Principal
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
    )
    req = OperationRequest.model_validate({
        "operation": "workload.deploy",
        "target": "snappymail",
    })
    resp = router.dispatch(req, viewer_principal)
    assert resp.success is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.ERR_PERMISSION_DENIED


def test_operational_router_rejects_non_existent_target(
    tmp_path: Path, admin_principal: Principal
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
    )
    req = OperationRequest.model_validate({
        "operation": "workload.get",
        "target": "nonexistent_workload_999",
    })
    resp = router.dispatch(req, admin_principal)
    assert resp.success is False
    assert resp.error is not None
    assert resp.error.code == ErrorCode.ERR_TARGET_NOT_FOUND


def test_operational_router_rejects_concurrent_mutation(
    tmp_path: Path, admin_principal: Principal, mock_workload_dir: Path
):
    lock_mgr = TargetLockManager(tmp_path / "locks")
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=lock_mgr,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.deploy",
        "target": "snappymail",
    })

    mock_app = MagicMock()
    mock_app.name = "snappymail"
    mock_app.dir_path = str(mock_workload_dir)

    # Hold the lock on snappymail
    with (
        patch("appctl_router.resolve_app", return_value=mock_app),
        lock_mgr.lock_target("snappymail"),
    ):
        resp = router.dispatch(req, admin_principal)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_CONCURRENT_MUTATION


def test_operational_router_dispatches_workload_deploy(
    tmp_path: Path, admin_principal: Principal, mock_workload_dir: Path
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_workload_dir.parent,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.deploy",
        "target": "snappymail",
        "parameters": {"recreate": True, "remove_orphans": True, "build": False},
    })

    mock_app = MagicMock()
    mock_app.name = "snappymail"
    mock_app.dir_path = str(mock_workload_dir)

    with (
        patch("appctl_router.resolve_app", return_value=mock_app),
        patch("appctl_router.run_bounded_subprocess", return_value=(0, "Container started", "")) as mock_run,
        patch("appctl_router.cmd_sync_homepage"),
    ):
        resp = router.dispatch(req, admin_principal)
        assert resp.success is True
        assert resp.result is not None
        assert resp.result["action"] == "deployed"
        mock_run.assert_called_once()
        assert "up" in mock_run.call_args[0][0]


def test_operational_router_workload_update_rejects_dirty_git_tree(
    tmp_path: Path, admin_principal: Principal, mock_workload_dir: Path
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_workload_dir.parent,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.update",
        "target": "snappymail",
    })

    mock_app = MagicMock()
    mock_app.name = "snappymail"
    mock_app.dir_path = str(mock_workload_dir)

    # Simulate dirty working tree from git status --porcelain
    def mock_subproc(cmd: list[str], **kwargs):
        if "status" in cmd:
            return 0, " M app.yaml\n", ""
        return 0, "", ""

    with (
        patch("appctl_router.resolve_app", return_value=mock_app),
        patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc),
    ):
        resp = router.dispatch(req, admin_principal)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_VALIDATION_FAILED
        assert "uncommitted changes" in resp.error.message


def test_operational_router_workload_update_rejects_branch_mismatch(
    tmp_path: Path, admin_principal: Principal, mock_workload_dir: Path
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_workload_dir.parent,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.update",
        "target": "snappymail",
        "parameters": {"expected_branch": "feature/branch"},
    })

    mock_app = MagicMock()
    mock_app.name = "snappymail"
    mock_app.dir_path = str(mock_workload_dir)

    def mock_subproc(cmd: list[str], **kwargs):
        if "status" in cmd:
            return 0, "", ""
        if "pull" in cmd:
            return 0, "Fast-forward", ""
        if "rev-parse" in cmd:
            return 0, "1234567", ""
        return 0, "", ""

    with (
        patch("appctl_router.resolve_app", return_value=mock_app),
        patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc),
    ):
        resp = router.dispatch(req, admin_principal)
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_VALIDATION_FAILED
        assert "Post-pull branch mismatch" in resp.error.message


def test_operational_router_workload_update_succeeds_on_clean_tree(
    tmp_path: Path, admin_principal: Principal, mock_workload_dir: Path
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_workload_dir.parent,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.update",
        "target": "snappymail",
        "parameters": {"expected_branch": "main"},
    })

    mock_app = MagicMock()
    mock_app.name = "snappymail"
    mock_app.dir_path = str(mock_workload_dir)

    executed_commands: list[list[str]] = []

    def mock_subproc(cmd: list[str], **kwargs):
        executed_commands.append(cmd)
        if "status" in cmd:
            return 0, "", ""
        if "pull" in cmd:
            return 0, "Updating 1111..2222\nFast-forward", ""
        if "rev-parse" in cmd:
            return 0, "a1b2c3d", ""
        return 0, "success", ""

    with (
        patch("appctl_router.resolve_app", return_value=mock_app),
        patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc),
        patch("appctl_router.cmd_sync_homepage"),
    ):
        resp = router.dispatch(req, admin_principal)
        assert resp.success is True
        assert resp.result is not None
        assert resp.result["commit_sha"] == "a1b2c3d"
        # Verify git pull was called with expected branch
        pull_calls = [c for c in executed_commands if "pull" in c]
        assert len(pull_calls) == 2  # git pull and docker compose pull


# ---------------------------------------------------------------------------
# Core Platform Stack & Core Service Dispatch (Issue #2)
# ---------------------------------------------------------------------------


def test_operational_router_dispatches_core_stack_operations(
    tmp_path: Path,
    admin_principal: Principal,
    mock_sites_dir: Path,
    mock_core_dir: Path,
):
    compose_content = """
services:
  traefik:
    image: "traefik:v3.6"
    container_name: "traefik"
  authelia:
    image: "authelia/authelia:latest"
    container_name: "authelia"
"""
    (mock_core_dir / "docker-compose.yml").write_text(compose_content.strip(), encoding="utf-8")

    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_sites_dir,
        core_dir=mock_core_dir,
    )

    executed_cmds: list[list[str]] = []

    def mock_subproc(cmd: list[str], **kwargs):
        executed_cmds.append(cmd)
        return 0, "ok", ""

    with (
        patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc),
        patch("appctl_router.cmd_sync_homepage") as mock_sync,
    ):
        # 1. Deploy core stack
        resp_deploy = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.deploy", "target": "core"}),
            admin_principal,
        )
        assert resp_deploy.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "up",
            "-d",
            "--remove-orphans",
        ]

        # 2. Stop core stack (uses `down` for full stack)
        resp_stop = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.stop", "target": "core"}),
            admin_principal,
        )
        assert resp_stop.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "down",
        ]

        # 3. Restart core stack
        resp_restart = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.restart", "target": "core"}),
            admin_principal,
        )
        assert resp_restart.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "restart",
        ]

        # 4. Config core stack
        resp_config = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.config", "target": "core"}),
            admin_principal,
        )
        assert resp_config.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "config",
        ]

        # Core operations do not trigger Homepage Sites sync
        mock_sync.assert_not_called()


def test_operational_router_dispatches_core_service_container_scoped_operations(
    tmp_path: Path,
    admin_principal: Principal,
    mock_sites_dir: Path,
    mock_core_dir: Path,
):
    (mock_core_dir / "docker-compose.yml").write_text(
        "services:\n  traefik:\n    image: traefik:v3.6\n", encoding="utf-8"
    )

    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_sites_dir,
        core_dir=mock_core_dir,
    )

    executed_cmds: list[list[str]] = []

    def mock_subproc(cmd: list[str], **kwargs):
        executed_cmds.append(cmd)
        return 0, "ok", ""

    with patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc):
        # 1. Deploy single core service (traefik)
        resp_deploy = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.deploy", "target": "traefik"}),
            admin_principal,
        )
        assert resp_deploy.success is True
        assert executed_cmds[-1][-1] == "traefik"
        assert "up" in executed_cmds[-1]

        # 2. Stop single core service (uses `stop traefik`, NOT `down`)
        resp_stop = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.stop", "target": "traefik"}),
            admin_principal,
        )
        assert resp_stop.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "stop",
            "traefik",
        ]

        # 3. Restart single core service
        resp_restart = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.restart", "target": "traefik"}),
            admin_principal,
        )
        assert resp_restart.success is True
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "restart",
            "traefik",
        ]

        # 4. Pull single core service
        resp_pull = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.pull", "target": "traefik"}),
            admin_principal,
        )
        assert resp_pull.success is True
        assert executed_cmds[-2] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "pull",
            "traefik",
        ]
        assert executed_cmds[-1] == [
            "docker",
            "compose",
            "-f",
            str(mock_core_dir / "docker-compose.yml"),
            "up",
            "-d",
            "--remove-orphans",
            "traefik",
        ]

        # 5. Logs single core service
        resp_logs = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.logs", "target": "traefik"}),
            admin_principal,
        )
        assert resp_logs.success is True
        assert executed_cmds[-1][-1] == "traefik"


def test_operational_router_dispatches_core_update_without_manifest(
    tmp_path: Path,
    admin_principal: Principal,
    mock_sites_dir: Path,
    mock_core_dir: Path,
):
    (mock_core_dir / "docker-compose.yml").write_text(
        "services:\n  traefik:\n    image: traefik:v3.6\n", encoding="utf-8"
    )

    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_sites_dir,
        core_dir=mock_core_dir,
    )

    executed_cmds: list[list[str]] = []

    def mock_subproc(cmd: list[str], **kwargs):
        executed_cmds.append(cmd)
        if "status" in cmd:
            return 0, "", ""
        if "rev-parse" in cmd:
            return 0, "c0ffee1", ""
        return 0, "ok", ""

    with patch("appctl_router.run_bounded_subprocess", side_effect=mock_subproc):
        resp = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.update", "target": "core"}),
            admin_principal,
        )
        assert resp.success is True
        assert resp.result is not None
        assert resp.result["commit_sha"] == "c0ffee1"
        assert resp.result["actions"] == ["compose_pull", "compose_up"]


def test_operational_router_rejects_operator_role_on_core_targets(
    tmp_path: Path,
    operator_principal: Principal,
    mock_sites_dir: Path,
    mock_core_dir: Path,
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_sites_dir,
        core_dir=mock_core_dir,
    )
    for target in ("core", "traefik", "authelia"):
        resp = router.dispatch(
            OperationRequest.model_validate({"operation": "workload.restart", "target": target}),
            operator_principal,
        )
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_PERMISSION_DENIED


def test_operational_router_workload_list_honors_filter_params(
    tmp_path: Path,
    viewer_principal: Principal,
    mock_workload_dir: Path,
    mock_core_dir: Path,
):
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=mock_workload_dir.parent,
        core_dir=mock_core_dir,
    )

    # Without --core: only Sites workloads (1)
    resp_sites = router.dispatch(
        OperationRequest.model_validate({
            "operation": "workload.list",
            "parameters": {"core": False, "fetch": False, "ssl": False},
        }),
        viewer_principal,
    )
    assert resp_sites.success is True
    assert resp_sites.result is not None
    assert resp_sites.result["total"] == 1
    assert resp_sites.result["workloads"][0]["name"] == "snappymail"

    # With --core and --ssl: includes Core services and SSL inspection
    with patch("appctl_router.check_ssl_cert", return_value={"status": "🟢 80d remaining", "domain": "example.test"}) as mock_ssl:
        resp_core = router.dispatch(
            OperationRequest.model_validate({
                "operation": "workload.list",
                "parameters": {"core": True, "fetch": False, "ssl": True},
            }),
            viewer_principal,
        )
        assert resp_core.success is True
        assert resp_core.result is not None
        names = {w["name"] for w in resp_core.result["workloads"]}
        assert "snappymail" in names
        assert "traefik" in names
        assert "authelia" in names
        assert mock_ssl.call_count >= 1


