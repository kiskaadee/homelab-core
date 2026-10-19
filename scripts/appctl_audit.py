"""
appctl_audit.py — Append-Only Audit Logger & Per-Target Lock Manager (P4 Slice 2)

Governs the audit telemetry and concurrency control for homelab-appctl.service:
- Append-only JSONL audit writer adhering to RFC-compliant schema (mode 0600)
- Pre-execution DISPATCHED fail-closed disk guard
- Sensitive parameter and error redaction (*_KEY, *_SECRET, *_PASSWORD, etc.)
- Non-blocking per-target flock context manager raising ConcurrentMutationError (409)
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import threading
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Annotated, Any, Literal, Self

from appctl_contracts import StrictModel
from pydantic import BeforeValidator, ConfigDict, Field, StrictInt, StrictStr

DEFAULT_AUDIT_LOG = Path("/var/log/homelab/appctl/audit.jsonl")
DEFAULT_LOCK_DIR = Path("/var/lib/homelab/appctl/locks")

REDACT_KEY_PATTERNS = re.compile(
    r".*(?:_KEY|_SECRET|_PASSWORD|_TOKEN|_PASS|_HASH)$", re.IGNORECASE
)


class AuditPhase(StrEnum):
    """Lifecycle phases for control plane audit events."""

    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DENIED = "DENIED"


def _coerce_phase(v: Any) -> Any:
    if isinstance(v, str):
        try:
            return AuditPhase(v)
        except ValueError:
            return v
    return v


FlexibleAuditPhase = Annotated[
    AuditPhase,
    BeforeValidator(_coerce_phase),
]


class PrincipalAudit(StrictModel):
    """Authenticated principal details captured in audit records."""

    identity: StrictStr
    role: StrictStr
    token_id: StrictStr | None = None
    auth_method: StrictStr


class AuditResult(StrictModel):
    """Execution outcome details captured in audit records."""

    status: Literal["SUCCESS", "FAILED", "DENIED", "IN_PROGRESS"]
    exit_code: StrictInt | None = None
    duration_ms: StrictInt | None = None
    commit_sha: StrictStr | None = None
    error_code: StrictStr | None = None
    message: StrictStr | None = None


class AuditEvent(StrictModel):
    """RFC-compliant single-line JSONL audit event model (ADR-0002 / DEC-03)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    audit_version: Literal["1.0"] = "1.0"
    event_id: StrictStr
    operation_id: StrictStr
    timestamp: StrictStr
    phase: FlexibleAuditPhase
    principal: PrincipalAudit
    action: StrictStr
    target: StrictStr | None = None
    transport: StrictStr
    origin: StrictStr
    client: StrictStr
    parameters: dict[str, Any] = Field(default_factory=dict)
    result: AuditResult | None = None


class AuditPersistenceError(Exception):
    """Raised when an audit record cannot be persisted or flushed to disk."""


class ConcurrentMutationError(Exception):
    """Raised when a per-target operation lock cannot be acquired (409 Conflict)."""

    def __init__(self, target: str, message: str | None = None) -> None:
        self.target = target
        msg = message or f"Operation on target '{target}' is locked by a concurrent task."
        super().__init__(msg)


def redact_sensitive_data(val: Any) -> Any:
    """
    Recursively redact values whose dictionary keys match sensitive patterns
    (*_KEY, *_SECRET, *_PASSWORD, *_TOKEN, *_PASS, *_HASH).
    """
    if isinstance(val, dict):
        return {
            k: "[REDACTED]" if REDACT_KEY_PATTERNS.match(str(k)) else redact_sensitive_data(v)
            for k, v in val.items()
        }
    if isinstance(val, list):
        return [redact_sensitive_data(v) for v in val]
    return val


def generate_event_id() -> str:
    """Generate a unique event identifier."""
    return f"evt_{secrets.token_hex(6)}"


def generate_operation_id() -> str:
    """Generate a unique operation identifier."""
    return f"op_{secrets.token_hex(6)}"


def current_iso_timestamp() -> str:
    """Current UTC timestamp formatted as ISO 8601 with millisecond precision."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AuditLogger:
    """
    Append-only thread-safe audit logger writing single-line JSONL records.
    Enforces mode 0600 on the log file and provides a fail-closed pre-execution
    DISPATCHED disk guard.
    """

    def __init__(self, log_path: Path | str | None = None) -> None:
        if log_path is not None:
            self.log_path = Path(log_path)
        elif "APPCTL_AUDIT_LOG" in os.environ:
            self.log_path = Path(os.environ["APPCTL_AUDIT_LOG"])
        else:
            self.log_path = DEFAULT_AUDIT_LOG
        self._lock = threading.Lock()

    def _ensure_log_file(self) -> None:
        """Ensure parent directories exist and file is initialized with mode 0600."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            fd = os.open(
                self.log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            os.close(fd)

    def emit_event(self, event: AuditEvent) -> None:
        """
        Append an audit event as a single JSON line to the audit log.
        Flushes and fsyncs to ensure durability. Raises AuditPersistenceError on failure.
        """
        with self._lock:
            try:
                self._ensure_log_file()
                line = event.model_dump_json() + "\n"
                fd = os.open(
                    self.log_path,
                    os.O_WRONLY | os.O_APPEND,
                    0o600,
                )
                try:
                    os.write(fd, line.encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as err:
                raise AuditPersistenceError(
                    f"Failed to persist audit event '{event.event_id}': {err}"
                ) from err

    def log_dispatched(
        self,
        operation_id: str,
        principal: PrincipalAudit,
        action: str,
        target: str | None,
        transport: str,
        origin: str,
        client: str,
        parameters: dict[str, Any],
    ) -> AuditEvent:
        """
        Fail-closed pre-execution disk guard: logs DISPATCHED phase before any
        subprocess or filesystem mutation begins. Raises AuditPersistenceError if disk write fails.
        """
        clean_params = redact_sensitive_data(parameters)
        event = AuditEvent(
            event_id=generate_event_id(),
            operation_id=operation_id,
            timestamp=current_iso_timestamp(),
            phase=AuditPhase.DISPATCHED,
            principal=principal,
            action=action,
            target=target,
            transport=transport,
            origin=origin,
            client=client,
            parameters=clean_params,
            result=AuditResult(status="IN_PROGRESS"),
        )
        self.emit_event(event)
        return event

    def log_completed(
        self,
        operation_id: str,
        principal: PrincipalAudit,
        action: str,
        target: str | None,
        transport: str,
        origin: str,
        client: str,
        parameters: dict[str, Any],
        duration_ms: int,
        commit_sha: str | None = None,
        message: str | None = None,
    ) -> AuditEvent:
        """Log COMPLETED phase upon clean operation termination."""
        clean_params = redact_sensitive_data(parameters)
        event = AuditEvent(
            event_id=generate_event_id(),
            operation_id=operation_id,
            timestamp=current_iso_timestamp(),
            phase=AuditPhase.COMPLETED,
            principal=principal,
            action=action,
            target=target,
            transport=transport,
            origin=origin,
            client=client,
            parameters=clean_params,
            result=AuditResult(
                status="SUCCESS",
                exit_code=0,
                duration_ms=duration_ms,
                commit_sha=commit_sha,
                error_code=None,
                message=message or "Operation completed successfully",
            ),
        )
        self.emit_event(event)
        return event

    def log_failed(
        self,
        operation_id: str,
        principal: PrincipalAudit,
        action: str,
        target: str | None,
        transport: str,
        origin: str,
        client: str,
        parameters: dict[str, Any],
        duration_ms: int | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> AuditEvent:
        """Log FAILED phase when validation or execution fails."""
        clean_params = redact_sensitive_data(parameters)
        clean_msg = redact_sensitive_data(message) if message else None
        event = AuditEvent(
            event_id=generate_event_id(),
            operation_id=operation_id,
            timestamp=current_iso_timestamp(),
            phase=AuditPhase.FAILED,
            principal=principal,
            action=action,
            target=target,
            transport=transport,
            origin=origin,
            client=client,
            parameters=clean_params,
            result=AuditResult(
                status="FAILED",
                exit_code=exit_code,
                duration_ms=duration_ms,
                error_code=error_code,
                message=clean_msg,
            ),
        )
        self.emit_event(event)
        return event

    def log_denied(
        self,
        operation_id: str,
        principal: PrincipalAudit,
        action: str,
        target: str | None,
        transport: str,
        origin: str,
        client: str,
        parameters: dict[str, Any],
        error_code: str,
        message: str,
    ) -> AuditEvent:
        """Log DENIED phase when authentication, RBAC, or concurrency lock rejects request."""
        clean_params = redact_sensitive_data(parameters)
        event = AuditEvent(
            event_id=generate_event_id(),
            operation_id=operation_id,
            timestamp=current_iso_timestamp(),
            phase=AuditPhase.DENIED,
            principal=principal,
            action=action,
            target=target,
            transport=transport,
            origin=origin,
            client=client,
            parameters=clean_params,
            result=AuditResult(
                status="DENIED",
                error_code=error_code,
                message=message,
            ),
        )
        self.emit_event(event)
        return event


class TargetLock:
    """Context manager acquiring a non-blocking per-target flock."""

    def __init__(self, lock_path: Path, target: str) -> None:
        self.lock_path = lock_path
        self.target = target
        self._fd: int | None = None

    def __enter__(self) -> Self:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as err:
            os.close(fd)
            raise ConcurrentMutationError(self.target) from err
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class TargetLockManager:
    """Manages operational per-target locks under /var/lib/homelab/appctl/locks."""

    def __init__(self, lock_dir: Path | str | None = None) -> None:
        if lock_dir is not None:
            self.lock_dir = Path(lock_dir)
        elif "APPCTL_LOCK_DIR" in os.environ:
            self.lock_dir = Path(os.environ["APPCTL_LOCK_DIR"])
        else:
            self.lock_dir = DEFAULT_LOCK_DIR

    def lock_target(self, target: str) -> TargetLock:
        """Return a non-blocking flock context manager for a workload target."""
        clean_target = re.sub(r"[^a-zA-Z0-9._-]", "_", target)
        lock_path = self.lock_dir / f"{clean_target}.lock"
        return TargetLock(lock_path, target)

    def homepage_sync_lock(self) -> TargetLock:
        """Return a non-blocking flock context manager for homepage compilation."""
        lock_path = self.lock_dir / "_homepage_sync.lock"
        return TargetLock(lock_path, "_homepage_sync")
