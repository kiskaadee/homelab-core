# 🛠️ Homelab Core — Operator Runbook & Procedures

This document contains operational procedures, system maintenance workflows, diagnostic commands, and recovery runbooks for administering **Homelab Core**.

---

## 1. System Lifecycle & NixOS Administration

The host system runs NixOS configured declaratively via the flake in `~/Core`.

### Rebuilding the System Configuration
After modifying any file in `nixos/` (including `configuration.nix`, `hardware-configuration.nix`, or any `nixos/modules/*.nix`), apply changes to the running host:

```bash
# Using the built-in shell alias:
nix-switch

# Or using the explicit command:
sudo nixos-rebuild switch --flake ~/Core#server
```

### Dry Build Validation & Rollback
```bash
# Validate build without activating (catches evaluation and syntax errors)
nix flake check
nix build .#nixosConfigurations.server.config.system.build.toplevel --no-link

# Roll back to the previous generation if needed
sudo nixos-rebuild switch --rollback
```

### System Maintenance & Disk Space Management
```bash
# Check disk usage across partitions
df -h

# Delete older system generations (older than 7 days)
sudo nix-env --delete-generations +7d --profile /nix/var/nix/profiles/system

# Prune unreachable Nix store paths
sudo nix-collect-garbage -d

# Prune old Docker resources (only containers stopped for >7 days)
docker system prune -a --volumes --filter "until=168h"
```

---

## 2. Managing Core Systemd Services

Homelab Core runs several platform services managed by systemd:

### `homeserver-core.service` (Core Compose Stack)
Manages the lifecycle of Traefik, Authelia, LLDAP, Stalwart, SnappyMail, Homepage, Dozzle, Watchtower, and Diun.

```bash
# Check service status
systemctl status homeserver-core

# View startup and runtime logs
journalctl -u homeserver-core -f

# Restart the entire Core stack
sudo systemctl restart homeserver-core

# Stop or Start the Core stack
sudo systemctl stop homeserver-core
sudo systemctl start homeserver-core
```

### `homelab-gitops.service` (Dynamic GitOps Webhook)
Listens on port 9000 for Gitea webhook requests and dispatches automated deployments.

```bash
# Check status and port binding
systemctl status homelab-gitops

# Stream webhook reception and deployment worker logs
journalctl -u homelab-gitops -f -n 50

# Restart the webhook dispatcher daemon
sudo systemctl restart homelab-gitops
```

### `dynu-monitor.service` & `dynu-monitor.timer` (Smart DDNS)
Runs every 30 minutes to detect WAN IP address changes and trigger `ddclient`.

```bash
# Check next scheduled timer execution
systemctl list-timers dynu-monitor.timer

# Check last monitor run status and output
systemctl status dynu-monitor.service

# Manually trigger an immediate IP check
sudo systemctl start dynu-monitor.service

# View historical WAN IP transitions log
tail -n 20 /var/lib/dynu/ip_history.jsonl
```

---

## 3. Workload Lifecycle Operations (`appctl`)

Applications in `~/Sites` are managed with the `appctl` CLI.

### Inspection & Status
```bash
# Check status of all applications and their Git tracking state
appctl list

# Fetch remote changes from Git remotes before rendering status
appctl list --fetch

# Check applications and Core infrastructure services together
appctl list --core

# Check SSL/TLS certificate validity across all domains
appctl list --ssl --core

# Inspect diagnostic details for a specific service
appctl info docs
appctl info jellyfin --ssl
```

### Deployment & Lifecycle Actions
```bash
# Start a service stack
appctl up docs

# Gracefully stop a service stack
appctl down docs

# Restart a service stack (re-injects environment)
appctl restart docs

# Restart an individual Core component without restarting the whole host
appctl restart traefik
appctl restart authelia

# Sequential stack update (clean working tree check -> git pull -> compose pull -> up -> sync)
appctl update docs
appctl update core

# Stream live container logs
appctl logs docs
appctl logs -f stalwart

# View resolved Docker Compose configuration
appctl config docs
```

---

## 4. User Identity & Access Management (LLDAP & Authelia)

User accounts and group memberships are managed dynamically in **LLDAP** rather than static configuration files.

### Accessing the Directory
1. Open `https://users.roadtotech.me` in your browser.
2. Authenticate using your admin credentials (`admin` / password configured in `nixos/secrets.yaml`).
3. From the LLDAP UI, you can:
   - Create and delete user accounts.
   - Reset user passwords.
   - Manage user email addresses.
   - Assign users to groups (e.g. `admin`, `users`, `media`).

### Authelia Session & Authentication
* **URL**: `https://auth.roadtotech.me`
* **Session Lifetime**: 7 days maximum session, 3 days inactivity timeout.
* **ForwardAuth Guard**: Applications requiring authentication attach the `authelia-auth@docker` middleware.

---

## 5. Troubleshooting Common Failure Modes

### A. SSL/TLS Certificate Failures (Let's Encrypt / Dynu)
* **Symptom**: Browser reports `NET::ERR_CERT_AUTHORITY_INVALID` or self-signed Traefik default certificate.
* **Diagnosis**:
  1. Verify certificate status: `appctl ssl <service>` or `appctl list --ssl`.
  2. Inspect Traefik logs: `appctl logs traefik | grep -i acme`.
  3. Verify Dynu API key is valid and not rate-limited.
  4. Check `config/letsencrypt/acme.json` permissions (must be `0600`).
* **Recovery**:
  ```bash
  # Check acme.json permissions
  ls -la ~/Core/config/letsencrypt/acme.json
  chmod 600 ~/Core/config/letsencrypt/acme.json

  # Restart Traefik to trigger challenge retry
  appctl restart traefik
  ```

### B. Dynamic DNS (WAN IP) Drift
* **Symptom**: External domain `roadtotech.me` does not resolve to the current ISP WAN address.
* **Diagnosis**:
  ```bash
  # Check current public IP vs resolved DNS IP
  curl -s https://api.ipify.org
  dig +short roadtotech.me @1.1.1.1

  # Check dynu-monitor logs
  systemctl status dynu-monitor
  journalctl -u dynu-monitor -n 30
  ```
* **Recovery**:
  ```bash
  # Trigger manual IP update run
  sudo systemctl start dynu-monitor
  ```

### C. Docker Socket Communication Errors
* **Symptom**: Traefik fails to discover routers, or Dozzle shows "Cannot connect to Docker daemon".
* **Diagnosis**:
  ```bash
  # Verify socket-proxy is running and healthy
  docker ps -f name=socket-proxy

  # Verify socket-proxy logs
  appctl logs socket-proxy

  # Test internal socket-proxy connectivity from another container
  docker exec -it traefik wget -qO- http://socket-proxy:2375/version
  ```
* **Recovery**:
  ```bash
  appctl restart socket-proxy
  appctl restart traefik
  ```

### D. GitOps Deployment Stuck or Failed
* **Symptom**: Pushed commit to Gitea did not deploy or container is in an inconsistent state.
* **Diagnosis**:
  ```bash
  # Check webhook daemon logs
  journalctl -u homelab-gitops -f -n 50

  # Check execution status file for the target service
  cat ~/.local/state/homelab/gitops/<service>.status.json

  # Check if a lock file is held
  ls -la ~/.local/state/homelab/gitops/
  ```
* **Recovery**:
  ```bash
  # Manually run deployment via appctl
  appctl update <service>
  ```

---

## 6. Backup & Disaster Recovery

### Critical Persistent State Locations
To back up the homelab, create secure snapshots of the following state directories:

| Component | Host Path | Description |
| :--- | :--- | :--- |
| **Secrets Key** | `/etc/ssh/ssh_host_ed25519_key` | Host SSH key used by sops-nix to decrypt secrets |
| **Encrypted Secrets** | `~/Core/nixos/secrets.yaml` | Encrypted secrets source |
| **SSL Certificates** | `~/Core/config/letsencrypt/acme.json` | Active Let's Encrypt certificates |
| **User Directory** | `~/Core/config/lldap/data/` | LLDAP SQLite database (`users.db`) |
| **Auth Database** | `~/Core/config/authelia/db.sqlite3` | Authelia session database |
| **Mail Storage** | `~/Core/config/stalwart/` | Stalwart database, mailboxes, and DKIM keys |
| **Webmail State** | `~/Core/config/snappymail/data/` | SnappyMail configuration and sessions |
| **Dashboard State** | `~/Core/config/homepage/` | Homepage configuration and custom icons |

### Host Recovery Requirements
To bring up a replacement host from version control:
1. Boot the target system with a NixOS installation medium and configure partitions according to `nixos/hardware-configuration.nix`.
2. **Restore the host SSH private key** to `/etc/ssh/ssh_host_ed25519_key` (mode `0600`) before running `nixos-install`, as `sops-nix` requires this key during system evaluation and activation.
3. Install NixOS targeting the server flake:
   ```bash
   nixos-install --flake /mnt/home/kiskaadee/Core#server
   reboot
   ```
4. Restore persistent data directories (`config/` backups) and start the Core stack:
   ```bash
   sudo systemctl start homeserver-core
   ```
