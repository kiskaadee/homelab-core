---
type: decision
status: accepted
project: homelab-core
date: 2026-10-07
tags: [architecture, homelab, appctl, control-plane, security, rbac, audit, gitops]
---

# ADR 0002: Homelab Operational Control Plane & Deployment Model Architecture

## Context and Problem Statement

Homelab Core historically relied on `appctl` as a procedural Bash CLI wrapper (`scripts/appctl`) coupled with a Python metadata helper (`scripts/appctl_engine.py`). Operational commands (`up`, `down`, `restart`, `update`, `logs`, `config`, `sync`) executed as local subprocesses in the operator's shell session, sourcing environment files directly from `/run/secrets/` and `$APP_DIR/.env`.

As the platform matured and AI coding agents began operating inside developer SSH sessions, this script-based model exposed five architectural and security liabilities:

1. **Conflated Host & Control Plane Authority**: Entering the server via SSH granted full operational mutation authority. Any unprivileged process or AI coding agent running under the operator's UID could invoke `appctl` to mutate containers, inspect secrets, or alter routing with zero authentication.
2. **Duplicated Execution & Pre-Flight Drift**: The local CLI (`scripts/appctl`) and the GitOps webhook worker (`scripts/gitops_dispatcher.py`) maintained separate implementations of `git pull` and `docker compose`, with the CLI path bypassing Compose AST security validation and sourcing `.env` files via shell `source`.
3. **Absence of Tamper-Evident Auditability**: Operational actions produced only ephemeral terminal output with no centralized, append-only audit log recording principal identity, transport, origin, target revision, and outcome.
4. **Secret Lifecycle Coupling**: Workload secrets were broadly sourced into parent shell environments or coupled to static SOPS rebuilds rather than resolved via scoped references.
5. **Manual Workload Authoring Friction**: Onboarding workloads required handwriting boilerplate descriptors without a structured boundary separating AI-generated proposals (`DeploymentProposal`) from human-approved declarative intent (`DeploymentModel`).

The central architectural question is: **How should Homelab Core transition `appctl` into an authoritative, fail-closed operational control plane with strict privilege isolation, unified execution, mandatory token authorization, and safe deployment authoring without introducing distributed cloud complexity?**

## Decision Drivers

* **Control Plane Centralization**: Exactly one authoritative execution pipeline for all workload and platform operations across CLI, GitOps, and future web clients.
* **Host Access Does Not Confer Mutation Authority**: Entering the host via SSH must not bypass control-plane authentication. Every mutating action and sensitive read requires cryptographic token verification.
* **OS Privilege Isolation**: The control plane daemon, token store, and audit log must be isolated under a dedicated system identity (`homelab-appctl`) inaccessible to unprivileged operator shell processes and AI sessions.
* **Unified Pre-Flight & Inert Environment Assembly**: Eliminate shell `eval` and `source`; enforce Pydantic v2 schema validation ([ADR-0003](0003-pydantic-v2-runtime-validation.md)) and Compose AST security checks before every Compose invocation.
* **Declarative Git Ground Truth & AI Boundary**: Git remains the sole authoritative source of deployment state; AI agents act strictly as upstream proposal generators (`DeploymentProposal`) with zero execution authority.
* **KISS Primitives**: Preserve synchronous, linear execution using local `flock` serialization and HTTP/1.1 without external message brokers or databases.

## Considered Options

* **Option 1: Status Quo with Modernized Local CLI**: Modernize `appctl_engine.py` into a type-safe Python CLI while keeping execution in-process inside the caller's SSH session.
* **Option 2: Cloud-Native Micro-Platform (Kubernetes / Nomad / ArgoCD)**: Replace `appctl` and Docker Compose with enterprise cluster orchestration and distributed control loops.
* **Option 3: Authoritative `homelab-appctl.service` Daemon with Single Command Router & Stable Deployment Model (Adopted)**: Evolve `appctl` into an isolated systemd daemon exposing one logical HTTP/1.1 command API (`POST /v1/operations`) across Unix domain socket and TCP, backed by mandatory Bearer token auth, 3-role RBAC, fail-closed audit logging, and a 5-stage `DeploymentModel` lifecycle.

## Decision Outcome

Chosen option: **Option 3: Authoritative `homelab-appctl.service` Daemon with Single Command Router & Stable Deployment Model**, because it enforces an OS-level security boundary between operator sessions and platform execution, unifies CLI and GitOps codepaths behind a single fail-closed pipeline, and establishes a clean roadmap for directory-backed identity (P5) and human-gated AI deployment authoring (P6).

### Summary of Architectural Commitments (Reconciled with Pre-P4 Locked Decisions `DEC-01`–`DEC-05`)

```text
                       Clients (CLI / GitOps Worker / Future Web UI)
                                             │
                            POST /v1/operations + GET /v1/health
                           (One Logical HTTP/1.1 Command Contract)
                                             │
                       ┌─────────────────────┴─────────────────────┐
                       ▼                                           ▼
        Unix Domain Socket (/run/homelab/appctl.sock)   HTTP Listener (127.0.0.1:9001)
          mode 0660 (homelab-appctl:homelab-operators)        (Loopback in P4)
                       │                                           │
                       └─────────────────────┬─────────────────────┘
                                             ▼
                     ┌───────────────────────────────────────────────┐
                     │   Unified Fail-Closed Pipeline (homelab-appctl)│
                     │ 1. Pydantic v2 Validator (extra="forbid", 64K)│
                     │ 2. Auth & 3-Role RBAC Gate (Bearer / PeerCred)│
                     │ 3. Per-Target flock (/var/lib/homelab/appctl) │
                     │ 4. Pre-Exec Audit Disk Guard (DISPATCHED)     │
                     │ 5. Pre-Flight (Manifest + Compose AST + .env) │
                     │ 6. Subprocess Execution (Fixed argv, 256K cap)│
                     │ 7. Post-Exec Audit Record (COMPLETED / FAILED)│
                     └───────────────────────────────────────────────┘
```

#### 1. One Logical Command API Across Dual Transports (`DEC-01`)
* `appctl` runs as a dedicated systemd daemon (`homelab-appctl.service`) speaking standard **HTTP/1.1** over both a local Unix Domain Socket (`/run/homelab/appctl.sock`) and a TCP listener (`127.0.0.1:9001` in P4).
* All 13 canonical domain operations (`workload.list`, `workload.get`, `workload.resolve`, `platform.ssl`, `platform.health`, `workload.config`, `workload.logs`, `workload.deploy`, `workload.stop`, `workload.restart`, `workload.pull`, `workload.update`, `platform.sync_homepage`) dispatch through a **single canonical endpoint**: `POST /v1/operations` (plus `GET /v1/health` for liveness). Multi-route REST aliases are rejected.
* `scripts/appctl` acts as a thin HTTP/1.1 UDS client with zero local Docker or Git execution logic.

#### 2. Host Access Does Not Confer Mutation Authority & 3-Role RBAC (`DEC-02`)
* Entering via SSH confers zero control-plane mutation authority. Every mutating operation, every sensitive read (`workload.config`, `workload.logs`), and every HTTP request requires an explicit Bearer token validated via constant-time SHA-256 comparison (`hmac.compare_digest`) against `/run/secrets/appctl/tokens.json` (`0400`).
* **UDS `SO_PEERCRED` Viewer Fallback**: Unauthenticated local Unix socket calls extract kernel peer credentials (`SO_PEERCRED`) and fall back strictly to `homelab-viewer` privileges for non-sensitive reads (`list`, `get`, `resolve`, `ssl`, `health`), preserving zero-friction CLI inspection and shell tab-completion while blocking all mutations and secret dumps (`401`/`403`).
* **3-Role Authorization Hierarchy**:
  * `homelab-admin`: Full access across `core`, `core_service`, and all valid `~/Sites/<app>` workloads.
  * `homelab-operator`: Mutations and sensitive reads strictly restricted to workloads admitted in `Core/config/deployments.yaml` (used by `gitops-dispatcher`). Denied (`403`) on `core`, `core_service`, and unregistered Sites directories.
  * `homelab-viewer`: Non-sensitive read-only operations only.
* *(Milestone P5 expands this gate with LLDAP directory group mapping and authenticated Traefik/Authelia ForwardAuth proxy headers).*

#### 3. Dedicated Service Identity, OS Isolation & Fail-Closed Audit Guard (`DEC-03`)
* The daemon executes as system user `homelab-appctl` (`Group = "homelab-operators"`, `SupplementaryGroups = [ "docker" ]`), with declarative recursive POSIX ACLs (`systemd.tmpfiles.rules`) granting shared read/write access on `~/Sites`, `~/var/lib/homelab`, and `~/Core/config/homepage`.
* **Append-Only Audit Telemetry (`/var/log/homelab/appctl/audit.jsonl`, mode `0600`)**: Records `DISPATCHED`, `COMPLETED`, `FAILED`, and `DENIED` lifecycle events with `principal`, `action`, `target`, `transport`, `origin`, `client`, `timestamp`, `commit_sha`, and secret-redacted `parameters`/`result`.
* **Fail-Closed Disk Guard**: Mutating operations must write and flush a `DISPATCHED` audit record **before** invoking any Docker or Git subprocess. If the audit write fails (`OSError`), the mutation aborts immediately with `500 ERR_EXECUTION_FAILED`.

#### 4. GitOps Execution Unification (`DEC-04`)
* `scripts/gitops_dispatcher.py` retains webhook HTTP ingress (port 9000), HMAC-SHA256 verification, `config/deployments.yaml` admission, and the commit-superseding `.pending.json` queue.
* The background worker delegates deployment execution to the daemon's `workload.update` operation over `/run/homelab/appctl.sock` using its `homelab-operator` Bearer token, eliminating duplicated `git pull` and `docker compose` execution codepaths.

#### 5. Unified Pre-Flight & Pure-Python Environment Assembly (`DEC-05`)
* Shell `eval` and `source` are completely eliminated. Subprocess environments are assembled in memory via an inert pure-Python `KEY=VALUE` reader enforcing `ENV_KEY_RE`, `RESERVED_ENV_KEYS`, and canonical domain precedence (`HOMELAB_DOMAIN` $\to$ `DOMAIN_SUFFIX` $\to$ `DOMAIN`).
* Every workload mutation and `workload.config` call enforces path containment, `core_manifest.parse_manifest()`, and `validate_compose_security()` pre-flight inspection before invoking Docker Compose.

#### 6. Declarative Git Truth, Model Context Protocol (MCP) Gateway & AI Boundary (P5–P7 Trajectory)
* **Git is Authoritative**: Approved deployment definitions are versioned in Git; `appctl` reconciles desired Git state into running containers.
* **Architecture Amendment (2026-10-23 — MCP Tool Gateway Supersedes On-Server Authoring)**:
  The 5-stage on-server AI deployment authoring compiler (`ApplicationProfile` $\to$ `DeploymentProposal` $\to$ `DeploymentModel`) originally planned for Milestone P6 is **deprecated and superseded by a decoupled Model Context Protocol (MCP) Tool Gateway**. Rather than building bespoke AI authoring engines or autonomous agents into the appliance host, platform management operations (`appctl_status`, `appctl_deploy`, `appctl_logs`, `appctl_audit`) will be exposed via standardized JSON-RPC MCP tools consumed by external developer clients (Antigravity, Claude Code, Cursor) with native human-in-the-loop confirmation. The deterministic boundary of `appctl` is preserved, with zero autonomous AI agent code executing inside the appliance host.
* **Secret Reference Invariant (P5)**: Manifests reference secrets by key (`secretKeyRef`) resolved at runtime via `SecretProvider`, never storing plaintext credentials in Git.
* **Client Decoupling (P7)**: Homepage remains a read-only status portal; interactive web management belongs to a dedicated Operations UI client consuming `POST /v1/operations`.

## Pros and Cons of the Options

### Option 1: Status Quo with Local CLI Modernization
* Good, because it requires no background daemon or socket listener.
* Bad, because every process running under the operator's SSH UID retains full unauthenticated mutation authority over containers and secrets.
* Bad, because CLI and GitOps execution remain split across separate processes without unified lock management or tamper-evident auditing.

### Option 2: Cloud-Native Micro-Platform
* Good, because Kubernetes/ArgoCD provide built-in API servers and RBAC.
* Bad, because cluster control planes impose heavy resource and operational complexity disproportionate to a single-node NixOS appliance.

### Option 3: Authoritative `homelab-appctl.service` Daemon (Adopted)
* Good, because it isolates operational authority, secrets (`/run/secrets/appctl/tokens.json`), and audit logs (`/var/log/homelab/appctl/audit.jsonl`) under `homelab-appctl`.
* Good, because `POST /v1/operations` guarantees 100% contract parity across UDS and HTTP transports.
* Good, because `SO_PEERCRED` viewer fallback preserves ergonomic local read commands (`appctl list`, `appctl info`) while requiring tokens for mutations and secret reads.
* Bad, because it introduces a core systemd daemon and requires provisioning SOPS-managed API tokens for mutating CLI and GitOps operations.

## Consequences

### Positive Consequences
* **Defense-in-Depth Against Session Compromise**: Unprivileged scripts or AI coding agents in the operator's shell cannot mutate infrastructure, read `tokens.json`, or alter `audit.jsonl`.
* **Single Execution Truth**: Interactive CLI commands and automated GitOps webhooks execute through the exact same pre-flight validator, lock manager, and environment builder.
* **Deterministic Concurrency**: Per-target non-blocking `flock` locks (`/var/lib/homelab/appctl/locks/<target>.lock`) prevent overlapping mutations (`409 ERR_CONCURRENT_MUTATION`).

### Negative Consequences & Accepted Trade-offs
* **Daemon Dependency**: Normal operational commands depend on `homelab-appctl.service` availability (emergency host recovery remains possible for human administrators via `sudo`).
* **Token Provisioning**: Mutating CLI invocations require `APPCTL_TOKEN` / token configuration rather than relying on ambient SSH user identity.

## More Information

* **P4 Readiness & Boundary Specification**: [`docs/plans/p4-readiness-plan.md`](../plans/p4-readiness-plan.md)
* **Companion ADRs**:
  * [ADR-0001: Homelab 3-Tier Architecture & Deployment Provenance Model](0001-3tier-architecture-and-provenance.md)
  * [ADR-0003: Centralized Runtime Validation & Serialization via Pydantic v2](0003-pydantic-v2-runtime-validation.md)
* **Governing Roadmap**: [`ROADMAP.md`](../../ROADMAP.md) (Milestones P4–P7)
