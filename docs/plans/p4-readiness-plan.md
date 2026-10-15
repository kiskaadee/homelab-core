# 📋 Control Plane Milestone P4: Readiness Plan & Boundary Specifications

**Status**: ✅ **Approved & Locked (Pre-P4 Security Gate Complete; Ready for Daemon Implementation)**  
**Target Milestone**: [Milestone P4: Appctl Control Plane Daemon & API](../../ROADMAP.md#milestone-p4-appctl-control-plane-daemon--command-oriented-api)  
**Governing ADR**: [`homelab-appctl-control-plane-architecture.md`](../decisions/README.md)  

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
| [`scripts/core_manifest.py`](../../scripts/core_manifest.py) | Python 3 | **Schema Library** | Strongly-typed Manifest dataclasses; transport validation (`https://`, `ssh://`, `git@`); ref semantics classification (`branch`, `tag`, `commit`). |

| [`scripts/provenance.py`](../../scripts/provenance.py) | Python 3 | **Provenance Library** | Two-axis status engine; local checkout inspection (`git status`, commit SHA); bounded upstream query (`git ls-remote` with 10s timeout). |
| [`scripts/gitops_dispatcher.py`](../../scripts/gitops_dispatcher.py) | Python 3 | **Independent Service** | Webhook listener (port 9000); HMAC validation; flock serialization; independent AST validation; executes its own `docker compose` and `git pull` subprocesses. |

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
| `logs` | `appctl logs <service\|core> [-f]` | `scripts/appctl` (Bash) | Read-Only (Streaming) | Dispatches `docker compose logs -f`. |
| `config` | `appctl config <service\|core>` | `scripts/appctl` (Bash) | Read-Only | Injects environment $\to$ executes `docker compose config` to view fully evaluated compose AST. |

### C. Active Call Sites Across the Repository

1. **System Path Integration**:
   - [`nixos/configuration.nix`](../../nixos/configuration.nix#L119-L122): Installs host wrapper `/run/current-system/sw/bin/appctl` calling `${CORE_DIR:-$HOME/Core}/scripts/appctl "$@"`.
2. **Internal Script Invocation**:
   - `scripts/appctl` calls `python3 "$ENGINE" resolve "$query"` to set up the Compose environment.
   - `scripts/appctl` calls `python3 "$ENGINE" sync` after mutating actions (`up`, `down`, `restart`, `update`).
3. **Automated Test Coverage**:
   - [`tests/unit/test_appctl_engine.py`](../../tests/unit/test_appctl_engine.py): 12 unit tests exercising `resolve_app`, `get_docker_status`, `check_ssl_cert`, and `cmd_sync_homepage`.
   - [`tests/unit/test_core_manifest.py`](../../tests/unit/test_core_manifest.py): 19 unit tests covering schema validation and ref classification.
   - [`tests/unit/test_provenance.py`](../../tests/unit/test_provenance.py): 10 unit tests covering two-axis status inspection and timeout handling.
   - [`tests/structural/test_repository_structure.py`](../../tests/structural/test_repository_structure.py): Asserts executability and paths of `scripts/appctl` and `scripts/appctl_engine.py`.

### D. Architectural Findings & Migration Deductions

1. **V1 Engine is Fully Superseded & V2 Shim Retired**: `scripts/appctl_engine.py` is the single authoritative metadata engine. The `scripts/appctl_engine_v2.py` compatibility shim has been retired.

2. **Duplicated Operational Logic in GitOps**: `scripts/gitops_dispatcher.py` and `scripts/appctl` maintain **separate, duplicate implementations** of `git pull` and `docker compose up`. This violates Invariant #1 (*Single Operational Truth*). In P4, GitOps webhook handlers must delegate execution to the control-plane daemon via its internal client.
3. **Environment Injection Vulnerability in Bash**: The current Bash CLI constructs shell exports dynamically using `eval "$(python3 ...)"`. The daemon replaces this subprocess string interpolation with in-process, typed dictionary manipulation.

---

## 2. Command and Error Contract (P4 Prerequisite)

### A. Canonical Domain Operations

Every client (local CLI, remote CLI, GitOps dispatcher, and future Operations UI) invokes one of these domain operations across either transport:

```
                            ┌─────────────────────────┐
                            │    Client Request       │
                            └────────────┬────────────┘
                                         ▼
                   ┌───────────────────────────────────────────┐
                   │        Operation Dispatch Router          │
                   └─────────────────────┬─────────────────────┘
                                         │
         ┌───────────────────────────────┴───────────────────────────────┐
         ▼                                                               ▼
   [ Read-Only Operations ]                                    [ Mutating Operations ]
   • workload.list                                             • workload.deploy
   • workload.get                                              • workload.stop
   • workload.resolve                                          • workload.restart
   • workload.config                                           • workload.update
   • workload.logs                                             • workload.pull
   • platform.ssl                                              • platform.sync_homepage
   • platform.health
```

### B. Typed Request and Response Models (Target Architecture: Pydantic v2 Pipeline)

All control plane API requests, responses, error payloads, and workload manifest schemas will be defined and validated through **Pydantic v2** (`pydantic.BaseModel`, `pydantic.Field`, `ConfigDict(extra="forbid", strict=True)`). While the dependency has been integrated into runtime environments, the schema migration and model definitions are the first implementation deliverables of Milestone P4:

- **Runtime Validation Boundary**: Incoming payloads over both Unix Domain Socket and HTTP endpoints are validated at the transport boundary before reaching the operational router.
- **Fail-Closed Extra Fields (`extra="forbid"`)**: Rejects any undeclared or unknown fields immediately.
- **Strict Typing vs. Coercion Rules**:
  - *Strict Validation*: Operations (`operation`), target identifiers (`target`), boolean flags (`recreate`, `remove_orphans`, `visible`, `auth`), and authentication tokens require strict types (`strict=True` or `StrictStr`, `StrictBool`). Coercion (e.g. converting `"true"` or `1` to `True`, or floats to ints) is strictly forbidden for control plane commands to prevent ambiguous execution.
  - *Permitted Coercion / Sanitization*: Only explicitly defined string stripping/normalization (e.g. whitespace trimming) is permitted via field validators.
- **Structured Error Transformation**: Transport handlers intercept `pydantic.ValidationError` and map it to canonical `ERR_VALIDATION_FAILED` (`400 Bad Request`) responses.
- **Information Disclosure Prevention**: Error details must include field names and validation issue descriptions, but must strictly redact and omit raw input values to prevent leaking secrets, credentials, or host filesystem paths.
- **Contract Enforcement**: Manifest definitions in `core_manifest.py` migrate to Pydantic v2 models, guaranteeing uniform schema enforcement across the CLI, daemon, and GitOps engines.

All requests and responses use strict JSON payloads with explicit schema versioning (`version: "1.0"`):

```json
// Example: POST /operations/deploy
// Request:
{
  "version": "1.0",
  "operation": "workload.deploy",
  "target": "jellyfin",
  "parameters": {
    "recreate": true,
    "remove_orphans": true
  }
}

// Response (Success):
{
  "version": "1.0",
  "status": "success",
  "operation_id": "op_01JAX71BC98E4",
  "target": "jellyfin",
  "timestamp": "2026-10-13T18:30:00Z",
  "duration_ms": 1420,
  "result": {
    "action": "recreated",
    "containers": ["jellyfin"],
    "health": "running"
  }
}
```

### C. Standardized Error Taxonomy

Failures are strongly typed and distinguished at three distinct architectural levels:

| Error Classification | HTTP Code | Error Code | Description / Fail-Closed Action |
| :--- | :--- | :--- | :--- |
| **Validation Error** | `400 Bad Request` | `ERR_VALIDATION_FAILED` | Request schema violation, invalid target name, malformed `app.yaml`, or Compose AST security violation. |
| **Authentication Error** | `401 Unauthorized` | `ERR_AUTH_REQUIRED` | Missing, expired, or malformed API token / HMAC signature. |
| **Authorization Error** | `403 Forbidden` | `ERR_PERMISSION_DENIED` | Authenticated principal lacks necessary role (`homelab-admin` vs. `homelab-viewer`) for the target operation. |
| **Target Not Found** | `404 Not Found` | `ERR_TARGET_NOT_FOUND` | Workload target does not exist under `~/Sites` or Core services registry. |
| **State Conflict** | `409 Conflict` | `ERR_CONCURRENT_MUTATION` | Another mutating operation on the target workload is currently holding the operation lock. |
| **Execution Error** | `500 Internal Error`| `ERR_EXECUTION_FAILED` | Docker daemon error, subprocess non-zero exit, or persistent volume failure. |

---

## 3. Authorization Design (Security Gate)

### A. The Core Principle: Host Access Does Not Confer Authority

Entering the host via SSH establishes Unix access, but **zero operational control plane privileges**. Every mutating request to `homelab-appctl.service` must present authenticated credentials.

### B. Principal Identity & Role Mapping

The platform defines three hierarchical authorization tiers:

| Role | Permitted Operations | Typical Principals |
| :--- | :--- | :--- |
| `homelab-admin` | All operations (including Core platform mutation, daemon reload, secret regeneration). | Break-glass admin token, human interactive operator (`kiskaadee`). |
| `homelab-operator` | Workload mutations (`deploy`, `stop`, `restart`, `update`, `pull`, `sync_homepage`). Cannot mutate Core platform infrastructure. | GitOps webhook runner token, automation scripts. |
| `homelab-viewer` | Read-only operations (`list`, `get`, `resolve`, `config`, `logs`, `ssl`, `health`). Zero mutations permitted. | Homepage dashboard status monitor, read-only diagnostic CLI. |

### C. Transport Authentication Rules

1. **Unix Domain Socket (`/run/homelab/appctl.sock`)**:
   - Socket ownership: `root:homelab-operators`, file mode `0660`.
   - Read-only operations allowed without token *if* the client UID belongs to `homelab-operators`.
   - **All mutating operations strictly require a valid user-scoped Bearer token** passed in the request header (`Authorization: Bearer <token>`).
2. **HTTP Network Listener (Local LAN / Traefik)**:
   - External Web: Traefik verifies Authelia session and forwards trusted identity headers (`Remote-User`, `Remote-Groups`). Headers are **strictly rejected** unless incoming connection originates from the trusted Docker `proxy-net` gateway.
   - Remote CLI / API: Requires user-scoped Bearer token.
3. **GitOps Ingress**:
   - Authenticated via HMAC-SHA256 signature (`X-Gitea-Signature` / `X-Hub-Signature-256`) against `/run/secrets/gitops/webhook_secret`.
   - Evaluated as an internal caller with `homelab-operator` role, constrained strictly to the target repository declared in [`config/deployments.yaml`](../../config/deployments.yaml).

---

## 4. Execution & Privilege Boundaries (P4 Prerequisite)

### A. Operational Capability Matrix

Each canonical operation is strictly bound to minimal execution permissions, allowable subprocesses, and filesystem boundaries:

| Domain Operation | Subprocess / Tool | Allowed Arguments / API | Required Filesystem Roots | Authority / Role Required |
| :--- | :--- | :--- | :--- | :--- |
| `workload.list` | In-process Python (`provenance.py`) | Read directory metadata; optional `git ls-remote` (10s timeout) | Read: `~/Sites/`, `Core/` | `homelab-viewer` |
| `workload.get` | In-process Python (`provenance.py`, `core_manifest.py`) | Parse `app.yaml`, inspect git status | Read: `~/Sites/<app>/` | `homelab-viewer` |
| `workload.resolve` | In-process Python (`appctl_engine.py`) | Name resolution & JSON export | Read: `~/Sites/`, `Core/` | `homelab-viewer` |
| `workload.config` | `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml config` | Read: `~/Sites/<app>/` | `homelab-viewer` |
| `workload.logs` | `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml logs [--tail=N] [--follow]` | Read: `~/Sites/<app>/` | `homelab-viewer` |
| `workload.deploy` | `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml up -d --remove-orphans` | Read/Write: `~/Sites/<app>/`, `~/var/lib/homelab/<app>/` | `homelab-operator` |
| `workload.stop` | `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml down` | Read/Write: `~/Sites/<app>/` | `homelab-operator` |
| `workload.restart`| `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml restart` | Read/Write: `~/Sites/<app>/` | `homelab-operator` |
| `workload.pull` | `/run/current-system/sw/bin/docker` | `compose -f <workload>/docker-compose.yml pull` | Read: `~/Sites/<app>/` | `homelab-operator` |
| `workload.update` | `/run/current-system/sw/bin/git`<br>`/run/current-system/sw/bin/docker` | 1. `-C <workload> pull --ff-only`<br>2. `compose pull`<br>3. `compose up -d --remove-orphans` | Read/Write: `~/Sites/<app>/`, `~/var/lib/homelab/<app>/` | `homelab-operator` |
| `platform.sync_homepage` | In-process Python (`appctl_engine.py`) | Internal YAML AST generator | Read: `~/Sites/`<br>Write: `Core/config/homepage/services.yaml` | `homelab-operator` |
| `platform.core_restart` | `/run/current-system/sw/bin/docker` | `compose -f Core/docker-compose.yml restart [service]` | Read: `Core/` | `homelab-admin` |

### B. Secret Reference Boundary

1. **Daemon Isolation**: The control plane daemon does **not** ingest master decryption keys (`key.txt`) or decrypt raw SOPS archives.
2. **Ephemeral Secret Projection**: The daemon only accesses runtime projections mounted by NixOS/sops-nix under `/run/secrets/`:
   - `/run/secrets/appctl/tokens.json`: Hashed token store for API authentication (mode `0400`, owned by daemon user).
   - `/run/secrets/gitops/webhook_secret`: Shared HMAC secret for webhook verification (mode `0400`).
   - `/run/secrets/rendered/traefik-deployments.env`: Projected workload secrets referenced during container startup.
3. **No Secret Material in Logs or Audit**: All environment injection is filtered: any environment variable or argument matching `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_TOKEN`, or `*_HASH` is redacted before logging.

### C. Systemd Unit Sandboxing (`homelab-appctl.service`)

Following the pattern established in Milestone P2 for `homelab-gitops.service`, the daemon runs with systemd sandboxing:
- `ProtectSystem = "strict"`: Host filesystem is read-only.
- `ProtectHome = "read-only"`: User home is read-only by default.
- `ReadWritePaths`: Explicitly allowlisted writable roots:
  - `/home/kiskaadee/Sites` (workload definitions and Git checkouts)
  - `/home/kiskaadee/var/lib/homelab` (workload persistent state roots)
  - `/home/kiskaadee/Core/config/homepage` (Homepage services.yaml destination)
  - `/var/log/homelab/appctl` or `/home/kiskaadee/.local/state/homelab/appctl` (audit log directory)
  - `/run/homelab` (socket runtime directory)
- `PrivateTmp = true`: Isolated temporary directories.
- `NoNewPrivileges = true`: Disallows privilege escalation.

### C. Platform Genericism & Domain Decoupling
Per Invariant #8 in `AGENTS.md`, the control plane and all platform components must remain strictly instance-agnostic:
- **No Hardcoded Domains**: Personal domain names (e.g. `roadtotech.me`) must never appear hardcoded in Python modules, NixOS definitions, or default configurations.
- **Canonical Precedence & Dynamic Resolution**: All domain resolutions follow strict, fail-closed precedence:
  1. `HOMELAB_DOMAIN` (Primary platform apex domain)
  2. `DOMAIN_SUFFIX` (Secondary routing suffix fallback)
  3. `DOMAIN` (Legacy environment fallback)
  If none are set, the platform fails closed rather than defaulting to hardcoded values.
- **Generic Placeholders & Test Fixtures**: Test fixtures, documentation examples, and schema defaults must use RFC 2606 reserved domains (`example.com`, `example.org`, `example.test`).


---

## 5. Audit Telemetry Semantics (P4 Prerequisite)

### A. Field Schema (RFC-Compliant JSONL)

Every operation lifecycle event is emitted as a structured JSON object to `audit.jsonl` (mode `0600`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "audit_version": "1.0",
  "event_id": "evt_01JAX73AB190",
  "timestamp": "2026-10-13T18:32:05.112Z",
  "phase": "COMPLETED",
  "principal": {
    "identity": "kiskaadee",
    "role": "homelab-admin",
    "token_id": "tok_99fa1b",
    "auth_method": "bearer_token"
  },
  "action": "workload.update",
  "target": "docs",
  "transport": "unix_socket",
  "origin": "peer_uid_1000",
  "client": "appctl-cli/2.0",
  "parameters": {
    "recreate": true,
    "remove_orphans": true
  },
  "result": {
    "status": "SUCCESS",
    "exit_code": 0,
    "duration_ms": 3210,
    "error_code": null,
    "message": "Workload updated and reconciled successfully"
  }
}
```

### B. Event Lifecycle States

| Phase | Description |
| :--- | :--- |
| `DISPATCHED` | Emitted immediately after authentication and validation pass, before invoking Docker/Git. |
| `COMPLETED` | Emitted when operation terminates cleanly with exit code 0. |
| `FAILED` | Emitted when validation, Docker, or Git execution raises a non-zero exit or exception. |
| `DENIED` | Emitted when authentication fails (`401`) or authorization check fails (`403`). |

### C. Failure Handling & Audit Guarantees

1. **Pre-Execution Write**: Mutating actions log a `DISPATCHED` record before calling external binaries. If the daemon crashes or the host loses power mid-operation, the pending operation is permanently preserved in the audit log.
2. **Fail-Closed Disk Guard**: If writing to `audit.jsonl` fails (e.g. out of disk space, permission error), the daemon **immediately aborts the requested mutation and returns `ERR_EXECUTION_FAILED`**. Unaudited mutations are strictly prohibited.
3. **Audit Log Location & Service Ownership**:
   - Primary: `/var/log/homelab/appctl/audit.jsonl` (systemd service root) or `~/.local/state/homelab/appctl/audit.jsonl`.
   - Permissions: Mode `0600` owned by `homelab-appctl` service user.

---

## 6. P4 Definition of Done & Acceptance Test Matrix

Before marking Milestone P4 complete, the following automated regression suite must pass:

### Test Suite Matrix

| Test Suite / Area | Assertion / Invariant Verified | Failure Action |
| :--- | :--- | :--- |
| **Transport Parity** | `test_transport_dual_equivalence`: Compare outputs of `appctl list` via Unix socket vs HTTP `GET /workloads`. Assert 100% semantic and data parity. | Reject PR |
| **Unauthenticated Rejection** | `test_auth_missing_token_fails_closed`: `POST /operations/deploy` without `Authorization` header returns `401 Unauthorized` and executes zero subprocesses. | Block Release |
| **RBAC Authorization Enforcement**| `test_rbac_viewer_mutation_denied`: Present valid token with `homelab-viewer` role against `POST /operations/restart`. Assert `403 Forbidden` and zero container restarts. | Block Release |
| **Path Traversal Containment** | `test_path_traversal_rejection`: Submit target `../../etc` or `/var/run`. Assert `ERR_VALIDATION_FAILED` (400) and zero filesystem escape. | Block Release |
| **Truthful Error Reporting** | `test_execution_failure_audit`: Mock failing Docker compose run (exit 1). Assert daemon returns `ERR_EXECUTION_FAILED` (500) and records `phase: FAILED` in audit log. | Reject PR |
| **Audit Persistence Gate** | `test_audit_failure_blocks_mutation`: Mock read-only audit log. Assert mutating command fails closed with zero Docker invocations. | Block Release |
| **Daemon Crash Recovery** | `test_daemon_restart_preserves_state`: Kill and restart `homelab-appctl.service`. Assert running container stacks are undisturbed and desired Git state remains intact. | Reject PR |
| **Existing Invariant Regression** | Run `./scripts/test`: All 121 existing unit, security, structural, and Nix flake sandbox tests pass with 0 regressions. | Block Merge |

