# 🚀 Homelab Core — Setup & Provisioning Guide

This document describes procedures for provisioning a host machine, bootstrapping **Homelab Core**, and onboarding new application workloads into `~/Sites`.

---

## 1. Host Provisioning & Bootstrapping

### A. Fresh Machine Provisioning via NixOS Install
When provisioning bare-metal hardware or a virtual machine from a NixOS Minimal ISO:

1. Partition and format disks according to `nixos/hardware-configuration.nix` (e.g. boot EFI partition and root filesystem).
2. Mount partitions under `/mnt`.
3. **Restore Host Secrets Key**: Copy the host SSH private key to `/mnt/etc/ssh/ssh_host_ed25519_key` (mode `0600`). This is required because `sops-nix` decrypts `nixos/secrets.yaml` during system activation.
4. Clone the Core repository to `/mnt/home/kiskaadee/Core`.
5. Run the NixOS installer:
   ```bash
   nixos-install --flake /mnt/home/kiskaadee/Core#server
   reboot
   ```

---

## 2. Platform Bootstrap on a Running Host

When initializing the platform on an installed system:

### Step 1: Verify Host Secrets Key
Ensure the host SSH key exists with strict permissions:
```bash
sudo ls -l /etc/ssh/ssh_host_ed25519_key
sudo chmod 600 /etc/ssh/ssh_host_ed25519_key
```

### Step 2: Apply Declarative NixOS Configuration
```bash
cd ~/Core
sudo nixos-rebuild switch --flake ~/Core#server
```
This configures:
* Docker daemon, host firewall rules, and system packages.
* Decrypted secret files in `/run/secrets/homeserver.env` and `/run/secrets/traefik-deployments.env`.
* Systemd service units (`homeserver-core`, `homelab-gitops`, `dynu-monitor`).

### Step 3: Create External Docker Networks
The Core Compose stack and workloads require the `proxy-net` and `socket-net` bridge networks:
```bash
docker network create proxy-net 2>/dev/null || true
docker network create socket-net 2>/dev/null || true
```

### Step 4: Start Platform Services
```bash
sudo systemctl start homeserver-core
```
Check running containers:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

### Step 5: Verify DNS & Certificates
1. Trigger an immediate DDNS update:
   ```bash
   sudo systemctl start dynu-monitor
   ```
2. Verify Let's Encrypt wildcard certificate generation:
   ```bash
   appctl ssl traefik
   ```

### Step 6: Initialize LLDAP Directory
1. Navigate to `https://users.roadtotech.me`.
2. Authenticate using `admin` and the password configured in `nixos/secrets.yaml` (`lldap/admin_password`).
3. Create standard user accounts and assign them to required groups.

---

## 3. Onboarding a New Application Workload

Applications live in isolated repositories under `~/Sites/`. To deploy a new service:

### Step 1: Create Repository Directory
```bash
mkdir -p ~/Sites/homelab-myapp
cd ~/Sites/homelab-myapp
git init
```

### Step 2: Create `app.yaml` Manifest
Create `~/Sites/homelab-myapp/app.yaml`:
```yaml
name: "myapp"
aliases:
  - "app"
domain: "myapp.roadtotech.me"
description: "My New Application Service"
visible: true
auth: false
networks:
  - proxy-net

env:
  APP_PORT: "8080"

deployment:
  branch: "main"
  actions:
    - git_pull
    - compose_up

homepage:
  title: "My App"
  group: "Applications"
  icon: "default.png"
  container: "myapp"
  weight: 50
```

### Step 3: Create `docker-compose.yml`
Create `~/Sites/homelab-myapp/docker-compose.yml`:
```yaml
services:
  myapp:
    image: myapp/image:latest
    container_name: ${CONTAINER_NAME:-myapp}
    restart: always
    environment:
      - PORT=${APP_PORT:-8080}
    networks:
      - proxy-net
    labels:
      - "traefik.enable=true"
      # HTTPS Router
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.rule=Host(`${SERVICE_DOMAIN}`)"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.entrypoints=websecure"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.tls=true"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}.service=${CONTAINER_NAME:-myapp}-svc"
      # HTTP -> HTTPS Redirect
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.rule=Host(`${SERVICE_DOMAIN}`)"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.entrypoints=web"
      - "traefik.http.routers.${CONTAINER_NAME:-myapp}-red.middlewares=https-redirect@docker"
      # Target Port
      - "traefik.http.services.${CONTAINER_NAME:-myapp}-svc.loadbalancer.server.port=${APP_PORT:-8080}"

networks:
  proxy-net:
    name: ${PROXY_NETWORK:-proxy-net}
    external: true
```

### Step 4: Launch and Sync
```bash
# Launch application stack via appctl
appctl up myapp

# Inspect application diagnostics
appctl info myapp
```

---

## 4. Verification

Before committing changes to Homelab Core, run the repository test runner:

```bash
cd ~/Core
./scripts/test
```
This runs:
* Code formatting and lint checks (`ruff`).
* Security and structural invariant tests (`pytest tests/`).
* Nix flake check (`nix flake check`).
