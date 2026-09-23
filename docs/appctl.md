# 📦 Workload Orchestration & `app.yaml` Reference (`appctl`)

The `appctl` tool is the command-line orchestrator and metadata engine for the **Homelab Core** platform. It coordinates interactions between independent workload repositories in `~/Sites` and platform services in `~/Core`.

---

## 1. How `appctl` Works

`appctl` consists of two cooperating scripts:
1. **`scripts/appctl`** (Bash CLI wrapper): Handles command-line arguments, sources secret environment templates, dispatches `docker compose` commands, and provides shell autocompletions.
2. **`scripts/appctl_engine.py`** (Python metadata engine): Scans directories in `~/Sites`, parses `app.yaml` manifests, computes Git synchronization status against remotes, queries SSL/TLS certificate validity, and compiles the Homepage dashboard configuration.

```
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
Resolves aliases, domain,                             /run/secrets/traefik-deployments.env
and manifest parameters                               /run/secrets/homeserver.env
       │                                                     │
       └──────────────────────────┬──────────────────────────┘
                                  ▼
                   ┌─────────────────────────────┐
                   │ Injected Compose Context    │
                   │ • PROXY_NETWORK             │
                   │ • SERVICE_DOMAIN / DOMAIN   │
                   │ • CONTAINER_NAME            │
                   │ • Custom manifest env       │
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
Lists applications discovered in `~/Sites` with container status, Git synchronization badge, routed domain, and filesystem path.

**Options**:
* `--fetch` (`-f`): Runs `git fetch --quiet` across repositories before computing sync badges.
* `--core` (`-c`): Includes Core infrastructure services (`traefik`, `authelia`, `lldap`, `stalwart`, etc.) in the output.
* `--ssl` (`-s`): Queries SSL/TLS certificates for each domain and displays status and expiration days.
* `--all` (`-a`): Equivalent to `--core`.

**Example Output**:
```text
SERVICE          STATUS          GIT SYNC        DOMAIN                       DIRECTORY
-------          ------          --------        ------                       ---------
docs             🟢 Running (1)  ✓ Synced        docs.roadtotech.me           ~/Sites/homelab-docs
jellyfin         🟢 Running (1)  ✓ Synced        jellyfin.roadtotech.me       ~/Sites/homelab-jellyfin
gitea            🟢 Running (2)  ⬇ 1 Behind      gitea.roadtotech.me          ~/Sites/homelab-gitea
mongodb          🔴 Stopped      ✓ Synced *      mongodb.roadtotech.me        ~/Sites/homelab-mongodb
```

#### `appctl ssl [service]`
Inspects SSL/TLS certificates over port 443. If a service is specified, shows detailed issuer, subject, and expiration data; otherwise, inspects all homelab domains.

```bash
# Check all domains
appctl ssl

# Check a specific service
appctl ssl jellyfin
```

#### `appctl info <service>`
Displays diagnostic information for a workload or Core service:
* Canonical name, directory, and aliases.
* Docker container running status.
* Primary routed domain and Authelia guard status.
* Git tracking details: branch, upstream remote, uncommitted file count.
* Injected environment variable defaults.
* Homepage card configuration.
* SSL certificate details (when `--ssl` is passed).

```bash
appctl info docs --ssl
```

---

### Lifecycle Management

#### `appctl up <service|core>`
Starts the specified service stack in detached mode (`--remove-orphans`).
* For a workload in `~/Sites`: starts containers and calls `appctl sync`.
* For `core`: starts the entire Core Compose stack using `/run/secrets/homeserver.env`.
* For an individual Core service (e.g. `appctl up traefik`): starts that specific container.

#### `appctl down <service|core>`
Stops and removes containers defined in the target's Compose file.

#### `appctl restart <service|core>`
Restarts the target stack or individual container.
* `appctl restart docs`: Restarts the docs workload.
* `appctl restart core`: Restarts all Core Compose services.
* `appctl restart traefik`: Restarts only the Traefik container.

#### `appctl update <service|core>`
Performs a sequential update workflow:
1. Checks that the local git working tree is clean (aborts if uncommitted changes exist).
2. Runs `git pull --ff-only` to fast-forward the current branch.
3. Runs `docker compose pull` to fetch updated container images.
4. Runs `docker compose up -d --remove-orphans` to recreate containers.
5. Runs `appctl sync` to update the Homepage dashboard.

#### `appctl pull <service|core>`
Pulls latest container images and recreates the target service.

#### `appctl logs <service|core>`
Streams container logs (`docker compose logs -f`).
* Supports passing container names: `appctl logs core traefik` or `appctl logs stalwart`.

#### `appctl config <service|core>`
Renders the resolved Docker Compose configuration with all injected variables.

#### `appctl sync`
Scans all `~/Sites/*/app.yaml` manifests and re-generates `config/homepage/services.yaml`.

#### `appctl completion [bash]`
Outputs the bash autocompletion script.

---

## 3. `app.yaml` Fields Consumed by `appctl` and GitOps

`app.yaml` files in `~/Sites` are read by `appctl_engine.py` and `gitops_dispatcher.py` for the following fields:

```yaml
# ── Identity & Routing ────────────────────────────────────────────────────────
name: "jellyfin"                     # Canonical service name (defaults to folder name minus homelab-)
aliases:                             # Alternate CLI aliases
  - "media"
  - "movies"
domain: "jellyfin.roadtotech.me"     # Primary routed domain (defaults to <name>.roadtotech.me)
description: "Media Streaming Server"

# ── Platform Integration ──────────────────────────────────────────────────────
visible: true                        # Display on Homepage dashboard (default: true)
auth: false                          # Authelia ForwardAuth protection (default: false)
networks:                            # Docker networks to join (default: [proxy-net])
  - proxy-net

# ── Injected Environment Defaults ─────────────────────────────────────────────
# Injected into the Compose environment when appctl commands run
env:
  MEDIA_PATH: "/media"
  JELLYFIN_TZ: "UTC"
  JELLYFIN_PUID: "1000"
  JELLYFIN_PGID: "1000"

# ── GitOps Deployment Policy ──────────────────────────────────────────────────
# Evaluated by scripts/gitops_dispatcher.py upon webhook reception
deployment:
  branch: "main"                     # Target branch that triggers deployment
  strategy: "compose"                # Deployment strategy (only 'compose' is permitted)
  actions:                           # Strictly allowlisted actions
    - git_pull
    - compose_up

# ── Homepage Dashboard Metadata ───────────────────────────────────────────────
homepage:
  title: "Jellyfin"                  # Card title
  group: "Media & Productivity"      # Group name
  icon: "jellyfin.png"               # Icon name (matched against config/homepage/icons/)
  container: "jellyfin"              # Container name for live status widget
  weight: 10                         # Card sort weight
```

---

## 4. Environment Injection & Resolution Precedence

When `appctl` executes commands against a workload, environment variables are resolved in this sequence:

1. **Global Secrets & Templates**: Sourced from `/run/secrets/homeserver.env` and `/run/secrets/traefik-deployments.env` (or `/run/secrets/rendered/*` fallbacks).
2. **Infrastructure Defaults**:
   - `PROXY_NETWORK` (defaults to `proxy-net`)
   - `CERT_RESOLVER` (defaults to `myresolver`)
   - `DOMAIN_SUFFIX` (defaults to `roadtotech.me`)
3. **Application Identity**:
   - `SERVICE_NAME` = `<canonical-name>`
   - `CONTAINER_NAME` = `<canonical-name>`
   - `SERVICE_DOMAIN` = `<resolved-domain>`
   - `DOMAIN` = `<resolved-domain>`
4. **Manifest Variables**: Declared under the `env:` block in `app.yaml`.
5. **Local Repository Files**: Sourced from `$APP_DIR/.env` and `$APP_DIR/app.env` if present.

---

## 5. Homepage Dashboard Compilation

When `appctl sync` runs:
1. `appctl_engine.py` reads all `~/Sites/*/app.yaml` files.
2. Core services (`traefik`, `authelia`, `lldap`, `stalwart`, `snappymail`, `dozzle`) are assigned to `Core Infrastructure`.
3. Workload applications with `visible: true` are grouped by their `homepage.group` (ordered: `Core Infrastructure`, `Knowledge & Notes`, `Media & Productivity`, `Development & AI`, `Applications`).
4. Custom icons matching files in `config/homepage/icons/` (SVG or PNG) are mapped to `/icons/<name>`.
5. The compiled configuration is written to `config/homepage/services.yaml`.
