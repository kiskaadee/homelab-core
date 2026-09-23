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
