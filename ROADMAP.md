# 🗺️ Homelab Core & Appctl Engineering Roadmap

This document serves as the repository-facing projection of the authoritative **Homelab Architecture Consolidation & Control Plane Roadmap (v6)** and the locked Milestone P4 boundary specification ([`docs/plans/p4-readiness-plan.md`](docs/plans/p4-readiness-plan.md)).

It describes the implementation trajectory and phased engineering milestones for transitioning Homelab Core into a unified platform driven by the `appctl` Operational Control Plane.

> **Governing Architectural Decisions**: See [`docs/decisions/README.md`](docs/decisions/README.md) for the authoritative ADR index ([ADR-0001: 3-Tier Provenance Model](docs/decisions/0001-3tier-architecture-and-provenance.md), [ADR-0002: Operational Control Plane Architecture](docs/decisions/0002-appctl-control-plane-architecture.md), and [ADR-0003: Pydantic v2 Runtime Validation](docs/decisions/0003-pydantic-v2-runtime-validation.md)).

---

## 🎯 Architectural Invariants Governing Execution

1. **Git is the Authoritative Source of Deployment State**: All workload configurations are versioned in Git. `appctl` reconciles desired Git state into running containers.
2. **One Logical API Across Dual Transports (`DEC-01`)**: The control-plane API is a single logical HTTP/1.1 command contract (`POST /v1/operations` + `GET /v1/health`) served identically over Unix domain socket (`/run/homelab/appctl.sock`) and loopback TCP (`127.0.0.1:9001`).
3. **Host Access Does Not Confer Mutation Authority (`DEC-02`)**: Entering via SSH does not bypass authentication. Every mutating operation and sensitive read (`workload.config`, `workload.logs`) requires an explicit Bearer token; tokenless local UDS calls fall back via `SO_PEERCRED` strictly to `homelab-viewer` read-only inspection.
4. **Dedicated Service Identity & Fail-Closed Audit Guard (`DEC-03`)**: `homelab-appctl.service` executes as isolated user `homelab-appctl:homelab-operators` with mode `0600` append-only `/var/log/homelab/appctl/audit.jsonl`. Mutating operations abort immediately (`500 ERR_EXECUTION_FAILED`) if the pre-execution `DISPATCHED` audit write fails.
5. **Unified Execution, Pre-Flight & Pure-Python Environment (`DEC-04`, `DEC-05`)**: Both CLI and GitOps worker paths execute through the daemon, enforcing Pydantic v2 manifest validation, `validate_compose_security()` AST checks, and inert pure-Python `.env` assembly (no shell `source` or `eval`).
6. **Separation of Intent from Implementation**: `DeploymentModel` captures implementation-agnostic intent; concrete platform descriptors (`app.yaml` + Compose) are materialized and enforced by `appctl`.
7. **AI Authoring Boundary**: AI agents act strictly as proposal generators (`DeploymentProposal`). Humans approve the intent (`DeploymentModel`). The AI possesses zero execution authority.
8. **Secret Reference Invariant**: Deployment definitions store secret references (`secretKeyRef`), never plaintext material, resolved at runtime via `SecretProvider`.
9. **Clean State Root**: Mutable application runtime data resides strictly under `~/var/lib/homelab/<app>/`, isolated from Git repositories.

---

## 🔍 Current Implementation Baseline vs. Target Architecture

| Architectural Area | Current Implementation Baseline (Milestone P4) | Target Architecture (Roadmap v6) |
| :--- | :--- | :--- |
| **`appctl` Architecture** | Dedicated daemon service (`homelab-appctl.service`, user `homelab-appctl`) exposing `POST /v1/operations` over Unix socket and loopback HTTP, with thin-client CLI (`scripts/appctl_cli.py`). | Expand with Operations UI and external ForwardAuth integration (P5–P7). |
| **Operational Auth** | Mandatory SHA-256 Bearer token + 3-role RBAC + UDS `SO_PEERCRED` viewer fallback (`/run/secrets/appctl/tokens.json`). | LLDAP directory RBAC & Authelia ForwardAuth (P5). |
| **Metadata & Schema Engine** | Control-plane daemon validates requests via Pydantic v2 (`scripts/appctl_contracts.py`) and invokes `core_manifest.py` / `appctl_engine.py`. | Unified across `DeploymentModel` materialization (P6). |
| **Deployment Model** | Direct authoring of `app.yaml` and `docker-compose.yml` in `~/Sites/<app>/`. | 5-stage lifecycle (planned in P6): `DeploymentProposal` $\to$ operator-approved `DeploymentModel` in Git $\to$ `appctl` materialization. |
| **GitOps Engine** | Ingress/queue in `gitops_dispatcher.py`; worker delegates `workload.update` to daemon over `/run/homelab/appctl.sock`. | Integrated with `DeploymentModel` reconciliation (P6). |
| **Secret Distribution** | Age-encrypted `nixos/secrets.yaml` decrypted by `sops-nix` to tmpfs `/run/secrets/`. | Host secrets remain in `sops-nix`; workload secrets referenced by key and projected via `SecretProvider` (planned in P5). |
| **Dashboard** | `appctl sync` compiles `config/homepage/services.yaml` from `app.yaml` manifests under `_homepage_sync.lock`. | Homepage consumes read-only live status from `appctl`; dedicated Operations UI client for management (planned in P7). |

---

## 📊 Milestone Overview

| Milestone | Title | Focus Area | Status | Deliverables Summary |
| :--- | :--- | :--- | :--- | :--- |
| **P0** | **Baseline Consolidation** | Git & Architecture | ✅ **Completed** | Synchronized `refactor/appctl-rework` with `main`, recorded control plane ADR, scaffolded v2 metadata engine. |
| **P1** | **Mail Topology Realignment** | Topology & State | ✅ **Completed** | Re-homed Stalwart to the dedicated `smtp.` subdomain; migrated SnappyMail to `~/Sites/snappymail` with zero data loss. |
| **P2** | **GitOps Hardening** | Ingress Security | ✅ **Completed** | Authoritative Core repository registry (`deployments.yaml`), Compose AST security validator, systemd sandboxing. |
| **P3** | **Manifest Contract** | Domain Engine | ✅ **Completed** | Schema v1.0 with Pydantic v2 fail-closed models (`core_manifest.py`), two-axis status inspection, unified `appctl_engine.py`. |
| **P4** | **Control Plane Daemon** | API & Dispatch | ✅ **Completed** | `homelab-appctl.service` daemon, dual transports (socket + HTTP), mandatory auth admission gate, thin-client CLI, append-only audit log. |
| **P5** | **Identity & Secrets** | Auth & Providers | 🎯 **Current / Up Next** | LLDAP RBAC group mapping, Authelia ForwardAuth header integration, user-scoped token lifecycle, `SecretProvider` reference resolution. |
| **P6** | **Deployment Model & AI**| Intent Authoring | ⏳ **Planned** | Capability catalog, `DeploymentProposal` $\to$ `DeploymentModel` compiler, Git commit pipeline. |
| **P7** | **Operations UI & Portal** | Client Integration | ⏳ **Planned** | Dedicated Operations UI client, Homepage read-only status integration widgets. |

---

## 🛠️ Detailed Milestones

### Milestone P0: Baseline Consolidation
**Status**: ✅ **Completed**
* Merged `refactor/appctl-rework` and `main` into a unified architectural baseline (commit `2297cd5`).
* Modernized metadata engine with strongly-typed models (`SitesApp`, `CoreService`, `SourceConfig`) and Docker status inspection.
* Formalized the control-plane ADR and consolidated roadmap; marked legacy roadmaps as superseded.
* Verified test suite and NixOS flake sandbox evaluation.

---

### Milestone P1: Mail Topology Realignment & Zero-Data-Loss Migration
**Status**: ✅ **Completed**
* **Objective**: Segregate platform mail routing from user-facing webmail while preserving all user data and settings.
* **Key Deliverables**:
  1. *State Freeze & Verified Snapshot*: Generate timestamped, SHA256-verified backup archive of `Core/config/snappymail/data`.
  2. *Workload Provisioning*: Scaffold `~/Sites/snappymail/` (Class B Image-Only workload) and provision persistent state root at `~/var/lib/homelab/snappymail/`.
  3. *Zero-Data-Loss State Replication*: Copy data using `cp -a` and prove byte parity (`diff -r`).
  4. *Stalwart Re-homing*: Updated Stalwart public endpoint in `Core/docker-compose.yml` to the configured `smtp.<domain>` subdomain; removed SnappyMail from Core stack.
  5. *Live Verification*: Verified webmail at the configured `mail.<domain>` endpoint, user logins, address books, IMAP/SMTP routing, and Diun SMTPS delivery on port 465.
* **Exit Criteria**: 100% of user data survives cutover; mail flow and Diun alert delivery confirmed.

---

### Milestone P2: GitOps Privilege & Authority Hardening
**Status**: ✅ **Completed**
* **Objective**: Close remaining trust boundaries in the webhook continuous deployment engine.
* **Key Deliverables**:
  1. *Authoritative Core Registry*: Establish `Core/config/deployments.yaml` as the sole authority for admitted deployment repositories.
  2. *Decouple Data Vault from Workload GitOps*: Evict vault special-casing (`BRAIN_DIR`) from GitOps engine; continuous deployment strictly manages Sites container workloads.
  3. *Compose AST Security Validator*: Pre-flight parser rejecting `privileged: true`, Docker socket bind mounts, host networking, and host mounts outside workload root or state root.
  4. *Systemd Sandboxing*: Apply `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths`, `NoNewPrivileges=true`, and private temporary directories to `homelab-gitops.service`.
* **Exit Criteria**: Path traversal rejection tests pass; Compose AST security validator rejects prohibited capabilities.

---

### Milestone P3: Manifest Contract Convergence & Provenance Engine
**Status**: ✅ **Completed**
* **Objective**: Establish a single authoritative manifest schema and two-axis inspection engine across the platform.
* **Key Deliverables**:
  1. *Schema v1.0 & Parser Library (`core_manifest.py`)*: Fail-closed Pydantic v2 manifest validation (`ConfigDict(extra="forbid")`), transport scheme validation (`https://`, `ssh://`, `git@`), and explicit ref semantics (moving branches vs. pinned tags vs. commit SHAs).
  2. *Two-Axis Inspection*: Local deployment checkout state (`✓ Synced`, `* Dirty`) evaluated independently of upstream software provenance (resolved via `git ls-remote` bounded by a 10s timeout during `--fetch`).
  3. *Engine Promotion & Cleanup*: Promoted unified engine to `scripts/appctl_engine.py`, retired `scripts/appctl_engine_v2.py` shim, and achieved zero Pyright static analysis warnings.
* **Exit Criteria**: Manifest validation active in CI; all provenance resolution tests passing.

---

### Milestone P4: Appctl Control Plane Daemon & Command-Oriented API
**Status**: ✅ **Completed**
* **Objective**: Evolve `appctl` from a collection of local shell scripts into the authoritative platform daemon while enforcing authorization gates from day one.
* **Key Deliverables**:
  1. *Daemon Service (`homelab-appctl.service`)*: Systemd daemon running as dedicated user `homelab-appctl` (`Group = "homelab-operators"`), exposing one logical HTTP/1.1 API across dual transports: Unix domain socket (`/run/homelab/appctl.sock`) on host and loopback HTTP listener (`127.0.0.1:9001`).
  2. *Mandatory Authorization Gate*: Enforce SOPS-projected SHA-256 Bearer tokens (`/run/secrets/appctl/tokens.json`) and 3-role RBAC (`homelab-admin`, `homelab-operator`, `homelab-viewer`) for all mutations, sensitive reads (`workload.config`, `workload.logs`), and HTTP calls; restrict `SO_PEERCRED` on UDS to non-sensitive `homelab-viewer` inspection.
  3. *Command-Oriented Operational Router*: Single canonical command endpoint (`POST /v1/operations` + `GET /v1/health`) invoking `core_manifest.py`, `appctl_engine.py`, `validate_compose_security()`, and pure-Python `.env` assembly with per-target `flock` serialization and revision-consistent `workload.update` execution (unifying CLI and `gitops_dispatcher.py` worker execution).
  4. *Thin-Client CLI*: Re-implement `scripts/appctl` as a lightweight dispatch client communicating with the local daemon socket.
  5. *Append-Only Audit Log*: Record every mutating command, sensitive read, and denied request to `/var/log/homelab/appctl/audit.jsonl` with restricted `0600` permissions (`homelab-appctl`) and a fail-closed pre-execution disk guard. Telemetry captures `principal`, `action`, `target`, `transport`, `origin`, `client`, `timestamp`, and `result`.
* **Exit Criteria**: Hermetic API test suite passing; unauthenticated and unauthorized mutation attempts rejected across both transports; audit log capturing multi-dimensional telemetry under `homelab-appctl` isolation.

---

### Milestone P5: Identity, RBAC & SecretProvider Interface
**Status**: ⏳ **Planned**
* **Objective**: Expand the P4 authorization gate with directory-backed RBAC identity providers and dynamic secret projection.
* **Key Deliverables**:
  1. *Identity Provider Integration*: Plug Traefik/Authelia trusted headers (`Remote-User`, `Remote-Groups`) for web requests, LLDAP user-scoped API token lifecycle for CLI requests, and HMAC-SHA256 verification for GitOps webhooks into the P4 authorization middleware.
  2. *Role-Based Authorization Engine*: Enforce LLDAP group-based access control (`homelab-admin`, `homelab-operator`, `homelab-viewer`).
  3. *SecretProvider Interface*: Control plane resolves secret references (`secretKeyRef`) at container runtime, eliminating Git history pollution for workload credentials.
* **Exit Criteria**: RBAC permission matrix verified by automated tests; secret references correctly materialized into runtime environments.

---

### Milestone P6: Homelab Deployment Model & AI Authoring Workflow
**Status**: ⏳ **Planned**
* **Objective**: Enable safe, human-reviewed workload authoring from upstream repositories.
* **Key Deliverables**:
  1. *Platform Capability Catalog*: Machine-readable specification of Core capabilities and security invariants.
  2. *DeploymentProposal Schema*: Candidate model format produced by AI discovery workflows.
  3. *DeploymentModel Git Pipeline*: Operator approves proposal into a `DeploymentModel`; the model is committed to the version-controlled deployment repository (Git).
  4. *Materialization & Reconciliation*: `appctl` validates the committed model, provisions state roots, compiles platform descriptors (`app.yaml` + Compose), and reconciles running containers.
* **Exit Criteria**: Human approval gate strictly enforced; AI possesses zero execution authority; generated workloads conform to all Core invariants.

---

### Milestone P7: Operations UI & Dashboard Integration
**Status**: ⏳ **Planned**
* **Objective**: Deliver a unified web-based management experience without coupling the control plane to specific frontend engines.
* **Key Deliverables**:
  1. *Operations UI Client*: Dedicated lightweight web client consuming the `appctl` API for proposal review, approval, real-time log streaming, and API token management.
  2. *Homepage Status Integration*: Homepage consumes live status widgets from `appctl` live endpoints or compiled service maps.
* **Exit Criteria**: End-to-end browser and CLI deployment verification; Homepage accurately reflecting live container health.

---

## 🔒 Verification & Quality Gates

Progress across milestones is governed by explicit verification criteria:
* **Behavioral Invariant Coverage**: All defined domain, authorization, deployment, and security invariants must have automated regression tests.
* **Pre-Merge Validation**: Every commit must pass `./scripts/test` (Ruff linter + Pytest suite + Nix flake check).
* **Zero Production Regressions**: Platform migrations must include verified pre-flight snapshots, state parity proofs, and documented rollback runbooks.
