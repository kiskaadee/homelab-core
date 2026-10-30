"""
Security invariant tests for Docker Compose and container isolation.
"""

import os
import shutil
import subprocess
from pathlib import Path

import yaml
from appctl_audit import AuditLogger, TargetLockManager
from appctl_auth import Principal, Role
from appctl_contracts import OperationRequest
from appctl_router import OperationalRouter, assemble_workload_env
from core_manifest import parse_manifest

# Comment-out reason: testing pyproject-level pythonpath definition
CORE_ROOT = Path(__file__).resolve().parent.parent.parent


def test_socket_proxy_is_strictly_read_only():
    """Ensure socket-proxy container blocks state-modifying requests (POST=0, DELETE=0)."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "socket-proxy" in services, "socket-proxy service must exist"

    env = services["socket-proxy"].get("environment", [])
    env_dict = {}
    for item in env:
        if "=" in item:
            k, v = item.split("=", 1)
            env_dict[k.strip()] = v.strip()

    assert env_dict.get("POST") == "0", "socket-proxy must strictly enforce POST=0"
    assert env_dict.get("DELETE") == "0", "socket-proxy must strictly enforce DELETE=0"
    assert env_dict.get("BUILD") == "0", "socket-proxy must strictly enforce BUILD=0"
    assert env_dict.get("EXEC") == "0", "socket-proxy must strictly enforce EXEC=0"


def test_no_direct_docker_sock_mounts_except_socket_proxy():
    """Ensure no application or control service mounts /var/run/docker.sock except socket-proxy:ro."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    for name, svc in services.items():
        volumes = svc.get("volumes", [])
        for vol in volumes:
            if isinstance(vol, str) and "/var/run/docker.sock" in vol:
                assert name == "socket-proxy", (
                    f"Service '{name}' directly mounts docker.sock!"
                )
                assert vol.endswith(":ro"), (
                    "socket-proxy docker.sock mount must be read-only (:ro)"
                )


def test_no_privileged_containers():
    """Ensure no service in core docker-compose runs with privileged: true."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    for name, svc in services.items():
        privileged = svc.get("privileged", False)
        assert privileged is not True, (
            f"Service '{name}' is configured as privileged: true!"
        )


def test_no_shell_true_in_core_scripts():
    """Ensure shell=True is completely absent from gitops execution engines."""
    scripts_dir = CORE_ROOT / "scripts"
    for py_file in scripts_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8")
        assert "shell=True" not in content, f"shell=True detected in {py_file.name}!"


def test_portainer_is_deprecated_and_absent():
    """Ensure portainer service and volume are completely absent from core docker-compose."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "portainer" not in services, (
        "Deprecated service 'portainer' must not exist in docker-compose.yml"
    )

    volumes = compose.get("volumes", {}) or {}
    assert "portainer_data" not in volumes, (
        "Deprecated volume 'portainer_data' must not exist in docker-compose.yml"
    )


def test_no_eval_in_appctl_script():
    """Ensure scripts/appctl never uses eval on dynamic environment or manifest payloads."""
    appctl_path = CORE_ROOT / "scripts" / "appctl"
    content = appctl_path.read_text(encoding="utf-8")
    assert "eval " not in content, "Dangerous 'eval' detected in scripts/appctl!"


def test_appctl_env_injection_resilience(tmp_path: Path, monkeypatch):
    """Ensure environment values from app.yaml cannot execute shell commands via command substitution
    during P4 production environment assembly and router dispatch. Executes OperationalRouter with a mock docker binary.
    """
    canary = tmp_path / "pwned.txt"
    payload = f"$(touch {canary})"

    # 1. Setup mock Sites directory with vulnerable app.yaml and docker-compose.yml
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "vuln-app"
    app_dir.mkdir(parents=True)

    app_yaml = f"""
name: vuln-app
domain: vuln.example.test
env:
  INJECTION_TEST: '{payload}'
  NORMAL_VAR: 'safe_value'
"""

    (app_dir / "app.yaml").write_text(app_yaml.strip(), encoding="utf-8")
    (app_dir / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: nginx\n", encoding="utf-8"
    )

    # 2. Verify unit-level pure Python environment assembly
    monkeypatch.setenv("HOMELAB_DOMAIN", "example.test")
    manifest = parse_manifest(app_dir / "app.yaml")
    assembled_env = assemble_workload_env(app_dir, manifest, "vuln-app")
    assert not canary.exists(), "Command substitution executed during assemble_workload_env!"
    assert assembled_env["INJECTION_TEST"] == payload
    assert assembled_env["NORMAL_VAR"] == "safe_value"

    # 3. Setup mock docker binary to capture environment without requiring a running Docker daemon
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_docker = mock_bin / "docker"
    bash_bin = shutil.which("bash") or "bash"

    mock_docker_script = f"""#!{bash_bin}
if [ "$1" = "compose" ] && [ "$4" = "config" ]; then
  echo "MOCK_DOCKER_COMPOSE_CONFIG"
  echo "CAPTURED_INJECTION=$INJECTION_TEST"
  echo "CAPTURED_NORMAL=$NORMAL_VAR"
  exit 0
fi
exit 0
"""
    mock_docker.write_text(mock_docker_script, encoding="utf-8")
    mock_docker.chmod(0o755)

    # 4. Dispatch workload.config through OperationalRouter
    monkeypatch.setenv("PATH", f"{mock_bin}:{os.environ.get('PATH', '')}")
    router = OperationalRouter(
        audit_logger=AuditLogger(tmp_path / "audit.jsonl"),
        lock_manager=TargetLockManager(tmp_path / "locks"),
        sites_dir=sites_dir,
        core_dir=CORE_ROOT,
    )
    req = OperationRequest.model_validate({
        "operation": "workload.config",
        "target": "vuln-app",
    })
    admin_principal = Principal(
        identity="operator",
        role=Role.ADMIN,
        token_id="tok_admin",
        auth_method="bearer_token",
    )

    resp = router.dispatch(req, admin_principal)

    # 5. Assertions: command substitution was not executed, router succeeded, mock docker received literal value
    assert not canary.exists(), (
        "Command substitution was executed on the host during environment setup!"
    )
    assert resp.success is True, (
        f"OperationalRouter dispatch failed: {resp.error}"
    )
    assert resp.result is not None
    output = resp.result.get("output", "")
    assert "MOCK_DOCKER_COMPOSE_CONFIG" in output
    assert f"CAPTURED_INJECTION={payload}" in output
    assert "CAPTURED_NORMAL=safe_value" in output


def test_platform_genericism_no_hardcoded_personal_domains_in_scripts_and_tests():
    """Ensure personal domains and operator usernames are absent from scripts, tests, and documentation."""
    forbidden = ["roadtotech.me", "kiskaadee"]
    root_dirs = [
        CORE_ROOT / "scripts",
        CORE_ROOT / "tests",
        CORE_ROOT / "docs",
        CORE_ROOT / "nixos" / "modules",
        CORE_ROOT / "config",
    ]
    root_files = [
        CORE_ROOT / "README.md",
        CORE_ROOT / "ROADMAP.md",
        CORE_ROOT / "AGENTS.md",
        CORE_ROOT / "SECURITY.md",
        CORE_ROOT / "flake.nix",
        CORE_ROOT / "docker-compose.yml",
        CORE_ROOT / "nixos" / "configuration.nix",
    ]
    violations = []

    candidate_files = list(root_files)
    for root_dir in root_dirs:
        candidate_files.extend(root_dir.rglob("*"))

    for file_path in candidate_files:
        if (
            file_path.is_file()
            and file_path.suffix
            in (".py", ".sh", ".bash", ".yml", ".yaml", ".md", ".nix", "")
            and file_path.name != "test_security_invariants.py"
        ):
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            for term in forbidden:
                if term in content:
                    violations.append(
                        f"{file_path.relative_to(CORE_ROOT)}: contains prohibited literal '{term}'"
                    )

    assert not violations, "Platform genericism violations detected:\n" + "\n".join(
        violations
    )


def test_docker_compose_authelia_and_lldap_base_dn():
    """Ensure both lldap and authelia services receive LLDAP_LDAP_BASE_DN in docker-compose.yml."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    assert "lldap" in services, "lldap service must exist in docker-compose.yml"
    assert "authelia" in services, "authelia service must exist in docker-compose.yml"

    def get_env_map(svc_def: dict) -> dict[str, str]:
        env_items = svc_def.get("environment", [])
        res = {}
        for item in env_items:
            if isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                res[k.strip()] = v.strip()
        return res

    lldap_env = get_env_map(services["lldap"])
    authelia_env = get_env_map(services["authelia"])

    assert "LLDAP_LDAP_BASE_DN" in lldap_env, (
        "LLDAP_LDAP_BASE_DN must be declared in lldap environment"
    )
    assert "LLDAP_LDAP_BASE_DN" in authelia_env, (
        "LLDAP_LDAP_BASE_DN must be passed to authelia environment for LDAP template expansion"
    )


def test_nixos_homeserver_env_template_defines_dynamic_base_dn():
    """Ensure homeserver.nix derives LLDAP_LDAP_BASE_DN dynamically from homelab.domain."""
    homeserver_nix = CORE_ROOT / "nixos" / "modules" / "homeserver.nix"
    content = homeserver_nix.read_text(encoding="utf-8")

    assert "LLDAP_LDAP_BASE_DN" in content, (
        "homeserver.nix must define LLDAP_LDAP_BASE_DN in homeserver.env template"
    )
    assert 'lib.splitString "."' in content or "splitString" in content, (
        "homeserver.nix must compute LDAP base DN dynamically from domain components"
    )


def test_nixos_tmpfiles_rules_use_recursive_acls():
    """Ensure tmpfiles.rules uses uppercase A+ for recursive ACL inheritance on sites and homepage."""
    homeserver_nix = CORE_ROOT / "nixos" / "modules" / "homeserver.nix"
    content = homeserver_nix.read_text(encoding="utf-8")

    assert '"A+ ${config.homelab.sitesDir}' in content, (
        "tmpfiles.rules must use uppercase 'A+' for recursive ACL inheritance on sitesDir"
    )
    assert '"A+ ${config.homelab.coreDir}/config/homepage' in content, (
        "tmpfiles.rules must use uppercase 'A+' for recursive ACL inheritance on homepage config"
    )


def test_default_homelab_domain_evaluates_to_example_test():
    """Assert default homelab.domain in NixOS config evaluates to 'example.test'."""
    homeserver_nix = CORE_ROOT / "nixos" / "modules" / "homeserver.nix"
    content = homeserver_nix.read_text(encoding="utf-8")
    assert 'default = "example.test";' in content, (
        "homeserver.nix must set default homelab.domain to 'example.test'"
    )

    nix_bin = shutil.which("nix")
    if nix_bin and not os.environ.get("NIX_BUILD_TOP"):
        cmd = [
            nix_bin,
            "eval",
            ".#nixosConfigurations.server.config.homelab.domain",
            "--raw",
        ]
        res = subprocess.run(
            cmd, cwd=CORE_ROOT, capture_output=True, text=True, check=True
        )
        assert res.stdout.strip() == "example.test", (
            f"Default homelab.domain must be 'example.test', got '{res.stdout.strip()}'"
        )


def test_dynamic_lldap_base_dn_splits_domain_components():
    """Assert dynamic LLDAP_LDAP_BASE_DN splits domain into dc=... components for any domain."""
    homeserver_nix = CORE_ROOT / "nixos" / "modules" / "homeserver.nix"
    content = homeserver_nix.read_text(encoding="utf-8")
    assert "LLDAP_LDAP_BASE_DN = ldapBaseDn;" in content, (
        "homeserver.nix must bind LLDAP_LDAP_BASE_DN to ldapBaseDn in homeserver.env template"
    )
    assert 'domainParts = lib.splitString "." config.homelab.domain;' in content, (
        "homeserver.nix must split homelab.domain by '.'"
    )
    assert 'ldapBaseDn = lib.concatStringsSep "," (map (part: "dc=${part}") domainParts);' in content, (
        "homeserver.nix must map domain components into dc=${part}"
    )

    nix_bin = shutil.which("nix")
    if nix_bin and not os.environ.get("NIX_BUILD_TOP"):
        cases = [
            ("example.test", "dc=example,dc=test"),
            ("my.homelab.internal", "dc=my,dc=homelab,dc=internal"),
            ("local", "dc=local"),
        ]
        for domain, expected_dn in cases:
            expr = (
                f'let flake = builtins.getFlake (toString ./.); lib = flake.inputs.nixpkgs.lib; '
                f'domainParts = lib.splitString "." "{domain}"; '
                f'in lib.concatStringsSep "," (map (part: "dc=${{part}}") domainParts)'
            )
            cmd = [
                nix_bin,
                "eval",
                "--impure",
                "--expr",
                expr,
                "--raw",
            ]
            res = subprocess.run(
                cmd, cwd=CORE_ROOT, capture_output=True, text=True, check=True
            )
            assert res.stdout.strip() == expected_dn, (
                f"Expected {expected_dn} for domain {domain}, got {res.stdout.strip()}"
            )

        # Also assert rendered sops template in default configuration contains correct LLDAP_LDAP_BASE_DN
        cmd_tpl = [
            nix_bin,
            "eval",
            '.#nixosConfigurations.server.config.sops.templates."homeserver.env".content',
            "--raw",
        ]
        res_tpl = subprocess.run(
            cmd_tpl, cwd=CORE_ROOT, capture_output=True, text=True, check=True
        )
        assert "LLDAP_LDAP_BASE_DN=dc=example,dc=test" in res_tpl.stdout


def test_dual_path_import_overrides_homelab_domain():
    """Assert dual-path import overrides homelab.domain when ./nixos/instance.nix is present."""
    nix_bin = shutil.which("nix")
    if nix_bin and not os.environ.get("NIX_BUILD_TOP"):
        instance_nix = CORE_ROOT / "nixos" / "instance.nix"
        try:
            instance_nix.write_text(
                '{ homelab.domain = "custom.internal"; }\n', encoding="utf-8"
            )
            cmd = [
                nix_bin,
                "eval",
                "--impure",
                "path:.#nixosConfigurations.server.config.homelab.domain",
                "--raw",
            ]
            res = subprocess.run(
                cmd, cwd=CORE_ROOT, capture_output=True, text=True, check=True
            )
            assert res.stdout.strip() == "custom.internal", (
                f"Expected homelab.domain to be overridden to 'custom.internal', got '{res.stdout.strip()}'"
            )
        finally:
            if instance_nix.exists():
                instance_nix.unlink()


def test_nixos_configuration_declares_dual_path_instance_imports():
    """Ensure nixos/configuration.nix includes optional dual-path instance imports."""
    config_nix = CORE_ROOT / "nixos" / "configuration.nix"
    content = config_nix.read_text(encoding="utf-8")
    assert "builtins.pathExists ./instance.nix" in content, (
        "nixos/configuration.nix must check for ./instance.nix via builtins.pathExists"
    )
    assert "builtins.pathExists /etc/nixos/homelab-instance.nix" in content, (
        "nixos/configuration.nix must check for /etc/nixos/homelab-instance.nix via builtins.pathExists"
    )


def test_gitignore_includes_instance_nix():
    """Ensure nixos/instance.nix is ignored in .gitignore to preserve platform genericism."""
    gitignore = CORE_ROOT / ".gitignore"
    content = gitignore.read_text(encoding="utf-8")
    lines = [line.strip() for line in content.splitlines()]
    assert "nixos/instance.nix" in lines, (
        "nixos/instance.nix must be explicitly ignored in .gitignore"
    )


