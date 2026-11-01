# 🛡️ Homelab Core — Security Controls & Hardening

This document outlines the security controls, container isolation rules, and operational boundaries implemented in **Homelab Core**.

---

## 1. Implemented Security Controls

### A. Docker Socket Isolation (`socket-proxy`)
* Direct container mounts of `/var/run/docker.sock` are restricted.
* Only the dedicated `socket-proxy` container mounts `/var/run/docker.sock:ro`.
* State-modifying and execution API endpoints are disabled in HAProxy (`POST=0`, `DELETE=0`, `BUILD=0`, `EXEC=0`, `COMMIT=0`, `CONFIGS=0`, `DISTRIBUTION=0`, `NODES=0`, `PLUGINS=0`, `SECRETS=0`, `SWARM=0`, `SYSTEM=0`).
* Traefik and Dozzle query Docker metrics and metadata via `tcp://socket-proxy:2375` rather than direct socket mounts.

### B. Identity & Access Management (`lldap` & `authelia`)
* **Directory Service**: User accounts and group memberships are stored in LLDAP (`users.${HOMELAB_DOMAIN}`).
* **ForwardAuth**: Traefik intercepts incoming HTTP/HTTPS requests and validates sessions with Authelia (`auth.${HOMELAB_DOMAIN}`) before routing to protected backends.
* **MFA Support**: Supports TOTP and FIDO2 authentication.

### C. Network Segmentation
* **`proxy-net`**: Bridge network connecting Traefik to backend application containers.
* **`socket-net`**: Restricted bridge network connecting infrastructure daemons to `socket-proxy`. Workload applications in `~/Sites` are not attached to `socket-net`.

### D. Webhook Admission Controls
* **HMAC-SHA256 Signatures**: Webhook requests are verified using `hmac.compare_digest` against `/run/secrets/gitops/webhook_secret`.
* **Trusted Name Resolution**: Webhook payloads specify logical repository names; filesystem path traversal (`..`, `/`) and symlink escapes are rejected.
* **Non-Shell Execution**: Deployment commands execute as fixed argument lists without `shell=True`. Custom shell commands fail validation closed.
* **Revision-Consistent Validation**: Manifest deployment policy is re-validated directly on the deployed commit immediately post-pull.

### E. Declarative Secrets Encryption (`sops-nix`)
* Secrets are stored encrypted in `nixos/secrets.yaml` using Age keys derived from the host SSH key (`/etc/ssh/ssh_host_ed25519_key`).
* Decrypted secrets reside in RAM-backed ephemeral filesystems (`/run/secrets/`, `/run/credentials/`) and are not committed to unencrypted persistent files.

### F. Operational Control Plane Security (`homelab-appctl.service`)
* **Transport Isolation**: Daemon listens on a restricted Unix Domain Socket (`/run/homelab/appctl.sock`, mode `0660`, group `homelab-operators`) and loopback HTTP (`127.0.0.1:9876`). It never binds to public interfaces.
* **Authentication & RBAC**:
  - **UDS Peer Credential Identification**: Local connections inspect kernel peer credentials (`SO_PEERCRED`). Callers in `homelab-operators` or `wheel` receive read-only viewer privileges automatically without requiring secrets on disk.
  - **Cryptographic Bearer Tokens**: Mutating actions require Bearer tokens defined in SOPS (`/run/secrets/appctl/tokens.json`). Tokens are validated using `hmac.compare_digest` against SHA-256 digests.
  - **Three-Tier RBAC**: Granular role enforcement distinguishes read-only inspectors (`viewer`), workload managers (`operator`), and core platform administrators (`admin`).
* **Per-Target Concurrency Control (`flock`)**: Non-blocking file locks (`/run/lock/homelab/<target>.lock`) serialize mutating actions per workload, returning HTTP 409 Conflict if an operation is already in flight.
* **Fail-Closed Structured Audit Logging**: Every operational command is appended synchronously to a tamper-evident audit log (`/var/log/homelab/appctl/audit.jsonl`, mode `0600`). If disk writing fails, the daemon fails closed and rejects the request.

---

## 2. Threat Model & Operational Considerations

### Host Access
* SSH root login is disabled (`PermitRootLogin = "no"`). Access is authenticated via ED25519 public keys.
* Unneeded services (such as printing, bluetooth, sleep/suspend targets) are disabled at the NixOS configuration level.

### ACME Certificates
* Let's Encrypt keys in `config/letsencrypt/acme.json` are maintained with mode `0600`.

### Workload Containment
* Applications run in dedicated containers attached to `proxy-net`.
* Application manifests cannot request arbitrary host commands through the GitOps engine.

---

## 3. Verification Checklist

- [x] Secrets encrypted in `nixos/secrets.yaml` via Age / SOPS.
- [x] User identities managed in LLDAP (`users.${HOMELAB_DOMAIN}`).
- [x] `socket-proxy` configured with `POST=0`, `DELETE=0`, `BUILD=0`, `EXEC=0`.
- [x] Webhook HMAC signature verification active on port 9000.
- [x] Invariant test runner (`./scripts/test`) passing on changes.
- [x] `config/letsencrypt/acme.json` permissions set to `0600`.
- [x] Host firewall ports restricted to declared services in `nixos/modules/homeserver.nix`.
- [x] Control plane daemon (`homelab-appctl.service`) isolated to UDS (`0660`) and loopback HTTP.
- [x] Operational audit log (`/var/log/homelab/appctl/audit.jsonl`) append-only with strict `0600` permissions.
- [x] Control plane mutation endpoints enforce per-target `flock` and SHA-256 Bearer tokens.
