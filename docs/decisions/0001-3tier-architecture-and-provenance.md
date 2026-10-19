---
type: decision
status: accepted
project: homelab-core
date: 2026-09-28
tags: [architecture, homelab, provenance, gitops, state, kiss]
---

# ADR 0001: Homelab 3-Tier Architecture & Deployment Provenance Model

## Context and Problem Statement

Homelab Core operates as a dedicated NixOS appliance hosting self-hosted productivity services and independent backend engineering projects. In early platform iterations (v1 through v3), several structural tensions emerged:

1. **Application & Deployment Coupling**: Custom applications co-located application source trees with host-specific Traefik labels, Docker bridge networks, and host path mounts inside individual Git repositories in `~/Sites/<app>`.
2. **Repository Inflation**: Third-party off-the-shelf applications (`jellyfin`, `stirling`, `snappymail`) required standalone Git repositories simply to store a minimal Compose file and manifest.
3. **Ambiguous Freshness**: Local Git status checks in `~/Sites/<app>` could only report whether deployment descriptors had uncommitted edits, never whether running containers matched current upstream application source code.
4. **Mutable State Entanglement**: Application databases, caches, and runtime state lived inside or adjacent to version-controlled Git working trees (`Core/config/` and `Sites/<app>/data`), risking dirty checkouts, accidental secret/state commits, or data loss during branch operations.
5. **Orchestrator Sprawl Risk**: Scaling operational scripts threatened to introduce secondary distributed infrastructure (queues, databases, message brokers) around basic single-node container deployments.

The central architectural question is: **How should Homelab Core structure application source code, deployment configuration, runtime state, and platform orchestration to maintain strict separation of concerns, verifiable provenance, and linear KISS operations?**

## Decision Drivers

* **Clean Separation of Concerns**: Application software projects must be developable, testable, and publishable independently of Homelab Core infrastructure, domains, or bridge networks.
* **Workload Simplicity**: Off-the-shelf third-party applications should not incur standalone source repository overhead.
* **Verifiable Two-Axis Provenance**: Operators must be able to independently verify local deployment configuration freshness (`~/Sites/<app>`) versus upstream software code freshness (`source.url` vs. running container image).
* **KISS Primitives & Minimal Control Paths**: Operational reconciliation must remain bounded, deterministic, and linear without distributed brokers, queues, or deployment databases.
* **Filesystem State Hygiene**: Mutable runtime data must never reside inside version-controlled Git trees.

## Considered Options

* **Option 1: Monolithic / Single-Repo Deployment (Status Quo v1–v3)**: Co-locate application source code, Dockerfiles, Compose files, and mutable runtime state inside a single Git repository per application.
* **Option 2: Miniature Cloud Platform**: Introduce message brokers (RabbitMQ/Redis), a deployment state database, multi-environment controllers, and asynchronous rollback daemons.
* **Option 3: Bounded 3-Tier Platform with OCI Provenance Labels & Linear Control Path (Adopted)**: Decouple Software, Deployment, and Platform tiers; connect them via OCI image artifacts carrying source revision labels and content-addressed digests; enforce two-axis provenance inspection, linear reconciliation, and isolated host state roots.

## Decision Outcome

Chosen option: **Option 3: Bounded 3-Tier Platform with OCI Provenance Labels & Linear Control Path**, because it resolves source-deployment coupling and provenance ambiguity while retaining a strictly bounded, single-appliance operational footprint.

### Summary of Architectural Commitments

1. **3-Tier Separation of Concerns**:
   * **Tier 1 — Software Layer (Application Repositories)**: Standalone Git repositories owning application source code, tests, Dockerfiles, and CI workflows. Zero Homelab infrastructure coupling.
   * **Intermediate Provenance Artifacts**: OCI container images produced by CI act as immutable provenance objects identified by their OCI digest and carrying source commit metadata via the `org.opencontainers.image.revision` label.
   * **Tier 2 — Deployment Layer (`~/Sites` Workload Plane)**: Declarative catalog in `~/Sites/<app>` owning workload-specific `app.yaml` manifests and `docker-compose.yml` topologies.
   * **Tier 3 — Platform Layer (Homelab Core Appliance)**: Declarative NixOS host foundation (`~/Core`) owning edge ingress (`traefik`), SSO/directory (`authelia`, `lldap`), mail (`stalwart`), monitoring (`dozzle`, `diun`), and operational controllers (`appctl`, `gitops_dispatcher`).
2. **Two Workload Deployment Classes**:
   * **Class A (Source-Backed)**: Declares an explicit upstream `source:` block (`source.url`, `source.ref`) in `app.yaml`; tracks upstream Git revisions against running container OCI labels.
   * **Class B (Image-Only)**: Standard third-party workloads omitting `source:`; deployment freshness relies on container registry image tags and content-addressed digests.
3. **Two Operational Inspection Axes (`scripts/provenance.py`)**:
   * **Axis 1 (Local Deployment Checkout)**: Evaluates local `~/Sites/<app>` working-tree cleanliness (`✓ Synced`, `* Dirty`, `⬆ Ahead`, `⬇ Behind`).
   * **Axis 2 (Upstream Software Provenance)**: Independently resolves upstream remote revisions via bounded `git ls-remote` (10-second timeout during `--fetch`) against running container revision labels.
4. **Minimal Linear Reconciliation Path**:
   * The deployment lifecycle remains strictly linear:
     $$\text{Source Push} \longrightarrow \text{CI Build} \longrightarrow \text{GitOps Ingress} \longrightarrow \text{appctl Reconcile} \longrightarrow \text{Compose Up}$$
   * Secondary message queues, brokers, and deployment databases are rejected.
5. **Isolated Persistent State Root**:
   * Mutable application runtime state (databases, caches, uploads, dynamic configs) resides strictly under `~/var/lib/homelab/<app>/`, isolated from version-controlled Git repositories.

## Pros and Cons of the Options

### Option 1: Monolithic / Single-Repo Deployment
* Good, because it requires only a single Git repository per custom application.
* Bad, because host-specific routing labels and bridge networks leak into application source repositories, preventing clean open-sourcing or reuse.
* Bad, because off-the-shelf tools require artificial wrapper repositories.
* Bad, because a single commit SHA conflates software code revisions with deployment configuration edits.
* Bad, because mutable state co-located in `Sites/<app>/data` creates dirty Git trees and risks accidental commits or data loss.

### Option 2: Miniature Cloud Platform
* Good, because it mirrors enterprise Kubernetes / ArgoCD continuous deployment pipelines.
* Bad, because running distributed brokers, queues, and state databases on a single appliance introduces disproportionate operational overhead and failure modes.
* Bad, because it violates the KISS principle.

### Option 3: Bounded 3-Tier Platform with OCI Provenance Labels (Adopted)
* Good, because custom software is completely decoupled from homelab infrastructure and remains portable and instance-agnostic.
* Good, because off-the-shelf software incurs zero source repository overhead.
* Good, because two-axis inspection resolves local configuration status and upstream code provenance unambiguously.
* Good, because runtime state is cleanly isolated under `~/var/lib/homelab/<app>/`.
* Bad, because custom applications manage source code and Sites deployment manifests as separate concerns.
* Bad, because verifying remote revisions without local clones (`git ls-remote`) evaluates commit equality (`HEAD` match) rather than full commit ancestry distance.

## Consequences

### Positive Consequences
* **Independent Software Lifecycle**: Custom applications can be developed, tested, and published without exposing homelab domains, secrets, or network topologies.
* **Precise Diagnostic Truth**: Operators and `appctl` can distinguish between uncommitted local deployment edits (`* Dirty`) and upstream software updates (`git ls-remote`).
* **Zero-Data-Loss State Isolation**: Proven during the Milestone P1 SnappyMail migration, moving persistent runtime data to `~/var/lib/homelab/<app>/` keeps Git trees pure and backups deterministic.
* **Operational Restraint**: Preserves a linear, local reconciliation path without distributed state stores.

### Negative Consequences & Accepted Trade-offs
* **Dual Repositories for Custom Workloads**: Deploying a custom application separates upstream source CI from the `~/Sites/<app>` deployment manifest.
* **Commit Equality Without Ancestry Graph**: Remote `git ls-remote` queries confirm whether the deployed revision matches upstream `HEAD`, without computing commit ahead/behind counts on uncloned source repos.

## More Information

* **Architecture Specification**: [`docs/architecture.md`](../architecture.md)
* **Secrets & State Contract**: [`docs/secrets-and-state.md`](../secrets-and-state.md)
* **Governing Roadmap**: [`ROADMAP.md`](../../ROADMAP.md) (Milestones P1 & P3)
