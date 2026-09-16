# 🏛️ Homelab Core — Architecture & System Design

This document details the architectural structure, component roles, networking models, and security controls of the **Homelab Core** platform for `roadtotech.me`.

---

## 1. System Structure: Ownership vs. Runtime Layers vs. Orchestration

To understand how the system is organized, three architectural dimensions are distinguished:

### A. Ownership Boundary
* **Core (`~/Core`) owns**: NixOS host configuration, shared platform services, edge ingress, identity boundaries, secrets distribution, and deployment orchestration mechanics.
* **Workload Plane (`~/Sites`) owns**: Independent application codebases, application-specific Docker Compose files, application-specific `app.yaml` metadata, and application data/state.

### B. Four Runtime Layers
The runtime environment consists of four functional layers:

```
┌────────────────────────────────────────────────────────────────────────┐
│ 1. EDGE GATEWAY & INGRESS                                              │
│    Traefik (v3.6) · Let's Encrypt Wildcard TLS · Dynu DNS-01 Provider  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. CONTROL & PLATFORM SERVICES (Core Compose Stack)                   │
│    Authelia SSO · LLDAP Directory · Stalwart Mail · SnappyMail         │
│    Homepage Dashboard · Portainer · Dozzle · Watchtower · Diun         │
│    socket-proxy (Read-Only Docker API Barrier)                         │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. WORKLOAD PLANE (~/Sites)                                            │
│    Autonomous Workload Repositories (Jellyfin, Docs, Gitea, DBs, etc.)  │
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
Orchestration coordinates operations across the runtime layers without being a runtime layer itself:
* **`appctl` CLI & Metadata Engine (`scripts/appctl`, `scripts/appctl_engine.py`)**: Slices across the Host Foundation (sourcing secrets from `/run/secrets/`), Workload Plane (parsing `~/Sites/*/app.yaml` and driving Docker Compose), and Platform Services (recompiling `config/homepage/services.yaml`).
* **GitOps Webhook Dispatcher (`scripts/gitops_dispatcher.py`)**: Governed by the `homelab-gitops.service` systemd unit, it receives webhooks on port 9000, enforces admission policies, and dispatches serialized deployment actions for workloads or data repositories.

---

## 2. Component Roles & Operational Boundaries

### A. Edge Gateway (`traefik`)
* **Role**: Ingress proxy for incoming HTTP (80) and HTTPS (443) traffic.
* **TLS Management**: Employs Traefik's native ACME resolver using the Dynu DNS-01 challenge provider (`myresolver`) to obtain and renew wildcard certificates for `*.roadtotech.me` and `roadtotech.me`. Certificate data is stored at `config/letsencrypt/acme.json` (mode `0600`).
* **Redirection**: Automatically upgrades HTTP traffic to HTTPS via the `https-redirect@docker` middleware.
* **Dynamic Routing**: Discovers routers and services dynamically by reading Docker container labels via `socket-proxy` (`tcp://socket-proxy:2375`).

### B. Security Barrier (`socket-proxy`)
* **Role**: Isolates the host Docker daemon socket from direct container access.
* **Access Rules**:
  - Mounted read-only: `/var/run/docker.sock:/var/run/docker.sock:ro`.
  - State-modifying and execution endpoints disabled: `POST=0`, `DELETE=0`, `BUILD=0`, `EXEC=0`, `COMMIT=0`, `CONFIGS=0`, `DISTRIBUTION=0`, `NODES=0`, `PLUGINS=0`, `SECRETS=0`, `SWARM=0`, `SYSTEM=0`.
  - Read-only inspection endpoints enabled: `CONTAINERS=1`, `NETWORKS=1`, `SERVICES=1`, `TASKS=1`, `IMAGES=1`, `VOLUMES=1`, `INFO=1`, `EVENTS=1`, `PING=1`, `VERSION=1`.
* **Network**: Attached exclusively to `socket-net`. Application containers in `~/Sites` are not attached to `socket-net`.

### C. Identity & Directory Services (`lldap` & `authelia`)
* **`lldap`**: Lightweight LDAP directory service storing user accounts, password hashes, and groups. Web UI exposed at `users.roadtotech.me` (protected by Authelia). SQLite database stored in `config/lldap/data/users.db`.
* **`authelia`**: Authentication and SSO gateway (`auth.roadtotech.me`).
  - Configured with an LDAP authentication backend querying `ldap://lldap:3890`.
  - Exposes the ForwardAuth endpoint (`/api/verify?rd=https://auth.roadtotech.me/`), registered as the `authelia-auth@docker` Traefik middleware.
  - Passes user identity headers (`Remote-User`, `Remote-Groups`, `Remote-Email`, `Remote-Name`) upon successful session verification.

### D. Mail Subsystem (`stalwart` & `snappymail`)
* **`stalwart`**: All-in-one mail server handling inbound SMTP (25), SMTPS (465), SMTP Submission (587), IMAPS (993), and ManageSieve (4190).
  - WebAdmin & JMAP UI accessible at `mail.roadtotech.me` via Traefik.
  - Authenticates users against the LLDAP directory.
  - Outbound mail relay configured to route through an external smart host (Brevo SMTP relay via SOPS secrets).
* **`snappymail`**: Webmail client accessible at `webmail.roadtotech.me` routing to Stalwart.

### E. Portal & Observability (`homepage`, `portainer`, `dozzle`)
* **`homepage`**: Service dashboard at `dashboard.roadtotech.me`. Synchronized from `app.yaml` manifests in `~/Sites` by `appctl sync`.
* **`portainer`**: Container management interface at `portainer.roadtotech.me`, connecting to Docker through `socket-proxy`.
* **`dozzle`**: Log viewing interface at `logs.roadtotech.me`, connecting through `socket-proxy`.

### F. Host Foundation & Daemons
* **`homeserver-core.service`**: Systemd unit managing the Core Docker Compose stack (`/home/kiskaadee/Core/docker-compose.yml`) using secrets projected to `/run/secrets/homeserver.env`.
* **`homelab-gitops.service`**: Systemd unit listening on port 9000 executing `scripts/gitops_dispatcher.py`.
* **`dynu-monitor.service` & `dynu-monitor.timer`**: IP change monitor executing `nixos/scripts/monitor.py` every 30 minutes to detect WAN IP changes and update DNS via `ddclient`.

---

## 3. Network Segmentation

Homelab Core configures two bridge networks:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                              HOST DOCKER ENGINE                         │
├────────────────────────────────────────────────────────────────────────┤
│                                                                        │
│   ┌──────────────────────────────────────────────────────────────┐     │
│   │                      proxy-net (Bridge)                      │     │
│   │                                                              │     │
│   │  • traefik (router)              • authelia (sso)            │     │
│   │  • lldap (directory)             • stalwart (mail)           │     │
│   │  • snappymail (webmail)          • homepage (dashboard)      │     │
│   │  • portainer (management)        • dozzle (logs)             │     │
│   │  • All User Workloads (~/Sites/*)                            │     │
│   └──────────────────────────────┬───────────────────────────────┘     │
│                                  │                                     │
│   ┌──────────────────────────────┴───────────────────────────────┐     │
│   │                      socket-net (Bridge)                     │     │
│   │                                                              │     │
│   │  • socket-proxy (HAProxy :2375)  • traefik                   │     │
│   │  • portainer                     • dozzle                    │     │
│   │  • watchtower                    • diun                      │     │
│   │  • homepage                                                  │     │
│   └──────────────────────────────┬───────────────────────────────┘     │
│                                  │ (ro mount)                          │
│                                  ▼                                     │
│                         /var/run/docker.sock                           │
└────────────────────────────────────────────────────────────────────────┘
```

1. **`proxy-net`**: Ingress routing network. Traefik directs traffic to backend service containers on this network. Workload containers in `~/Sites` join `proxy-net`.
2. **`socket-net`**: Isolated Docker socket proxy network. Only Core infrastructure components needing container metadata join this network.

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

Workloads in `~/Sites/<app>` integrate with Core through two mechanisms:

1. **Manifest Metadata (`app.yaml`)**: Declares identity, routed domain, environment variable defaults, homepage card layout, and deployment policy.
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
3. **Execution**: The `appctl` CLI and GitOps dispatcher supply environment variables (such as `SERVICE_DOMAIN`, `CONTAINER_NAME`, and projected secrets) when invoking Docker Compose.
