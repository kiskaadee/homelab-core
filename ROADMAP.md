# 🗺️ Homelab Core & Appctl Engineering Roadmap

This document serves as the repository-facing projection of the authoritative **Homelab Architecture Consolidation & Control Plane Roadmap (v5)**.

It describes the implementation trajectory and phased engineering milestones for transitioning Homelab Core into a unified platform driven by the `appctl` Operational Control Plane.

> **Authoritative Master Roadmap**: `01-plans/homelab/consolidation/architecture-consolidation-roadmap-v5.md` in the central knowledge vault.
>
> **Governing Architectural Decisions**:
> - `homelab-appctl-control-plane-architecture.md` (Operational Control Plane & Deployment Model)
> - `homelab-3tier-architecture-and-provenance.md` (3-Tier Architecture & Provenance)

---

## 🎯 Architectural Invariants Governing Execution

1. **Git is the Authoritative Source of Deployment State**: All workload configurations are versioned in Git. `appctl` reconciles desired Git state into running containers.
2. **One Logical API Across Dual Transports**: The control-plane API is a single logical command-oriented contract. Unix domain socket (`/run/homelab/appctl.sock`) on the host and HTTP for network clients are transport details.
3. **Host Access Does Not Confer Authority**: Entering via SSH does not bypass authentication. Every mutating operation requires an explicit, authenticated user-scoped token.
4. **Separation of Intent from Implementation**: `DeploymentModel` captures implementation-agnostic intent; concrete platform descriptors (`app.yaml` + Compose) are materialized and enforced by `appctl`.
5. **AI Authoring Boundary**: AI agents act strictly as proposal generators (`DeploymentProposal`). Humans approve the intent (`DeploymentModel`). The AI possesses zero execution authority.
6. **Secret Reference Invariant**: Deployment definitions store secret references (`secretKeyRef`), never plaintext material, resolved at runtime via `SecretProvider`.
7. **Clean State Root**: Mutable application runtime data resides strictly under `~/var/lib/homelab/<app>/`, isolated from Git repositories.

---

## 📊 Milestone Overview

| Milestone | Title | Focus Area | Status | Deliverables Summary |
| :--- | :--- | :--- | :--- | :--- |
| **P0** | **Baseline Consolidation** | Git & Architecture | ✅ **Completed** | Synchronized `refactor/appctl-rework` with `main`, recorded control plane ADR, scaffolded v2 metadata engine. |
| **P1** | **Mail Topology Realignment** | Topology & State | 🎯 **Current / Up Next** | Move Stalwart to `smtp.roadtotech.me`; migrate SnappyMail to `~/Sites/snappymail` with zero data loss. |
| **P2** | **GitOps Hardening** | Ingress Security | ⏳ **Planned** | Authoritative Core repository registry (`deployments.yaml`), HMAC verification, systemd execution sandboxing. |
| **P3** | **Manifest Contract** | Domain Engine | ⏳ **Planned** | Schema v1.0, authoritative `core_manifest.py` library, `source.ref` semantics, two-axis status inspection. |
| **P4** | **Control Plane Daemon** | API & Dispatch | ⏳ **Planned** | `homelab-appctl.service` daemon, dual transports (socket + HTTP), thin-client CLI, append-only audit log. |
| **P5** | **Identity & Secrets** | Auth & Providers | ⏳ **Planned** | ForwardAuth header parsing, user-scoped API tokens, `SecretProvider` reference resolution. |
| **P6** | **Deployment Model & AI**| Intent Authoring | ⏳ **Planned** | Capability catalog, `DeploymentProposal` $\to$ `DeploymentModel` compiler, Git commit pipeline. |
| **P7** | **Operations UI & Portal** | Client Integration | ⏳ **Planned** | Dedicated Operations UI client, Homepage read-only status integration widgets. |

---

## 🛠️ Detailed Milestones

### Milestone P0: Baseline Consolidation
**Status**: ✅ **Completed**
* Merged `refactor/appctl-rework` and `main` into a unified architectural baseline (commit `2297cd5`).
* Modernized metadata engine (`scripts/appctl_engine_v2.py`) with strongly-typed dataclasses (`SitesApp`, `CoreService`, `SourceConfig`) and Docker status inspection.
* Formalized the control-plane ADR and Roadmap v5 in the central knowledge vault; marked legacy roadmaps as superseded.
* Verified test suite (47 tests passing) and NixOS flake sandbox evaluation.

---

### Milestone P1: Mail Topology Realignment & Zero-Data-Loss Migration
**Status**: 🎯 **Current / Up Next**
* **Objective**: Segregate platform mail routing from user-facing webmail while preserving all user data and settings.
* **Key Deliverables**:
  1. *State Freeze & Verified Snapshot*: Generate timestamped, SHA256-verified backup archive of `Core/config/snappymail/data`.
  2. *Workload Provisioning*: Scaffold `~/Sites/snappymail/` (Class B Image-Only workload) and provision persistent state root at `~/var/lib/homelab/snappymail/`.
  3. *Zero-Data-Loss State Replication*: Copy data using `cp -a` and prove byte parity (`diff -r`).
  4. *Stalwart Re-homing*: Update Stalwart public endpoint in `Core/docker-compose.yml` to `smtp.roadtotech.me`; remove SnappyMail from Core stack.
  5. *Live Verification*: Verify webmail at `mail.roadtotech.me`, user logins, address books, IMAP/SMTP routing, and Diun SMTPS delivery on port 465.
* **Exit Criteria**: 100% of user data survives cutover; mail flow and Diun alert delivery confirmed.

---

### Milestone P2: GitOps Privilege & Authority Hardening
**Status**: ⏳ **Planned**
* **Objective**: Close remaining trust boundaries in the webhook continuous deployment engine.
* **Key Deliverables**:
  1. *Authoritative Core Registry*: Establish `Core/config/deployments.yaml` as the sole authority for admitted deployment repositories.
  2. *Data Vault Pull-Only Policy*: Designate `~/Brain` as a read-only repository with `strategy: pull-only`, isolated from Docker socket access.
  3. *Compose AST Security Validator*: Pre-flight parser rejecting `privileged: true`, Docker socket bind mounts, host networking, and mounts outside `~/var/lib/homelab/<app>/`.
  4. *Systemd Sandboxing*: Apply `ProtectSystem=strict`, `NoNewPrivileges=true`, and private temporary directories to `homelab-gitops.service`.
* **Exit Criteria**: Path traversal rejection tests pass; Compose AST security validator rejects prohibited capabilities.

---

### Milestone P3: Manifest Contract Convergence & Provenance Engine
**Status**: ⏳ **Planned**
* **Objective**: Establish a single authoritative manifest schema and two-axis inspection engine across the platform.
* **Key Deliverables**:
  1. *Schema v1.0 & Parser Library (`core_manifest.py`)*: Strongly-typed manifest parsing, transport scheme validation (`https://`, `ssh://`, `git@`), and explicit ref semantics (moving branches vs. pinned tags vs. commit SHAs).
  2. *Two-Axis Inspection*: Local deployment checkout state (`✓ Synced`, `* Dirty`) evaluated independently of upstream software provenance (resolved via `git ls-remote` bounded by a 10s timeout during `--fetch`).
  3. *Engine Promotion*: Promote `scripts/appctl_engine_v2.py` to `scripts/appctl_engine.py` with complete behavioral test coverage.
* **Exit Criteria**: Manifest validation active in CI; all provenance resolution tests passing.

---

### Milestone P4: Appctl Control Plane Daemon & Command-Oriented API
**Status**: ⏳ **Planned**
* **Objective**: Evolve `appctl` from a collection of local shell scripts into the authoritative platform daemon.
* **Key Deliverables**:
  1. *Daemon Service (`homelab-appctl.service`)*: Systemd daemon exposing one logical API across dual transports: Unix domain socket (`/run/homelab/appctl.sock`) on host and HTTP listener for network clients.
  2. *Command-Oriented Operational Router*: Strongly-typed endpoints (`POST /operations/deploy`, `POST /operations/restart`, `POST /operations/pull`, `GET /workloads`, `GET /workloads/{id}/status`, `POST /operations/sync`).
  3. *Thin-Client CLI*: Re-implement `scripts/appctl` as a lightweight dispatch client communicating with the local daemon socket.
  4. *Append-Only Audit Log*: Record every mutating command to `~/.local/state/homelab/appctl/audit.jsonl` with restricted `0600` permissions. Telemetry captures `principal`, `action`, `target`, `transport`, `origin`, `client`, `timestamp`, and `result`.
* **Exit Criteria**: Hermetic API test suite passing; local socket and HTTP transport tests passing; audit log correctly capturing multi-dimensional telemetry.

---

### Milestone P5: Identity, RBAC & SecretProvider Interface
**Status**: ⏳ **Planned**
* **Objective**: Implement least-privilege identity boundaries and dynamic secret projection.
* **Key Deliverables**:
  1. *Authentication Gateway*: Accept trusted identity headers from Traefik/Authelia (`Remote-User`, `Remote-Groups`) for web requests; validate explicit user-scoped API tokens for all CLI requests (local and remote); validate HMAC signatures for GitOps webhooks.
  2. *Role-Based Authorization Engine*: Enforce group-based access control (`homelab-admin`, `homelab-operator`, `homelab-viewer`).
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
