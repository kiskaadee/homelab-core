# Homelab Core

**Homelab Core** is the declarative NixOS host foundation, shared platform infrastructure stack, and `appctl` operational control plane for a self-hosted homelab appliance.

It is designed around a strict two-tier boundary:
1. **Homelab Core (this repository)**: Owns NixOS operating system configuration, edge TLS ingress, SSO/LDAP directory identity, mail routing, read-only Docker socket isolation, and orchestration/deployment mechanics.
2. **Workload Repositories (`~/Sites/<app>`)**: Independently deployed application stacks that integrate with Core through self-describing `app.yaml` manifests and workload-scoped `docker-compose.yml` definitions.

All platform definitions are strictly instance-agnostic; domain names (`HOMELAB_DOMAIN`) and secrets (`/run/secrets/`) are injected declaratively at runtime.

> [!NOTE]
> **Current Implementation Baseline vs. Target Architecture**: This README documents the **currently implemented CLI and runtime baseline (Milestone P3)**. Under [Milestone P4 in `ROADMAP.md`](ROADMAP.md), `appctl` is transitioning from a host CLI orchestrator into a dedicated daemon service (`homelab-appctl.service`) exposing a unified logical API across local Unix socket and HTTP transports. See [`ROADMAP.md`](ROADMAP.md) for the full baseline-to-target matrix.

---

## System Architecture at a Glance

```text
                                 Internet
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │   Traefik Edge Proxy (:443)   │
                    │  Wildcard TLS (*.example.com) │
                    └───────┬───────────────┬───────┘
                            │               │
      Public Traffic        │               │ ForwardAuth Protected
      (e.g., SMTP, public)  │               │ (authelia-auth@docker)
                            ▼               ▼
                    [ Public Services ]  [ Authelia SSO / 2FA ]
                            │               │ (LDAP -> LLDAP)
                            ▼               ▼
                    ┌───────────────────────────────┐
                    │       Docker proxy-net        │
                    ├───────────────────────────────┤
                    │ • Core Services (Homepage,    │
                    │   Dozzle, Stalwart, etc.)     │
                    │ • Workload Apps (~/Sites/*)   │
                    └───────────────┬───────────────┘
                                    │ (Read-Only API via TCP:2375)
                                    ▼
                    ┌───────────────────────────────┐
                    │      Docker socket-proxy      │
                    │    (socket-net, Read-Only)    │
                    └───────────────┬───────────────┘
                                    │ (read-only mount :ro)
                                    ▼
                        /var/run/docker.sock
                                    ▲
                                    │ (Host Orchestration & CD)
                    ┌───────────────┴───────────────┐
                    │  Host Control Plane Baseline  │
                    │  • appctl CLI + Engine        │
                    │  • homelab-gitops.service     │
                    └───────────────────────────────┘
```

For detailed runtime layers, network segmentation, and the P4 control-plane transport architecture, see [`docs/architecture.md`](docs/architecture.md) and [`ROADMAP.md`](ROADMAP.md).

### Core Platform Services (`docker-compose.yml`)

| Service | Default Endpoint | Role | Access Policy |
| :--- | :--- | :--- | :--- |
| **`traefik`** | `traefik.${HOMELAB_DOMAIN}` | Edge reverse proxy & DNS-01 ACME wildcard TLS | Authelia Guard |
| **`socket-proxy`** | Internal (`socket-net:2375`) | Read-only Docker API security barrier (`POST=0`, `DELETE=0`) | Internal Only |
| **`lldap`** | `users.${HOMELAB_DOMAIN}` | Central LDAP user directory | Authelia Guard |
| **`authelia`** | `auth.${HOMELAB_DOMAIN}` | Single Sign-On (SSO), 2FA & ForwardAuth | Public (Auth Endpoint) |
| **`stalwart`** | `smtp.${HOMELAB_DOMAIN}` | Mail server (SMTP/IMAP/JMAP/Sieve) | Native / LLDAP |
| **`homepage`** | `dashboard.${HOMELAB_DOMAIN}` | Service portal dynamically synced via `appctl` | Authelia Guard |
| **`dozzle`** | `logs.${HOMELAB_DOMAIN}` | Real-time container log viewer (via `socket-proxy`) | Authelia Guard |
| **`watchtower`** | Internal | Automated container image updater | Internal Only |
| **`diun`** | Internal | Container image update notifier | Internal Only |

---

## Repository Structure

```text
.
├── flake.nix                  # NixOS host configuration, devShell (.venv symlink), and CI checks
├── docker-compose.yml         # Core platform service stack
├── nixos/                     # Declarative NixOS modules, systemd units, and SOPS secret wiring
├── config/                    # Service configurations (Traefik, Authelia, Homepage, deployments.yaml)
├── scripts/
│   ├── appctl                 # Operational CLI entrypoint and Compose environment orchestrator
│   ├── appctl_engine.py       # Discovery, two-axis provenance, TLS inspection, and Homepage sync engine
│   ├── core_manifest.py       # Authoritative Pydantic v2 schema v1.0 parser for workload app.yaml
│   ├── gitops_dispatcher.py   # Authenticated webhook CD engine with Compose AST security validation
│   └── test                   # Automated verification gate (Ruff, Pyright, Pytest, Nix flake checks)
├── tests/                     # Unit, structural, and security invariant test suites
└── docs/                      # Architecture references, operational runbooks, and MADR decisions
```

---

## Quickstart & Operations (Current CLI Baseline)

### Development & Verification

Entering the directory via `direnv` (`use flake`) or `nix develop` provisions the hermetic Nix Python environment and symlinks `.venv` for IDE static analysis:

```bash
# Run full automated verification (Ruff + Pyright + Pytest + nix flake check + NixOS drv eval)
./scripts/test

# Run individual checks directly
ruff check .
pyright
.venv/bin/pytest tests/
```

### Workload Management (`appctl` CLI)

```bash
# List all workloads with container health and local Git checkout status
appctl list

# Include upstream Git provenance (--fetch), TLS certificate expiry (--ssl), and Core services (--core)
appctl list --fetch --ssl --core

# Inspect detailed manifest, provenance, and container diagnostics for a workload
appctl info <service>

# Manage workload or Core service lifecycle (executes docker compose locally in the current baseline)
appctl up <service|core>
appctl down <service|core>
appctl restart <service|core>

# Full stack update (clean tree check -> git pull --ff-only -> compose pull -> recreate -> sync)
appctl update <service|core>

# Recompile Homepage dashboard configuration from discovered app.yaml manifests
appctl sync
```

### Host System Management

```bash
# Rebuild and activate the NixOS host configuration
sudo nixos-rebuild switch --flake .#server

# Inspect platform systemd units
systemctl status homeserver-core.service
systemctl status homelab-gitops.service
systemctl status dynu-monitor.service
```

---

## Documentation Index

| Document | Scope |
| :--- | :--- |
| [**`docs/architecture.md`**](docs/architecture.md) | Runtime layers, network segmentation, and Core / `~/Sites` boundaries. |
| [**`docs/decisions/README.md`**](docs/decisions/README.md) | Architecture Decision Records (MADR 4.0) governing platform evolution. |
| [**`ROADMAP.md`**](ROADMAP.md) | Phased engineering milestones (P0–P7) and baseline-to-target architecture matrix. |
| [**`docs/appctl.md`**](docs/appctl.md) | Current `appctl` CLI commands, resolution hierarchy, and `app.yaml` schema v1.0 contract. |
| [**`docs/gitops.md`**](docs/gitops.md) | Webhook admission pipeline, HMAC verification, Compose AST validation, and locking. |
| [**`docs/secrets-and-state.md`**](docs/secrets-and-state.md) | SOPS secret management, `/run/secrets/` projections, and persistent state paths. |
| [**`docs/operations.md`**](docs/operations.md) | Day-2 operational runbook, updates, backups, and recovery procedures. |
| [**`docs/setup_guide.md`**](docs/setup_guide.md) | Bare-metal NixOS installation and host bootstrapping guide. |
| [**`SECURITY.md`**](SECURITY.md) | Security controls, Docker socket isolation, and threat model. |
| [**`AGENTS.md`**](AGENTS.md) | Architectural invariants, TDD rules, and verification gates for contributors and agents. |

---

## License

This repository is released into the public domain under [The Unlicense](UNLICENSE).
