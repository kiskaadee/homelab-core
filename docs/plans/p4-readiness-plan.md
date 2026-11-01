# 📋 Control Plane Milestone P4: Readiness Plan & Boundary Specifications

**Status**: ✅ **Approved & Locked (Pre-P4 Security Gate Complete; Ready for Daemon Implementation)**  
**Target Milestone**: [Milestone P4: Appctl Control Plane Daemon & API](../../ROADMAP.md#milestone-p4-appctl-control-plane-daemon--command-oriented-api)  
**Governing ADR**: [`0002-appctl-control-plane-architecture.md`](../decisions/0002-appctl-control-plane-architecture.md)  

---

## 🎯 Purpose & Scope

Before initiating code implementation for `homelab-appctl.service`, this readiness plan formalizes the operational contracts, execution boundaries, and security invariants that P4 depends upon. 

Milestones P0–P3 and the Pre-P4 Security Gate established the core foundation:
- **P0**: Baseline consolidation & Pytest/Nix test harness.
- **P1**: Zero-data-loss mail realignment & state isolation (`~/var/lib/homelab/snappymail`).
- **P2**: GitOps repository registry (`deployments.yaml`), AST security validation, and Systemd sandboxing.
- **P3**: Manifest Schema v1.0 ([`core_manifest.py`](../../scripts/core_manifest.py)), two-axis inspection ([`provenance.py`](../../scripts/provenance.py)), and authoritative engine ([`appctl_engine.py`](../../scripts/appctl_engine.py)).
- **Pre-P4 Security Gate (Completed)**:
  - Eliminated shell `eval` and unvalidated environment injection in `scripts/appctl`.
  - Enforced `Core/config/deployments.yaml` as sole GitOps registry authority, removing dynamic host discovery.
  - Hardened Compose AST validator against relative traversal, cross-tenant mounts, and host capability escapes.
  - Unified manifest parsing on `core_manifest.py` and enforced fail-closed `cmd_resolve` behavior.

This plan resolves the six architectural prerequisites necessary to implement P4 as a secure, fail-closed operational control plane rather than discovering boundaries ad-hoc during coding.

---

## 📑 Sequence of Deliverables

| Deliverable | Phase | Focus | Status |
| :--- | :--- | :--- | :--- |
| **1. Implementation Inventory** | P0 Verification | Baseline truth, callers, dependencies, and migration state | ✅ **Completed** |
| **2. Command & Error Contract** | P4 Prerequisite (Blocker) | Canonical domain operations, typed schemas, and error taxonomy | ✅ **Locked & Approved** |
| **3. Authorization Design** | Security Gate (Blocker) | Token lifecycle, RBAC matrix, and fail-closed evaluation | ✅ **Locked & Approved** |
| **4. Execution & Privilege Boundaries** | P4 Prerequisite | Subprocess allowlist, Docker API capabilities, and filesystem roots | ✅ **Locked & Approved** |
| **5. Audit Telemetry Semantics** | P4 Prerequisite | RFC JSONL audit schema, persistent ownership, and lifecycle | ✅ **Locked & Approved** |
| **6. P4 Acceptance Test Suite** | Verification | Concrete DoD, black-box transport parity, and security regression gates | ✅ **Locked & Approved** |

---

## 1. Reconcile the Implementation Inventory

### A. Current File & Module Roles

| File / Component | Language | Authority Status | Functional Responsibilities |
| :--- | :--- | :--- | :--- |
| [`scripts/appctl`](../../scripts/appctl) | Bash | **Active CLI Client** | CLI entry point; secret environment sourcing (`homeserver.env`, `traefik-deployments.env`); resolves metadata via engine; directly runs `docker compose` and `git pull` subprocesses. |
| [`scripts/appctl_engine.py`](../../scripts/appctl_engine.py) | Python 3 | **Authoritative Engine** | Domain models (`SitesApp`, `CoreService`); schema v1.0 parsing (`core_manifest.py`); two-axis inspection (`provenance.py`); SSL cert inspection; Homepage compilation; metadata serialization (`resolve`). |
| [`scripts/core_manifest.py`](../../scripts/core_manifest.py) | Python 3 | **Schema Library** | Authoritative Pydantic v2 models (`ConfigDict(extra="forbid")`); transport validation (`https://`, `ssh://`, `git@`); ref semantics classification (`branch`, `tag`, `commit`). |
| [`scripts/provenance.py`](../../scripts/provenance.py) | Python 3 | **Provenance Library** | Two-axis status engine; local checkout inspection (`git status`, commit SHA); bounded upstream query (`git ls-remote` with 10s timeout). |
| [`scripts/gitops_dispatcher.py`](../../scripts/gitops_dispatcher.py) | Python 3 | **Webhook Ingress & Queue** | Webhook listener (port 9000); HMAC validation; `deployments.yaml` admission; `.pending.json` commit-superseding queue; Compose AST validator (`validate_compose_security`). |

### B. Command Inventory & Implementation Division

| Command | CLI Syntax | Handled By | Operation Type | Current Behavior |
| :--- | :--- | :--- | :--- | :--- |
| `list` / `status` | `appctl list [--fetch] [--core] [--ssl]` | `appctl_engine.py` | Read-Only | Scans `~/Sites`, evaluates two-axis checkout and upstream provenance, queries container status, prints formatted table. |
| `info` | `appctl info <service> [--ssl]` | `appctl_engine.py` | Read-Only | Prints detailed metadata, two-axis local checkout vs. upstream provenance, network memberships, and TLS certificate info. |
| `ssl` | `appctl ssl [service]` | `appctl_engine.py` | Read-Only | Connects over TLS socket (port 443), extracts peer X.509 certificate, calculates days until expiration. |
| `resolve` | `appctl resolve <query>` | `appctl_engine.py` | Read-Only | Resolves canonical name / alias; emits JSON string consumed by Bash `setup_app_env`. |
| `complete` | `appctl complete` | `appctl_engine.py` | Read-Only | Emits space-separated list of known workload names, aliases, and core services for Bash shell autocompletion. |
| `sync` | `appctl sync [--homepage-dir=PATH]` | `appctl_engine.py` | Mutating (Filesystem) | Reads all `app.yaml` manifests, groups and sorts cards by weight, writes `config/homepage/services.yaml`. |
| `up` | `appctl up <service\|core>` | `scripts/appctl` (Bash) | Mutating (Containers) | Sourced environment variables $\to$ `docker compose up -d --remove-orphans` $\to$ triggers `appctl sync`. |
| `down` | `appctl down <service\|core>` | `scripts/appctl` (Bash) | Mutating (Containers) | `docker compose down` (or `stop` for Core services) $\to$ triggers `appctl sync`. |
| `restart` | `appctl restart <service\|core>` | `scripts/appctl` (Bash) | Mutating (Containers) | Sourced environment variables $\to$ `docker compose restart` (or down/up) $\to$ triggers `appctl sync`. |
| `update` | `appctl update <service\|core>` | `scripts/appctl` (Bash) | Mutating (Git + Containers) | Verifies clean git working tree $\to$ `git pull --ff-only` $\to$ `docker compose pull` $\to$ `docker compose up -d` $\to$ triggers `appctl sync`. |
| `pull` | `appctl pull <service\|core>` | `scripts/appctl` (Bash) | Mutating (Images) | `docker compose pull` $\to$ `docker compose up -d --remove-orphans`. |
| `logs` | `appctl logs <service\|core>` | `scripts/appctl` (Bash) | Read-Only (Sensitive) | Dispatches `docker compose logs`. (Bounded tail output in P4; live streaming deferred to P7.) |
| `config` | `appctl config <service\|core>` | `scripts/appctl` (Bash) | Read-Only (Sensitive) | Injects environment $\to$ executes `docker compose config` to view fully evaluated compose AST. |

### C. Active Call Sites Across the Repository

1. **System Path Integration**:
   - [`nixos/configuration.nix`](../../nixos/configuration.nix#L119-L122): Installs host wrapper `/run/current-system/sw/bin/appctl` calling `${CORE_DIR:-$HOME/Core}/scripts/appctl "$@"`.
2. **Internal Script Invocation**:
   - `scripts/appctl` calls `python3 "$ENGINE" resolve "$query"` to set up the Compose environment.
   - `scripts/appctl` calls `python3 "$ENGINE" sync` after mutating actions (`up`, `down`, `restart`, `update`).
3. **Automated Test Coverage (124 Passing Tests)**:
   - [`tests/unit/test_core_manifest.py`](../../tests/unit/test_core_manifest.py): 24 unit tests covering Pydantic v2 schema validation, extra-field rejection, and ref classification.
   - [`tests/unit/test_appctl_engine.py`](../../tests/unit/test_appctl_engine.py): 20 unit tests exercising `resolve_app`, `get_docker_status`, `check_ssl_cert`, and `cmd_sync_homepage`.
   - [`tests/unit/test_provenance.py`](../../tests/unit/test_provenance.py): 10 unit tests covering two-axis status inspection and timeout handling.
   - [`tests/security/test_gitops_security.py`](../../tests/security/test_gitops_security.py): 60 security tests covering HMAC verification, `deployments.yaml` containment, Compose AST validation, and worker serialization.
   - [`tests/security/test_security_invariants.py`](../../tests/security/test_security_invariants.py): 7 security invariant tests covering `socket-proxy`, `eval` absence, CLI env injection resilience, and platform genericism.
   - [`tests/structural/test_repository_structure.py`](../../tests/structural/test_repository_structure.py): 3 structural tests asserting required paths and license preservation.

### D. Architectural Findings & Migration Deductions

1. **V1 Engine is Fully Superseded & V2 Shim Retired**: `scripts/appctl_engine.py` is the single authoritative metadata engine, and `scripts/core_manifest.py` is migrated to Pydantic v2 (`ADR-0003`).
2. **Duplicated Operational Execution in GitOps (`DEC-04`)**: `scripts/gitops_dispatcher.py` and `scripts/appctl` maintain separate implementations of `git pull` and `docker compose up`, and `gitops_dispatcher.py` currently omits workload environment injection (`SERVICE_DOMAIN`, `CONTAINER_NAME`, `app.yaml` `env:`, `traefik-deployments.env`). In P4, `gitops_dispatcher.py` retains webhook HMAC verification, `deployments.yaml` admission, and the asynchronous `.pending.json` commit-superseding queue, while its worker delegates deployment execution to the daemon's `workload.update` operation over `/run/homelab/appctl.sock` and uses `core_manifest.parse_manifest()`.
3. **Shell `.env` Sourcing & Missing Compose AST Pre-Flight on CLI Path (`DEC-05`)**: While `eval` was removed from `scripts/appctl`, the Bash CLI still runs `source "$APP_DIR/.env"` and `source "$APP_DIR/app.env"` and does not invoke `validate_compose_security()`. The P4 daemon replaces Bash `source` with an inert pure-Python key-value parser (enforcing `ENV_KEY_RE` and `RESERVED_ENV_KEYS`) and runs `validate_compose_security()` before every workload Compose operation.

---

## 2. Command and Error Contract (P4 Prerequisite — Locked via `DEC-01` & `DEC-04`)

### A. Wire Protocol, Endpoint Surface & Canonical Domain Operations (`DEC-01`)

1. **HTTP/1.1 Over Both Transports**: Both the local Unix Domain Socket (`/run/homelab/appctl.sock`) and the loopback TCP listener (`127.0.0.1:9001`) speak standard **HTTP/1.1** and traverse a single shared request pipeline (Body Size Limit $\to$ JSON Parse $\to$ Pydantic v2 Validate $\to$ Auth/RBAC Gate $\to$ Target Resolution & Lock $\to$ Pre-Execution Audit $\to$ Execution $\to$ Post-Execution Audit).
2. **Single Canonical Operation Endpoint**:
   - `POST /v1/operations`: Dispatches all 13 canonical domain operations (both read-only and mutating).
   - `GET /v1/health`: Convenience liveness probe that internally dispatches `platform.health`.
   - No multi-route REST aliases (`GET /workloads`, `POST /operations/deploy`, etc.) are exposed in P4, eliminating URL-vs-body ambiguity.
3. **Bounded Non-Streaming Logs**: `workload.logs` is strictly non-streaming in P4 (`tail: StrictInt` bounded `1..1000`, default `100`). Live streaming (`--follow` / SSE) is deferred to Milestone P7.
4. **Payload & Output Bounds**: Maximum HTTP request body size is `64 KiB` (larger bodies are rejected with `400 ERR_VALIDATION_FAILED`). Maximum captured subprocess `stdout`/`stderr` is `256 KiB` per invocation.

```text
                             ┌─────────────────────────┐
                             │    Client Request       │
                             │  POST /v1/operations    │
                             └────────────┬────────────┘
                                          ▼
                    ┌───────────────────────────────────────────┐
                    │        Operation Dispatch Router          │
                    └─────────────────────┬─────────────────────┘
                                          │
          ┌───────────────────────────────┴───────────────────────────────┐
          ▼                                                               ▼
    [ Read-Only Operations ]                                    [ Mutating Operations ]
    • workload.list      (viewer)                               • workload.deploy        (operator / admin*)
    • workload.get       (viewer)                               • workload.stop          (operator / admin*)
    • workload.resolve   (viewer)                               • workload.restart       (operator / admin*)
    • platform.ssl       (viewer)                               • workload.update        (operator / admin*)
    • platform.health    (viewer)                               • workload.pull          (operator / admin*)
    • workload.config    (operator / admin* — sensitive read)   • platform.sync_homepage (operator / admin)
    • workload.logs      (operator / admin* — sensitive read)
    (* targeting 'core' or a 'core_service' strictly requires 'homelab-admin')
```

### B. Typed Request and Response Models (Pydantic v2 Pipeline)

All control plane API requests, responses, and error payloads are defined and validated through **Pydantic v2** (`pydantic.BaseModel`, `pydantic.Field`, `ConfigDict(extra="forbid", strict=True)`), joining the existing Pydantic v2 manifest schema in [`scripts/core_manifest.py`](../../scripts/core_manifest.py):

- **Runtime Validation Boundary**: Incoming JSON payloads over both Unix Domain Socket and HTTP endpoints are validated at the transport boundary before reaching the operational router.
- **Fail-Closed Extra Fields (`extra="forbid"`)**: Rejects any undeclared or unknown fields immediately.
- **Strict Typing vs. Coercion Rules (`strict=True`)**:
  - *Strict Validation*: Operations (`operation`), target identifiers (`target`), integer bounds (`tail`), boolean flags (`recreate`, `remove_orphans`, `build`, `fetch`, `core`, `ssl`), and optional strings (`expected_branch`) enforce strict types (`StrictStr`, `StrictBool`, `StrictInt`). Implicit coercion (e.g., `"true"` or `1` to `True`, or floats to ints) is forbidden.
  - *Permitted Normalization*: Only explicitly defined string whitespace trimming via field validators is permitted.
- **Operation-Specific Parameters (`DEC-04`)**:
  - `workload.deploy`: `recreate: StrictBool = True`, `remove_orphans: StrictBool = True`, `build: StrictBool = False`.
  - `workload.update`: `expected_branch: StrictStr | None = None`, `build: StrictBool = False`. Enforces revision-consistent post-pull validation of `app.yaml` (`deployment.branch` and `deployment.actions`, supporting `git_pull`, `compose_pull`, `compose_build`, `compose_up`, `compose_restart`).
  - `workload.logs`: `tail: StrictInt = Field(default=100, ge=1, le=1000)`.
  - `workload.list`: `fetch: StrictBool = False`, `core: StrictBool = False`, `ssl: StrictBool = False`.
  - `workload.get`: `ssl: StrictBool = False`.
- **Information Disclosure Prevention**: Transport handlers intercept `pydantic.ValidationError`, strip raw input values (`e.errors(include_input=False, include_url=False)`), and map violations to `ERR_VALIDATION_FAILED` (`400 Bad Request`).

All requests and responses use strict JSON payloads with explicit schema versioning (`version: "1.0"`):

```json
// Example: POST /v1/operations
// Request:
{
  "version": "1.0",
  "operation": "workload.deploy",
  "target": "jellyfin",
  "parameters": {
    "recreate": true,
    "remove_orphans": true,
    "build": false
  }
}

// Response (Success):
{
  "version": "1.0",
  "status": "success",
  "operation_id": "op_01JAX71BC98E4",
  "target": "jellyfin",
  "timestamp": "2026-10-16T12:00:00Z",
  "duration_ms": 1420,
  "result": {
    "action": "deployed",
    "target_type": "app",
    "health": "🟢 Running (1)"
  }
}
```

### C. Standardized Error Taxonomy

Failures are strongly typed and distinguished across six canonical error codes:

| Error Classification | HTTP Code | Error Code | Description / Fail-Closed Action |
| :--- | :--- | :--- | :--- |
| **Validation Error** | `400 Bad Request` | `ERR_VALIDATION_FAILED` | Request schema violation, body $>64\text{ KiB}$, invalid target format, malformed `app.yaml`, dirty git working tree on update, post-pull branch mismatch, or Compose AST security violation. |
| **Authentication Error** | `401 Unauthorized` | `ERR_AUTH_REQUIRED` | Missing, unknown, or malformed Bearer token (or unauthenticated UDS caller attempting a token-gated operation). |
| **Authorization Error** | `403 Forbidden` | `ERR_PERMISSION_DENIED` | Authenticated principal lacks required role (e.g., `homelab-viewer` attempting mutation or `config`/`logs`; `homelab-operator` targeting `core`/`core_service` or a workload not in `deployments.yaml`). |
| **Target Not Found** | `404 Not Found` | `ERR_TARGET_NOT_FOUND` | Workload target does not exist under `~/Sites` or Core services registry. |
| **State Conflict** | `409 Conflict` | `ERR_CONCURRENT_MUTATION` | Another mutating operation on the target workload is currently holding the per-target operation lock. |
| **Execution Error** | `500 Internal Error`| `ERR_EXECUTION_FAILED` | Audit log write failure, Docker/Git subprocess non-zero exit, or subprocess timeout expiration. |

---

## 3. Authorization Design (Security Gate — Locked via `DEC-02` & `DEC-05`)

### A. The Core Principle: Host Access Does Not Confer Mutation Authority

Entering the host via SSH establishes Unix access, but **zero operational control plane mutation or secret-inspection privileges**. Every mutating request—and every sensitive read (`workload.config`, `workload.logs`)—to `homelab-appctl.service` must present an authenticated Bearer token.

### B. Principal Identity, Role Mapping & Target Scope (`DEC-02`, `DEC-05`)

The platform enforces three hierarchical authorization tiers:

| Role | Permitted Operations & Target Scope | Typical Principals |
| :--- | :--- | :--- |
| `homelab-admin` | All operations across **all targets**: `core` stack, individual `core_service` containers (`traefik`, `authelia`, `stalwart`, etc.), and any valid `~/Sites/<app>` directory contained within `SITES_DIR`. | Interactive human operator token (`operator`). |
| `homelab-operator` | Workload mutations (`deploy`, `stop`, `restart`, `update`, `pull`, `sync_homepage`) and sensitive reads (`config`, `logs`) **strictly restricted to workloads registered in [`config/deployments.yaml`](../../config/deployments.yaml)**. Cannot target `core`, `core_service`, or unregistered `~/Sites` directories (`403 ERR_PERMISSION_DENIED`). | GitOps webhook worker token (`gitops-dispatcher`), scoped automation scripts. |
| `homelab-viewer` | Non-sensitive read-only operations (`workload.list`, `workload.get`, `workload.resolve`, `platform.ssl`, `platform.health`). Zero mutations permitted; `workload.config` and `workload.logs` are denied (`403 ERR_PERMISSION_DENIED`). | Local UDS `SO_PEERCRED` operator session fallback, read-only status monitors. |

### C. Transport Authentication Rules (P4 vs. P5 Boundary)

1. **SOPS-Projected Hashed Token Store (`/run/secrets/appctl/tokens.json`)**:
   - Owned by `homelab-appctl` (mode `0400`).
   - Stores an array of token descriptors (`token_id`, `identity`, `role`, and 64-char hex `sha256` digest of the bearer secret).
   - Incoming `Authorization: Bearer <secret>` headers are hashed with SHA-256 and compared via `hmac.compare_digest`.
   - Required for **all mutations**, **all `workload.config` / `workload.logs` calls**, and **all HTTP listener requests**.
2. **Unix Domain Socket (`/run/homelab/appctl.sock`) & `SO_PEERCRED`**:
   - Socket ownership: `homelab-appctl:homelab-operators`, file mode `0660`.
   - The UDS listener extracts kernel peer credentials (`SO_PEERCRED`: `uid`, `gid`, `pid`) on every connection and records `origin = f"peer_uid_{uid}"`.
   - If no `Authorization` header is supplied over UDS and `uid` belongs to an authorized local operator, the request is authenticated as `Principal(identity=f"unix_uid_{uid}", role="homelab-viewer", token_id=None, auth_method="unix_peercred")`. This enables zero-friction local inspection (`appctl list`, `appctl info`, `appctl ssl`) and shell tab-completion (`appctl complete`) while blocking all mutations and secret dumps (`401`/`403`).
3. **HTTP Network Listener (`127.0.0.1:9001`)**:
   - Bound strictly to loopback (`127.0.0.1`) in P4 (`APPCTL_HTTP_HOST=127.0.0.1`, `APPCTL_HTTP_PORT=9001`).
   - Every `POST /v1/operations` call over HTTP requires a valid Bearer token (`401 ERR_AUTH_REQUIRED` if missing).
   - **ForwardAuth Headers Deferred to P5**: Proxy headers (`Remote-User`, `Remote-Groups`) are **ignored and never trusted** in P4. (In P5, ForwardAuth will be introduced with dedicated proxy authentication so workload containers on `proxy-net` cannot spoof headers.)
4. **GitOps Ingress (`homelab-gitops.service` $\to$ Daemon UDS)**:
   - `gitops_dispatcher.py` verifies the Gitea webhook HMAC-SHA256 signature against `/run/secrets/gitops/webhook_secret`, admits the push against `config/deployments.yaml`, and enqueues `<target>.pending.json`.
   - The serialized background worker invokes `POST /v1/operations` (`operation: "workload.update"`, `parameters: {"expected_branch": branch}`) over `/run/homelab/appctl.sock` authenticating with its `homelab-operator` Bearer token.

---

## 4. Execution & Privilege Boundaries (P4 Prerequisite — Locked via `DEC-03` & `DEC-05`)

### A. Operational Capability Matrix & Timeouts

Each canonical operation is bound to minimal execution permissions, fixed argument lists (`shell=False`), hard timeouts, and role checks:

| Domain Operation | Subprocess / Tool | Allowed Arguments / API | Timeout | Required Role (`Sites` / `Core`) |
| :--- | :--- | :--- | :--- | :--- |
| `workload.list` | In-process (`appctl_engine.py`, `provenance.py`) | Scan `~/Sites/`, `git status` (`5s`), optional `git ls-remote` (`10s`), `docker compose ps` (`15s`) | `15s` / cmd | `homelab-viewer` |
| `workload.get` | In-process (`appctl_engine.py`, `core_manifest.py`) | Parse `app.yaml`, inspect git status (`5s`), `git ls-remote` (`10s`), `docker inspect` (`15s`) | `15s` / cmd | `homelab-viewer` |
| `workload.resolve` | In-process (`appctl_engine.py`) | Canonical target resolution & metadata dictionary | In-process | `homelab-viewer` |
| `platform.ssl` | In-process (`appctl_engine.py`) | TLS peer certificate check on port 443 | `3s` / host | `homelab-viewer` |
| `platform.health` | In-process | Daemon liveness, socket/lock/audit readiness check | In-process | `homelab-viewer` |
| `workload.config` | `docker` | `compose -f <compose_file> config` (with secret redaction for non-admin) | `15s` | `homelab-operator` / `homelab-admin` |
| `workload.logs` | `docker` | `compose -f <compose_file> logs --no-color --tail <1..1000>` | `15s` | `homelab-operator` / `homelab-admin` |
| `workload.deploy` | `docker` | Optional `compose -f <compose_file> build` (`if build=True`) $\to$ `compose -f <compose_file> up -d [--remove-orphans]` $\to$ Homepage sync | `300s` | `homelab-operator` / `homelab-admin` |
| `workload.stop` | `docker` | `compose -f <compose_file> down` (or `stop <container>` for `core_service`) $\to$ Homepage sync | `300s` | `homelab-operator` / `homelab-admin` |
| `workload.restart`| `docker` | `compose -f <compose_file> restart [<container>]` $\to$ Homepage sync | `300s` | `homelab-operator` / `homelab-admin` |
| `workload.pull` | `docker` | `compose -f <compose_file> pull [<container>]` $\to$ `compose -f <compose_file> up -d --remove-orphans` | `300s` | `homelab-operator` / `homelab-admin` |
| `workload.update` | `git`, `docker` | 1. Clean tree check (`git status --porcelain`, `5s`)<br>2. `git -C <dir> pull --ff-only [origin <expected_branch>]` (`60s`)<br>3. Re-validate `app.yaml` & `expected_branch` + `validate_compose_security()`<br>4. Execute declared actions (`compose_pull`, `compose_build`, `compose_up`, `compose_restart`, `300s` each) $\to$ Homepage sync | `60s` (git)<br>`300s` (docker) | `homelab-operator` / `homelab-admin` |
| `platform.sync_homepage` | In-process (`appctl_engine.py`) | Atomic compile of `Core/config/homepage/services.yaml` under `_homepage_sync.lock` | In-process | `homelab-operator` / `homelab-admin` |

### B. Unified Pre-Flight, Environment Assembly & Secret Boundary (`DEC-05`)

1. **Pre-Flight Validation Gate**: Every workload mutation and `workload.config` call executes in strict order:
   - **Path Containment**: Target directory must resolve strictly inside `SITES_DIR.resolve()` (or equal `CORE_DIR.resolve()` for `homelab-admin`).
   - **Registry Admission**: If `principal.role == "homelab-operator"`, target must be present in `config/deployments.yaml`.
   - **Manifest Validation**: `core_manifest.parse_manifest(app_dir / "app.yaml")` must succeed with zero errors.
   - **Compose AST Security Validation**: `validate_compose_security(compose_file, app_dir, app_name=manifest.name)` must pass (blocking `privileged: true`, `cap_add`, `devices`, `security_opt: unconfined`, host namespaces, `/var/run/docker.sock`, top-level `include`, escaping `env_file`, undeclared volumes, and cross-tenant bind mounts).
2. **Inert Pure-Python Environment Assembly (No Shell `source`)**:
   - The daemon constructs the subprocess environment dictionary in memory:
     1. Base system `PATH` and `DOCKER_CONFIG`.
     2. Global secret files (`/run/secrets/rendered/homeserver.env`, `/run/secrets/rendered/traefik-deployments.env` with `/run/secrets/*` fallback) parsed via a pure-Python `KEY=VALUE` reader (no shell expansion).
     3. Canonical domain lock (`HOMELAB_DOMAIN` resolved via `HOMELAB_DOMAIN -> DOMAIN_SUFFIX -> DOMAIN`).
     4. Infrastructure defaults (`PROXY_NETWORK=proxy-net`, `CERT_RESOLVER=myresolver`, `DOMAIN_SUFFIX=$HOMELAB_DOMAIN`) and workload identity (`SERVICE_NAME`, `CONTAINER_NAME`, `SERVICE_DOMAIN`, `DOMAIN`).
     5. Validated `manifest.env` dictionary from `app.yaml`.
     6. Optional `$APP_DIR/.env` and `$APP_DIR/app.env` parsed via pure-Python `KEY=VALUE` reader with every key validated against `ENV_KEY_RE` and `RESERVED_ENV_KEYS`.
3. **Secret Isolation & Redaction**:
   - The daemon never accesses SOPS master keys (`/etc/ssh/ssh_host_ed25519_key`).
   - Any key matching `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN`, `*_PASS`, or `*_HASH` (and any known secret value from `/run/secrets/`) is replaced with `"[REDACTED]"` in audit logs, error responses, and diagnostic messages.

### C. Systemd Unit Sandboxing & Service Identity (`homelab-appctl.service` — `DEC-03`)

- **Dedicated Service Identity**:
  - `User = "homelab-appctl"`
  - `Group = "homelab-operators"`
  - `SupplementaryGroups = [ "docker" ]`
- **Managed Runtime, State & Log Directories**:
  - `RuntimeDirectory = "homelab"` (`RuntimeDirectoryMode = "0750"`): Hosts `/run/homelab/appctl.sock` (mode `0660`, `homelab-appctl:homelab-operators`).
  - `StateDirectory = "homelab/appctl"` (`StateDirectoryMode = "0700"`): Hosts `/var/lib/homelab/appctl/locks/<target>.lock` and `_homepage_sync.lock`.
  - `LogsDirectory = "homelab/appctl"` (`LogsDirectoryMode = "0700"`): Hosts `/var/log/homelab/appctl/audit.jsonl` (mode `0600`, `homelab-appctl:homelab-operators`).
- **Declarative Filesystem Access (`systemd.tmpfiles.rules` POSIX ACLs)**:
  - Grants `homelab-appctl` traverse (`x`) on `/home/<operator>` and `/home/<operator>/Core`.
  - Applies recursive default POSIX ACLs (`A+`) granting `d:u:homelab-appctl:rwx,u:homelab-appctl:rwx` on `/home/<operator>/Sites`, `/home/<operator>/var/lib/homelab`, and `/home/<operator>/Core/config/homepage` so both `<operator>` and `homelab-appctl` can update Git checkouts and write `services.yaml` without permission conflicts.
- **Systemd Hardening**:
  - `ProtectSystem = "strict"`, `ProtectHome = "read-only"`, `PrivateTmp = true`, `NoNewPrivileges = true`, `ProtectKernelTunables = true`, `ProtectControlGroups = true`.
  - `ReadWritePaths`: `/home/<operator>/Sites`, `/home/<operator>/var/lib/homelab`, `/home/<operator>/Core/config/homepage`, `/var/log/homelab/appctl`, `/var/lib/homelab/appctl`, `/run/homelab`.

### D. Platform Genericism & Domain Decoupling
Per `AGENTS.md` §3, all control-plane modules, NixOS definitions, Compose stacks, and tests must remain instance-agnostic:
- Canonical domain precedence: `HOMELAB_DOMAIN` $\to$ `DOMAIN_SUFFIX` $\to$ `DOMAIN` (fails closed if unset).
- Test fixtures and documentation examples use RFC 2606 domains (`example.com`, `example.org`, `example.test`).

---

## 5. Audit Telemetry & Operational State Semantics (`DEC-03`)

### A. Distinction Between Operation Result, Audit Event & Deployment State

1. **Operation Result (`OperationResponse`)**: Synchronous HTTP/1.1 response payload returned immediately to the caller.
2. **Audit Event (`/var/log/homelab/appctl/audit.jsonl`)**: Authoritative append-only control-plane security and mutation log. Captures pre-execution (`DISPATCHED`), post-execution (`COMPLETED` / `FAILED`), and admission rejection (`DENIED`) records correlated by `operation_id`, including pre/post `commit_sha` on `workload.update` and `workload.deploy`.
3. **GitOps Status Snapshot (`~/.local/state/homelab/gitops/<target>.status.json`)**: Lightweight latest-run status file written by `gitops_dispatcher.py` worker upon completion for backward-compatible webhook status inspection. (No separate rollback database or `appctl history` command is introduced in P4 v1.)

### B. Field Schema (RFC-Compliant JSONL)

Every audited lifecycle event is appended as a single line to `/var/log/homelab/appctl/audit.jsonl` (mode `0600`, overridable via `APPCTL_AUDIT_LOG` in hermetic tests):

```json
{
  "audit_version": "1.0",
  "event_id": "evt_01JAX73AB190",
  "operation_id": "op_01JAX71BC98E4",
  "timestamp": "2026-10-16T12:00:03.112Z",
  "phase": "COMPLETED",
  "principal": {
    "identity": "operator",
    "role": "homelab-admin",
    "token_id": "tok_admin_01",
    "auth_method": "bearer_token"
  },
  "action": "workload.update",
  "target": "docs",
  "transport": "unix_socket",
  "origin": "peer_uid_1000",
  "client": "appctl-cli/2.0",
  "parameters": {
    "expected_branch": null,
    "build": false
  },
  "result": {
    "status": "SUCCESS",
    "exit_code": 0,
    "duration_ms": 3210,
    "commit_sha": "d4f8a1e",
    "error_code": null,
    "message": "Workload updated and reconciled successfully"
  }
}
```

### C. Event Lifecycle States & Audited Scope

| Phase | Trigger & Scope |
| :--- | :--- |
| `DISPATCHED` | Written and flushed **before** any subprocess or file mutation executes for all mutating operations (`deploy`, `stop`, `restart`, `pull`, `update`, `sync_homepage`). |
| `COMPLETED` | Written when a mutating operation or sensitive read (`workload.config`, `workload.logs`) terminates cleanly (`exit_code == 0`). |
| `FAILED` | Written when a mutating operation or sensitive read fails during pre-flight validation (`400`) or execution (`500`). |
| `DENIED` | Written when authentication (`401`), RBAC authorization (`403`), or concurrency lock acquisition (`409`) rejects a request. |

*(Note: High-frequency non-sensitive viewer reads—`list`, `get`, `resolve`, `ssl`, `health`—do not emit `COMPLETED` audit records, preventing log bloat during CLI status checks and shell tab-completion.)*

### D. Failure Handling & Explicit Audit Threat Model

1. **Fail-Closed Pre-Execution Disk Guard**: If writing or flushing the `DISPATCHED` record to `audit.jsonl` fails (`OSError`), the daemon **immediately aborts the mutation and returns `500 ERR_EXECUTION_FAILED`** with zero Docker or Git invocations.
2. **Post-Execution Persistence Failure**: If writing the `COMPLETED`/`FAILED` record fails after subprocess execution, the daemon logs a critical alert to `stderr` (`journald`) and returns `500 ERR_EXECUTION_FAILED`.
3. **Explicit Threat Model**:
   - **Protected Boundary**: Running the daemon as `homelab-appctl` with `/var/log/homelab/appctl/audit.jsonl` (`0600`) and `/run/secrets/appctl/tokens.json` (`0400`) enforces an OS user boundary against unprivileged processes and AI coding agents running inside the human operator's SSH session (`<operator>` UID). Session processes cannot read `tokens.json`, forge/truncate `audit.jsonl`, or signal the daemon without `sudo`.
   - **Out-of-Scope Boundary**: Protection against an actor who possesses host `root` / `sudo` / `docker`-group escalation privileges is out of scope for single-node P4 (as `root` can modify any local file on the host).

---

## 6. P4 Implementation Slices (Dependency-Ordered)

1. **Slice 1 — Pydantic v2 API Contracts & Error Sanitizer (`scripts/appctl_contracts.py`)**:
   - Strict request/response/error/audit models (`ConfigDict(extra="forbid", strict=True)`), operation parameter schemas, and `ValidationError` $\to$ `400 ERR_VALIDATION_FAILED` sanitizer (`include_input=False`).
   - Add `"compose_pull"` to `ALLOWED_ACTIONS` in `scripts/core_manifest.py`.
2. **Slice 2 — Append-Only Audit Logger & Per-Target Lock Manager**:
   - `AuditLogger` (`DISPATCHED`, `COMPLETED`, `FAILED`, `DENIED`, `0600` enforcement, secret redaction, fail-closed disk guard) and non-blocking `fcntl.flock` manager (`/var/lib/homelab/appctl/locks/<target>.lock` + `_homepage_sync.lock`).
3. **Slice 3 — Authentication & 3-Role RBAC Gate**:
   - SOPS `tokens.json` SHA-256 authenticator (`hmac.compare_digest`), UDS `SO_PEERCRED` extractor (`unix_peercred` viewer fallback for non-sensitive reads), and role-scoped authorization policy (`homelab-admin` vs `homelab-operator` `deployments.yaml` restriction vs `homelab-viewer`).
4. **Slice 4 — Operational Router & Unified Execution Engine**:
   - Pure-Python `.env` parser (no shell `source`), mandatory `parse_manifest()` + `validate_compose_security()` pre-flight, revision-consistent `workload.update` (`expected_branch`, dynamic action adoption, `build`), bounded `workload.logs` (`tail`), and atomic Homepage sync.
5. **Slice 5 — Dual-Transport HTTP/1.1 Daemon (`scripts/appctl_daemon.py`) & GitOps Cutover**:
   - HTTP/1.1 server over UDS (`/run/homelab/appctl.sock`) and TCP (`127.0.0.1:9001`), graceful `SIGTERM`/`SIGINT` cleanup, and rewiring `scripts/gitops_dispatcher.py` to parse manifests via `core_manifest.parse_manifest()` and execute worker deployments through the daemon.
6. **Slice 6 — Thin-Client CLI (`scripts/appctl`), NixOS Service & Platform Genericism Cleanup**:
   - Re-implement `scripts/appctl` as a thin UDS HTTP client, declare `homelab-appctl.service` (`homelab-appctl:homelab-operators` + POSIX ACLs) in `nixos/modules/homeserver.nix`, scrub remaining hardcoded domain/username literals across `nixos/`, `config/deployments.yaml`, and `docker-compose.yml`, and add `pyright` to `flake.nix` checks and `.githooks/pre-commit`.

---

## 7. P4 Definition of Done & Acceptance Test Matrix

Before marking Milestone P4 complete, the following automated regression suite must pass:

| Test Suite / Area | Assertion / Invariant Verified | Failure Action |
| :--- | :--- | :--- |
| **Transport Parity** | `test_transport_dual_equivalence`: Compare responses of `POST /v1/operations` (`workload.list`, `workload.get`, errors) over Unix socket vs HTTP `127.0.0.1`. Assert 100% schema and status parity. | Reject PR |
| **Unauthenticated Rejection** | `test_auth_missing_token_fails_closed`: Mutating operations and `workload.config`/`workload.logs` without `Authorization: Bearer` return `401 ERR_AUTH_REQUIRED` on both UDS and HTTP and execute zero subprocesses. | Block Release |
| **UDS Peercred Viewer Scope** | `test_uds_peercred_viewer_boundary`: Tokenless UDS call from operator UID succeeds for `workload.list` (`auth_method: "unix_peercred"`), but fails with `401`/`403` on any mutation or `workload.config`. | Block Release |
| **RBAC & Role-Scoped Resolution**| `test_rbac_role_matrix`: `homelab-viewer` denied (`403`) on all mutations; `homelab-operator` allowed on `deployments.yaml` workloads but denied (`403`) on `core`, `core_service`, and unregistered `~/Sites` dirs; `homelab-admin` allowed on all valid targets. | Block Release |
| **Pre-Flight & `.env` Safety** | `test_daemon_preflight_and_dotenv_safety`: Workload with malicious `.env` (`$(touch ...)`) or prohibited Compose directive (`privileged: true`, `/var/run/docker.sock`) is blocked / parsed inertly with zero shell execution. | Block Release |
| **Concurrency Lock (`409`)** | `test_concurrent_mutation_conflict`: Second concurrent mutation on the same target immediately returns `409 ERR_CONCURRENT_MUTATION` and logs `DENIED`. | Block Release |
| **Truthful Error Reporting** | `test_execution_failure_audit`: Failing or timing-out Docker/Git subprocess returns `500 ERR_EXECUTION_FAILED` and records `phase: FAILED` in `audit.jsonl`. | Reject PR |
| **Audit Persistence Gate** | `test_audit_failure_blocks_mutation`: Unwritable `audit.jsonl` aborts mutating command with `500 ERR_EXECUTION_FAILED` and zero Docker/Git invocations. | Block Release |
| **Daemon Crash Recovery** | `test_daemon_restart_preserves_state`: Clean stop and restart of daemon unlinks/rebinds socket cleanly and leaves running workloads and Git state undisturbed. | Reject PR |
| **Existing Invariant Regression** | Run `./scripts/test`: All 124 existing unit, security, structural, Ruff, Pyright, and Nix flake sandbox checks pass with 0 regressions. | Block Merge |
