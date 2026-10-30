# 📋 Implementation Plan: Control Plane Hardening, Instance Decoupling & MCP Alignment

**Document Version**: 1.0  
**Status**: 🚀 Approved for Implementation  
**Date**: 2026-10-23  
**Target Milestone**: P4 Hardening & P6 Deprecation  
**Branch**: `refactor/appctl-rework`  

---

## 1. Executive Summary & Context

During the pre-merge readiness investigation of the `refactor/appctl-rework` branch, four deployment blockers and several architectural divergences were identified:
1. **Unconfigured Root Domain**: `options.homelab.domain` defaulted to `"example.test"`, which would overwrite live routing templates (`homeserver.env`, `traefik-deployments.env`) upon `nixos-rebuild switch`.
2. **Authelia/LLDAP Base DN Desynchronization**: Compose defaulted `LLDAP_LDAP_BASE_DN` to `dc=example,dc=test`, conflicting with the existing on-host LLDAP database (`dc=<domain-prefix>,dc=<tld>`).
3. **Operator Identity Coupling**: Core configurations referenced legacy operator username literals rather than a generic system operator (`operator`).
4. **POSIX ACL & Git Dubious Ownership**: `tmpfiles.rules` used lowercase `a+` (non-recursive), leaving existing checkouts in `~/Sites` unwriteable to `homelab-appctl`. Furthermore, Git 2.35.2+ lacked `safe.directory` declarations for `homelab-appctl`.
5. **AI Authoring Boundary Evolution**: Earlier architectural planning explored an on-server 5-stage deployment authoring pipeline (`ApplicationProfile` $\to$ `DeploymentProposal` $\to$ `DeploymentModel` compiler). This requirement is formally deprecated in favor of a decoupled Model Context Protocol (MCP) tool gateway.

This plan establishes the concrete implementation work packages to resolve these findings and ensure 100% deployment safety.

---

## 2. Architectural Decisions & Scope

### A. Operator Identity Decoupling (Legacy Operator $\to$ `operator`)
- Standardize the primary platform operator as `operator` with home directory `/home/operator`.
- `config.homelab.operatorUser` defaults to `"operator"`.
- NixOS declares user `operator` (`uid = 1000` or standard user, `extraGroups = [ "wheel" "docker" "homelab-operators" "networkmanager" ]`).
- Eliminates hardcoded personal usernames from the Core platform foundation.

### B. Platform Genericism & Instance Profile (Decision D1)
- Core modules in `nixos/modules/` remain strictly generic, defaulting to RFC 2606 placeholders (`example.test` and `operator`).
- **Decision D1: Dual-Path Instance Domain Injection**: Rather than committing host-specific production domains to tracked Git files, `nixos/configuration.nix` implements an optional dual-path import mechanism:
  ```nix
  imports = [
    ./hardware-configuration.nix
    ./modules/homeserver.nix
    ./modules/traefik-deployments.nix
    ./modules/dynu.nix
    ./modules/shell.nix
  ] ++ lib.optional (builtins.pathExists ./instance.nix) ./instance.nix
    ++ lib.optional (builtins.pathExists /etc/nixos/homelab-instance.nix) /etc/nixos/homelab-instance.nix;
  ```
- `nixos/instance.nix` is git-ignored, allowing host-specific overrides (like `homelab.domain = "example.com";`) without dirtying version control. Alternatively, `/etc/nixos/homelab-instance.nix` provides a persistent host-level configuration path outside the repository checkout.
- `LLDAP_LDAP_BASE_DN` is computed dynamically in Nix from `config.homelab.domain` (`domain: example.test` $\implies$ `dc=example,dc=test`), eliminating hardcoded domain strings across templates and Compose files.

### C. Control Plane Security & POSIX Permissions
- **Recursive ACLs**: Change `a+` to uppercase `A+` for `${config.homelab.sitesDir}` and `${config.homelab.coreDir}/config/homepage` in `systemd.tmpfiles.rules`.
- **Git Safe Directory**: Configure `programs.git.config.safe.directory = [ "/home/operator/Sites/*" ]` for the host.
- **Fail-Closed CLI**: If `/run/homelab/appctl.sock` is unavailable, `scripts/appctl` fails closed with an informative error rather than silently falling back to unauthenticated Bash execution.
- **CLI Status Restoration**: Restore live container inspection (`get_docker_status()`) in `workload.list` and format the `STATUS` column in `scripts/appctl_cli.py`.

### D. Deprecation of On-Server AI Authoring Pipeline (MCP Gateway Adoption)
- As documented in `~/Brain/02-discussions/homelab/mcp-gateway-and-platform-evolution.md`, on-server AI deployment agents (`DeploymentProposal` compilers) are deprecated.
- Milestone P6 is redefined as the **Homelab MCP Gateway**:
  - Exposes deterministic `appctl` operations over JSON-RPC Model Context Protocol tools (`appctl_status`, `appctl_deploy`, `appctl_logs`, `appctl_audit`).
  - Gated by native client-side human confirmation (Antigravity, Claude Code, Cursor).
  - Keeps Core 100% deterministic with zero autonomous agent loops executing on the appliance host.

---

## 3. Work Breakdown Structure (WBS)

### Phase 1: Host Configuration & Identity Migration
- [x] **Task 1.1**: Update `nixos/configuration.nix` to declare user `operator` and configure `homelab.operatorUser = "operator"` and `homelab.domain` with the configured production domain.
- [x] **Task 1.2**: Declare `programs.git.config.safe.directory = [ "/home/operator/Sites/*" ]` in `nixos/configuration.nix`.
- [x] **Task 1.3**: Derive `LLDAP_LDAP_BASE_DN` dynamically from `config.homelab.domain` in `nixos/modules/homeserver.nix` and pass it to `sops.templates."homeserver.env"`.
- [x] **Task 1.4**: Update `docker-compose.yml` to consume `LLDAP_LDAP_BASE_DN` from `homeserver.env`.
- [x] **Task 1.5**: Update `systemd.tmpfiles.rules` in `nixos/modules/homeserver.nix` from `a+` to recursive `A+` on `sitesDir`.

### Phase 2: CLI Hardening & Status Parity
- [x] **Task 2.1**: Update `scripts/appctl` to fail closed when `[ ! -S "$SOCKET_PATH" ]`, removing the silent fallback to legacy Bash.
- [x] **Task 2.2**: Update `_op_workload_list` in `scripts/appctl_router.py` to query live container health (`get_docker_status()`).
- [x] **Task 2.3**: Update `_format_workload_table` in `scripts/appctl_cli.py` to render the `STATUS` column (`🟢 Running (1)` / `🔴 Stopped`).
- [x] **Task 2.4**: Bootstrap operator admin token documentation and setup instructions in `docs/appctl.md` and `docs/secrets-and-state.md`.
- [x] **Task 2.5**: Wire `--build` and `--no-cache` in `scripts/appctl_cli.py`, pass `--pull` during `compose_build` in `scripts/appctl_router.py`, and document source-built action policy in `docs/appctl.md`.

### Phase 3: Architectural Records & Roadmap Synchronization
- [x] **Task 3.1**: Update `ROADMAP.md` (P6 redefined as Homelab MCP Gateway; on-server AI compiler deprecated).
- [x] **Task 3.2**: Add architectural amendment note to `docs/decisions/0002-appctl-control-plane-architecture.md`.
- [x] **Task 3.3**: Update `docs/decisions/README.md`.
- [x] **Task 3.4**: Update `docs/appctl.md` (clarifying bounded log snapshots and Core update workflow).
- [x] **Task 3.5**: Synchronize `todo.txt` with active implementation tasks.

### Phase 4: Work Package 6 (WP6: Instance Decoupling & Operator Cutover Runbook)
- [x] **Task 4.1**: Update `nixos/configuration.nix` with optional dual-path imports for `./instance.nix` and `/etc/nixos/homelab-instance.nix` (Decision D1).
- [x] **Task 4.2**: Add `nixos/instance.nix` to `.gitignore` to preserve 100% repository genericism.
- [x] **Task 4.3**: Implement automated invariant tests in `tests/security/test_security_invariants.py` (default domain evaluation, dynamic LDAP base DN, instance override, platform genericism).
- [x] **Task 4.4**: Document instance domain injection procedure in `docs/setup_guide.md`.
- [x] **Task 4.5**: Codify the 4-stage Staged Coexistence Cutover Runbook (Decision D2) in `docs/operations.md` with explicit notice blocking live host execution pending maintenance window.

---

## 4. Verification & Quality Gates

The implementation must pass:
1. **Static Analysis & Linting**: `ruff check .` and `pyright` (0 errors, 0 warnings).
2. **Full Regression Suite**: All 283 existing unit, security, and invariant tests passing.
3. **New Test Coverage**:
   - Assert dynamic LDAP base DN computation from domain.
   - Assert `scripts/appctl` fails closed when socket is absent.
   - Assert `appctl list` output formatting includes container status.
   - Assert platform genericism invariants across updated Nix modules.
4. **Hermetic Flake Check**: `nix flake check` builds cleanly.
5. **NixOS Evaluation**: `nix eval .#nixosConfigurations.server.config.system.build.toplevel.drvPath` evaluates without warnings.
