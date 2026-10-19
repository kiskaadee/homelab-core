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
