# Homelab Core — Agent Guidelines

## 1. Project Overview & Architectural Boundaries

**Homelab Core** defines the declarative NixOS host foundation, shared platform infrastructure services, security boundaries, and the `appctl` operational control plane. Independent application workloads live in `~/Sites/<app>` and integrate through declarative `app.yaml` manifests and workload-scoped Docker Compose files.

### Core vs. `~/Sites` Ownership
- **Core owns**: NixOS host configuration, shared platform services (edge ingress, SSO/LDAP, mail, log/update monitors), platform-level ports and security policy, and orchestration/deployment mechanics.
- **`~/Sites` owns**: Independently deployed application workloads, workload-specific `docker-compose.yml` topologies, and workload intent (`app.yaml`).
- **Boundary Rule**: Never hardcode *workload-specific* domains, ports, containers, environment variables, or deployment policies in Core (`scripts/appctl*`, GitOps, or Core `docker-compose.yml`), and never move Core platform services into `~/Sites`. When ownership is ambiguous, consult [`docs/architecture.md`](docs/architecture.md) before changing the boundary.

### Context Discovery Hierarchy
Before proposing or implementing changes, establish context in this order:
1. **Architectural Decisions (`docs/decisions/`)**: Check [`docs/decisions/README.md`](docs/decisions/README.md) and local ADRs for binding design commitments.
2. **Runtime Contracts (`docs/`)**: Consult [`docs/architecture.md`](docs/architecture.md), [`docs/appctl.md`](docs/appctl.md), [`docs/gitops.md`](docs/gitops.md), and [`docs/secrets-and-state.md`](docs/secrets-and-state.md) for current behavior.
3. **Trajectory & Active Work (`ROADMAP.md`, `todo.txt`, `git log`)**: Verify whether a capability belongs to the current baseline or a planned milestone before modifying code.

When a requested change conflicts with an architectural invariant or accepted ADR, **stop and surface the conflict** before implementing a workaround.

---

## 2. Build, Lint & Verification Commands

The Python environment (`python3`, `pytest`, `pydantic`, `pyyaml`, `types-pyyaml`) and static analysis tools (`ruff`, `pyright`) are provisioned hermetically by `flake.nix` and automatically symlinked to `.venv` via the Nix `devShell` hook (`.envrc`). Do not create separate `uv` or `pip` virtual environments that split from the Nix store environment.

Every implementation step must close with the automated verification gate before proposing a commit:

```bash
# Full verification gate: Ruff + Pyright + Pytest (124+ tests) + nix flake check + NixOS drv eval
./scripts/test

# Targeted fast iteration commands (inside direnv / nix develop, or via .venv)
ruff check .
pyright
.venv/bin/pytest tests/
```

When modifying container or Compose definitions, also validate Compose syntax:

```bash
docker compose config -q
```

---

## 3. Code Style & Contract Guidelines

- **Fail-Closed Validation (`scripts/core_manifest.py`)**: All external inputs (`app.yaml` manifests, webhook payloads, CLI arguments, daemon payloads) must be validated through strict Pydantic v2 models (`ConfigDict(extra="forbid")`).
- **Explicit Error Handling**: Never swallow diagnostic `stderr` silently (e.g., avoid blind `2>/dev/null` on engine or manifest commands). Surface actionable validation errors while redacting sensitive material.
- **Platform Genericism**: Keep all code, templates, tests, and documentation strictly instance-agnostic:
  - Inject routing and service identities at runtime via environment variables (`HOMELAB_DOMAIN`, `DOMAIN_SUFFIX`).
  - Use RFC 2606 placeholders (`example.com`, `example.org`, `example.test`) in tests and configuration examples—never personal domains or operator usernames.
  - In historical documentation or milestone records, use descriptive phrasing (e.g., *"the configured `smtp.<domain>` endpoint"*) rather than fabricating placeholder domains as historical facts.

---

## 4. Testing Instructions (TDD)

- **TDD-First**: Write or update failing unit, structural, or security tests in `tests/` before or alongside implementation changes.
- **No Tautological Tests**: Every test must import and execute real production modules or scripts (`core_manifest`, `appctl_engine`, `gitops_dispatcher`, `scripts/appctl`) and assert on observable outcomes. Never write tests that only inspect local mock strings, hardcode expected outputs into the fixture under test, or continue passing when the production invariant is broken.
- **Protect Invariants**: Do not bypass failing tests by weakening or deleting an invariant assertion unless the architecture itself has intentionally changed via an approved ADR.

---

## 5. Security & State Invariants

1. **Workload Manifests (`app.yaml`)**: Describe declarative workload identity, routing metadata, and deployment intent only. Never introduce arbitrary command execution, host filesystem paths, privileged flags, or Docker control capabilities into `app.yaml`.
2. **GitOps Trust Boundary (`scripts/gitops_dispatcher.py`)**:
   - Webhook admission requires cryptographic authentication (HMAC-SHA256).
   - Repository identity and filesystem paths must be resolved strictly through trusted Core configuration (`config/deployments.yaml`)—never directly from webhook payloads.
   - Execution must remain allowlisted, serialized (`flock`), pre-validated by the Compose AST security checker, and free of shell interpolation (`shell=False`, no `eval`).
3. **Docker & Privilege Isolation**:
   - `socket-proxy` is the **only** service permitted to mount `/var/run/docker.sock` (strictly `:ro`, read-only API capabilities only; `POST=0`, `DELETE=0`, `BUILD=0`, `EXEC=0`).
   - Never add `privileged: true`, host namespace sharing, or direct Docker socket mounts to workload or control-plane containers.
4. **Declarative Configuration vs. Secrets & Runtime State**:
   - Git stores declarative configuration and reproducible definitions only. Never commit plaintext secrets, keys, password hashes, certificates, or runtime databases.
   - Host secrets are managed via SOPS (`sops-nix`) and projected to `/run/secrets/`.
   - Persistent mutable runtime state belongs in dedicated host state directories (`~/var/lib/homelab/<app>/`) or Docker volumes excluded from version control.

---

## 6. Documentation, Task Tracking & Git Workflow

- **Synchronize Documentation (`docs/`)**: Update repository documentation alongside code changes that alter behavior, contracts, or operations. Keep all Markdown formatted in GitHub-Flavored Markdown compatible with both Obsidian and the live documentation viewer.
- **Architecture Decision Records (`docs/decisions/`)**: Record binding repository-level architectural decisions in MADR 4.0 format (`NNNN-slug.md`) and register them in [`docs/decisions/README.md`](docs/decisions/README.md).
- **Live Task Tracking (`todo.txt`)**: Maintain a comment-free root `todo.txt` formatted for `tuxedo` / `todo.sh` (`(A)`–`(C)` priorities, `+<project>`, `@<context>`) reflecting the current milestone. Mark verified deliverables completed (`x YYYY-MM-DD ...`) immediately.
- **Git & Commits**:
  - Never commit directly to `main`/`master`.
  - Never run destructive Git commands (`reset --hard`, `restore`, `clean -fd`, `push --force`) without explicit user confirmation.
  - Stage atomic changes and use Conventional Commits (`feat`, `fix`, `refactor`, `test`, `docs`, `chore`), waiting for user approval before committing.
  - Preserve [The Unlicense](UNLICENSE) across the repository.
