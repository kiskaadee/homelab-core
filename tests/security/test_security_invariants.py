"""
Security invariant tests for Docker Compose and container isolation.
"""

from pathlib import Path

import yaml

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
                assert name == "socket-proxy", f"Service '{name}' directly mounts docker.sock!"
                assert vol.endswith(":ro"), "socket-proxy docker.sock mount must be read-only (:ro)"


def test_no_privileged_containers():
    """Ensure no service in core docker-compose runs with privileged: true."""
    compose_path = CORE_ROOT / "docker-compose.yml"
    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    services = compose.get("services", {})
    for name, svc in services.items():
        privileged = svc.get("privileged", False)
        assert privileged is not True, f"Service '{name}' is configured as privileged: true!"


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
    assert "portainer" not in services, "Deprecated service 'portainer' must not exist in docker-compose.yml"

    volumes = compose.get("volumes", {}) or {}
    assert "portainer_data" not in volumes, "Deprecated volume 'portainer_data' must not exist in docker-compose.yml"


def test_no_eval_in_appctl_script():
    """Ensure scripts/appctl never uses eval on dynamic environment or manifest payloads."""
    appctl_path = CORE_ROOT / "scripts" / "appctl"
    content = appctl_path.read_text(encoding="utf-8")
    assert "eval " not in content, "Dangerous 'eval' detected in scripts/appctl!"


def test_appctl_env_injection_resilience(tmp_path: Path):
    """Ensure environment values from app.yaml cannot execute shell commands via command substitution."""
    canary = tmp_path / "pwned.txt"
    payload = f"$(touch {canary})"

    import json
    import subprocess

    appctl_path = CORE_ROOT / "scripts" / "appctl"
    appctl_content = appctl_path.read_text(encoding="utf-8")
    assert "eval " not in appctl_content
    assert "while IFS= read -r -d '' k && IFS= read -r -d '' v; do" in appctl_content

    resolved_json = json.dumps({
        "name": "test-app",
        "domain": "test.example.com",
        "dir_path": str(tmp_path),
        "env": {"INJECTION_TEST": payload, "NORMAL_VAR": "safe_value"},
    })

    bash_code = f"""
    set -euo pipefail
    resolved_json='{resolved_json}'
    while IFS= read -r -d '' k && IFS= read -r -d '' v; do
      export "$k=$v"
    done < <(python3 -c "
import json, re, sys
RESERVED = {{'PATH', 'LD_PRELOAD', 'IFS', 'SHELL', 'USER', 'CORE_DIR', 'SITES_DIR', 'ENV_FILE', 'HOMESERVER_ENV'}}
d = json.loads(sys.argv[1])
env_dict = d.get('env', {{}})
if isinstance(env_dict, dict):
    for k, v in env_dict.items():
        k_str = str(k).strip()
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', k_str) or k_str in RESERVED:
            continue
        if v is not None and v != '':
            sys.stdout.buffer.write(k_str.encode('utf-8') + b'\\0' + str(v).encode('utf-8') + b'\\0')
" "$resolved_json")

    echo "INJECTION_TEST=$INJECTION_TEST"
    echo "NORMAL_VAR=$NORMAL_VAR"
    """

    res = subprocess.run(
        ["bash", "-c", bash_code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert not canary.exists(), "Command substitution was executed on the host during environment setup!"
    assert res.returncode == 0, f"Extraction failed: {res.stderr}"
    assert f"INJECTION_TEST={payload}" in res.stdout
    assert "NORMAL_VAR=safe_value" in res.stdout


def test_platform_genericism_no_hardcoded_personal_domains_in_scripts_and_tests():
    """Ensure personal domains and operator usernames are absent from scripts and test files."""
    forbidden = ["roadtotech.me", "kiskaadee"]
    root_dirs = [CORE_ROOT / "scripts", CORE_ROOT / "tests"]
    violations = []

    for root_dir in root_dirs:
        for file_path in root_dir.rglob("*"):
            if (
                file_path.is_file()
                and file_path.suffix in (".py", ".sh", ".bash", ".yml", ".yaml", "")
                and file_path.name != "test_security_invariants.py"
            ):
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for term in forbidden:
                    if term in content:
                        violations.append(f"{file_path.relative_to(CORE_ROOT)}: contains prohibited literal '{term}'")

    assert not violations, "Platform genericism violations detected:\n" + "\n".join(violations)




