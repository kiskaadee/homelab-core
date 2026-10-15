# ⚡ Homelab Core — GitOps Continuous Deployment Engine

Homelab Core includes an automated GitOps deployment engine implemented in [`scripts/gitops_dispatcher.py`](../scripts/gitops_dispatcher.py). It processes Git push webhooks from Gitea (or external Git forges) and executes declared deployment actions strictly for admitted application workloads in `~/Sites`.

---

## 1. Architectural Model

The GitOps engine is invoked via the `homelab-gitops.service` systemd unit running on internal/firewall port 9000:

```
                      Webhook Request (:9000)
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 1. Cryptographic HMAC-SHA256 Verification     │
         │    Constant-time comparison with SOPS secret  │
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 2. Event & Ref Format Admission               │
         │    Admits 'push' to 'refs/heads/*'            │
         │    Rejects PRs, issues, releases, and tags    │
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 3. Authoritative Registry Resolution          │
         │    Maps names via Core/config/deployments.yaml│
         │    Fails closed; zero dynamic discovery on host│
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 4. Pre-Pull Manifest & Branch Policy Check    │
         │    Validates app.yaml & matches target branch │
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 5. Atomic Enqueue & Immediate HTTP 200        │
         │    Writes pending state & spawns async worker │
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼ (Detached Worker Process)
         ┌───────────────────────────────────────────────┐
         │ 6. Serialized Worker & Lock Acquisition       │
         │    Non-blocking flock; supersedes stale queue │
         └───────────────────────┬───────────────────────┘
                                 │
                                 ▼
         ┌───────────────────────────────────────────────┐
         │ 7. Revision-Consistent Validation & Execution │
         │    git pull -> re-validate manifest on-tree   │
         │    Compose AST security validator (containment│
         │    & privilege checks) -> compose up -> status│
         └───────────────────────────────────────────────┘
```

---

## 2. Admission & Execution Gates

### A. Non-Shell Subprocess Execution
* The deployment engine contains no `shell=True` subprocess invocations.
* Commands are executed as fixed argument lists (e.g. `["docker", "compose", "-f", ..., "up", "-d"]`).
* Arbitrary `custom:` shell actions fail validation closed.

### B. Cryptographic HMAC-SHA256 Authentication
* Incoming webhook payloads must provide a signature via `X-Gitea-Signature` or `X-Hub-Signature-256`.
* Signatures are verified using HMAC-SHA256 with `hmac.compare_digest` against the shared secret stored in `nixos/secrets.yaml` (projected to `/run/secrets/gitops/webhook_secret`).
* Missing, empty, or mismatched signatures are rejected.

### C. Authoritative Repository Registry (`deployments.yaml`)
* Webhook payloads identify targets strictly by logical name (e.g. `docs`, `homelab-docs`), never by raw filesystem path.
* `Core/config/deployments.yaml` is the **sole admission authority** for GitOps workloads.
* **No Dynamic Discovery**: Scanning `~/Sites` dynamically is strictly prohibited. An unapproved directory created on disk cannot receive webhook deployment rights merely by providing an `app.yaml`.
* **Fail-Closed Guarantee**: If `deployments.yaml` is missing, unreadable, or malformed, the dispatcher fails closed and admits zero repositories.
* **Input Validation**: Names must match `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`. Slashes, backslashes, and path traversal sequences (`..`) are rejected.
* **Containment Check**: Declared repository paths must resolve strictly within `~/Sites`. Paths escaping `~/Sites` are rejected during registry loading.

### D. Revision-Consistent Manifest Validation
* When a webhook is admitted, an initial policy check verifies that the pushed branch matches the declared `deployment.branch`.
* After `git pull --ff-only` runs, the working tree reflects the newly pulled commit.
* The engine **re-reads and re-validates `app.yaml` from the newly pulled working tree** before running subsequent container actions (`compose_up`, `compose_build`, etc.).
* If the pulled commit introduces an invalid action, unsupported strategy, or changes the target branch away from the pushed branch, deployment is aborted.

### E. Compose AST Security Validation
Before executing `docker compose up -d`, `validate_compose_security()` parses the application's Compose file and enforces platform security invariants:
* **File Integrity**: `docker-compose.yml` and `app.yaml` must be regular files, not symlinks escaping the workload root.
* **Privilege Containment**: Rejects `privileged: true`, `cap_add` capabilities, `devices` mounts, and `security_opt: ["apparmor:unconfined"]`.
* **Namespace Isolation**: Rejects host namespaces (`network_mode: host`, `pid: host`, `ipc: host`, `uts: host`, `userns_mode: host`).
* **Docker Socket Ban**: Rejects direct bind mounts of `/var/run/docker.sock` (reserved exclusively for Core `socket-proxy:ro`).
* **Top-Level Directives**: Rejects top-level `include:` directives to prevent out-of-tree file inclusions.
* **Bind-Mount & State Isolation**:
  - Resolves all bind mounts (both relative and absolute) against the authorized workload directory.
  - Bind mounts are strictly restricted to **either** the workload root (`~/Sites/<app>/`) **or** the dedicated per-workload persistent state root (`~/var/lib/homelab/<app>/`).
  - Cross-tenant mounts (mounting another application's directory under `~/var/lib/homelab/`) are rejected fail-closed.

---


## 3. Asynchronous Queueing, Serialization & Superseding

To avoid blocking HTTP connections and handle overlapping pushes:

1. **Immediate HTTP 200 Return**: Upon passing admission, the dispatcher writes a pending file (`~/.local/state/homelab/gitops/<target>.pending.json`), spawns a detached worker (`--worker <target>`), and returns HTTP 200.
2. **Per-Target `flock` Serialization**: The worker acquires an exclusive non-blocking `fcntl.flock` on `~/.local/state/homelab/gitops/<target>.lock`. If a worker is already running for this target, the second worker exits safely while the running worker continues to drain the pending file.
3. **Commit Superseding**:
   - Commit A begins deploying.
   - Commit B arrives and is written to the pending file.
   - Commit C arrives while A is still deploying; C overwrites B in the pending file.
   - Commit A completes; the worker processes C, skipping intermediate commit B.

---

## 4. Manifest Deployment Configuration (`app.yaml`)

Workload repositories declare their deployment policy under the `deployment` block in `app.yaml`:

```yaml
deployment:
  branch: "main"           # Target branch that triggers deployment
  strategy: "compose"      # Deployment strategy (only 'compose' is permitted)
  actions:                 # Strictly allowlisted actions
    - git_pull             # Pulls latest branch revision (--ff-only)
    - compose_up           # Runs docker compose up -d --remove-orphans
```

### Allowlisted Deployment Actions
* **`git_pull`**: Executes `git -C <target> pull --ff-only origin <branch>`.
* **`compose_up`**: Executes `docker compose -f <compose_file> up -d --remove-orphans`.
* **`compose_build`**: Executes `docker compose -f <compose_file> build` followed by `compose_up`.
* **`compose_restart`**: Executes `docker compose -f <compose_file> restart`.

Any action outside this list causes manifest validation to fail closed.

---

## 5. Execution State & Monitoring

Each deployment records its result to a status file:

**Path**: `~/.local/state/homelab/gitops/<target>.status.json`

**Example Content**:
```json
{
  "target": "homelab-docs",
  "branch": "main",
  "commit_sha": "d4f8a1e2c3b5a7",
  "success": true,
  "timestamp": "2026-09-16T12:00:00+00:00"
}
```

### Diagnostics & Manual Invocations
```bash
# View live webhook dispatcher logs
journalctl -u homelab-gitops -f

# Inspect deployment status for a service
cat ~/.local/state/homelab/gitops/homelab-docs.status.json

# Trigger a local manual deployment run
python3 ~/Core/scripts/gitops_dispatcher.py --repo docs --branch main
```

---

## 6. Pre-Merge CI Gating vs. Post-Merge CD Dispatcher

The continuous deployment engine operates strictly **downstream** of CI verification:

```
[Developer / PR] ──► [Gitea Actions CI] ──► [Merge to main] ──► [Gitea Webhook] ──► [homelab-gitops] ──► [Deploy]
                     (Ephemeral Container)                                           (Host Daemon)
```

1. **Pre-Merge CI Invariant (`.gitea/workflows/ci.yaml`)**:
   - Executes inside containerized `act_runner` environments using **pre-baked toolchain images** (`container: nixery.dev/shell/coreutils/git/nix/nodejs:latest`).
   - Runs hermetic validation (`nix flake check`, `pytest`, `ruff`) with Git safe directory enforcement.
   - Prevents broken code, misconfigured flakes, or regression bugs from entering `main`.
2. **Post-Merge CD Execution (`homelab-gitops.service`)**:
   - Triggers only after commits are admitted into `main`.
   - Executes non-shell allowlisted actions (`git_pull`, `compose_up`) on the server host.
   - Operates fully decoupled from the CI runner runtime.

