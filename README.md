# 🌐 Homelab Core — Appliance & Platform Reference

**Homelab Core** is the declarative NixOS appliance and platform foundation for the `roadtotech.me` homelab cluster.

It provides host operating system configuration, edge routing, automated wildcard TLS certificate issuance, identity and access management, mail transport, container socket isolation, workload orchestration, and continuous GitOps deployment.

---

## 🏛️ System Architecture & Boundaries

The homelab ecosystem separates **ownership**, **runtime architecture**, and **cross-cutting orchestration**:

### 1. Ownership: Core vs. Sites
* **Core (`~/Core`) owns platform mechanics**: System services, host firewall, edge ingress, security barriers, deployment serialization, and secret distribution.
* **Workload Plane (`~/Sites`) owns application intent**: Workloads are self-describing via `app.yaml` and independent `docker-compose.yml` configurations. Workloads declare application intent; Core executes deployment.

### 2. Runtime Architecture
The system operates across four runtime layers:
1. **Host Foundation**: Headless NixOS Linux, disk configuration, Docker daemon, systemd daemons, firewall rules, and in-memory secret files under `/run/secrets/`.
2. **Edge Gateway**: Traefik (v3.6) reverse proxy terminating wildcard TLS (`*.roadtotech.me`) via Dynu DNS-01 Let's Encrypt validation.
3. **Core Platform Services**: Central services in `docker-compose.yml` (Authelia SSO, LLDAP directory, Stalwart mail, SnappyMail, Homepage dashboard, Portainer, Dozzle, Watchtower, Diun, and socket-proxy).
4. **Workload Plane**: Independent application stacks in `~/Sites` connected to the platform over the `proxy-net` Docker network.

### 3. Cross-Cutting Orchestration
* **`appctl` CLI & Engine**: Coordinates discovery, environment injection, container lifecycle actions, and Homepage compilation across workloads and Core.
* **GitOps Webhook Dispatcher**: Listens on port 9000 (`homelab-gitops.service`), validating incoming Git push webhooks and executing declared deployment actions.

---

## 🗺️ System Topology & Network Architecture

```
                                 Internet
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Traefik Edge Proxy (:443)   │
                    │  Wildcard TLS (*.roadtotech.me│
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
                    │   Portainer, Dozzle, etc.)    │
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
| **`stalwart`** | `mail.roadtotech.me` | Mail Server (SMTP/IMAP/JMAP/Sieve) | Native / LLDAP |
| **`snappymail`** | `webmail.roadtotech.me` | Webmail client connected to Stalwart | Native / Stalwart |
| **`homepage`** | `dashboard.roadtotech.me` | Service portal & system dashboard | Authelia Guard |
| **`portainer`** | `portainer.roadtotech.me` | Container management GUI (via socket-proxy) | Authelia Guard |
| **`dozzle`** | `logs.roadtotech.me` | Container log viewer (via socket-proxy) | Authelia Guard |
| **`watchtower`** | Internal | Automated container image updater | Internal Only |
| **`diun`** | Internal | Container image update notifier | Internal Only |

---

## ⚡ Quick Start & Operator Workflows

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

### Workload Management (`appctl`)
```bash
# List all application stacks with container health and Git tracking status
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
# Run the repository test suite (linting + pytest + nix flake check)
./scripts/test

# Run Nix sandbox flake evaluation and checks
nix flake check
```

---

## 📚 Repository Documentation Index

| Document | Description |
| :--- | :--- |
| [**Architecture Guide**](docs/architecture.md) | Details system layers, component roles, network isolation, and security controls. |
| [**Operator Runbook**](docs/operations.md) | Day-to-day administration, updates, maintenance, troubleshooting, and recovery procedures. |
| [**Appctl Reference**](docs/appctl.md) | CLI command reference and consumed `app.yaml` manifest fields. |
| [**GitOps Engine**](docs/gitops.md) | Webhook admission pipeline, HMAC authentication, lock serialization, and execution boundaries. |
| [**Secrets & State**](docs/secrets-and-state.md) | SOPS encryption, runtime secret projections, user directory management, and persistent storage. |
| [**Setup & Provisioning**](docs/setup_guide.md) | Procedures for provisioning a server host and onboarding workloads. |
| [**Security Policy**](SECURITY.md) | Security controls, container isolation policies, and threat model. |

---

## 🧠 Relationship to Brain Vault

* **Repository Documentation (`~/Core/docs/`)**: Describes *what exists* and *how to operate it*. It serves as the operational manual for the current repository.
* **Brain Vault (`~/Brain/projects/homelab/`)**: Captures *why it was built this way* — preserving architectural decision records (ADRs), historical trade-off discussions, active roadmaps, and deep debugging discoveries.

---

## 📄 License

This repository is released into the public domain under [The Unlicense](UNLICENSE).
