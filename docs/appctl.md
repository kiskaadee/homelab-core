# 📦 Workload Orchestration & `app.yaml` Reference (`appctl`)

The `appctl` tool is the command-line orchestrator and metadata engine for the **Homelab Core** platform. It coordinates interactions between independent workload repositories in `~/Sites` and platform services in `~/Core`.

> [!NOTE]
> **Implementation Reference**: This document serves as the **current implementation manual (Milestone P3)** for the `appctl` Bash CLI wrapper (`scripts/appctl`), metadata/provenance engine (`scripts/appctl_engine.py`), and Pydantic v2 manifest parser (`scripts/core_manifest.py`).
> For the platform's planned Milestone P4 control-plane daemon transition and phased engineering roadmap, see [`ROADMAP.md`](../ROADMAP.md) and the [Architecture Decision Index](decisions/README.md).

---

## 1. How `appctl` Works

`appctl` consists of three cooperating modules in `scripts/`:
1. **[`scripts/appctl`](../scripts/appctl)** (Bash CLI wrapper): Parses CLI arguments, sources runtime secret templates, injects validated workload environment variables, dispatches `docker compose` commands, and provides shell autocompletions.
2. **[`scripts/appctl_engine.py`](../scripts/appctl_engine.py)** (Discovery, Provenance & TLS Engine): Scans `~/Sites/*`, evaluates **two-axis status** (local deployment checkout state vs. upstream software provenance), inspects live container health and TLS certificates, and compiles the Homepage dashboard configuration.
3. **[`scripts/core_manifest.py`](../scripts/core_manifest.py)** (Schema v1.0 Parser & Validator): Enforces fail-closed Pydantic v2 validation (`ConfigDict(extra="forbid")`) on `app.yaml` manifests, validates upstream Git transport schemes (`https://`, `ssh://`, `git@`), classifies reference mutability (branch, tag, commit SHA), and blocks reserved host environment keys (`PATH`, `LD_PRELOAD`, `IFS`, `CORE_DIR`, `SITES_DIR`, etc.).

```text
                  ┌───────────────────────────────┐
                  │      appctl <command>         │
                  └───────────────┬───────────────┘
                                  │
                                  ▼
                    ┌───────────────────────────┐
                    │     setup_app_env()       │
                    └─────────────┬─────────────┘
                                  │
       ┌──────────────────────────┴──────────────────────────┐
       ▼                                                     ▼
[ appctl_engine.py resolve ]                          [ Source Secrets ]
• Parses app.yaml via core_manifest.py                /run/secrets/traefik-deployments.env
• Fails closed on manifest_error                      /run/secrets/homeserver.env
       │                                                     │
       └──────────────────────────┬──────────────────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ Injected Compose Context    │
                   │ • PROXY_NETWORK             │
                   │ • SERVICE_DOMAIN / DOMAIN   │
                   │ • CONTAINER_NAME            │
                   │ • Validated manifest env    │
                   └──────────────┬──────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ docker compose <subcommand> │
                   └─────────────────────────────┘
```

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

#### `appctl up <service|core>`
Starts the specified service stack in detached mode (`--remove-orphans`).
* For a workload in `~/Sites`: starts containers and triggers `appctl sync`.
* For `core`: starts the entire Core Compose stack using `/run/secrets/homeserver.env`.
* For an individual Core service (e.g., `appctl up traefik`): starts that specific container.

#### `appctl down <service|core>`
Stops and removes containers defined in the target's Compose file (or stops the targeted Core container).

#### `appctl restart <service|core>`
Restarts the target stack or individual container:
* `appctl restart docs`: Recreates the `docs` workload stack and syncs Homepage.
* `appctl restart core`: Restarts all Core Compose services.
* `appctl restart traefik`: Restarts only the Traefik container.

#### `appctl update <service|core>`
Performs a sequential update workflow:
1. Verifies that the local Git working tree is clean (aborts if uncommitted changes exist).
2. Runs `git pull --ff-only` to fast-forward the current branch.
3. Runs `docker compose pull` to fetch updated container images.
4. Runs `docker compose up -d --remove-orphans` to recreate containers.
5. Runs `appctl sync` to update the Homepage dashboard.

#### `appctl pull <service|core>`
Pulls latest container images and recreates the target service.

#### `appctl logs <service|core>`
Streams container logs (`docker compose logs -f`). Supports passing specific container names (`appctl logs core traefik` or `appctl logs stalwart`).

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

When `appctl` executes commands against a workload, environment variables are resolved in this sequence:

1. **Global Secrets & Templates**: Sourced from `/run/secrets/rendered/homeserver.env` and `/run/secrets/rendered/traefik-deployments.env` (with `/run/secrets/*` fallbacks).
2. **Canonical Root Domain Lock**: `HOMELAB_DOMAIN` is locked at startup using precedence `HOMELAB_DOMAIN -> DOMAIN_SUFFIX -> DOMAIN` so per-workload `DOMAIN` exports cannot corrupt the platform apex domain.
3. **Infrastructure Defaults**:
   - `PROXY_NETWORK` (defaults to `proxy-net`)
   - `CERT_RESOLVER` (defaults to `myresolver`)
   - `DOMAIN_SUFFIX` (defaults to `$HOMELAB_DOMAIN`)
4. **Application Identity**:
   - `SERVICE_NAME` = `<canonical-name>`
   - `CONTAINER_NAME` = `<canonical-name>`
   - `SERVICE_DOMAIN` = `<resolved-domain>`
   - `DOMAIN` = `<resolved-domain>`
5. **Manifest Variables**: Validated key-value pairs from the `env:` block in `app.yaml` (emitted via null-delimited stream without shell `eval`).
6. **Local Repository Files**: Sourced from `$APP_DIR/.env` and `$APP_DIR/app.env` if present.

---

## 5. Homepage Dashboard Compilation

When `appctl sync` runs:
1. `appctl_engine.py` scans all `~/Sites/*/app.yaml` manifests (skipping any workload with a `manifest_error`).
2. Core services (`traefik`, `authelia`, `lldap`, `stalwart`, `homepage`, `dozzle`, `watchtower`) are assigned to `Core Infrastructure`.
3. Workload applications with `visible: true` are grouped by their `homepage.group` (ordered: `Core Infrastructure`, `Knowledge & Notes`, `Media & Productivity`, `Development & AI`, `Applications`) and sorted by `(weight, title)`.
4. Custom icons matching files in `config/homepage/icons/` (`.svg` or `.png`) are mapped to `/icons/<name>`.
5. The compiled configuration is written to `config/homepage/services.yaml`.
