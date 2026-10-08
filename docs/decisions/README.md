# 🏛️ Homelab Core & Appctl Architecture Decision Index

This document serves as the repository-facing index for Architectural Decision Records (ADRs) governing **Homelab Core** and the **`appctl` Operational Control Plane**.

In accordance with platform governance, authoritative ADRs are maintained in the central knowledge vault (`03-records/decisions/`) using the MADR 4.0 specification. This index provides navigation, context, and one-line summaries without duplicating architectural specifications or creating competing sources of truth.

---

## 📋 Active Architectural Decisions

| Decision Record | Status | Date | Scope & One-Line Purpose |
| :--- | :--- | :--- | :--- |
| [**Homelab Operational Control Plane & Deployment Model Architecture**](#homelab-operational-control-plane--deployment-model-architecture) | `accepted` | 2026-10-07 | Establishes `appctl` as the central operational control plane daemon, one logical API across dual transports (Unix socket and HTTP), explicit token authentication on host, Git as authoritative deployment state, the 5-stage model lifecycle, AI proposal boundaries, and append-only audit telemetry. |
| [**Homelab 3-Tier Architecture & Deployment Provenance Model**](#homelab-3-tier-architecture--deployment-provenance-model) | `accepted` | 2026-09-28 | Establishes 3-tier separation (Software, OCI Artifact, Sites Deployment, Core Platform), two workload classes (Source-Backed vs. Image-Only), isolated state root (`~/var/lib/homelab/<app>/`), and two operational inspection axes. |

---

## 🧭 Summaries of Governing Decisions

### Homelab Operational Control Plane & Deployment Model Architecture
* **Canonical Document**: `03-records/decisions/homelab-appctl-control-plane-architecture.md`
* **Status**: `accepted` (Active)
* **Governing Implementation Plan**: [ROADMAP.md](../../ROADMAP.md) (Roadmap v5)
* **Core Commitments**:
  1. **Control Plane Daemon**: `appctl` transitions from a collection of local shell scripts into the authoritative platform service (`homelab-appctl.service`).
  2. **One Logical API Across Dual Transports**: Command-oriented operations exposed over a Unix domain socket (`/run/homelab/appctl.sock`) on host and an HTTP listener for network clients.
  3. **Explicit Token Authentication Everywhere**: Host SSH access does not confer appctl authority; every mutating operation requires an explicit, authenticated user-scoped token.
  4. **Git as Authoritative Deployment State**: Approved `DeploymentModels` are committed to Git. `appctl` acts as the reconciliation engine, validating the model, materializing platform descriptors (`app.yaml` + Compose), and reconciling running containers.
  5. **Five-Stage Deployment Model Lifecycle**: Separates factual `ApplicationProfile`, candidate `DeploymentProposal`, validated `DeploymentModel` (in Git), materialized `Deployment Definition`, and runtime `Reconciliation`.
  6. **AI Proposal Boundary**: AI agents act strictly as proposal generators with zero mutation or execution authority. After human approval into a `DeploymentModel`, the AI is completely out of the loop.
  7. **Secret Reference Invariant**: Manifests contain secret references (`secretKeyRef`), never secret material, resolved at runtime via `SecretProvider`.
  8. **Append-Only Audit Log**: Audit entries capture `principal`, `action`, `target`, `transport`, `origin`, `client`, `timestamp`, and `result`.

---

### Homelab 3-Tier Architecture & Deployment Provenance Model
* **Canonical Document**: `03-records/decisions/homelab-3tier-architecture-and-provenance.md`
* **Status**: `accepted` (Active)
* **Governing Implementation Plan**: [ROADMAP.md](../../ROADMAP.md) (Roadmap v5)
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

The following historical plans and design iterations have been formally superseded by the active ADRs and Roadmap v5:

* **Roadmap v4 & v3** (`architecture-consolidation-roadmap-v4.md`): Superseded by Roadmap v5.
* **appctl-v2 Modernization Plan** (`appctl-engine-v2-refactoring.md`): Scaffolded and absorbed into Roadmap v5 Phases P0 and P3.
* **Smart Selective Deployment Pipeline Plan** (`smart-deployment-pipeline.md`): Absorbed into Roadmap v5 Phase P4 control-plane reconciliation.
* **Architecture Implementation Guide v3** (`architecture-consolidation-implementation-guide-v3.md`): Superseded by Roadmap v5.
