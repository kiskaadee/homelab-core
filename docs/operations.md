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
sudo nixos-rebuild switch --flake path:/home/operator/Core#server --impure
```

> [!NOTE]
> **Flake Fetcher Scheme for Untracked Files**:
> When importing the gitignored `nixos/instance.nix`, Nix's default Git fetcher filters it out unless the `path:` scheme is used (`path:/home/operator/Core#server` or `path:~/Core#server`). The `path:` scheme instructs Nix to evaluate directly from the filesystem without filtering by the Git index.
> If using `/etc/nixos/homelab-instance.nix`, a standard `--flake ~/Core#server --impure` works directly since `/etc/nixos` is outside the repository.


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
Manages the lifecycle of Traefik, Authelia, LLDAP, Stalwart, Homepage, Dozzle, Watchtower, and Diun.

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
1. Open `https://users.${HOMELAB_DOMAIN}` (e.g., `https://users.example.com`) in your browser.
2. Authenticate using your admin credentials (`admin` / password configured in `nixos/secrets.yaml`).
3. From the LLDAP UI, you can:
   - Create and delete user accounts.
   - Reset user passwords.
   - Manage user email addresses.
   - Assign users to groups (e.g. `admin`, `users`, `media`).

### Authelia Session & Authentication
* **URL**: `https://auth.${HOMELAB_DOMAIN}` (e.g., `https://auth.example.com`)
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
* **Symptom**: External domain (`${HOMELAB_DOMAIN}`) does not resolve to the current ISP WAN address.
* **Diagnosis**:
  ```bash
  # Check current public IP vs resolved DNS IP
  curl -s https://api.ipify.org
  dig +short example.com @1.1.1.1

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
| **Workload State** | `~/var/lib/homelab/<app>/` | Per-workload persistent runtime state (e.g., `snappymail`) |
| **Dashboard State** | `~/Core/config/homepage/` | Homepage configuration and custom icons |

### Host Recovery Requirements
To bring up a replacement host from version control:
1. Boot the target system with a NixOS installation medium and configure partitions according to `nixos/hardware-configuration.nix`.
2. **Restore the host SSH private key** to `/etc/ssh/ssh_host_ed25519_key` (mode `0600`) before running `nixos-install`, as `sops-nix` requires this key during system evaluation and activation.
3. Install NixOS targeting the server flake:
   ```bash
   nixos-install --flake /mnt/home/<operator>/Core#server
   reboot
   ```
4. Restore persistent data directories (`config/` and `~/var/lib/homelab/` backups) and start the Core stack:
   ```bash
   sudo systemctl start homeserver-core
   ```

---

## 7. Operator Account Migration Runbook (Decision D2: Staged Coexistence Cutover)

> [!CAUTION]
> **MAINTENANCE WINDOW REQUIRED — EXECUTION ON LIVE HOST CURRENTLY BLOCKED**  
> Live execution of this runbook against production hosts is strictly blocked until an authorized platform maintenance window is scheduled. This runbook is codified for procedural readiness and dry-run verification only.

This runbook defines the 4-stage staged coexistence cutover procedure to migrate host operation from a legacy personal operator account (`<legacy-operator>`) to the platform standard `operator` user (`uid = 1000`, `extraGroups = [ "wheel" "docker" "homelab-operators" "networkmanager" ]`), fulfilling Decision D2 and eliminating personal identity coupling.

### Stage 1: Pre-Flight Checks
Before initiating data synchronization or service cutover, verify the target `operator` account environment:

1. **Verify `operator` account existence and home directory**:
   ```bash
   id operator
   # Expected output must include membership in: wheel, docker, homelab-operators
   ls -ld /home/operator
   ```

2. **Verify SSH key provisioning**:
   Ensure the operator's authorized keys are installed with strict permissions:
   ```bash
   sudo ls -la /home/operator/.ssh/authorized_keys
   sudo chmod 700 /home/operator/.ssh
   sudo chmod 600 /home/operator/.ssh/authorized_keys
   sudo chown -R operator:users /home/operator/.ssh
   ```

3. **Verify administrative (`sudo`) access**:
   Test that `operator` can authenticate and execute privileged commands without failure:
   ```bash
   sudo -u operator sudo -n true || echo "Passwordless or password sudo required for operator"
   ```

4. **Verify persistent directories**:
   Ensure `/home/operator/Sites` and `/home/operator/Core` target parent paths are ready:
   ```bash
   sudo mkdir -p /home/operator/{Core,Sites,.config/homelab}
   sudo chown -R operator:homelab-operators /home/operator/{Core,Sites,.config}
   ```

---

### Stage 2: State and Repository Synchronization
Synchronize repository checkouts, environment definitions, and runtime state from the legacy user account to `/home/operator/` using non-destructive archive flags (`-aHAX` preserves hard links, attributes, and extended ACLs):

1. **Initial background synchronization**:
   Seed the file trees for Core and Sites while services continue running:
   ```bash
   sudo rsync -aHAX --info=progress2 \
     --exclude=".venv" \
     --exclude=".pytest_cache" \
     /home/<legacy-operator>/Core/ /home/operator/Core/

   sudo rsync -aHAX --info=progress2 \
     /home/<legacy-operator>/Sites/ /home/operator/Sites/

   if [ -d "/home/<legacy-operator>/.config/homelab" ]; then
     sudo rsync -aHAX --info=progress2 \
       /home/<legacy-operator>/.config/homelab/ /home/operator/.config/homelab/
   fi
   ```

2. **Quiesce active database writers**:
   Stop the platform stack to guarantee point-in-time consistency across SQLite databases (e.g. LLDAP `users.db`, Authelia `db.sqlite3`, Stalwart storage) before the cutover sync:
   ```bash
   sudo systemctl stop homeserver-core
   ```

3. **Final delta synchronization (`--delete`)**:
   Perform a rapid final delta synchronization with `--delete` to capture the quiesced database state and prune removed files:
   ```bash
   sudo rsync -aHAX --delete --info=progress2 \
     --exclude=".venv" \
     --exclude=".pytest_cache" \
     /home/<legacy-operator>/Core/ /home/operator/Core/

   sudo rsync -aHAX --delete --info=progress2 \
     /home/<legacy-operator>/Sites/ /home/operator/Sites/
   ```

4. **Fix ownership and permissions**:
   Set primary ownership to `operator` and group to `homelab-operators`:
   ```bash
   sudo chown -R operator:homelab-operators /home/operator/Core /home/operator/Sites /home/operator/.config
   sudo chmod -R u+rwX,g+rX /home/operator/Core /home/operator/Sites

   # ACME certificate security invariant: Traefik strictly requires 0600 on acme.json;
   # recursive group read permissions will cause Traefik to refuse to start.
   if [ -f /home/operator/Core/config/letsencrypt/acme.json ]; then
     sudo chmod 600 /home/operator/Core/config/letsencrypt/acme.json
   fi
   ```

---

### Stage 3: Service Rebuild & Environment Cutover
Activate the declarative configuration under the `operator` profile:

1. **Configure instance domain injection (Decision D1)**:
   Ensure `nixos/instance.nix` (or `/etc/nixos/homelab-instance.nix`) is in place with the host's production domain:
   ```bash
   cat << 'EOF' | sudo -u operator tee /home/operator/Core/nixos/instance.nix > /dev/null
   {
     homelab.domain = "example.com";
   }
   EOF
   chmod 600 /home/operator/Core/nixos/instance.nix
   ```

2. **Rebuild NixOS host configuration**:
   Execute the rebuild from the new canonical Core path using `--impure` and the `path:` scheme to import the untracked instance profile:
   ```bash
   cd /home/operator/Core
   sudo nixos-rebuild switch --flake path:/home/operator/Core#server --impure
   ```

   > [!NOTE]
   > The `path:` scheme instructs Nix to evaluate directly from the filesystem without filtering by the Git index, ensuring untracked/gitignored `nixos/instance.nix` is evaluated. (If using `/etc/nixos/homelab-instance.nix`, bare `--flake .#server --impure` works directly).

3. **Restart platform services**:
   Restart the Core Compose stack and control plane services to consume updated paths:
   ```bash
   sudo systemctl restart homeserver-core
   sudo systemctl restart homelab-appctl
   sudo systemctl restart homelab-gitops
   ```

---

### Stage 4: Post-Flight Verification
Validate system health and operational readiness under the `operator` identity:

1. **Verify Git safe directory**:
   Ensure Git does not complain about dubious ownership when invoked by `operator` or `homelab-appctl`:
   ```bash
   sudo -u operator git -C /home/operator/Core status
   sudo -u operator git -C /home/operator/Sites/<any-workload> status
   ```

2. **Verify appctl control plane status**:
   Execute CLI inspection using both UDS peercred authentication and token authentication:
   ```bash
   # Peercred UDS inspection
   sudo -u operator appctl list --core
   
   # Workload health check
   sudo -u operator appctl status
   ```

3. **Verify SOPS secrets and tokens**:
   Ensure `/run/secrets/appctl/tokens.json` is readable by `homelab-appctl` and valid tokens exist for operator administration:
   ```bash
   sudo ls -l /run/secrets/appctl/tokens.json
   ```

4. **Verify container reachability & proxy routing**:
   Check that external endpoints and internal reverse proxies resolve:
   ```bash
   docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
   curl -s -k https://127.0.0.1 -H "Host: dashboard.example.com" | head -n 5
   ```

---

### Rollback Procedure
If unexpected service degradation occurs during the cutover:

1. **Revert NixOS system generation**:
   ```bash
   sudo nixos-rebuild switch --rollback
   ```

2. **Restart legacy Core services**:
   ```bash
   sudo systemctl restart homeserver-core
   sudo systemctl restart homelab-appctl
   sudo systemctl restart homelab-gitops
   ```

3. **Log rollback reason**:
   Record the diagnostic failure logs in `journalctl -xe` and investigate before attempting another cutover window.
