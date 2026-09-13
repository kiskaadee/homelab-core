"""
Structural invariant tests for Homelab Core repository layout.
Ensures required architectural components exist and deprecated paths do not return.
"""

from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent.parent


def test_required_architectural_paths_exist():
    """Ensure core architectural files and directories exist."""
    required_paths = [
        "docker-compose.yml",
        "flake.nix",
        "UNLICENSE",
        "AGENTS.md",
        "README.md",
        "SECURITY.md",
        "nixos",
        "nixos/configuration.nix",
        "nixos/modules/homeserver.nix",
        "nixos/secrets.yaml",
        "scripts/gitops_dispatcher.py",
        "scripts/appctl",
        "scripts/appctl_engine.py",
        "tests",
    ]

    for p in required_paths:
        target = CORE_ROOT / p
        assert target.exists(), f"Required architectural path missing: {p}"


def test_deprecated_legacy_structures_are_absent():
    """Ensure deprecated files and legacy layouts are not reintroduced."""
    prohibited_paths = [
        "infra/core",
        "up.sh",
        "down.sh",
        "scripts/check-ssl.sh",
        "scripts/purge-dynu.sh",
        "scripts/ip-monitor.sh",
    ]

    for p in prohibited_paths:
        target = CORE_ROOT / p
        assert not target.exists(), f"Prohibited deprecated path detected: {p}"


def test_unlicense_preserved():
    """Ensure The Unlicense (public domain invariant) is maintained."""
    unlicense = CORE_ROOT / "UNLICENSE"
    assert unlicense.is_file(), "UNLICENSE file must be present"
    content = unlicense.read_text(encoding="utf-8")
    assert "This is free and unencumbered software released into the public domain." in content
