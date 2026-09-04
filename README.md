# 🌐 Traefik Homelab Core

Welcome to the **Traefik Homelab Core Control Plane** — the central orchestrator, security perimeter, and edge gateway for the `roadtotech.me` homelab cluster.

This repository implements a **Hardened Hub** architecture where routing, SSL termination, identity management, container isolation, and orchestration are managed centrally, while user applications live in independent repositories in `~/Sites`.

---

## 🏗️ Architectural Overview & Component Roles

The infrastructure is strictly divided into two operational tiers:

```
                          Internet (roadtotech.me)
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │    Traefik Edge Proxy (:443)   │
                    │  (Wildcard Let's Encrypt TLS) │
                    └───────┬───────────────┬───────┘
                            │               │
        Public Traffic      │               │ Auth Guarded
                            ▼               ▼
                   [ Landing / Public ]  [ Authelia SSO / 2FA ]
                            │               │
                            │               ▼
                            └───────► [ Data Plane Apps ]
                                      (Sites: jellyfin, docs, etc.)
```

### 1. Control Plane Components (`~/Core`)
* **`Traefik` (v3.6):** The edge reverse proxy and SSL terminator. Automatically obtains and maintains Let's Encrypt wildcard certificates (`*.roadtotech.me`, `roadtotech.me`) using the Dynu DNS-01 challenge. Routes incoming HTTP/HTTPS traffic to internal Docker containers over `proxy-net`.
* **`socket-proxy` (tecnativa):** Security barrier. Prevents Traefik and other containers from having direct access to `/var/run/docker.sock`, restricting access exclusively to safe container read operations via TCP (`socket-net`).
* **`Authelia`:** Single Sign-On (SSO) and Multi-Factor Authentication (2FA) identity gateway. Provides ForwardAuth middleware (`authelia-auth@docker`) that gates administrative dashboards and sensitive applications before traffic touches them.
* **`Portainer CE`:** Visual GUI for container inspection, stack lifecycle management, and log tracking.
* **`Dozzle`:** Lightweight real-time log viewer for all running containers, protected by Authelia SSO.
* **`Watchtower` & `Diun`:** Background daemons responsible for container image update notifications and automated updates.

### 2. Data Plane Applications (`~/Sites`)
User and service applications (e.g. `dashboard`, `docs`, `jellyfin`, `excalidraw`, `gitea`, `mermaid`, `minecraft`, `ollama`, `pgsql`, `mongodb`) live in isolated, dedicated git repositories under `~/Sites`. They integrate with the Core control plane dynamically via the `proxy-net` Docker network and Traefik container labels.

---

## 🔒 Security Model & Secrets Architecture

The security model is built on zero hardcoded secrets and defense-in-depth:

```
[~/Config/hosts/desktop/secrets.yaml] (Encrypted with SOPS + age keys)
       │
       ▼ (Decrypted at boot by sops-nix using SSH host key)
[/run/secrets/rendered/homeserver.env] & [/run/secrets/rendered/traefik-deployments.env]
       │
       ▼ (Fed via --env-file and appctl)
[Core & Sites Containers]
```

1. **SOPS & sops-nix Integration:** All sensitive tokens (Dynu API key, Authelia session secrets, JWT keys, database passwords) are encrypted in `~/Config/hosts/desktop/secrets.yaml` and decrypted by NixOS at runtime into in-memory `/run/secrets/rendered/`.
2. **Edge TLS & HSTS:** All external traffic is forced over HTTPS using Let's Encrypt wildcard certificates with strict redirect schemes (`https-redirect@docker`).
3. **Authelia ForwardAuth:** Applications that require authentication declare `traefik.http.routers.<name>.middlewares=authelia-auth@docker`, delegating identity verification to Authelia.
4. **Socket Isolation:** Direct Docker daemon sockets are completely hidden behind `socket-proxy`.

---

## 🚀 Orchestration with `appctl`

Applications in `~/Sites` are managed using the custom `appctl` CLI tool located in `scripts/appctl`.

### Key Commands:
```bash
# List all application stacks with runtime container health and Git sync status
appctl list

# Fetch upstream git changes across all repositories before rendering status
appctl list --fetch

# List all applications AND core control plane services
appctl list --core

# Display comprehensive metadata, routing, environment, and Git diagnostics
appctl info docs
appctl info jellyfin

# Start, stop, or restart an application stack
appctl up docs
appctl down jellyfin
appctl restart dash

# Full atomic stack upgrade (clean check -> git pull --ff-only -> docker compose pull -> docker compose up -d -> appctl sync)
appctl update docs
appctl update core
appctl update all

# Validate resolved docker compose configuration
appctl config docs

# Synchronize Homepage dashboard services.yaml from app.yaml manifests
appctl sync
```

---

## 📦 Adding a New Application Stack

Adding a new application to the homelab is completely decentralized:

### 1. Create the App Directory
```bash
mkdir -p ~/Sites/homelab-myapp
cd ~/Sites/homelab-myapp
git init
```

### 2. Create `app.yaml` Manifest
Define the application metadata:
```yaml
name: "myapp"
aliases:
  - "app"
domain: "myapp.roadtotech.me"
description: "My Awesome New Homelab App"
visible: true
auth: false
networks:
  - proxy-net
env:
  CUSTOM_VAR: "value"
homepage:
  title: "My App"
  group: "Knowledge & Notes"
  icon: "custom.png"
  container: "myapp"
  weight: 50
```

### 3. Create `docker-compose.yml`
```yaml
services:
  myapp:
    image: myapp/image:latest
    container_name: ${CONTAINER_NAME:-myapp}
    restart: unless-stopped
    networks:
      - proxy-net
    labels:
      - "traefik.enable=true"
      # HTTPS Router
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.rule=Host(`${SERVICE_DOMAIN}`)"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.entrypoints=websecure"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.tls=true"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.service=${CONTAINER_NAME:-myapp}-svc"
      # HTTP to HTTPS Redirect
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.rule=Host(`${SERVICE_DOMAIN}`)"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.entrypoints=web"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.middlewares=https-redirect@docker"
      # Service Target Port
      - "traefik.http.services.${CONTAINER_NAME:-myapp}-svc.loadbalancer.server.port=8080"

networks:
  proxy-net:
    name: ${PROXY_NETWORK:-proxy-net}
    external: true
```

### 4. Deploy and Sync
```bash
appctl up myapp
```
`appctl` will start the container, inject standard environment variables, and automatically recompile [`homelab-dashboard/config/services.yaml`](file:///home/kiskaadee/Sites/homelab-dashboard/config/services.yaml).

---

## ⚙️ NixOS Service Management

The Core Control Plane runs as a native systemd unit managed declaratively by NixOS:

* **Service Unit:** `homeserver-core.service`
* **Nix Module:** [`~/Config/hosts/desktop/homeserver.nix`](file:///home/kiskaadee/Config/hosts/desktop/homeserver.nix)
* **Status Inspection:** `systemctl status homeserver-core`
* **Logs:** `journalctl -u homeserver-core -f`
* **Restart:** `sudo systemctl restart homeserver-core`

---

## 📄 License
This repository is released into the public domain under the [Unlicense](LICENSE).
