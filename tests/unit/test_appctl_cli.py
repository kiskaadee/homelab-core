"""
test_appctl_cli.py — Unit Tests for Thin UDS Client (scripts/appctl_cli.py)
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from appctl_cli import format_response, resolve_token, run_cli
from appctl_contracts import (
    ErrorCode,
    OperationError,
    OperationRequest,
    OperationResponse,
    OperationType,
)


def test_cli_token_resolution(tmp_path: Path, monkeypatch):
    """Ensure token resolution checks CLI flag, environment, and config file."""
    # 1. Direct CLI argument precedence
    assert resolve_token("explicit-secret") == "explicit-secret"

    # 2. Environment variable precedence
    monkeypatch.setenv("APPCTL_TOKEN", "env-secret")
    assert resolve_token(None) == "env-secret"

    # 3. File fallback
    monkeypatch.delenv("APPCTL_TOKEN", raising=False)
    cfg_token = tmp_path / "token"
    cfg_token.write_text("file-secret\n", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=tmp_path):
        dot_config = tmp_path / ".config" / "homelab"
        dot_config.mkdir(parents=True)
        (dot_config / "token").write_text("user-file-secret\n", encoding="utf-8")
        assert resolve_token(None) == "user-file-secret"


def test_cli_format_response_success_and_error():
    """Ensure response formatter produces human-friendly strings and JSON."""
    success_resp = OperationResponse(
        version="1.0",
        success=True,
        operation_id="op_123",
        operation=OperationType.WORKLOAD_DEPLOY,
        output="Container started successfully",
    )
    formatted = format_response(success_resp, raw_json=False)
    assert "Container started successfully" in formatted

    raw_json = format_response(success_resp, raw_json=True)
    assert '"operation": "workload.deploy"' in raw_json
    assert '"success": true' in raw_json

    error_resp = OperationResponse(
        version="1.0",
        success=False,
        operation_id="op_456",
        operation=OperationType.WORKLOAD_DEPLOY,
        error=OperationError(code=ErrorCode.ERR_PERMISSION_DENIED, message="Forbidden"),
    )
    formatted_err = format_response(error_resp, raw_json=False)
    assert "❌ Error [ERR_PERMISSION_DENIED]: Forbidden" in formatted_err


def test_cli_dispatches_list_command():
    """Ensure appctl list dispatches OperationRequest(workload.list)."""
    mock_resp = OperationResponse(
        version="1.0",
        success=True,
        operation_id="op_001",
        operation=OperationType.WORKLOAD_LIST,
        output="NAME    STATUS\ndocs    running\n",
    )

    with patch("appctl_cli.send_daemon_request", return_value=mock_resp) as mock_send, patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        ret = run_cli(["list", "--core"])
        assert ret == 0
        assert mock_send.call_count == 1
        req: OperationRequest = mock_send.call_args[1]["operation_request"]
        assert req.operation == OperationType.WORKLOAD_LIST
        assert req.parameters["core"] is True
        assert "NAME    STATUS" in mock_stdout.getvalue()


def test_cli_dispatches_mutation_command():
    """Ensure appctl up <target> dispatches OperationRequest(workload.deploy)."""
    mock_resp = OperationResponse(
        version="1.0",
        success=True,
        operation_id="op_002",
        operation=OperationType.WORKLOAD_DEPLOY,
        target="jellyfin",
        result={"action": "deployed", "message": "Workload deployed"},
    )

    with patch("appctl_cli.send_daemon_request", return_value=mock_resp) as mock_send, patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        ret = run_cli(["up", "jellyfin", "--token", "tok_secret"])
        assert ret == 0
        assert mock_send.call_count == 1
        assert mock_send.call_args[1]["token"] == "tok_secret"
        req: OperationRequest = mock_send.call_args[1]["operation_request"]
        assert req.operation == OperationType.WORKLOAD_DEPLOY
        assert req.target == "jellyfin"
        assert "Workload deployed" in mock_stdout.getvalue()


def test_cli_handles_missing_socket():
    """Ensure friendly stderr message when socket is absent."""
    with patch("appctl_cli.send_daemon_request", side_effect=FileNotFoundError), patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
        ret = run_cli(["list", "--socket", "/nonexistent/appctl.sock"])
        assert ret == 1
        assert "socket not found" in mock_stderr.getvalue()


def test_cli_format_response_renders_workload_list_table():
    """Ensure workload.list renders a human-readable table instead of suppressing it behind result['message']."""
    resp = OperationResponse(
        version="1.0",
        success=True,
        operation_id="op_list_01",
        operation=OperationType.WORKLOAD_LIST,
        result={
            "total": 2,
            "workloads": [
                {
                    "name": "snappymail",
                    "type": "app",
                    "domain": "mail.example.test",
                    "description": "Webmail client",
                },
                {
                    "name": "traefik",
                    "type": "core_service",
                    "domain": "traefik.example.test",
                    "description": "Edge Reverse Proxy",
                },
            ],
            "message": "Found 2 workloads",
        },
        output="",
    )
    rendered = format_response(resp, raw_json=False)
    assert "SERVICE" in rendered
    assert "DOMAIN" in rendered
    assert "snappymail" in rendered
    assert "mail.example.test" in rendered
    assert "traefik" in rendered
    assert "Edge Reverse Proxy" in rendered
    assert "✨ Found 2 workloads" not in rendered


def test_cli_format_response_renders_workload_get_info():
    """Ensure workload.get (appctl info) renders structured workload metadata."""
    resp = OperationResponse(
        version="1.0",
        success=True,
        operation_id="op_get_01",
        operation=OperationType.WORKLOAD_GET,
        target="snappymail",
        result={
            "workload": {
                "name": "snappymail",
                "type": "app",
                "dir_path": "/home/operator/Sites/snappymail",
                "domain": "mail.example.test",
                "description": "Lightweight Webmail",
                "aliases": ["webmail", "mail"],
                "networks": ["proxy-net"],
                "auth": True,
                "visible": True,
            },
            "message": "Retrieved metadata for snappymail",
        },
        output="",
    )
    rendered = format_response(resp, raw_json=False)
    assert "Workload Overview: snappymail" in rendered
    assert "/home/operator/Sites/snappymail" in rendered
    assert "mail.example.test" in rendered
    assert "webmail, mail" in rendered
    assert "✨ Retrieved metadata" not in rendered


def test_appctl_wrapper_fast_paths_completion_when_socket_exists(tmp_path: Path, monkeypatch):
    """Ensure scripts/appctl executes `complete` and `completion` via appctl_engine.py even when socket exists."""
    import os
    import shutil
    import socket as std_socket
    import subprocess
    import sys

    bash_bin = shutil.which("bash") or "bash"
    sock_path = tmp_path / "appctl.sock"
    srv = std_socket.socket(std_socket.AF_UNIX, std_socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    try:
        appctl_script = Path(__file__).resolve().parent.parent.parent / "scripts" / "appctl"
        env = {
            "PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
            "PYTHON_BIN": sys.executable,
            "HOMELAB_DOMAIN": "example.test",
            "APPCTL_SOCKET_PATH": str(sock_path),
            "SITES_DIR": str(tmp_path),
        }
        res_comp = subprocess.run(
            [bash_bin, str(appctl_script), "completion", "bash"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_comp.returncode == 0
        assert "_appctl_completions()" in res_comp.stdout

        res_words = subprocess.run(
            [bash_bin, str(appctl_script), "complete", "services"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_words.returncode == 0
        assert "traefik" in res_words.stdout
        assert "core" in res_words.stdout
    finally:
        srv.close()

