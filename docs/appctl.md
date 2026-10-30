# 📦 Workload Orchestration & `app.yaml` Reference (`appctl`)

The `appctl` tool is the command-line orchestrator and metadata engine for the **Homelab Core** platform. It coordinates interactions between independent workload repositories in `~/Sites` and platform services in `~/Core`.

> [!NOTE]
> **Implementation Reference**: This document serves as the **current implementation manual (Milestone P4)** for the `appctl` Operational Control Plane Daemon (`scripts/appctl_daemon.py`), Operational Router (`scripts/appctl_router.py`), Thin-Client CLI (`scripts/appctl`, `scripts/appctl_cli.py`), metadata/provenance engine (`scripts/appctl_engine.py`), and Pydantic v2 manifest parser (`scripts/core_manifest.py`).
> For the platform's phased engineering roadmap, see [`ROADMAP.md`](../ROADMAP.md) and the [Architecture Decision Index](decisions/README.md).

---

## 1. How `appctl` Works

Under Milestone P4, `appctl` operates as a centralized operational control plane daemon (`homelab-appctl.service`, running as isolated user `homelab-appctl:homelab-operators`) backed by cooperating modules in `scripts/`:

1. **[`scripts/appctl`](../scripts/appctl) & [`scripts/appctl_cli.py`](../scripts/appctl_cli.py)** (Thin-Client CLI): Dispatches operational commands over the Unix domain socket (`/run/homelab/appctl.sock`) using HTTP/1.1 (`POST /v1/operations`), resolves operator Bearer tokens (`--token`, `APPCTL_TOKEN`, `~/.config/homelab/token`), renders human-readable tables, and fast-paths local shell autocompletions (`complete`, `completion`, `resolve`).
2. **[`scripts/appctl_daemon.py`](../scripts/appctl_daemon.py) & [`scripts/appctl_contracts.py`](../scripts/appctl_contracts.py)** (Dual-Transport Control Plane Server & Contracts): Serves a single logical HTTP/1.1 API (`POST /v1/operations` + `GET /v1/health`) identically across `/run/homelab/appctl.sock` (mode `0660`) and loopback TCP (`127.0.0.1:9001`), enforcing 64 KiB request caps and strict Pydantic v2 contracts (`ConfigDict(extra="forbid", strict=True)`).
3. **[`scripts/appctl_auth.py`](../scripts/appctl_auth.py) & [`scripts/appctl_audit.py`](../scripts/appctl_audit.py)** (Zero-Trust Auth, 3-Role RBAC & Fail-Closed Audit Guard): Verifies SHA-256 Bearer tokens against `/run/secrets/appctl/tokens.json` (mode `0400`) using `hmac.compare_digest`, maps tokenless local UDS `SO_PEERCRED` callers strictly to `homelab-viewer` (non-sensitive read-only inspection), enforces `homelab-admin` / `homelab-operator` / `homelab-viewer` RBAC, serializes mutations via per-target `fcntl.flock`, and writes append-only audit records to `/var/log/homelab/appctl/audit.jsonl` (mode `0600`) before any mutation executes.
4. **[`scripts/appctl_router.py`](../scripts/appctl_router.py)** (Operational Router & Unified Execution Engine): Enforces pre-flight path containment, `core_manifest.parse_manifest()`, and `validate_compose_security()` AST checks; assembles the 6-layer execution environment in pure Python (zero shell `source` or `eval`); and runs bounded `git` and `docker compose` subprocesses (`shell=False`, 256 KiB output cap, hard timeouts).
5. **[`scripts/appctl_engine.py`](../scripts/appctl_engine.py) & [`scripts/core_manifest.py`](../scripts/core_manifest.py)** (Discovery, Provenance & Schema v1.0 Validator): Scans `~/Sites/*`, evaluates **two-axis status** (local deployment checkout state vs. upstream software provenance), inspects live container health and TLS certificates, validates `app.yaml` manifests, and atomically compiles the Homepage dashboard configuration under `_homepage_sync.lock`.

```text
      ┌──────────────────────────┐        ┌──────────────────────────┐
      │   appctl <cmd> (CLI)     │        │  homelab-gitops.service  │
      └────────────┬─────────────┘        └────────────┬─────────────┘
                   │ UDS (/run/homelab/appctl.sock)    │ Bearer Token (homelab-operator)
                   └─────────────────┬─────────────────┘
                                     ▼
            ┌────────────────────────────────────────────────┐
            │   homelab-appctl.service (appctl_daemon.py)    │
            │   • POST /v1/operations & GET /v1/health       │
            │   • Auth: SHA-256 tokens.json + SO_PEERCRED    │
            │   • RBAC: admin / operator / viewer            │
            └────────────────────────┬───────────────────────┘
                                     ▼
            ┌────────────────────────────────────────────────┐
            │      OperationalRouter (appctl_router.py)      │
            │   • Per-target flock + Fail-closed DISPATCHED  │
            │     audit write (/var/log/homelab/appctl/...)  │
            │   • Pre-flight: parse_manifest() + Compose AST │
            │   • Pure-Python 6-layer .env assembly          │
            └────────────────────────┬───────────────────────┘
                                     ▼
            ┌────────────────────────────────────────────────┐
            │   Bounded Subprocess Engine (shell=False)      │
            │   • git status / pull --ff-only                │
            │   • docker compose -f <file> <subcommand>      │
            └────────────────────────────────────────────────┘
```

### Operator Admin Token Bootstrapping & Rotation

`appctl` implements zero-trust authorization across three roles: `homelab-admin`, `homelab-operator`, and `homelab-viewer`. Local Unix domain socket (`/run/homelab/appctl.sock`) callers without a token are granted `homelab-viewer` privileges via `SO_PEERCRED` for non-sensitive read-only commands (e.g., `appctl list`, `appctl status`, `appctl ssl`).

Sensitive inspection (e.g., `appctl config <workload>` which renders secrets in Compose definitions) and all mutating operations (e.g., `appctl up`, `appctl restart`, `appctl update`) require an authenticated Bearer token.

#### Role of `~/.config/homelab/token`
The thin CLI client resolves Bearer tokens in the following order of precedence:
1. Direct CLI flag: `--token <secret>`
2. Environment variable: `APPCTL_TOKEN`
3. Local operator token file: `~/.config/homelab/token` (mode `0600`)
4. System service token file: `/run/secrets/appctl/token`

The standard operator setup stores a 256-bit hex Bearer token with mode `0600` in `~/.config/homelab/token`.

#### Generating a Token (`scripts/bootstrap_token.py`)
To generate an operator token and its matching SHA-256 descriptor:

```bash
python3 scripts/bootstrap_token.py
```

This utility:
1. Generates a cryptographically secure 256-bit token (`secrets.token_hex(32)`).
2. Computes the SHA-256 digest: `hashlib.sha256(raw_token.encode("utf-8")).hexdigest()`.
3. Ensures `~/.config/homelab` exists with mode `0700`.
4. Writes the raw Bearer token to `~/.config/homelab/token` with mode `0600`.
5. Outputs the JSON descriptor matching `TokenDescriptor` in `scripts/appctl_auth.py`:

```json
{
  "token_id": "operator-admin",
  "identity": "operator",
  "role": "homelab-admin",
  "sha256": "4a12...<64-hex-sha256>"
}
```

> [!NOTE]
> The raw secret is **never** printed to stdout or stderr. Only the descriptor snippet (containing the SHA-256 digest) is output. To print only the JSON snippet for automation or piping, pass `--print-json`.

#### Registering the Descriptor in SOPS
Insert the generated JSON descriptor into your encrypted SOPS secrets store (`nixos/secrets.yaml`):

```bash
sops nixos/secrets.yaml
```

Under `appctl.tokens_json`, add the entry to the JSON list:
```yaml
appctl:
  tokens_json: |
    [
      {
        "token_id": "operator-admin",
        "identity": "operator",
        "role": "homelab-admin",
        "sha256": "<64-hex-sha256>"
      }
    ]
```

Save and exit your editor (SOPS will re-encrypt the file), then project the updated secrets:
```bash
sudo nixos-rebuild switch --flake ~/Core#server
```
This updates `/run/secrets/appctl/tokens.json` (owned by `homelab-appctl:homelab-operators`, mode `0400`), which `homelab-appctl.service` consumes.

#### Verifying Token Authentication
Verify permissions using both unauthenticated and authenticated queries:

1. **Viewer Inspection** (succeeds without token via `SO_PEERCRED`):
   ```bash
   appctl health
   # Output: OK (homelab-appctl.service operational)
   ```

2. **Admin-Gated Operation** (requires `homelab-admin` Bearer token):
   ```bash
   appctl config docs
   # Renders the resolved Compose YAML with injected secrets
   ```
   If the token is missing or mismatched, `appctl` returns `401 Unauthorized` or `403 Forbidden` with `ERR_PERMISSION_DENIED`.

#### Token Rotation Workflow
To rotate an operator token:
1. Run `python3 scripts/bootstrap_token.py --force --token-id "operator-admin-$(date +%Y%m%d)"`.
2. Open `sops nixos/secrets.yaml` and add the new descriptor to `appctl.tokens_json`. Keep the old entry temporarily if multiple sessions share it, or replace it immediately.
3. Run `sudo nixos-rebuild switch --flake ~/Core#server` to apply the updated token store.
4. Verify CLI operations (`appctl config docs`).
5. Remove the deprecated token descriptor from `nixos/secrets.yaml` and re-run `nixos-rebuild switch`.

---

## 2. CLI Command Reference

### Status & Inspection

#### `appctl list` / `appctl status`
Lists applications discovered in `~/Sites` using **two-axis inspection**:
- **`DEPLOYMENT` (Axis 1 — Local Checkout)**: Inspects the local `~/Sites/<app>` Git working tree (`✓ Synced`, `* Dirty`, `⬇ Behind`, `⚠️ Non-Git`).
- **`PROVENANCE` (Axis 2 — Upstream Software)**: Evaluates upstream software provenance for Class A (Source-Backed) vs. Class B (`— Image-Only`) workloads.

**Options**:
* `--fetch` (`-f`): Queries upstream Git remotes (`git ls-remote` with a 10s timeout) and fetches local deployment tracking branches before computing provenance badges.
* `--core` (`-c` / `--all` / `-a`): Appends Core infrastructure services (`traefik`, `authelia`, `lldap`, `stalwart`, `homepage`, `dozzle`, `watchtower`) to the output.
* `--ssl` (`-s`): Queries SSL/TLS certificates over port 443 for each public domain and appends a `CERT` column.

**Example Output**:
```text
SERVICE          DEPLOYMENT             PROVENANCE               STATUS          DOMAIN
-------          ----------             ----------               ------          ------
docs             ✓ Synced (main)        ✓ Synced                 🟢 Running (1)  docs.example.com
jellyfin         ✓ Synced (main)        — Image-Only             🟢 Running (1)  jellyfin.example.com
gitea            ✓ Synced (main)        ⬆ Update Available       🟢 Running (2)  gitea.example.com
mongodb          * Dirty (1) (main)     — Image-Only             🔴 Stopped      mongodb.example.com
```

#### `appctl ssl [service]`
Inspects SSL/TLS certificates over port 443. If a service is specified, shows detailed issuer, subject, and expiration data; otherwise, inspects all non-internal homelab domains.

```bash
# Check all domains
appctl ssl

# Check a specific service
appctl ssl jellyfin
```

#### `appctl info <service> [--ssl]`
Displays comprehensive two-axis diagnostics for a workload or Core service:
* Canonical name, directory path, aliases, and workload class (`Class A (Source-Backed)` vs. `Class B (Image-Only)`).
* Primary routed domain, networks, and Authelia guard status.
* **Axis 1 (Local Deployment Checkout)**: Branch, commit hash, dirty file count, and sync status.
* **Axis 2 (Upstream Software Provenance)**: Upstream Git URL, tracked ref (`branch`, `tag`, or `commit`), resolved remote commit, and running container OCI revision label.
* Injected environment variable defaults and Homepage card metadata.
* SSL certificate details (when `--ssl` is passed).

```bash
appctl info docs --ssl
```

---

### Lifecycle Management

#### `appctl up <service|core> [--build] [--no-cache]`
Starts the specified service stack in detached mode (`--remove-orphans`).
* For a workload in `~/Sites`: starts containers and triggers `appctl sync`.
* For `core`: starts the entire Core Compose stack using `/run/secrets/homeserver.env`.
* For an individual Core service (e.g., `appctl up traefik`): starts that specific container.
* When `--build` is passed: invokes `docker compose build --pull` prior to deployment. The `--pull` flag guarantees base images in Dockerfile `FROM` lines are refreshed from remote registries.
* When `--no-cache` is passed: `--build` is implied (`build: True`) and `docker compose build --pull --no-cache` is executed, forcing a clean rebuild without cached layers.

#### `appctl down <service|core>`
Stops and removes containers defined in the target's Compose file (or stops the targeted Core container).

#### `appctl restart <service|core>`
Restarts the target stack or individual container:
* `appctl restart docs`: Recreates the `docs` workload stack and syncs Homepage.
* `appctl restart core`: Restarts all Core Compose services.
* `appctl restart traefik`: Restarts only the Traefik container.

#### `appctl update <service> [--branch <branch>] [--build] [--no-cache]`
Performs a sequential, revision-consistent update workflow for a Sites workload stack:
1. Verifies that the local Git working tree is clean (aborts if uncommitted changes exist).
2. Runs `git pull --ff-only [origin <expected_branch>]` to fast-forward the current branch.
3. Re-validates `app.yaml`, expected branch declaration, and Compose security rules against the newly pulled working tree.
4. Executes declared actions in `app.yaml` (`actions: [git_pull, compose_pull, compose_build, compose_up, compose_restart]`):
   * `compose_pull`: Runs `docker compose pull` to fetch updated container images.
   * `compose_build`: Runs `docker compose build --pull` (and `--no-cache` if requested).
   * `compose_up`: Runs `docker compose up -d --remove-orphans` to recreate containers.
   * When `--build` is passed via CLI, `compose_build` is automatically executed prior to `compose_up`, even if omitted from manifest actions.
   * When `--no-cache` is passed, build is implied and executes with `--no-cache`.
5. Runs `appctl sync` to update the Homepage dashboard.

> [!NOTE]
> **Source-Built Workload Policy**: For workloads built directly from local source Dockerfiles rather than pre-built registry images, declare `compose_build` in `app.yaml` `deployment.actions` (`actions: [git_pull, compose_build, compose_up]`). Both manual `appctl update` and automated GitOps dispatcher runs will execute `docker compose build --pull` before recreating containers, ensuring reproducible builds with current upstream base layers.

> [!NOTE]
> Core platform updates are managed via version-controlled NixOS generations (`git pull` followed by `nixos-rebuild switch`), not through `appctl update core` (as Core is mounted read-only for `homelab-appctl.service`).

#### `appctl pull <service|core>`
Pulls latest container images and recreates the target service.

#### `appctl logs <service|core> [--tail N]`
Fetches a bounded snapshot of container logs (default: `--tail 100`). Supports passing a workload or targeted Core service container (e.g., `appctl logs traefik`).
*(Note: As an HTTP/1.1 request-response API, log streaming `-f` is replaced by bounded snapshots).*

#### `appctl config <service|core>`
Validates the manifest and renders the resolved Docker Compose configuration with all injected variables. Fails closed if `app.yaml` is missing or invalid.

#### `appctl sync`
Scans all valid `~/Sites/*/app.yaml` manifests and compiles `config/homepage/services.yaml`.

#### `appctl completion [bash]`
Outputs the bash autocompletion script.

---

## 3. `app.yaml` Schema v1.0 Contract

Workload manifests in `~/Sites/<app>/app.yaml` are validated by [`scripts/core_manifest.py`](../scripts/core_manifest.py) using strict Pydantic v2 models (`extra="forbid"`). Any unknown field, invalid transport URL, or prohibited deployment action causes validation to fail closed.

```yaml
schema_version: "1.0"                # Schema version (only "1.0" is supported)

# ── Identity & Routing ────────────────────────────────────────────────────────
name: "jellyfin"                     # Canonical name: ^[a-zA-Z0-9][a-zA-Z0-9._-]*$
aliases:                             # Optional CLI aliases
  - "media"
  - "movies"
domain: "jellyfin.example.com"       # Primary routed domain (defaults to <name>.${HOMELAB_DOMAIN})
description: "Media Streaming Server"

# ── Platform Integration ──────────────────────────────────────────────────────
visible: true                        # Display on Homepage dashboard (default: true)
auth: false                          # Authelia ForwardAuth protection (default: false)
networks:                            # Docker networks to join (default: [proxy-net])
  - proxy-net

# ── Upstream Software Provenance (Optional: Defines Class A vs. Class B) ─────
# Omit 'source' for Class B (Image-Only third-party workloads).
# Include 'source' for Class A (Source-Backed workloads):
source:
  url: "https://github.com/example/jellyfin-app.git"  # Strictly https://, ssh://, or git@host:path
  ref: "main"                                         # Branch, v* tag, or 40-char commit SHA

# ── Injected Environment Defaults ─────────────────────────────────────────────
# Keys must match ^[a-zA-Z_][a-zA-Z0-9_]*$ and cannot override reserved host keys
# (PATH, LD_PRELOAD, IFS, SHELL, USER, HOME, CORE_DIR, SITES_DIR, etc.)
env:
  MEDIA_PATH: "/media"
  JELLYFIN_TZ: "UTC"

# ── GitOps Deployment Policy ──────────────────────────────────────────────────
# Evaluated by scripts/gitops_dispatcher.py upon webhook admission
deployment:
  branch: "main"                     # Target branch that triggers deployment
  strategy: "compose"                # Only 'compose' strategy is permitted
  actions:                           # Strictly allowlisted: git_pull, compose_pull,
    - git_pull                       # compose_up, compose_build, compose_restart
    - compose_up

# ── Homepage Dashboard Metadata ───────────────────────────────────────────────
homepage:
  title: "Jellyfin"                  # Card title
  group: "Media & Productivity"      # Dashboard group name
  icon: "jellyfin.png"               # Icon name (matched against config/homepage/icons/)
  container: "jellyfin"              # Container name for live status widget
  weight: 10                         # Card sort weight (lower sorts first)
```

---

## 4. Environment Injection & Resolution Precedence

When `OperationalRouter` (`scripts/appctl_router.py`) executes commands against a workload or Core stack, the 6-layer execution environment is assembled in memory using a pure-Python inert `KEY=VALUE` parser (`parse_dotenv`) with zero shell `source` or variable expansion:

1. **Base System Path & Docker Config**: `PATH` and `DOCKER_CONFIG`.
2. **Global Secrets & Templates**: Parsed inertly from `/run/secrets/rendered/homeserver.env` and `/run/secrets/rendered/traefik-deployments.env` (with `/run/secrets/*` fallbacks).
3. **Canonical Root Domain Lock**: `HOMELAB_DOMAIN` is locked using precedence `HOMELAB_DOMAIN -> DOMAIN_SUFFIX -> DOMAIN` (failing closed if unset).
4. **Infrastructure Defaults & Application Identity**:
   - `PROXY_NETWORK` (defaults to `proxy-net`)
   - `CERT_RESOLVER` (defaults to `myresolver`)
   - `DOMAIN_SUFFIX` (`$HOMELAB_DOMAIN`)
   - `SERVICE_NAME` = `<canonical-name>`
   - `CONTAINER_NAME` = `<canonical-name>`
   - `SERVICE_DOMAIN` = `<canonical-name>.<HOMELAB_DOMAIN>`
   - `DOMAIN` = `<HOMELAB_DOMAIN>`
5. **Manifest Variables**: Validated key-value pairs from the `env:` block in `app.yaml`.
6. **Local Repository Files**: Parsed inertly from `$APP_DIR/.env` and `$APP_DIR/app.env` if present, rejecting any key violating `ENV_KEY_RE` or matching `RESERVED_ENV_KEYS`.

---

## 5. Homepage Dashboard Compilation

When `appctl sync` runs:
1. `appctl_engine.py` scans all `~/Sites/*/app.yaml` manifests (skipping any workload with a `manifest_error`).
2. Core services (`traefik`, `authelia`, `lldap`, `stalwart`, `homepage`, `dozzle`, `watchtower`) are assigned to `Core Infrastructure`.
3. Workload applications with `visible: true` are grouped by their `homepage.group` (ordered: `Core Infrastructure`, `Knowledge & Notes`, `Media & Productivity`, `Development & AI`, `Applications`) and sorted by `(weight, title)`.
4. Custom icons matching files in `config/homepage/icons/` (`.svg` or `.png`) are mapped to `/icons/<name>`.
5. The compiled configuration is written to `config/homepage/services.yaml`.
