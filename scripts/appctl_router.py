"""
appctl_router.py — Operational Router & Unified Execution Engine (P4 Slice 4)

Implements the single command execution pipeline for homelab-appctl.service:
- Pure-Python inert .env loader (enforcing ENV_KEY_RE, RESERVED_ENV_KEYS, zero shell evaluation)
- Canonical domain precedence resolution (HOMELAB_DOMAIN -> DOMAIN_SUFFIX -> DOMAIN)
- Mandatory pre-flight validation (Path containment, parse_manifest, validate_compose_security)
- Bounded subprocess execution engine (fixed argv, shell=False, 256 KiB output cap, hard timeouts)
- Revision-consistent workload.update (clean tree check, git pull, post-pull validation, declared action execution)
- Synchronous atomic Homepage compilation under _homepage_sync.lock
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from appctl_audit import (
    AuditLogger,
    AuditPersistenceError,
    ConcurrentMutationError,
    PrincipalAudit,
    TargetLockManager,
    current_iso_timestamp,
    generate_operation_id,
    redact_sensitive_data,
)
from appctl_auth import (
    AuthorizationError,
    Principal,
    Role,
    enforce_rbac,
    load_admitted_workloads,
)
from appctl_contracts import (
    MUTATING_OPERATIONS,
    SENSITIVE_READ_OPERATIONS,
    TARGET_REQUIRED_OPERATIONS,
    DeployParams,
    ErrorCode,
    GetParams,
    ListParams,
    LogsParams,
    OperationError,
    OperationRequest,
    OperationResponse,
    OperationType,
    UpdateParams,
)
from appctl_engine import (
    CORE_DIR as ENGINE_CORE_DIR,
)
from appctl_engine import (
    HOMELAB_DOMAIN as ENGINE_HOMELAB_DOMAIN,
)
from appctl_engine import (
    SITES_DIR as ENGINE_SITES_DIR,
)
from appctl_engine import (
    check_ssl_cert,
    cmd_sync_homepage,
    get_sites_apps,
    resolve_app,
)
from core_manifest import (
    ENV_KEY_RE,
    RESERVED_ENV_KEYS,
    Manifest,
    ManifestValidationError,
    parse_manifest,
)
from gitops_dispatcher import validate_compose_security

MAX_OUTPUT_BYTES = 256 * 1024  # 256 KiB cap per invocation


class RouterValidationError(Exception):
    """Raised when request payload or pre-flight validation fails (400 Bad Request)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ErrorCode.ERR_VALIDATION_FAILED
        self.http_status = 400


class RouterExecutionError(Exception):
    """Raised when a subprocess or internal operation fails (500 Internal Error)."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = ErrorCode.ERR_EXECUTION_FAILED
        self.exit_code = exit_code
        self.http_status = 500


def parse_dotenv(path: Path, validate_keys: bool = True) -> dict[str, str]:
    """
    Pure-Python inert KEY=VALUE parser.
    Never invokes a shell or evaluates variables. Enforces ENV_KEY_RE and RESERVED_ENV_KEYS.
    """
    if not path.is_file():
        return {}

    env: dict[str, str] = {}
    content = path.read_text(encoding="utf-8", errors="replace")
    for line_num, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()

        # Strip optional surrounding single or double quotes
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]

        if validate_keys:
            if not ENV_KEY_RE.match(key):
                raise RouterValidationError(
                    f"Prohibited environment key syntax in {path.name}:{line_num}: '{key}'"
                )
            if key in RESERVED_ENV_KEYS:
                raise RouterValidationError(
                    f"Prohibited reserved environment key in {path.name}:{line_num}: '{key}'"
                )
        env[key] = val
    return env


def resolve_canonical_domain() -> str:
    """Resolve canonical platform domain: HOMELAB_DOMAIN -> DOMAIN_SUFFIX -> DOMAIN (fails closed)."""
    domain = (
        os.environ.get("HOMELAB_DOMAIN")
        or os.environ.get("DOMAIN_SUFFIX")
        or os.environ.get("DOMAIN")
        or ENGINE_HOMELAB_DOMAIN
    )
    if not domain or not domain.strip():
        raise RouterValidationError(
            "Canonical domain is unset: HOMELAB_DOMAIN, DOMAIN_SUFFIX, or DOMAIN must be configured."
        )
    return domain.strip()


def assemble_workload_env(
    app_dir: Path,
    manifest: Manifest | None,
    app_name: str,
) -> dict[str, str]:
    """
    Assemble the 6-layer in-memory execution environment for Docker Compose subprocesses:
    1. Base PATH and DOCKER_CONFIG
    2. Global rendered secrets (homeserver.env, traefik-deployments.env)
    3. Canonical domain lock
    4. Infrastructure defaults (PROXY_NETWORK, CERT_RESOLVER, SERVICE_NAME, DOMAIN)
    5. Validated manifest.env from app.yaml
    6. Workload .env and app.env files parsed inertly
    """
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", "/run/current-system/sw/bin:/usr/bin:/bin"),
    }
    if "DOCKER_CONFIG" in os.environ:
        env["DOCKER_CONFIG"] = os.environ["DOCKER_CONFIG"]

    # Layer 2: Global secrets from /run/secrets/rendered/ or /run/secrets/
    secrets_candidates = [
        Path("/run/secrets/rendered/homeserver.env"),
        Path("/run/secrets/homeserver.env"),
        Path("/run/secrets/rendered/traefik-deployments.env"),
        Path("/run/secrets/traefik-deployments.env"),
    ]
    if "HOMESERVER_ENV_FILE" in os.environ:
        secrets_candidates.insert(0, Path(os.environ["HOMESERVER_ENV_FILE"]))

    for sec_path in secrets_candidates:
        if sec_path.is_file():
            env.update(parse_dotenv(sec_path, validate_keys=False))

    # Layer 3 & 4: Domain & Infrastructure defaults
    domain = resolve_canonical_domain()
    env["HOMELAB_DOMAIN"] = domain
    env["DOMAIN_SUFFIX"] = domain
    env["DOMAIN"] = domain
    env["PROXY_NETWORK"] = os.environ.get("PROXY_NETWORK", "proxy-net")
    env["CERT_RESOLVER"] = os.environ.get("CERT_RESOLVER", "myresolver")
    env["SERVICE_NAME"] = app_name
    env["CONTAINER_NAME"] = app_name
    env["SERVICE_DOMAIN"] = f"{app_name}.{domain}"

    # Layer 5: Validated manifest env
    if manifest and manifest.env:
        env.update(manifest.env)

    # Layer 6: Workload-scoped .env and app.env
    for local_env in [app_dir / ".env", app_dir / "app.env"]:
        if local_env.is_file():
            env.update(parse_dotenv(local_env, validate_keys=True))

    return env


def run_bounded_subprocess(
    cmd: list[str],
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> tuple[int, str, str]:
    """
    Execute subprocess with fixed argv (shell=False), hard timeout, and 256 KiB output cap.
    Returns (exit_code, stdout_str, stderr_str).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        raw_stdout, raw_stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.communicate()
        raise RouterExecutionError(
            f"Command '{' '.join(cmd)}' timed out after {timeout} seconds",
            exit_code=124,
        ) from exc

    # Enforce 256 KiB cap on output
    stdout_str = raw_stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(raw_stdout) > MAX_OUTPUT_BYTES:
        stdout_str += "\n[TRUNCATED: Output exceeded 256 KiB limit]"

    stderr_str = raw_stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(raw_stderr) > MAX_OUTPUT_BYTES:
        stderr_str += "\n[TRUNCATED: Stderr exceeded 256 KiB limit]"

    return proc.returncode, stdout_str, stderr_str


def validate_workload_preflight(
    app_dir: Path,
    allowed_root: Path,
    app_name: str | None = None,
) -> tuple[Manifest, Path]:
    """
    Enforce strict pre-flight gate:
    1. Target directory must be contained within allowed_root.
    2. app.yaml must parse cleanly via core_manifest.parse_manifest().
    3. docker-compose.yml must pass validate_compose_security().
    """
    app_dir_resolved = app_dir.resolve()
    allowed_root_resolved = allowed_root.resolve()
    if not (
        app_dir_resolved == allowed_root_resolved
        or app_dir_resolved.is_relative_to(allowed_root_resolved)
    ):
        raise RouterValidationError(
            f"Path traversal rejected: '{app_dir}' is outside '{allowed_root}'"
        )

    manifest_file = app_dir / "app.yaml"
    if not manifest_file.is_file():
        raise RouterValidationError(f"Workload manifest not found at '{manifest_file}'")

    try:
        manifest = parse_manifest(manifest_file)
    except ManifestValidationError as err:
        raise RouterValidationError(f"Workload manifest validation failed: {err}") from err

    effective_name = app_name or manifest.name
    compose_file = app_dir / "docker-compose.yml"
    if compose_file.exists():
        ok, reason = validate_compose_security(
            compose_file=compose_file,
            allowed_root=app_dir,
            app_name=effective_name,
        )
        if not ok:
            raise RouterValidationError(
                f"Compose AST security validation failed for '{effective_name}': {reason}"
            )

    return manifest, compose_file


class OperationalRouter:
    """Executes validated domain operations according to P4 boundaries and contracts."""

    def __init__(
        self,
        audit_logger: AuditLogger | None = None,
        lock_manager: TargetLockManager | None = None,
        sites_dir: Path | str | None = None,
        core_dir: Path | str | None = None,
    ) -> None:
        self.audit_logger = audit_logger or AuditLogger()
        self.lock_manager = lock_manager or TargetLockManager()
        self.sites_dir = Path(sites_dir or ENGINE_SITES_DIR)
        self.core_dir = Path(core_dir or ENGINE_CORE_DIR)

    def dispatch(
        self,
        request: OperationRequest,
        principal: Principal,
        transport: str = "unix_socket",
        origin: str = "peer_uid_1000",
        client: str = "appctl-cli/2.0",
    ) -> OperationResponse:
        """Single entrypoint dispatching all 13 canonical domain operations."""
        op_id = generate_operation_id()
        start_time = time.perf_counter()
        target = request.target
        operation = request.operation
        admitted = load_admitted_workloads()
        principal_audit = PrincipalAudit(
            identity=principal.identity,
            role=principal.role.value,
            token_id=principal.token_id,
            auth_method=principal.auth_method,
        )

        # 1. RBAC Gate
        try:
            enforce_rbac(
                principal=principal,
                operation=operation,
                target=target,
                admitted_workloads=admitted,
            )
        except AuthorizationError as err:
            self.audit_logger.log_denied(
                operation_id=op_id,
                principal=principal_audit,
                action=operation.value,
                target=target,
                transport=transport,
                origin=origin,
                client=client,
                parameters=request.parameters,
                error_code=err.code.value,
                message=str(err),
            )
            return OperationResponse(
                version="1.0",
                success=False,
                operation_id=op_id,
                operation=operation,
                target=target,
                error=OperationError(code=err.code, message=str(err)),
            )

        # 2. Target Resolution (for operations requiring target)
        resolved_app = None
        if operation in TARGET_REQUIRED_OPERATIONS:
            if not target:
                return OperationResponse(
                    version="1.0",
                    success=False,
                    operation_id=op_id,
                    operation=operation,
                    target=target,
                    error=OperationError(
                        code=ErrorCode.ERR_VALIDATION_FAILED,
                        message=f"Operation '{operation.value}' requires a non-empty target",
                    ),
                )
            resolved_app = resolve_app(target, sites_dir=str(self.sites_dir))
            if not resolved_app:
                return OperationResponse(
                    version="1.0",
                    success=False,
                    operation_id=op_id,
                    operation=operation,
                    target=target,
                    error=OperationError(
                        code=ErrorCode.ERR_TARGET_NOT_FOUND,
                        message=f"Workload or core target '{target}' not found",
                    ),
                )

        # 3. Concurrency Lock & Pre-Execution Audit (Mutating Operations)
        if operation in MUTATING_OPERATIONS:
            lock_ctx = (
                self.lock_manager.homepage_sync_lock()
                if operation == OperationType.PLATFORM_SYNC_HOMEPAGE
                else self.lock_manager.lock_target(target or "_unknown")
            )
            try:
                with lock_ctx:
                    # Fail-closed pre-execution DISPATCHED audit guard
                    try:
                        self.audit_logger.log_dispatched(
                            operation_id=op_id,
                            principal=principal_audit,
                            action=operation.value,
                            target=target,
                            transport=transport,
                            origin=origin,
                            client=client,
                            parameters=request.parameters,
                        )
                    except AuditPersistenceError as audit_err:
                        return OperationResponse(
                            version="1.0",
                            success=False,
                            operation_id=op_id,
                            operation=operation,
                            target=target,
                            error=OperationError(
                                code=ErrorCode.ERR_EXECUTION_FAILED,
                                message=f"Pre-execution audit guard failed: {audit_err}",
                            ),
                        )

                    # Execute Mutation
                    return self._execute_operation(
                        request=request,
                        principal=principal,
                        principal_audit=principal_audit,
                        resolved_app=resolved_app,
                        op_id=op_id,
                        start_time=start_time,
                        transport=transport,
                        origin=origin,
                        client=client,
                    )
            except ConcurrentMutationError as lock_err:
                self.audit_logger.log_denied(
                    operation_id=op_id,
                    principal=principal_audit,
                    action=operation.value,
                    target=target,
                    transport=transport,
                    origin=origin,
                    client=client,
                    parameters=request.parameters,
                    error_code=ErrorCode.ERR_CONCURRENT_MUTATION.value,
                    message=str(lock_err),
                )
                return OperationResponse(
                    version="1.0",
                    success=False,
                    operation_id=op_id,
                    operation=operation,
                    target=target,
                    error=OperationError(
                        code=ErrorCode.ERR_CONCURRENT_MUTATION,
                        message=str(lock_err),
                    ),
                )

        # 4. Read-Only Operations (no concurrency lock required)
        return self._execute_operation(
            request=request,
            principal=principal,
            principal_audit=principal_audit,
            resolved_app=resolved_app,
            op_id=op_id,
            start_time=start_time,
            transport=transport,
            origin=origin,
            client=client,
        )

    def _execute_operation(
        self,
        request: OperationRequest,
        principal: Principal,
        principal_audit: PrincipalAudit,
        resolved_app: Any,
        op_id: str,
        start_time: float,
        transport: str,
        origin: str,
        client: str,
    ) -> OperationResponse:
        """Route to specific operation handler with post-execution audit logging."""
        operation = request.operation
        target = request.target
        commit_sha: str | None = None
        duration_ms = 0

        try:
            if operation == OperationType.WORKLOAD_LIST:
                result_data = self._op_workload_list(request)
            elif operation == OperationType.WORKLOAD_GET:
                result_data = self._op_workload_get(request, resolved_app)
            elif operation == OperationType.WORKLOAD_RESOLVE:
                result_data = self._op_workload_resolve(resolved_app)
            elif operation == OperationType.PLATFORM_SSL:
                result_data = self._op_platform_ssl(target)
            elif operation == OperationType.PLATFORM_HEALTH:
                result_data = self._op_platform_health()
            elif operation == OperationType.WORKLOAD_CONFIG:
                result_data = self._op_workload_config(resolved_app, principal)
            elif operation == OperationType.WORKLOAD_LOGS:
                result_data = self._op_workload_logs(request, resolved_app)
            elif operation == OperationType.WORKLOAD_DEPLOY:
                result_data = self._op_workload_deploy(request, resolved_app)
            elif operation == OperationType.WORKLOAD_STOP:
                result_data = self._op_workload_stop(resolved_app)
            elif operation == OperationType.WORKLOAD_RESTART:
                result_data = self._op_workload_restart(resolved_app)
            elif operation == OperationType.WORKLOAD_PULL:
                result_data = self._op_workload_pull(resolved_app)
            elif operation == OperationType.WORKLOAD_UPDATE:
                result_data, commit_sha = self._op_workload_update(request, resolved_app)
            elif operation == OperationType.PLATFORM_SYNC_HOMEPAGE:
                result_data = self._op_platform_sync_homepage()
            else:
                raise RouterValidationError(f"Unsupported operation: {operation.value}")

            duration_ms = int((time.perf_counter() - start_time) * 1000)

            # Audit COMPLETED for mutating ops and sensitive reads
            if operation in MUTATING_OPERATIONS or operation in SENSITIVE_READ_OPERATIONS:
                self.audit_logger.log_completed(
                    operation_id=op_id,
                    principal=principal_audit,
                    action=operation.value,
                    target=target,
                    transport=transport,
                    origin=origin,
                    client=client,
                    parameters=request.parameters,
                    duration_ms=duration_ms,
                    commit_sha=commit_sha,
                    message=result_data.get("message", "Operation completed successfully"),
                )

            return OperationResponse(
                version="1.0",
                success=True,
                operation_id=op_id,
                operation=operation,
                target=target,
                result=result_data,
                output=result_data.get("output", ""),
                duration_ms=duration_ms,
                timestamp=current_iso_timestamp(),
            )

        except (RouterValidationError, RouterExecutionError) as err:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            if operation in MUTATING_OPERATIONS or operation in SENSITIVE_READ_OPERATIONS:
                self.audit_logger.log_failed(
                    operation_id=op_id,
                    principal=principal_audit,
                    action=operation.value,
                    target=target,
                    transport=transport,
                    origin=origin,
                    client=client,
                    parameters=request.parameters,
                    duration_ms=duration_ms,
                    exit_code=getattr(err, "exit_code", 1),
                    error_code=err.code.value,
                    message=str(err),
                )
            return OperationResponse(
                version="1.0",
                success=False,
                operation_id=op_id,
                operation=operation,
                target=target,
                duration_ms=duration_ms,
                error=OperationError(code=err.code, message=str(err)),
            )

    # -----------------------------------------------------------------------
    # Domain Operation Implementations
    # -----------------------------------------------------------------------

    def _op_workload_list(self, request: OperationRequest) -> dict[str, Any]:
        params = request.parse_parameters()
        assert isinstance(params, ListParams)
        apps = get_sites_apps(sites_dir=str(self.sites_dir))
        apps_data = [a.to_dict() for a in apps]
        return {
            "total": len(apps_data),
            "workloads": apps_data,
            "message": f"Found {len(apps_data)} workloads",
        }

    def _op_workload_get(self, request: OperationRequest, app: Any) -> dict[str, Any]:
        params = request.parse_parameters()
        assert isinstance(params, GetParams)
        data = app.to_dict()
        return {"workload": data, "message": f"Retrieved metadata for {app.name}"}

    def _op_workload_resolve(self, app: Any) -> dict[str, Any]:
        return {"workload": app.to_dict(), "message": f"Resolved target to {app.name}"}

    def _op_platform_ssl(self, target: str | None) -> dict[str, Any]:
        domain = target or resolve_canonical_domain()
        try:
            cert_info = check_ssl_cert(domain)
            return {"ssl": cert_info, "message": f"SSL certificate inspected for {domain}"}
        except Exception as err:
            raise RouterExecutionError(f"SSL inspection failed for '{domain}': {err}") from err

    def _op_platform_health(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "audit_log": str(self.audit_logger.log_path),
            "lock_dir": str(self.lock_manager.lock_dir),
            "message": "Control plane daemon healthy",
        }

    def _op_workload_config(self, app: Any, principal: Principal) -> dict[str, Any]:
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        cmd = ["docker", "compose", "-f", str(compose_file), "config"]
        code, stdout, stderr = run_bounded_subprocess(cmd, cwd=app_dir, env=env, timeout=15.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose config failed: {stderr}", exit_code=code)

        config_output = stdout
        if principal.role != Role.ADMIN:
            # Redact secrets for non-admin callers
            config_output = redact_sensitive_data(config_output)

        return {"output": config_output, "message": "Evaluated compose configuration"}

    def _op_workload_logs(self, request: OperationRequest, app: Any) -> dict[str, Any]:
        params = request.parse_parameters()
        assert isinstance(params, LogsParams)
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        cmd = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "logs",
            "--no-color",
            "--tail",
            str(params.tail),
        ]
        code, stdout, stderr = run_bounded_subprocess(cmd, cwd=app_dir, env=env, timeout=15.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose logs failed: {stderr}", exit_code=code)

        return {"output": stdout, "tail": params.tail, "message": f"Tail {params.tail} logs"}

    def _op_workload_deploy(self, request: OperationRequest, app: Any) -> dict[str, Any]:
        params = request.parse_parameters()
        assert isinstance(params, DeployParams)
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        # Optional build
        if params.build:
            build_cmd = ["docker", "compose", "-f", str(compose_file), "build"]
            code, out, err = run_bounded_subprocess(build_cmd, cwd=app_dir, env=env, timeout=300.0)
            if code != 0:
                raise RouterExecutionError(f"docker compose build failed: {err}", exit_code=code)

        # Deploy up
        up_cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d"]
        if params.remove_orphans:
            up_cmd.append("--remove-orphans")

        code, out, err = run_bounded_subprocess(up_cmd, cwd=app_dir, env=env, timeout=300.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose up failed: {err}", exit_code=code)

        # Trigger Homepage sync
        self._trigger_homepage_sync()

        return {
            "output": out,
            "action": "deployed",
            "message": f"Workload '{app.name}' deployed successfully",
        }

    def _op_workload_stop(self, app: Any) -> dict[str, Any]:
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        cmd = ["docker", "compose", "-f", str(compose_file), "down"]
        code, out, err = run_bounded_subprocess(cmd, cwd=app_dir, env=env, timeout=300.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose down failed: {err}", exit_code=code)

        self._trigger_homepage_sync()
        return {"output": out, "action": "stopped", "message": f"Workload '{app.name}' stopped"}

    def _op_workload_restart(self, app: Any) -> dict[str, Any]:
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        cmd = ["docker", "compose", "-f", str(compose_file), "restart"]
        code, out, err = run_bounded_subprocess(cmd, cwd=app_dir, env=env, timeout=300.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose restart failed: {err}", exit_code=code)

        self._trigger_homepage_sync()
        return {"output": out, "action": "restarted", "message": f"Workload '{app.name}' restarted"}

    def _op_workload_pull(self, app: Any) -> dict[str, Any]:
        app_dir = Path(app.dir_path)
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        env = assemble_workload_env(app_dir, manifest, app.name)

        pull_cmd = ["docker", "compose", "-f", str(compose_file), "pull"]
        code, _, err = run_bounded_subprocess(pull_cmd, cwd=app_dir, env=env, timeout=300.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose pull failed: {err}", exit_code=code)

        up_cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"]
        code, out_up, err_up = run_bounded_subprocess(up_cmd, cwd=app_dir, env=env, timeout=300.0)
        if code != 0:
            raise RouterExecutionError(f"docker compose up failed after pull: {err_up}", exit_code=code)

        self._trigger_homepage_sync()
        return {"output": out_up, "action": "pulled", "message": f"Workload '{app.name}' pulled and updated"}

    def _op_workload_update(
        self, request: OperationRequest, app: Any
    ) -> tuple[dict[str, Any], str | None]:
        """
        Revision-consistent workload update pipeline (DEC-04):
        1. Clean working tree check (git status --porcelain, 5s)
        2. git pull --ff-only (60s)
        3. Post-pull re-validation of app.yaml & expected_branch & compose security
        4. Execute declared actions (compose_pull, compose_build, compose_up, compose_restart)
        5. Homepage sync
        """
        params = request.parse_parameters()
        assert isinstance(params, UpdateParams)
        app_dir = Path(app.dir_path)

        # 1. Clean working tree check
        code, status_out, _ = run_bounded_subprocess(
            ["git", "-C", str(app_dir), "status", "--porcelain"],
            cwd=app_dir,
            timeout=5.0,
        )
        if code != 0 or status_out.strip():
            raise RouterValidationError(
                f"Workload '{app.name}' has uncommitted changes in git working tree; update aborted."
            )

        # 2. git pull --ff-only
        pull_cmd = ["git", "-C", str(app_dir), "pull", "--ff-only"]
        if params.expected_branch:
            pull_cmd.extend(["origin", params.expected_branch])
        code, pull_out, pull_err = run_bounded_subprocess(
            pull_cmd, cwd=app_dir, timeout=60.0
        )
        if code != 0:
            raise RouterExecutionError(f"git pull failed for '{app.name}': {pull_err}", exit_code=code)

        # 3. Post-pull re-validation of manifest and expected_branch
        manifest, compose_file = validate_workload_preflight(app_dir, self.sites_dir, app.name)
        if (
            params.expected_branch
            and manifest.source
            and manifest.source.ref != params.expected_branch
        ):
            raise RouterValidationError(
                f"Post-pull branch mismatch for '{app.name}': expected '{params.expected_branch}', "
                f"manifest declared '{manifest.source.ref}'"
            )

        # 4. Execute declared actions from manifest
        env = assemble_workload_env(app_dir, manifest, app.name)
        declared_actions = manifest.deployment.actions or ["compose_up"]
        output_log = [f"git: {pull_out.strip()}"]

        for action in declared_actions:
            if action == "git_pull":
                continue  # already executed in step 2
            elif action == "compose_pull":
                cmd = ["docker", "compose", "-f", str(compose_file), "pull"]
            elif action == "compose_build" or (params.build and action == "compose_up"):
                cmd = ["docker", "compose", "-f", str(compose_file), "build"]
            elif action == "compose_up":
                cmd = ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"]
            elif action == "compose_restart":
                cmd = ["docker", "compose", "-f", str(compose_file), "restart"]
            else:
                continue

            code, act_out, act_err = run_bounded_subprocess(cmd, cwd=app_dir, env=env, timeout=300.0)
            if code != 0:
                raise RouterExecutionError(
                    f"Action '{action}' failed during workload.update for '{app.name}': {act_err}",
                    exit_code=code,
                )
            output_log.append(f"{action}: {act_out.strip()}")

        # 5. Extract current commit SHA
        _, sha_out, _ = run_bounded_subprocess(
            ["git", "-C", str(app_dir), "rev-parse", "--short", "HEAD"],
            cwd=app_dir,
            timeout=5.0,
        )
        commit_sha = sha_out.strip() or None

        # 6. Trigger Homepage sync
        self._trigger_homepage_sync()

        return {
            "output": "\n".join(output_log),
            "commit_sha": commit_sha,
            "actions": declared_actions,
            "message": f"Workload '{app.name}' updated and reconciled successfully",
        }, commit_sha

    def _op_platform_sync_homepage(self) -> dict[str, Any]:
        self._trigger_homepage_sync()
        return {"action": "sync_homepage", "message": "Homepage dashboard synchronized successfully"}

    def _trigger_homepage_sync(self) -> None:
        """Atomic compile of Homepage dashboard services.yaml under _homepage_sync.lock."""
        try:
            cmd_sync_homepage()
        except (OSError, ValueError, RuntimeError) as exc:
            import sys
            print(f"Warning: Homepage sync non-fatal error: {exc}", file=sys.stderr)
