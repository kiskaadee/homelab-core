# Setup & Secrets Management Guide (Traefik Homelab)

This document provides an exhaustive operational guide for configuring, deploying, securing, and maintaining the **Traefik Homelab Core Control Plane** on NixOS.

---

## 🏗️ Architectural Overview

The core infrastructure runs as a consolidated Docker Compose stack in [`/home/kiskaadee/Core`](file:///home/kiskaadee/Core):

* **[`docker-compose.yml`](file:///home/kiskaadee/Core/docker-compose.yml)**: Root compose specification defining all control plane services (`traefik`, `authelia`, `portainer`, `dozzle`, `watchtower`, `diun`, `socket-proxy`).
* **[`config/`](file:///home/kiskaadee/Core/config/)**: Persistent state directories:
  * `config/letsencrypt/`: Contains SSL certificate storage (`acme.json`).
  * `config/authelia/`: Contains configuration (`configuration.yml`, `users.yml`) and authentication database (`db.sqlite3`).
  * `config/diun/`: Diun container update tracking database (`diun.db`).

---

## 🔒 Secrets Architecture & SOPS Flow

All sensitive tokens, encryption keys, and credentials are encrypted using **`sops-nix`** (Mozilla SOPS with age keys derived from the host SSH key). No plaintext secrets exist in any git repository.

### Secrets Lifecycle:

```text
[nixos/secrets.yaml] (Encrypted with Age)
       │
       ▼ (Decrypted at boot by sops-nix daemon)
1. [/run/secrets/rendered/homeserver.env]          --> Fed to Core Compose Stack
2. [/run/secrets/rendered/traefik-deployments.env] --> Fed to appctl & App Stacks
       │
       ▼
[Active Docker Containers]
```

---

## 🔑 Modifying and Adding Secrets

To update credentials (e.g. changing your Dynu API key or updating Authelia user passwords):

1. Navigate to the core repository:
   ```bash
   cd ~/Core
   ```
2. Decrypt and open the secrets file in your editor:
   ```bash
   sops nixos/secrets.yaml
   ```
3. Update or append keys under the root block:
   ```yaml
   dynu:
     api_key: "your_dynu_api_key"
   traefik:
     acme_email: "your_email@domain.com"
   authelia:
     session_secret: "secure_64_char_secret"
     storage_encryption_key: "secure_64_char_key"
     jwt_secret: "secure_64_char_jwt_secret"
     users:
       kiskaadee:
         password_hash: "$argon2id$v=19$m=65536..."
   ```
   *To generate an Argon2 password hash securely, run:*
   ```bash
   docker run --rm -it authelia/authelia:latest authelia crypto hash generate argon2
   ```
4. Save and close. SOPS will automatically encrypt the modified file before saving.
5. Rebuild the system to apply new secret templates:
   ```bash
   sudo nixos-rebuild switch --flake ~/Core#server
   ```

---

## ⚙️ Declarative NixOS Modules

The Core stack and deployment environments are defined declaratively in `nixos/modules/`:

### 1. Control Plane Module ([`nixos/modules/homeserver.nix`](../nixos/modules/homeserver.nix))
Generates `/run/secrets/rendered/homeserver.env`, `/run/secrets/rendered/users.yml`, and manages the `homeserver-core.service` and `homelab-gitops.service` systemd daemons.

### 2. Applications Secret Module ([`nixos/modules/traefik-deployments.nix`](../nixos/modules/traefik-deployments.nix))
Generates `/run/secrets/rendered/traefik-deployments.env` consumed by `appctl`.

### 3. Dynamic DNS Module ([`nixos/modules/dynu.nix`](../nixos/modules/dynu.nix))
Configures the smart IP monitor timer and updates ddclient when IP rotations occur.

### 4. Server Shell Module ([`nixos/modules/shell.nix`](../nixos/modules/shell.nix))
Defines interactive bash aliases, git helpers, and terminal tools via system `/etc/bashrc`.

---

## 🚀 Running and Managing the Stack

### Standard Systemd Operations:
* **Start:** `sudo systemctl start homeserver-core`
* **Stop:** `sudo systemctl stop homeserver-core`
* **Restart:** `sudo systemctl restart homeserver-core`
* **Status:** `systemctl status homeserver-core`
* **Live Logs:** `journalctl -u homeserver-core -f`

### Dynamic Tweaks without Rebuilding NixOS:
When making configuration adjustments to `docker-compose.yml` (e.g. tuning Traefik middlewares):
```bash
cd ~/Core
docker compose --env-file /run/secrets/rendered/homeserver.env up -d --remove-orphans
```
