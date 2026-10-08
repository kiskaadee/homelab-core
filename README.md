# 🏛️ Homelab Core & Appctl Operational Control Plane

**Homelab Core** defines the host foundation, platform infrastructure services, security boundaries, and operational control plane for the `roadtotech.me` homelab appliance.

This repository serves two primary roles:
1. **The Host Platform & Infrastructure Appliance**: Declarative NixOS operating system definitions, edge ingress routing, wildcard TLS certificates, identity/SSO directory services, mail routing, and Docker socket isolation.
2. **The Appctl Operational Control Plane**: The centralized engine governing workload lifecycle management, continuous GitOps deployment, source provenance tracking, and dashboard synchronization across independent application workloads in `~/Sites`.

---

## 🧭 Architectural Direction & Mental Model

Under the approved architectural baseline ([`ADR: Homelab Operational Control Plane`](docs/decisions/README.md#homelab-operational-control-plane--deployment-model-architecture) and [`ADR: 3-Tier Architecture & Provenance`](docs/decisions/README.md#homelab-3-tier-architecture--deployment-provenance-model)), `appctl` is evolving from a local procedural CLI wrapper into the **authoritative operational control plane service for Homelab Core**.

### The Core Architectural Spine

```text
                        Human Operator / Automation
                                     │
                 ┌───────────────────┴───────────────────┐
                 │                                       │
           Operations UI                                CLI (Local & Remote)
                 │                                       │
                 └───────────────────┬───────────────────┘
                                     │
                             appctl Control Plane API
                       (One Logical API across Transports)
                                     │
                        ┌────────────┴────────────┐
                        │                         │
                   Unix Socket               HTTP Listener
                  (Host Clients)            (Remote/LAN Clients)
                        │                         │
                        └────────────┬────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         │                           │                           │
    Authentication             Deployment Model              Operations
    & Authorization                  Plane                     Execution
         │                           │                           │
  Token Validation &          ┌──────┴──────┐              Docker Compose
  LLDAP Directory RBAC        │             │              Traefik Routing
                       AI Investigation   Human            Host Secrets
                              │          Approval          Reconciliation
                              ▼             │
                      DeploymentProposal    │
                              │             ▼
                              └─────▶ DeploymentModel
                                            │
                                            ▼ (Committed to Git)
                                  Deployment Repository
                                       (Git Truth)
                                            │
                                            ▼ (appctl validates & materializes)
                                  Deployment Definition
                               (Sites app.yaml + compose)
                                            │
                                            ▼
                                   Runtime Reconciliation
```

### Core Architectural Invariants

* **Single Operational Truth**: Exactly one implementation of every operational action (`deploy`, `restart`, `inspect`, `sync`, `validate`) exists in the platform. Clients (local CLI, remote CLI, web UI, GitOps) invoke the exact same logical API contract.
* **Host Access Does Not Confer Authority**: Entering the server via SSH is merely host access; it does not grant Appctl privileges. **Every mutating operation requires an explicit, authenticated user-scoped token**, regardless of transport. Unix socket peer credentials identify the transport context, not user authority.
* **Git is the Authoritative Source of Deployment State**: Approved workload configurations are versioned in Git. `appctl` reconciles desired Git state into running containers, preserving immutable revision history, commit provenance, peer reviews, and atomic rollbacks (`git revert`).
* **Five-Stage Model Lifecycle (Intent vs. Implementation)**:
  $$\text{Repository} \xrightarrow{\text{Investigation}} \text{Application Profile} \xrightarrow{\text{Proposal}} \text{DeploymentProposal} \xrightarrow{\text{Human Selection}} \text{DeploymentModel (Git)} \xrightarrow{\text{appctl Materialize}} \text{Deployment Definition} \xrightarrow{\text{Reconcile}} \text{Containers}$$
  * `DeploymentModel`: The operator-selected, implementation-agnostic representation of intent (ports, domains, volumes, dependencies, secret references). Free of Compose syntax and Traefik label boilerplate.
  * `Deployment Definition`: Concrete platform descriptors (`app.yaml` + `docker-compose.yml`) materialized and enforced by `appctl`.
* **AI Authoring Boundary**: AI agents act strictly as upstream proposal generators with **zero execution or mutation authority**. The AI analyzes repositories and drafts `DeploymentProposals`. After human approval into a `DeploymentModel`, the AI is completely out of the loop.
* **Secret Reference Invariant**: Deployment definitions contain secret references (`secretKeyRef`), never secret material. References are resolved at runtime through a defined `SecretProvider` interface. Host platform secrets remain declarative in `sops-nix`.
* **Clean State Root**: Mutable application data lives strictly under `~/var/lib/homelab/<app>/`, isolated from Git deployment repositories.

---

## 🔍 Current Implementation Baseline vs. Target Architecture

To maintain clarity during development, this repository explicitly separates what is **currently implemented** from what is **planned in the roadmap**:

| Architectural Area | Current Implementation Baseline (Milestone P0) | Target Architecture (Roadmap v5) |
| :--- | :--- | :--- |
| **`appctl` Architecture** | Bash CLI wrapper (`scripts/appctl`) driving local subprocesses (`docker compose`, `git`) and metadata engines. | Dedicated daemon service (`homelab-appctl.service`) exposing one logical API over Unix socket and HTTP. |
| **Operational Auth** | Sudo / Unix user permissions on the host. Sourcing secrets from `/run/secrets/`. | LLDAP directory identity. Explicit user-scoped API tokens for all mutating actions; Authelia ForwardAuth for Web. |
| **Metadata Engine** | Strongly-typed dataclass engine (`scripts/appctl_engine_v2.py`) with source provenance and Docker label inspection, operating alongside legacy engine. | Single unified engine (`core_manifest.py` / `appctl_engine.py`) embedded directly within the control plane daemon. |
| **Deployment Model** | Manual authoring of `app.yaml` and `docker-compose.yml` directly in `~/Sites/<app>/`. | 5-stage lifecycle: AI/human `DeploymentProposal` $\to$ operator-approved `DeploymentModel` committed to Git $\to$ `appctl` materialization. |
| **GitOps Engine** | Webhook dispatcher (`scripts/gitops_dispatcher.py`) listening on port 9000 with HMAC-SHA256 authentication and flock serialization. | Fully integrated into control-plane admission, calling central reconciliation endpoints. |
| **Secret Distribution** | Static Age-encrypted `nixos/secrets.yaml` decrypted by `sops-nix` to tmpfs `/run/secrets/`. | Host secrets remain in `sops-nix`; workload secrets referenced by key and projected at runtime via `SecretProvider`. |
| **Dashboard** | `appctl sync` statically compiles `config/homepage/services.yaml`. | Homepage consumes read-only live status from `appctl`; dedicated Operations UI client for interactive management. |

---

## 🗺️ System Topology & Runtime Layers

```text
                                 Internet
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Traefik Edge Proxy (:443)   │
                    │  Wildcard TLS (*.roadtotech.me)│
                    └───────┬───────────────┬───────┘
                            │               │
      Public Traffic        │               │ ForwardAuth Protected
      (e.g., mail, landing) │               │ (authelia-auth@docker)
                            ▼               ▼
                    [ Public Services ]  [ Authelia SSO / 2FA ]
                            │               │ (LDAP -> LLDAP)
                            │               │
                            ▼               ▼
                    ┌───────────────────────────────┐
                    │      Docker proxy-net         │
                    ├───────────────────────────────┤
                    │ • Core Services (Homepage,    │
                    │   Dozzle, etc.)               │
                    │ • Workload Apps (~/Sites/*)   │
                    └───────────────────────────────┘
                                    │
                     (Protected API via TCP:2375)
                                    ▼
                    ┌───────────────────────────────┐
                    │   Docker socket-proxy         │
                    │   (socket-net, Read-Only)     │
                    └───────────────┬───────────────┘
                                    │ (read-only mount)
                                    ▼
                        /var/run/docker.sock
```

### Core Services (`docker-compose.yml`)
| Service | Domain / Ingress | Function | Auth Policy |
| :--- | :--- | :--- | :--- |
| **`traefik`** | `traefik.roadtotech.me` | Edge reverse proxy, Dynu DNS-01 ACME TLS | Authelia Guard |
| **`socket-proxy`** | Internal (`socket-net:2375`) | Read-only Docker API gateway barrier | Internal Only |
| **`lldap`** | `users.roadtotech.me` | LDAP directory & user database | Authelia Guard |
| **`authelia`** | `auth.roadtotech.me` | Single Sign-On (SSO) & ForwardAuth middleware | Public (Bypass) |
| **`stalwart`** | `mail.roadtotech.me` | Mail Server (SMTP/IMAP/JMAP/Sieve) *(Moving to smtp in P1)* | Native / LLDAP |
| **`homepage`** | `dashboard.roadtotech.me` | Service portal & system dashboard | Authelia Guard |
| **`dozzle`** | `logs.roadtotech.me` | Container log viewer (via socket-proxy) | Authelia Guard |
| **`watchtower`** | Internal | Automated container image updater | Internal Only |
| **`diun`** | Internal | Container image update notifier | Internal Only |

---

## ⚡ Quick Start & Operator Workflows (Current Baseline)

### Host & System Operations
```bash
# Rebuild and switch NixOS configuration
sudo nixos-rebuild switch --flake ~/Core#server
# or use the built-in shell alias:
nix-switch

# Inspect systemd platform services
systemctl status homeserver-core.service   # Core Compose stack
systemctl status homelab-gitops.service     # GitOps webhook engine
systemctl status dynu-monitor.service       # Smart DDNS IP monitor
systemctl status dynu-monitor.timer         # DDNS timer (every 30m)
```

### Workload Management (`appctl` CLI)
```bash
# List all application stacks with container health, Git tracking, and provenance status
appctl list

# Fetch upstream changes across all repos and inspect with SSL certificates
appctl list --fetch --ssl --core

# Inspect full diagnostic metadata for a service
appctl info jellyfin

# Start, stop, or restart an application or Core service
appctl up docs
appctl down mongodb
appctl restart traefik

# Sequential stack update (clean working tree check -> git pull -> compose pull -> up -> sync)
appctl update docs
appctl update core

# Recompile Homepage services.yaml from active app.yaml manifests
appctl sync
```

### Testing & Verification
```bash
# Run the complete test suite (Ruff linter + Pytest invariants + Nix flake check)
./scripts/test

# Run pure Nix flake evaluation and checks
nix flake check
```

---

## 📚 Documentation Navigation & Ownership

| Document | Ownership & Epistemic Role |
| :--- | :--- |
| [**`ROADMAP.md`**](ROADMAP.md) | **Implementation Trajectory**: Phased milestones (P0 through P7), active status, and deliverables based on Roadmap v5. |
| [**ADR Index**](docs/decisions/README.md) | **Architectural Decisions**: Navigation index to authoritative ADRs governing the control plane and 3-tier provenance. |
| [**Architecture Reference**](docs/architecture.md) | **Runtime Architecture**: Deep-dive into runtime layers, network segmentation, and component boundaries. |
| [**Appctl Reference**](docs/appctl.md) | **Current CLI Implementation**: Command syntax, arguments, resolution rules, and `app.yaml` fields consumed today. |
| [**GitOps Engine**](docs/gitops.md) | **Current CD Implementation**: Webhook admission pipeline, HMAC verification, and lock serialization. |
| [**Secrets & State**](docs/secrets-and-state.md) | **Current Runtime Secrets**: SOPS encryption, tmpfs projections, and persistent state roots. |
| [**Operator Runbook**](docs/operations.md) | **Operational Runbook**: Host maintenance, updates, garbage collection, and recovery procedures. |
| [**Setup & Provisioning**](docs/setup_guide.md) | **Host Provisioning**: Bare-metal NixOS installation and platform bootstrapping. |
| [**Agent Guidelines**](AGENTS.md) | **Agent Invariants**: Hard rules, trust boundaries, Core/Sites boundaries, and testing rules. |
| [**Security Policy**](SECURITY.md) | **Security Controls**: Docker socket isolation, threat models, and hardening checklists. |

---

## 🧠 Governance & Knowledge Architecture

Durable architectural decisions, exploratory design trade-offs, and master execution roadmaps are governed in the operator's central knowledge vault under standard epistemic taxonomy:
* **Architectural Decisions**: Captured in MADR 4.0 format under `03-records/decisions/`.
* **Execution Roadmaps**: Phased engineering blueprints under `01-plans/`.
* **System Discussions**: Exploratory trade-off analyses under `02-discussions/`.

The repository documentation reflects the accepted architectural state and serves as the operational guide for the implementation.

---

## 📄 License

This repository is released into the public domain under [The Unlicense](UNLICENSE).
