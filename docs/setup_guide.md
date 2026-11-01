# 🚀 Homelab Core — Setup & Provisioning Guide

This document describes procedures for provisioning a host machine, bootstrapping **Homelab Core**, and onboarding new application workloads into `~/Sites`.

---

## 1. Host Provisioning & Bootstrapping

### A. Fresh Machine Provisioning via NixOS Install
When provisioning bare-metal hardware or a virtual machine from a NixOS Minimal ISO:

1. Partition and format disks according to `nixos/hardware-configuration.nix` (e.g. boot EFI partition and root filesystem).
2. Mount partitions under `/mnt`.
3. **Restore Host Secrets Key**: Copy the host SSH private key to `/mnt/etc/ssh/ssh_host_ed25519_key` (mode `0600`). This is required because `sops-nix` decrypts `nixos/secrets.yaml` during system activation.
4. Clone the Core repository to `/mnt/home/<operator>/Core`.
5. Run the NixOS installer:
   ```bash
   nixos-install --flake /mnt/home/<operator>/Core#server
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

### Step 2: Configure Appliance Instance Domain (Decision D1)
To preserve strict platform genericism, the repository files tracked in Git never contain production domains or personal operator identities, defaulting to RFC 2606 placeholders (`example.test` and `operator`).

An appliance host supplies its production domain and local configuration overrides via one of two untracked dual-path files:
1. **Repository-local instance profile (`nixos/instance.nix`)**:
   Create an untracked, git-ignored file in `nixos/instance.nix`:
   ```nix
   {
     homelab.domain = "example.com";
   }
   ```
   When rebuilding via `sudo nixos-rebuild switch --flake path:/home/operator/Core#server --impure` (or `path:~/Core#server --impure`), Nix automatically imports `nixos/instance.nix` if present.

   > [!NOTE]
   > **Flake Fetcher Scheme for Untracked Files**:
   > Because `nixos/instance.nix` is gitignored, Nix's default Git fetcher filters it out when evaluating standard Git flake references (e.g. `--flake ~/Core#server`). Prefixing the path with the `path:` scheme (e.g. `path:/home/operator/Core#server` or `path:~/Core#server`) instructs Nix to evaluate directly from the filesystem without filtering by the Git index.
   > If using the host-level `/etc/nixos/homelab-instance.nix` instead, a bare `--flake .#server --impure` works directly because `/etc/nixos` resides outside the Git repository tree.

2. **Host-level instance profile (`/etc/nixos/homelab-instance.nix`)**:
   Alternatively, create a host-level file outside the Git checkout:
   ```nix
   {
     homelab.domain = "example.com";
   }
   ```
   This persists configuration across fresh repository clones or ephemeral branches.

Both paths are declared optionally in `nixos/configuration.nix`:
```nix
imports = [
  ./hardware-configuration.nix
  ./modules/homeserver.nix
  ./modules/traefik-deployments.nix
  ./modules/dynu.nix
  ./modules/shell.nix
] ++ lib.optional (builtins.pathExists ./instance.nix) ./instance.nix
  ++ lib.optional (builtins.pathExists /etc/nixos/homelab-instance.nix) /etc/nixos/homelab-instance.nix;
```
When present, `homelab.domain` overrides the default (`example.test`), and all generated templates (`homeserver.env`, Traefik routing rules, and the dynamic `LLDAP_LDAP_BASE_DN`) automatically reflect the production domain without altering any tracked Git files.

### Step 3: Apply Declarative NixOS Configuration
```bash
cd ~/Core
sudo nixos-rebuild switch --flake path:/home/operator/Core#server --impure
```
This configures:
* Docker daemon, host firewall rules, and system packages.
* Decrypted secret files in `/run/secrets/homeserver.env` and `/run/secrets/traefik-deployments.env`.
* Systemd service units (`homeserver-core`, `homelab-appctl`, `homelab-gitops`, `dynu-monitor`).

### Step 4: Create External Docker Networks
The Core Compose stack and workloads require the `proxy-net` and `socket-net` bridge networks:
```bash
docker network create proxy-net 2>/dev/null || true
docker network create socket-net 2>/dev/null || true
```

### Step 5: Start Platform Services
```bash
sudo systemctl start homeserver-core
```
Check running containers:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

### Step 6: Verify DNS & Certificates
1. Trigger an immediate DDNS update:
   ```bash
   sudo systemctl start dynu-monitor
   ```
2. Verify Let's Encrypt wildcard certificate generation:
   ```bash
   appctl ssl traefik
   ```

### Step 7: Initialize LLDAP Directory
1. Navigate to `https://users.${HOMELAB_DOMAIN}` (e.g., `https://users.example.com`).
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
schema_version: "1.0"
name: "myapp"
aliases:
  - "app"
domain: "myapp.example.com"
description: "My New Application Service"
visible: true
auth: false
networks:
  - proxy-net

env:
  APP_PORT: "8080"

deployment:
  branch: "main"
  strategy: "compose"
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
