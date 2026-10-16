# 🏛️ Homelab Core — Architecture & System Design

This document details the architectural structure, component roles, networking models, and security controls of the **Homelab Core** platform.

> [!NOTE]
> **Implementation Scope**: This document details the **current runtime architecture (Milestone P3)**, edge routing, container topologies, and network isolation models of Homelab Core.
> For the planned Milestone P4 control-plane daemon transition and phased engineering milestones, see [`ROADMAP.md`](../ROADMAP.md) and the [Architecture Decision Index](decisions/README.md).

---

## 1. System Structure: Ownership vs. Runtime Layers vs. Orchestration

To understand how the system is organized, three architectural dimensions are distinguished:

### A. Ownership Boundary
* **Core (`~/Core`) owns**: NixOS host configuration, shared platform services, edge ingress, identity boundaries, secrets distribution, and deployment orchestration mechanics.
* **Workload Plane (`~/Sites`) owns**: Independent application deployments, workload-specific Docker Compose files, declarative `app.yaml` manifests, and workload-scoped runtime state (`~/var/lib/homelab/<app>/`).

### B. Four Runtime Layers
The runtime environment consists of four functional layers:

```text
┌────────────────────────────────────────────────────────────────────────┐
│ 1. EDGE GATEWAY & INGRESS                                              │
│    Traefik (v3.6) · Let's Encrypt Wildcard TLS · Dynu DNS-01 Provider  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. CONTROL & PLATFORM SERVICES (Core Compose Stack)                    │
│    Authelia SSO · LLDAP Directory · Stalwart Mail Server               │
│    Homepage Dashboard · Dozzle · Watchtower · Diun                     │
│    socket-proxy (Read-Only Docker API Barrier)                         │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. WORKLOAD PLANE (~/Sites)                                            │
│    Autonomous Workload Repositories (SnappyMail, Docs, Gitea, etc.)    │
│    Declarative app.yaml Manifests & Compose Stacks                     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. HOST FOUNDATION                                                     │
│    NixOS Linux · sops-nix Secrets · Docker Engine · Systemd Units      │
│    Smart Dynu DDNS Monitor · Host Firewall · /run Secret Projections   │
└────────────────────────────────────────────────────────────────────────┘
```

### C. Cross-Cutting Orchestration
Orchestration coordinates operations across the runtime layers:
* **`appctl` CLI, Provenance Engine & Schema Parser (`scripts/appctl`, `scripts/appctl_engine.py`, `scripts/core_manifest.py`)**: Current CLI implementation coordinating workload discovery, fail-closed Pydantic v2 `app.yaml` validation, two-axis provenance inspection, and Homepage compilation across workloads and Core. Under Milestone P4, this transitions into the centralized operational control plane daemon (`homelab-appctl.service`).
* **GitOps Webhook Dispatcher (`scripts/gitops_dispatcher.py`)**: Governed by the `homelab-gitops.service` systemd unit, it receives webhooks on port 9000, verifies HMAC-SHA256 signatures, enforces the `config/deployments.yaml` allowlist and Compose AST security checks, and dispatches serialized deployment actions for `~/Sites` workloads.

---

## 2. Component Roles & Operational Boundaries

### A. Edge Gateway (`traefik`)
* **Role**: Ingress proxy for incoming HTTP (80) and HTTPS (443) traffic.
* **TLS Management**: Employs Traefik's native ACME resolver using the Dynu DNS-01 challenge provider (`myresolver`) to obtain and renew wildcard certificates for `*.${HOMELAB_DOMAIN}` and `${HOMELAB_DOMAIN}` (e.g., `*.example.com`). Certificate data is stored at `config/letsencrypt/acme.json` (mode `0600`).
* **Redirection**: Automatically upgrades HTTP traffic to HTTPS via the `https-redirect@docker` middleware.
* **Dynamic Routing**: Discovers routers and services dynamically by reading Docker container labels via `socket-proxy` (`tcp://socket-proxy:2375`).

### B. Security Barrier (`socket-proxy`)
* **Role**: Isolates the host Docker daemon socket from direct container access.
* **Access Rules**:
  - Mounted read-only: `/var/run/docker.sock:/var/run/docker.sock:ro`.
  - State-modifying and execution endpoints disabled: `POST=0`, `DELETE=0`, `BUILD=0`, `EXEC=0`, `COMMIT=0`, `CONFIGS=0`, `DISTRIBUTION=0`, `NODES=0`, `PLUGINS=0`, `SECRETS=0`, `SWARM=0`, `SYSTEM=0`.
  - Read-only inspection endpoints enabled: `CONTAINERS=1`, `NETWORKS=1`, `SERVICES=1`, `TASKS=1`, `IMAGES=1`, `VOLUMES=1`, `INFO=1`, `EVENTS=1`, `PING=1`, `VERSION=1`.
* **Network**: Attached exclusively to `socket-net`. Application containers in `~/Sites` are never attached to `socket-net`.

### C. Identity & Directory Services (`lldap` & `authelia`)
* **`lldap`**: Lightweight LDAP directory service storing user accounts, password hashes, and groups. Web UI exposed at `users.${HOMELAB_DOMAIN}` (protected by Authelia). SQLite database stored in `config/lldap/data/users.db`.
* **`authelia`**: Authentication and SSO gateway (`auth.${HOMELAB_DOMAIN}`).
  - Configured with an LDAP authentication backend querying `ldap://lldap:3890`.
  - Exposes the ForwardAuth endpoint (`/api/verify?rd=https://auth.${HOMELAB_DOMAIN}/`), registered as the `authelia-auth@docker` Traefik middleware.
  - Passes user identity headers (`Remote-User`, `Remote-Groups`, `Remote-Email`, `Remote-Name`) upon successful session verification.

### D. Mail Subsystem (`stalwart`)
* **`stalwart`**: Core mail transport and storage server handling inbound SMTP (25), SMTPS (465), SMTP Submission (587), IMAPS (993), and ManageSieve (4190).
  - WebAdmin & JMAP endpoint accessible at `smtp.${HOMELAB_DOMAIN}` via Traefik.
  - Authenticates users against the LLDAP directory.
  - Outbound mail relay configured to route through an external smart host (Brevo SMTP relay via SOPS secrets).
  - *(Note: User-facing webmail, such as SnappyMail, runs as an independent Class B workload in `~/Sites/snappymail` with isolated state under `~/var/lib/homelab/snappymail/`.)*

### E. Portal & Observability (`homepage`, `dozzle`)
* **`homepage`**: Service dashboard at `dashboard.${HOMELAB_DOMAIN}`. Synchronized from `app.yaml` manifests in `~/Sites` by `appctl sync`.
* **`dozzle`**: Real-time container log viewer at `logs.${HOMELAB_DOMAIN}`, connecting read-only through `socket-proxy`.

### F. Host Foundation & Daemons
* **`homeserver-core.service`**: Systemd unit managing the Core Docker Compose stack (`~/Core/docker-compose.yml`) using secrets projected to `/run/secrets/homeserver.env`.
* **`homelab-gitops.service`**: Systemd unit listening on port 9000 executing `scripts/gitops_dispatcher.py` under strict systemd sandboxing (`ProtectSystem=strict`, `ProtectHome=read-only`).
* **`dynu-monitor.service` & `dynu-monitor.timer`**: IP change monitor executing `nixos/scripts/monitor.py` every 30 minutes to detect WAN IP changes and update DNS via `ddclient`.

---

## 3. Network Segmentation

Homelab Core configures two isolated Docker bridge networks:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                              HOST DOCKER ENGINE                        │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   ┌──────────────────────────────────────────────────────────────┐     │
│   │                      proxy-net (Bridge)                      │     │
│   │                                                              │     │
│   │  • traefik (router)              • authelia (sso)            │     │
│   │  • lldap (directory)             • stalwart (mail)           │     │
│   │  • homepage (dashboard)          • dozzle (logs)             │     │
│   │  • All User Workloads (~/Sites/*)                            │     │
│   └──────────────────────────────┬───────────────────────────────┘     │
│                                  │                                     │
│   ┌──────────────────────────────┴───────────────────────────────┐     │
│   │                      socket-net (Bridge)                     │     │
│   │                                                              │     │
│   │  • socket-proxy (HAProxy :2375)  • traefik                   │     │
│   │  • dozzle                        • watchtower                │     │
│   │  • diun                          • homepage                  │     │
│   └──────────────────────────────┬───────────────────────────────┘     │
│                                  │ (ro mount)                          │
│                                  ▼                                     │
│                         /var/run/docker.sock                           │
└────────────────────────────────────────────────────────────────────────┘
```

1. **`proxy-net`**: Ingress routing network. Traefik directs traffic to backend service containers on this network. Workload containers in `~/Sites` join `proxy-net`.
2. **`socket-net`**: Isolated Docker socket proxy network. Only Core infrastructure components needing read-only container metadata join this network.

---

## 4. Host Firewall & Port Exposure

The host firewall is configured in `nixos/modules/homeserver.nix`:

| Port | Protocol | Service | Description | Exposure |
| :--- | :--- | :--- | :--- | :--- |
| **80** | TCP | Traefik | HTTP Ingress (Redirects to 443) | Public |
| **443** | TCP | Traefik | HTTPS Ingress (TLS Termination) | Public |
| **22** | TCP | OpenSSH | Host SSH Access (`PermitRootLogin = "no"`) | Host Access |
| **2223** | TCP | Gitea SSH | Git over SSH for Workloads & Gitea | Public |
| **9000** | TCP | GitOps Webhook | GitOps Webhook Receiver | Public / Gitea |
| **25** | TCP | Stalwart | Inbound SMTP | Public |
| **465** | TCP | Stalwart | SMTPS (Implicit TLS) | Public |
| **587** | TCP | Stalwart | SMTP Submission (STARTTLS) | Public |
| **993** | TCP | Stalwart | IMAPS (Secure IMAP) | Public |
| **4190** | TCP | Stalwart | ManageSieve (Mail Filters) | Public |

Application containers in `~/Sites` do not bind host ports directly; they receive traffic routed by Traefik over `proxy-net`.

---

## 5. Workload Integration Model

Workloads in `~/Sites/<app>` integrate with Core through three mechanisms:

1. **Manifest Metadata (`app.yaml`)**: Validated by `scripts/core_manifest.py` (Schema v1.0). Declares canonical identity, routed domain, upstream source provenance (`source.url`, `source.ref`), environment defaults, Homepage card layout, and allowlisted GitOps deployment policy.
2. **Compose Configuration (`docker-compose.yml`)**:
   - Attaches to the external `proxy-net` network.
   - Declares Traefik routing labels:
     ```yaml
     labels:
       - "traefik.enable=true"
       - "traefik.http.routers.${CONTAINER_NAME}.rule=Host(`${SERVICE_DOMAIN}`)"
       - "traefik.http.routers.${CONTAINER_NAME}.entrypoints=websecure"
       - "traefik.http.routers.${CONTAINER_NAME}.tls=true"
       - "traefik.http.routers.${CONTAINER_NAME}.service=${CONTAINER_NAME}-svc"
       - "traefik.http.routers.${CONTAINER_NAME}-red.rule=Host(`${SERVICE_DOMAIN}`)"
       - "traefik.http.routers.${CONTAINER_NAME}-red.entrypoints=web"
       - "traefik.http.routers.${CONTAINER_NAME}-red.middlewares=https-redirect@docker"
       - "traefik.http.services.${CONTAINER_NAME}-svc.loadbalancer.server.port=8080"
       # If auth is required:
       - "traefik.http.routers.${CONTAINER_NAME}.middlewares=authelia-auth@docker"
     ```
3. **Execution & State Isolation**: `appctl` and `gitops_dispatcher.py` validate the manifest and Compose AST (blocking privilege escalation and out-of-bounds mounts) before supplying runtime environment variables (`SERVICE_DOMAIN`, `CONTAINER_NAME`, and projected secrets). Persistent mutable workload data resides strictly under `~/var/lib/homelab/<app>/`.
