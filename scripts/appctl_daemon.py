#!/usr/bin/env python3
"""
appctl_daemon.py — Dual-Transport HTTP/1.1 Control Plane Daemon for Homelab

Implements Milestone P4 Control Plane Daemon:
- Multi-threaded Unix domain socket listener (/run/homelab/appctl.sock, mode 0660)
- Multi-threaded loopback HTTP listener (127.0.0.1:9001)
- Bounded 64 KiB request body enforcement
- Endpoints: GET /v1/health, POST /v1/operations
- Linux SO_PEERCRED credential extraction on UDS transport
- Bearer token authentication + RBAC gating + append-only audit logging
- Pure-Python Unix domain socket HTTP client helper
- Graceful SIGINT and SIGTERM shutdown unlinking socket file
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from appctl_audit import (
    AuditLogger,
    PrincipalAudit,
    TargetLockManager,
    current_iso_timestamp,
    generate_operation_id,
)
from appctl_auth import (
    AuthenticationError,
    TokenStore,
    authenticate_request,
)
from appctl_contracts import (
    ErrorCode,
    OperationError,
    OperationRequest,
    OperationResponse,
    OperationType,
    sanitize_validation_error,
)
from appctl_router import OperationalRouter
from pydantic import ValidationError

logger = logging.getLogger("appctl_daemon")

CORE_DIR = Path(os.environ.get("CORE_DIR", Path(__file__).resolve().parent.parent))
SITES_DIR = Path(os.environ.get("SITES_DIR", os.path.expanduser("~/Sites")))
DEPLOYMENTS_FILE = Path(
    os.environ.get("HOMELAB_DEPLOYMENTS_FILE", CORE_DIR / "config" / "deployments.yaml")
)

DEFAULT_SOCKET_PATH = Path(
    os.environ.get("APPCTL_SOCKET_PATH", "/run/homelab/appctl.sock")
)
DEFAULT_HTTP_HOST = os.environ.get("APPCTL_HTTP_HOST", "127.0.0.1")
DEFAULT_HTTP_PORT = int(os.environ.get("APPCTL_HTTP_PORT", "9001"))
MAX_REQUEST_BODY = 64 * 1024  # 64 KiB


def extract_peer_credentials(sock: socket.socket) -> tuple[int, int, int] | None:
    """
    Extract (pid, uid, gid) from a connected Unix domain socket on Linux via SO_PEERCRED.
    Returns None if unsupported or on error.
    """
    try:
        so_peercred = getattr(socket, "SO_PEERCRED", 17)
        creds = sock.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", creds)
        return pid, uid, gid
    except (OSError, struct.error, AttributeError):
        return None


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTP client connection over a local Unix domain socket."""

    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if self.timeout is not None:
            sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def send_daemon_request(
    endpoint: str | Path,
    operation_request: OperationRequest | dict[str, Any],
    token: str | None = None,
    timeout: float = 30.0,
) -> OperationResponse:
    """
    Client helper to send an OperationRequest over UDS or HTTP to the daemon.
    Accepts:
    - Path object or path ending in .sock -> UnixHTTPConnection
    - URL starting with http:// or host:port -> standard HTTPConnection
    """
    if isinstance(operation_request, OperationRequest):
        req_body = operation_request.model_dump_json(exclude_none=True)
    elif isinstance(operation_request, dict):
        req_body = json.dumps(operation_request)
    else:
        raise TypeError("operation_request must be OperationRequest or dict")

    headers = {
        "Content-Type": "application/json",
        "Host": "localhost",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    is_unix = False
    endpoint_str = str(endpoint)

    if (
        isinstance(endpoint, Path)
        or endpoint_str.endswith(".sock")
        or ("/" in endpoint_str and not endpoint_str.startswith("http"))
    ):
        is_unix = True

    if is_unix:
        conn = UnixHTTPConnection(endpoint_str, timeout=timeout)
    else:
        if endpoint_str.startswith(("http://", "https://")):
            parsed = urlparse(endpoint_str)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        elif ":" in endpoint_str:
            host, port_str = endpoint_str.split(":", 1)
            port = int(port_str)
        else:
            host = endpoint_str
            port = DEFAULT_HTTP_PORT
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    try:
        conn.request("POST", "/v1/operations", body=req_body.encode("utf-8"), headers=headers)
        res = conn.getresponse()
        resp_data = res.read().decode("utf-8")
        parsed_json = json.loads(resp_data)
        return OperationResponse.model_validate(parsed_json)
    finally:
        conn.close()


class AppctlHTTPHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP/1.1 request handler for appctl control plane operations.
    Supports both UDS and loopback TCP listeners.
    """

    server_version = "AppctlDaemon/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def appctl_server(self) -> ThreadingUnixStreamServer | ThreadingHTTPServer:
        return cast("ThreadingUnixStreamServer | ThreadingHTTPServer", self.server)

    def address_string(self) -> str:
        """Safely format client address avoiding slow DNS lookups or UDS unpack errors."""
        client_addr = self.client_address
        if isinstance(client_addr, tuple) and len(client_addr) >= 2:
            return f"{client_addr[0]}:{client_addr[1]}"
        if isinstance(client_addr, str) and client_addr:
            return client_addr
        return "unix_socket"

    def log_message(self, format: str, *args: Any) -> None:
        """Forward access logs to Python standard logger."""
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status_code: int, data: dict[str, Any] | str) -> None:
        """Send formatted JSON HTTP response."""
        body_bytes = data.encode("utf-8") if isinstance(data, str) else json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_operation_response(self, status_code: int, response: OperationResponse) -> None:
        """Serialize and send an OperationResponse payload."""
        payload_str = response.model_dump_json(exclude_none=True)
        self._send_json(status_code, payload_str)

    def _send_operation_error(
        self,
        status_code: int,
        error: OperationError,
        operation: Any = None,
        target: str | None = None,
        op_id: str | None = None,
    ) -> None:
        """Send standardized OperationResponse representing a failure."""
        resp = OperationResponse(
            version="1.0",
            success=False,
            status="error",
            operation_id=op_id or generate_operation_id(),
            operation=operation if isinstance(operation, (OperationType, str)) else None,
            target=target if isinstance(target, str) else None,
            error=error,
            timestamp=current_iso_timestamp(),
        )
        self._send_operation_response(status_code, resp)

    def _send_error_response(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: list[dict[str, Any]] | None = None,
        op_id: str | None = None,
        operation: Any = None,
        target: str | None = None,
    ) -> None:
        """Send structured error response with canonical error code."""
        error = OperationError(code=code, message=message, details=details)
        self._send_operation_error(
            status_code=status_code,
            error=error,
            operation=operation,
            target=target,
            op_id=op_id,
        )

    def do_GET(self) -> None:
        """Handle health check endpoint."""
        if self.path in ("/v1/health", "/health"):
            health = self.appctl_server.daemon_instance.get_health_status()
            self._send_json(200, health)
            return

        self._send_error_response(
            status_code=404,
            code=ErrorCode.ERR_TARGET_NOT_FOUND,
            message=f"Endpoint '{self.path}' not found",
        )

    def do_POST(self) -> None:
        """Handle operational dispatch requests."""
        if self.path != "/v1/operations":
            self._send_error_response(
                status_code=404,
                code=ErrorCode.ERR_TARGET_NOT_FOUND,
                message=f"Endpoint '{self.path}' not found. Supported endpoint: POST /v1/operations",
            )
            return

        cl_header = self.headers.get("Content-Length")
        if cl_header is None:
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message="Missing Content-Length header",
            )
            return

        try:
            content_length = int(cl_header.strip())
        except ValueError:
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message="Malformed Content-Length header",
            )
            return

        if content_length < 0:
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message="Content-Length cannot be negative",
            )
            return

        if content_length > MAX_REQUEST_BODY:
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message=f"Request body ({content_length} bytes) exceeds limit of {MAX_REQUEST_BODY} bytes",
            )
            return

        raw_body = self.rfile.read(content_length)

        try:
            body_dict = json.loads(raw_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message=f"Invalid JSON payload: {e}",
            )
            return

        if not isinstance(body_dict, dict):
            self._send_error_response(
                status_code=400,
                code=ErrorCode.ERR_VALIDATION_FAILED,
                message="Payload root must be a JSON object mapping",
            )
            return

        try:
            req = OperationRequest.model_validate(body_dict)
        except ValidationError as exc:
            sanitized = sanitize_validation_error(exc)
            self._send_operation_error(
                status_code=400,
                error=sanitized,
                operation=body_dict.get("operation") or body_dict.get("action"),
                target=body_dict.get("target"),
            )
            return

        # Determine transport scheme and peer credentials
        is_unix = getattr(self.connection, "family", None) == socket.AF_UNIX
        transport = "unix_socket" if is_unix else "http"
        peer_uid: int | None = None

        if is_unix:
            creds = extract_peer_credentials(self.connection)
            if creds is not None:
                _, peer_uid, _ = creds
            origin = f"peer_uid_{peer_uid}" if peer_uid is not None else "unix_socket"
        else:
            origin = self.address_string()

        client_header = self.headers.get("User-Agent", "appctl-client/1.0")
        auth_header = self.headers.get("Authorization")

        # Authenticate principal
        try:
            principal = authenticate_request(
                auth_header=auth_header,
                transport=transport,
                peer_uid=peer_uid,
                token_store=self.appctl_server.token_store,
            )
        except AuthenticationError as auth_err:
            op_id = generate_operation_id()
            auth_msg = str(auth_err)
            self.appctl_server.audit_logger.log_denied(
                operation_id=op_id,
                principal=PrincipalAudit(
                    identity=f"peer_uid_{peer_uid}" if peer_uid is not None else "unknown",
                    role="homelab-viewer" if is_unix else "unknown",
                    token_id=None,
                    auth_method="unix_peercred" if is_unix and auth_header is None else "bearer_token",
                ),
                action=req.operation.value,
                target=req.target,
                transport=transport,
                origin=origin,
                client=client_header,
                parameters=req.parameters,
                error_code=auth_err.code.value,
                message=auth_msg,
            )
            self._send_operation_error(
                status_code=auth_err.code.http_status,
                error=OperationError(code=auth_err.code, message=auth_msg),
                operation=req.operation,
                target=req.target,
                op_id=op_id,
            )
            return

        # Dispatch operation to OperationalRouter
        resp = self.appctl_server.router.dispatch(
            request=req,
            principal=principal,
            transport=transport,
            origin=origin,
            client=client_header,
        )

        status_code = resp.error.code.http_status if (not resp.success and resp.error) else 200
        self._send_operation_response(status_code, resp)


class ThreadingUnixStreamServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Multi-threaded Unix Domain Socket HTTP server."""

    daemon_threads = True
    allow_reuse_address = True

    router: OperationalRouter
    token_store: TokenStore
    audit_logger: AuditLogger
    daemon_instance: AppctlDaemon


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded TCP Loopback HTTP server."""

    daemon_threads = True
    allow_reuse_address = True

    router: OperationalRouter
    token_store: TokenStore
    audit_logger: AuditLogger
    daemon_instance: AppctlDaemon


class AppctlDaemon:
    """
    Control Plane Daemon managing dual-transport operational listeners (UDS + TCP).
    """

    def __init__(
        self,
        router: OperationalRouter | None = None,
        token_store: TokenStore | None = None,
        audit_logger: AuditLogger | None = None,
        lock_manager: TargetLockManager | None = None,
        socket_path: Path | str | None = None,
        http_host: str | None = None,
        http_port: int | None = None,
        sites_dir: Path | str | None = None,
        core_dir: Path | str | None = None,
        deployments_file: Path | str | None = None,
    ) -> None:
        self.socket_path = Path(socket_path or os.environ.get("APPCTL_SOCKET_PATH", DEFAULT_SOCKET_PATH))
        self.http_host = str(http_host or os.environ.get("APPCTL_HTTP_HOST", DEFAULT_HTTP_HOST))
        self.http_port = int(http_port or os.environ.get("APPCTL_HTTP_PORT", str(DEFAULT_HTTP_PORT)))

        self.audit_logger = audit_logger or AuditLogger()
        self.token_store = token_store or TokenStore()
        self.lock_manager = lock_manager or TargetLockManager()

        self.router = router or OperationalRouter(
            audit_logger=self.audit_logger,
            lock_manager=self.lock_manager,
            sites_dir=sites_dir or SITES_DIR,
            core_dir=core_dir or CORE_DIR,
        )

        self.uds_server: ThreadingUnixStreamServer | None = None
        self.tcp_server: ThreadingHTTPServer | None = None
        self.uds_thread: threading.Thread | None = None
        self.tcp_thread: threading.Thread | None = None

        self.running = False
        self.start_time: float = 0.0

    def start(self) -> None:
        """Bind both transports and start listener worker threads."""
        if self.running:
            return

        self.start_time = time.time()

        # 1. Prepare and bind UDS listener
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink(missing_ok=True)

        self.uds_server = ThreadingUnixStreamServer(str(self.socket_path), AppctlHTTPHandler)
        self.uds_server.router = self.router
        self.uds_server.token_store = self.token_store
        self.uds_server.audit_logger = self.audit_logger
        self.uds_server.daemon_instance = self

        try:
            os.chmod(self.socket_path, 0o660)
        except OSError as e:
            logger.warning("Could not set mode 0660 on socket %s: %e", self.socket_path, e)

        # 2. Prepare and bind TCP listener
        self.tcp_server = ThreadingHTTPServer((self.http_host, self.http_port), AppctlHTTPHandler)
        self.tcp_server.router = self.router
        self.tcp_server.token_store = self.token_store
        self.tcp_server.audit_logger = self.audit_logger
        self.tcp_server.daemon_instance = self

        # 3. Start listener threads
        self.uds_thread = threading.Thread(target=self.uds_server.serve_forever, daemon=True, name="uds-listener")
        self.uds_thread.start()

        self.tcp_thread = threading.Thread(target=self.tcp_server.serve_forever, daemon=True, name="tcp-listener")
        self.tcp_thread.start()

        self.running = True
        logger.info(
            "appctl daemon started: UDS=%s, HTTP=%s:%d",
            self.socket_path,
            self.http_host,
            self.http_port,
        )

    def stop(self) -> None:
        """Gracefully shut down both transport servers and unlink socket."""
        if not self.running:
            return

        self.running = False
        logger.info("Shutting down appctl daemon...")

        if self.uds_server:
            self.uds_server.shutdown()
            self.uds_server.server_close()
            self.uds_server = None

        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink(missing_ok=True)

        if self.tcp_server:
            self.tcp_server.shutdown()
            self.tcp_server.server_close()
            self.tcp_server = None

        if self.uds_thread and self.uds_thread.is_alive():
            self.uds_thread.join(timeout=2.0)
            self.uds_thread = None

        if self.tcp_thread and self.tcp_thread.is_alive():
            self.tcp_thread.join(timeout=2.0)
            self.tcp_thread = None

        logger.info("appctl daemon shut down successfully.")

    def get_health_status(self) -> dict[str, Any]:
        """Produce non-sensitive health metadata."""
        uptime = round(time.time() - self.start_time, 2) if self.running else 0.0
        return {
            "status": "healthy" if self.running else "stopped",
            "version": "1.0",
            "uptime_seconds": uptime,
            "transports": {
                "unix_socket": str(self.socket_path),
                "http_loopback": f"{self.http_host}:{self.http_port}",
            },
            "timestamp": current_iso_timestamp(),
        }


def main() -> None:
    """CLI entrypoint for running the daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [appctl-daemon] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    daemon = AppctlDaemon()

    def handle_signal(sig: int, _frame: Any) -> None:
        logger.info("Received signal %d, stopping daemon...", sig)
        daemon.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    daemon.start()

    try:
        while daemon.running:
            time.sleep(1.0)
    except KeyboardInterrupt:
        daemon.stop()


if __name__ == "__main__":
    main()
