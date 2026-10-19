"""
test_appctl_daemon.py — Unit and Integration Test Suite for Milestone P4 Daemon

Asserts:
- Dual-transport equivalence between UDS (/run/homelab/appctl.sock) and TCP (127.0.0.1:9001)
- 64 KiB request body limit enforcement (400 ERR_VALIDATION_FAILED)
- Health check route GET /v1/health
- Unauthenticated rejection fails closed (401 ERR_AUTH_REQUIRED)
- UDS SO_PEERCRED viewer fallback boundary
- 3-role RBAC enforcement across daemon transports
- Pre-flight and inert dotenv safety through daemon
- Concurrency conflict (409 ERR_CONCURRENT_MUTATION)
- Truthful execution failure reporting (500 ERR_EXECUTION_FAILED)
- Fail-closed pre-execution audit guard
- Daemon restart socket unlinking and clean rebinding
- gitops_dispatcher delegation to daemon UDS
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from unittest.mock import patch

import gitops_dispatcher
import pytest
from appctl_audit import AuditLogger, TargetLockManager
from appctl_auth import Role, TokenDescriptor, TokenStore
from appctl_contracts import ErrorCode, OperationRequest, OperationType
from appctl_daemon import (
    AppctlDaemon,
    UnixHTTPConnection,
    send_daemon_request,
)
from appctl_router import OperationalRouter

ADMIN_SECRET = "secret-admin-key-789"
OPERATOR_SECRET = "secret-operator-key-456"

ADMIN_HASH = hashlib.sha256(ADMIN_SECRET.encode()).hexdigest()
OPERATOR_HASH = hashlib.sha256(OPERATOR_SECRET.encode()).hexdigest()


def get_free_port() -> int:
    """Find an available TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """
    Spins up an isolated AppctlDaemon instance with:
    - Dedicated UDS socket in tmp_path
    - Dynamic loopback TCP port
    - Mock Sites directory with admitted 'docs' and unregistered 'rogue' workloads
    - Valid 0400 tokens store with admin and operator tokens
    - Valid 0600 audit log file
    """
    monkeypatch.setenv("HOMELAB_DOMAIN", "example.test")
    socket_path = tmp_path / "appctl.sock"
    http_port = get_free_port()
    http_host = "127.0.0.1"

    # Tokens store
    tokens_file = tmp_path / "tokens.json"
    tokens_data = [
        TokenDescriptor(
            token_id="tok_admin",
            identity="operator",
            role=Role.ADMIN,
            sha256=ADMIN_HASH,
        ).model_dump(),
        TokenDescriptor(
            token_id="tok_gitops",
            identity="gitops-dispatcher",
            role=Role.OPERATOR,
            sha256=OPERATOR_HASH,
        ).model_dump(),
    ]
    tokens_file.write_text(json.dumps(tokens_data), encoding="utf-8")
    tokens_file.chmod(0o400)
    token_store = TokenStore(tokens_file)

    # Audit log
    audit_file = tmp_path / "audit.jsonl"
    audit_file.touch()
    audit_file.chmod(0o600)
    audit_logger = AuditLogger(audit_file)

    # Locks
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    lock_manager = TargetLockManager(lock_dir)

    # Workloads
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    core_dir = tmp_path / "Core"
    core_dir.mkdir()

    # Admitted workload: docs
    docs_dir = sites_dir / "docs"
    docs_dir.mkdir()
    (docs_dir / "app.yaml").write_text(
        "name: docs\n"
        "domain: docs\n"
        "deployment:\n"
        "  branch: main\n"
        "  actions:\n"
        "    - compose_up\n",
        encoding="utf-8",
    )
    (docs_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: nginx:alpine\n",
        encoding="utf-8",
    )

    # Unregistered workload: rogue
    rogue_dir = sites_dir / "rogue"
    rogue_dir.mkdir()
    (rogue_dir / "app.yaml").write_text(
        "name: rogue\n"
        "deployment:\n"
        "  branch: main\n"
        "  actions:\n"
        "    - compose_up\n",
        encoding="utf-8",
    )
    (rogue_dir / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    image: alpine:latest\n",
        encoding="utf-8",
    )

    # deployments.yaml
    deployments_file = tmp_path / "deployments.yaml"
    deployments_file.write_text(
        'schemaVersion: "1.0"\n'
        "repositories:\n"
        "  docs:\n"
        f'    path: "{docs_dir}"\n',
        encoding="utf-8",
    )

    router = OperationalRouter(
        audit_logger=audit_logger,
        lock_manager=lock_manager,
        sites_dir=sites_dir,
        core_dir=core_dir,
    )

    daemon = AppctlDaemon(
        router=router,
        token_store=token_store,
        audit_logger=audit_logger,
        lock_manager=lock_manager,
        socket_path=socket_path,
        http_host=http_host,
        http_port=http_port,
        sites_dir=sites_dir,
        core_dir=core_dir,
        deployments_file=deployments_file,
    )

    daemon.start()

    class Context:
        def __init__(self):
            self.daemon = daemon
            self.socket_path = socket_path
            self.http_url = f"http://{http_host}:{http_port}"
            self.sites_dir = sites_dir
            self.core_dir = core_dir
            self.audit_file = audit_file
            self.deployments_file = deployments_file
            self.docs_dir = docs_dir
            self.rogue_dir = rogue_dir

    ctx = Context()
    try:
        yield ctx
    finally:
        daemon.stop()


def test_health_endpoint(daemon_env):
    """GET /v1/health returns HTTP 200 with health metadata over UDS and TCP."""
    # Over UDS
    conn_uds = UnixHTTPConnection(str(daemon_env.socket_path))
    conn_uds.request("GET", "/v1/health")
    res_uds = conn_uds.getresponse()
    assert res_uds.status == 200
    data_uds = json.loads(res_uds.read().decode())
    assert data_uds["status"] == "healthy"
    assert data_uds["version"] == "1.0"
    assert "uptime_seconds" in data_uds
    conn_uds.close()

    # Over TCP
    import http.client
    conn_tcp = http.client.HTTPConnection("127.0.0.1", daemon_env.daemon.http_port)
    conn_tcp.request("GET", "/v1/health")
    res_tcp = conn_tcp.getresponse()
    assert res_tcp.status == 200
    data_tcp = json.loads(res_tcp.read().decode())
    assert data_tcp["status"] == "healthy"
    assert data_tcp["version"] == "1.0"
    conn_tcp.close()


def test_transport_dual_equivalence(daemon_env):
    """
    Compare responses of POST /v1/operations over Unix socket vs HTTP 127.0.0.1.
    Asserts 100% schema and status parity.
    """
    list_req = OperationRequest(operation=OperationType.WORKLOAD_LIST)

    # 1. Successful query with admin token
    resp_uds = send_daemon_request(
        endpoint=daemon_env.socket_path,
        operation_request=list_req,
        token=ADMIN_SECRET,
    )
    resp_http = send_daemon_request(
        endpoint=daemon_env.http_url,
        operation_request=list_req,
        token=ADMIN_SECRET,
    )

    assert resp_uds.success is True
    assert resp_http.success is True
    assert resp_uds.status == resp_http.status
    assert resp_uds.operation == resp_http.operation
    assert resp_uds.version == resp_http.version
    assert resp_uds.result == resp_http.result

    # 2. Error case parity (404 target not found)
    get_req = OperationRequest(operation=OperationType.WORKLOAD_GET, target="nonexistent")
    err_uds = send_daemon_request(
        endpoint=daemon_env.socket_path,
        operation_request=get_req,
        token=ADMIN_SECRET,
    )
    err_http = send_daemon_request(
        endpoint=daemon_env.http_url,
        operation_request=get_req,
        token=ADMIN_SECRET,
    )

    assert err_uds.success is False
    assert err_http.success is False
    assert err_uds.error is not None and err_http.error is not None
    assert err_uds.error.code == ErrorCode.ERR_TARGET_NOT_FOUND
    assert err_http.error.code == ErrorCode.ERR_TARGET_NOT_FOUND
    assert err_uds.error.code.http_status == 404
    assert err_http.error.code.http_status == 404


def test_request_body_size_limit(daemon_env):
    """Requests exceeding 64 KiB are rejected with 400 ERR_VALIDATION_FAILED."""
    oversized_data = {"version": "1.0", "operation": "workload.list", "padding": "x" * (65 * 1024)}
    body_str = json.dumps(oversized_data)

    conn = UnixHTTPConnection(str(daemon_env.socket_path))
    headers = {"Content-Type": "application/json", "Host": "localhost"}
    conn.request("POST", "/v1/operations", body=body_str, headers=headers)
    res = conn.getresponse()
    assert res.status == 400
    data = json.loads(res.read().decode())
    assert data["status"] == "error"
    assert data["error"]["code"] == ErrorCode.ERR_VALIDATION_FAILED.value
    assert "exceeds limit" in data["error"]["message"]
    conn.close()


def test_auth_missing_token_fails_closed(daemon_env):
    """
    Mutating operations and sensitive reads without Bearer token return 401 ERR_AUTH_REQUIRED
    and execute zero subprocesses.
    """
    deploy_req = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")

    with patch("subprocess.run") as mock_subproc:
        # Over HTTP without token
        resp_http = send_daemon_request(
            endpoint=daemon_env.http_url,
            operation_request=deploy_req,
            token=None,
        )
        assert resp_http.success is False
        assert resp_http.error is not None
        assert resp_http.error.code == ErrorCode.ERR_AUTH_REQUIRED
        assert resp_http.error.code.http_status == 401
        assert mock_subproc.call_count == 0

        # Over UDS without token (deploy is mutating, peercred viewer fallback must be rejected)
        resp_uds = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_req,
            token=None,
        )
        assert resp_uds.success is False
        assert resp_uds.error is not None
        assert resp_uds.error.code == ErrorCode.ERR_PERMISSION_DENIED
        assert resp_uds.error.code.http_status == 403
        assert mock_subproc.call_count == 0


def test_uds_peercred_viewer_boundary(daemon_env):
    """
    Tokenless UDS call from operator UID succeeds for workload.list (auth_method: unix_peercred),
    but fails with 403 on mutation or sensitive reads.
    """
    # Read-only query over UDS without token succeeds as viewer
    list_req = OperationRequest(operation=OperationType.WORKLOAD_LIST)
    resp = send_daemon_request(
        endpoint=daemon_env.socket_path,
        operation_request=list_req,
        token=None,
    )
    assert resp.success is True
    assert resp.result is not None
    assert "workloads" in resp.result

    # Sensitive read (workload.config) without token fails
    config_req = OperationRequest(operation=OperationType.WORKLOAD_CONFIG, target="docs")
    resp_cfg = send_daemon_request(
        endpoint=daemon_env.socket_path,
        operation_request=config_req,
        token=None,
    )
    assert resp_cfg.success is False
    assert resp_cfg.error is not None
    assert resp_cfg.error.code == ErrorCode.ERR_PERMISSION_DENIED
    assert resp_cfg.error.code.http_status == 403


def test_rbac_role_matrix(daemon_env):
    """
    homelab-operator allowed on deployments.yaml workloads but denied on unregistered workloads;
    homelab-admin allowed on all valid targets.
    """
    deploy_registered = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")
    deploy_unregistered = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="rogue")

    with patch("appctl_router.run_bounded_subprocess") as mock_subproc, patch("appctl_router.OperationalRouter._trigger_homepage_sync"):
        mock_subproc.return_value = (0, "Container docs created", "")

        # Operator on registered target -> SUCCESS
        resp_op_reg = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_registered,
            token=OPERATOR_SECRET,
        )
        assert resp_op_reg.success is True
        assert resp_op_reg.result is not None
        assert resp_op_reg.result["action"] == "deployed"

        # Operator on unregistered target -> 403 ERR_PERMISSION_DENIED
        resp_op_unreg = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_unregistered,
            token=OPERATOR_SECRET,
        )
        assert resp_op_unreg.success is False
        assert resp_op_unreg.error is not None
        assert resp_op_unreg.error.code == ErrorCode.ERR_PERMISSION_DENIED
        assert resp_op_unreg.error.code.http_status == 403

        # Admin on unregistered target -> SUCCESS
        resp_adm_unreg = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_unregistered,
            token=ADMIN_SECRET,
        )
        assert resp_adm_unreg.success is True
        assert resp_adm_unreg.result is not None
        assert resp_adm_unreg.result["action"] == "deployed"


def test_daemon_preflight_and_dotenv_safety(daemon_env):
    """
    Workload with prohibited Compose directive (privileged: true) is blocked by Compose AST
    pre-flight before any subprocess runs.
    """
    bad_compose = daemon_env.docs_dir / "docker-compose.yml"
    bad_compose.write_text(
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    privileged: true\n",
        encoding="utf-8",
    )

    deploy_req = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")

    with patch("subprocess.run") as mock_subproc:
        resp = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_req,
            token=ADMIN_SECRET,
        )
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_VALIDATION_FAILED
        assert resp.error.code.http_status == 400
        assert "privileged: true" in resp.error.message
        assert mock_subproc.call_count == 0


def test_concurrent_mutation_conflict(daemon_env):
    """
    Second concurrent mutation on the same target returns 409 ERR_CONCURRENT_MUTATION.
    """
    deploy_req = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")

    # Manually hold lock on target
    lock_ctx = daemon_env.daemon.lock_manager.lock_target("docs")
    with lock_ctx:
        resp = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_req,
            token=ADMIN_SECRET,
        )
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_CONCURRENT_MUTATION
        assert resp.error.code.http_status == 409


def test_execution_failure_audit(daemon_env):
    """
    Failing Docker subprocess returns 500 ERR_EXECUTION_FAILED and records phase: FAILED.
    """
    deploy_req = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")

    with patch("appctl_router.run_bounded_subprocess") as mock_subproc:
        mock_subproc.return_value = (1, "", "Error: docker daemon not reachable")

        resp = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_req,
            token=ADMIN_SECRET,
        )
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_EXECUTION_FAILED
        assert resp.error.code.http_status == 500

    # Verify audit.jsonl contains FAILED event
    lines = [json.loads(line) for line in daemon_env.audit_file.read_text().splitlines() if line.strip()]
    failed_events = [ev for ev in lines if ev.get("phase") == "FAILED"]
    assert len(failed_events) >= 1
    assert failed_events[-1]["target"] == "docs"
    assert failed_events[-1]["result"]["error_code"] == ErrorCode.ERR_EXECUTION_FAILED.value


def test_audit_failure_blocks_mutation(daemon_env):
    """
    Unwritable audit.jsonl aborts mutating command with 500 ERR_EXECUTION_FAILED
    and zero Docker invocations.
    """
    daemon_env.audit_file.chmod(0o400)
    deploy_req = OperationRequest(operation=OperationType.WORKLOAD_DEPLOY, target="docs")

    with patch("subprocess.run") as mock_subproc:
        resp = send_daemon_request(
            endpoint=daemon_env.socket_path,
            operation_request=deploy_req,
            token=ADMIN_SECRET,
        )
        assert resp.success is False
        assert resp.error is not None
        assert resp.error.code == ErrorCode.ERR_EXECUTION_FAILED
        assert resp.error.code.http_status == 500
        assert mock_subproc.call_count == 0

    # Restore write permission for teardown
    daemon_env.audit_file.chmod(0o600)


def test_daemon_restart_preserves_state(daemon_env):
    """
    Clean stop and restart of daemon unlinks and rebinds socket cleanly.
    """
    daemon_env.daemon.stop()
    assert not daemon_env.socket_path.exists()

    # Restart
    daemon_env.daemon.start()
    assert daemon_env.socket_path.is_socket()

    # Re-verify communication
    list_req = OperationRequest(operation=OperationType.WORKLOAD_LIST)
    resp = send_daemon_request(
        endpoint=daemon_env.socket_path,
        operation_request=list_req,
        token=ADMIN_SECRET,
    )
    assert resp.success is True


def test_gitops_dispatcher_delegates_to_daemon(daemon_env, monkeypatch):
    """
    When daemon socket is active, gitops_dispatcher calls daemon over UDS with Bearer token.
    """
    monkeypatch.setattr(gitops_dispatcher, "DEFAULT_APPCTL_SOCKET_PATH", daemon_env.socket_path)
    monkeypatch.setenv("GITOPS_APPCTL_TOKEN", OPERATOR_SECRET)
    monkeypatch.setenv("APPCTL_SOCKET_PATH", str(daemon_env.socket_path))

    with patch.object(daemon_env.daemon.router, "_op_workload_update") as mock_update:
        mock_update.return_value = ({"action": "updated", "message": "Updated via daemon"}, "sha12345")

        success = gitops_dispatcher.execute_deployment(
            target_dir=daemon_env.docs_dir,
            deployment_config={"branch": "main", "actions": ["git_pull", "compose_up"]},
            branch="main",
        )
        assert success is True
        assert mock_update.call_count == 1
