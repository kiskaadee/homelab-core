# Homelab Core — Agent Guidelines

This repository defines the host foundation and Core platform for the
`roadtotech.me` homelab.

It contains NixOS configuration, shared infrastructure services, orchestration
tools, security policy, and operational configuration.

Independent application workloads live in `~/Sites` and are integrated with
Core through their application manifests and Docker Compose configuration.

---

## Architectural Invariants

### 1. Core / Sites Boundary

**Core owns:**
- NixOS host configuration and system services
- Shared platform and infrastructure services
- Ingress, identity, security boundaries, and host-level policy
- Orchestration and deployment mechanics

**`~/Sites` owns:**
- Independently deployed application workloads
- Application-specific Docker Compose configuration
- Application-specific `app.yaml` metadata and workload intent

Do not move workload-specific configuration into Core merely because Core
consumes or manages the workload.

Do not move Core-owned platform services into `~/Sites`.

When ownership is unclear, consult current architecture documentation before
changing the boundary.

### 2. Workload Manifests

Every `~/Sites` workload is self-describing through its `app.yaml`.

Do not hardcode workload-specific domains, ports, environment variables,
containers, or deployment configuration inside `scripts/appctl`,
`scripts/appctl_engine.py`, or GitOps logic.

`app.yaml` describes workload identity, integration metadata, and deployment
intent. Core owns deployment mechanics.

Never introduce arbitrary command execution, host filesystem paths,
privileged operations, or Docker control capabilities into workload manifests.

### 3. GitOps Trust Boundary

GitOps must remain a constrained deployment mechanism.

- Webhook authentication is mandatory.
- Repository identity is resolved through trusted Core logic.
- Filesystem paths must never be selected directly by webhook payloads.
- Deployment execution must remain closed and explicitly allowlisted.
- Repository-controlled configuration must never become arbitrary shell input.
- Deployment serialization and superseding behavior must be preserved.

Do not weaken these constraints for convenience.

### 4. Docker Isolation

- `socket-proxy` is the only Core Compose service that may mount
  `/var/run/docker.sock`.
- The socket proxy must expose only explicitly approved read-only Docker API capabilities; state-changing and command-execution endpoints must remain disabled.
- Do not add `privileged: true` without an explicit architectural decision.
- Workload containers must not gain direct access to the Docker socket.

### 5. Secrets & Persistent State

Never commit plaintext secrets, credentials, password hashes, private keys,
certificates, runtime databases, or other generated/persistent state.

Secrets are managed through SOPS and projected to runtime paths under
`/run/secrets/`.

Do not move secrets into application manifests or source code.

### 6. Declarative Configuration vs Runtime State

Git contains declarative configuration and reproducible definitions.

Persistent runtime state belongs in the appropriate state directories or
Docker volumes and must remain excluded from version control.

Do not convert runtime state into committed configuration merely to simplify
development or recovery.

### 7. Core Service Ownership

Core Compose is the execution boundary for Core-owned platform services.
Workload-specific services belong in `~/Sites` unless an explicit architectural
decision establishes them as Core-owned.

Do not introduce application-specific deployment policy into the Core stack.

Do not make Core orchestration depend on implementation details of a workload
when the workload's declared contract can be used instead.

### 8. License

Preserve The Unlicense across Core and related Sites repositories.

---

## Verification & Architectural Integrity

When a requested change conflicts with an invariant, stop and resolve the
architectural conflict before implementing a workaround.

Before committing architectural or security-sensitive changes:

```bash
./scripts/test
nix flake check
```

Run additional validation appropriate to the change, such as:

```bash
docker compose config -q
```

Architectural invariants should be encoded as tests whenever practical.

Do not bypass failing invariant tests by weakening or removing the invariant
unless the architecture itself has intentionally changed.

---

## Documentation

Use the repository's current architecture and operational documentation as the
source of truth for ownership, protocols, and system behavior.

Historical material under `docs/legacy/` is archival and must not be treated as
current system behavior.

Record significant architectural decisions in the appropriate ADR or Brain
documentation rather than expanding this file into an architecture manual.

All documentation must remain GitHub-flavored Markdown compatible with Obsidian
and the live documentation viewer.

---

## Brain Vault

Capture significant:

- Architectural decisions
- Security/threat-model changes
- Debugging discoveries
- Implementation lessons

in the appropriate `/home/kiskaadee/Brain/homelab/` or
`/home/kiskaadee/Brain/learning/` location.

Do not duplicate entire repository documentation in the Brain Vault.
