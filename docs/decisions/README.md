# 🏛️ Homelab Core & Appctl Architecture Decision Index

This document serves as the authoritative repository index for Architectural Decision Records (ADRs) governing **Homelab Core** and the **`appctl` Operational Control Plane**.

Repository-level architectural decisions are recorded in `docs/decisions/` using the MADR 4.0 specification (`NNNN-slug.md`).

---

## 📋 Active Architectural Decisions

| Decision Record | Status | Date | Scope & One-Line Purpose |
| :--- | :--- | :--- | :--- |
| [**ADR-0003: Centralized Runtime Validation & Serialization via Pydantic v2**](0003-pydantic-v2-runtime-validation.md) | `accepted` | 2026-10-15 | Adopts Pydantic v2 as the single runtime schema and validation authority across workload manifests and daemon API to enforce fail-closed type safety, strict schema-based boundaries, and secret-safe serialization. |
| [**ADR-0002: Homelab Operational Control Plane & Deployment Model Architecture**](0002-appctl-control-plane-architecture.md) | `accepted` | 2026-10-07 | Establishes `homelab-appctl.service` as the central operational control plane daemon, one logical command API (`POST /v1/operations`) across dual transports (UDS and HTTP), mandatory token auth with UDS `SO_PEERCRED` viewer fallback, dedicated service isolation, fail-closed audit logging, GitOps worker unification, and deterministic Git-backed declarative deployment state. |
| [**ADR-0001: Homelab 3-Tier Architecture & Deployment Provenance Model**](0001-3tier-architecture-and-provenance.md) | `accepted` | 2026-09-28 | Establishes 3-tier separation (Software, OCI Artifact, Sites Deployment, Core Platform), two workload classes (Source-Backed vs. Image-Only), isolated state root (`~/var/lib/homelab/<app>/`), and two operational inspection axes. |

---

## 🧭 Summaries of Governing Decisions

### [ADR-0003: Centralized Runtime Validation & Serialization via Pydantic v2](0003-pydantic-v2-runtime-validation.md)
* **Canonical Document**: [`0003-pydantic-v2-runtime-validation.md`](0003-pydantic-v2-runtime-validation.md)
* **Status**: `accepted` (Active)
* **Governing Implementation Plan**: [ROADMAP.md](../../ROADMAP.md) (Milestone P4)
* **Core Commitments**:
  1. **Single Schema & Validation Layer**: Workload manifests (`core_manifest.py`) and API command models inherit from `pydantic.BaseModel`.
  2. **Fail-Closed Extra Fields (`extra="forbid"`)**: Immediate rejection of unexpected or undocumented payload properties.
  3. **Strict Typing Policy (`strict=True`)**: Coercion forbidden on operations, targets, identifiers, and flags.
  4. **Secret-Safe Error Transformation**: Transport-level conversion of `ValidationError` into standard `ERR_VALIDATION_FAILED` (400) with raw input values redacted.
  5. **Dual-Transport Parity**: Identical validated models across Unix domain socket and HTTP transports.

### [ADR-0002: Homelab Operational Control Plane & Deployment Model Architecture](0002-appctl-control-plane-architecture.md)
* **Canonical Document**: [`0002-appctl-control-plane-architecture.md`](0002-appctl-control-plane-architecture.md)
* **Status**: `accepted` (Active — Reconciled with Pre-P4 Locked Decisions `DEC-01`–`DEC-05`)
* **Governing Implementation Plan**: [ROADMAP.md](../../ROADMAP.md) (Roadmap v6)
* **Core Commitments**:
  1. **One Logical Command API Across Dual Transports (`DEC-01`)**: `homelab-appctl.service` exposes `POST /v1/operations` (13 canonical domain operations) and `GET /v1/health` over HTTP/1.1 on `/run/homelab/appctl.sock` and `127.0.0.1:9001`.
  2. **Host Access Does Not Confer Mutation Authority (`DEC-02`)**: Every mutating operation and sensitive read (`workload.config`, `workload.logs`) requires an authenticated SHA-256 Bearer token (`/run/secrets/appctl/tokens.json`); tokenless UDS calls fall back via `SO_PEERCRED` strictly to `homelab-viewer` read-only inspection.
  3. **Dedicated Service Identity & Fail-Closed Audit Guard (`DEC-03`)**: Daemon executes as `homelab-appctl:homelab-operators` with mode `0600` append-only `/var/log/homelab/appctl/audit.jsonl` and a pre-execution `DISPATCHED` disk guard.
  4. **GitOps Worker Unification & Pure-Python Pre-Flight (`DEC-04`, `DEC-05`)**: `gitops_dispatcher.py` worker delegates `workload.update` to the daemon; shell `source`/`eval` are replaced with pure-Python `.env` parsing and mandatory `validate_compose_security()` pre-flight checks.
  5. **Git Truth, Model Context Protocol (MCP) Gateway & AI Boundary (P5–P7)**: Git remains authoritative deployment state; on-server AI proposal compilers deprecated in favor of a decoupled Model Context Protocol (MCP) tool gateway with human confirmation gating; resolves workload secrets via `SecretProvider` (`secretKeyRef`).

### [ADR-0001: Homelab 3-Tier Architecture & Deployment Provenance Model](0001-3tier-architecture-and-provenance.md)
* **Canonical Document**: [`0001-3tier-architecture-and-provenance.md`](0001-3tier-architecture-and-provenance.md)
* **Status**: `accepted` (Active)
* **Governing Implementation Plan**: [ROADMAP.md](../../ROADMAP.md) (Roadmap v6)
* **Core Commitments**:
  1. **3-Tier Separation of Concerns**:
     * *Tier 1 (Software Layer)*: Upstream application repositories owning code, tests, and CI. Zero Homelab infrastructure coupling.
     * *Intermediate Provenance Artifact*: OCI container images produced by CI carrying commit provenance in metadata labels and content-addressed digests.
     * *Tier 2 (Sites Deployment Layer)*: Declarative manifests (`app.yaml`) and Compose specs in `~/Sites`.
     * *Tier 3 (Platform Core Appliance)*: NixOS host foundation, edge routing, SSO, and operational controllers.
  2. **Two Workload Deployment Classes**:
     * *Class A (Source-Backed)*: Declares upstream Git source and ref (`source.url`, `source.ref`); tracks upstream Git revisions against container labels.
     * *Class B (Image-Only)*: Standard third-party tools; tracks container registry image tags and digests.
  3. **Isolated Persistent State Root**: Mutable data lives strictly under `~/var/lib/homelab/<app>/`, isolated from Git repositories.
  4. **Two Operational Inspection Axes**: Local deployment checkout state evaluated independently of upstream software provenance (resolved via `git ls-remote`).

---

## 📜 Superseded Historical Decisions & Lineage

The following historical plans and design iterations have been formally superseded by the active ADRs and Roadmap v6:

* **Roadmap v5 (`architecture-consolidation-roadmap-v5.md`)**: Superseded by Roadmap v6 (`DEC-01`–`DEC-05` pre-P4 boundary lock).
* **Roadmap v4, v3, v2 & v1** (`architecture-consolidation-roadmap-v4.md`): Superseded by Roadmap v5/v6.
* **appctl-v2 Modernization Plan** (`appctl-engine-v2-refactoring.md`): Scaffolded and absorbed into Roadmap Phases P0 and P3.
* **Smart Selective Deployment Pipeline Plan** (`smart-deployment-pipeline.md`): Absorbed into Phase P4 control-plane reconciliation.
* **Architecture Implementation Guide v3 & v2** (`architecture-consolidation-implementation-guide-v3.md`): Superseded by unified Roadmap v4–v6.
