"""
Security invariant tests for homelab GitOps execution and admission.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add Core to path so scripts can be imported
CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

import gitops_dispatcher


def test_custom_shell_action_is_rejected_without_execution(tmp_path: Path):
    """Ensure that arbitrary 'custom' actions fail closed and never invoke a shell."""
    deployment_config = {
        "branch": "main",
        "actions": [
            {"custom": "touch /tmp/pwned"}
        ]
    }

    with patch("subprocess.run") as mock_run:
        success = gitops_dispatcher.execute_deployment(tmp_path, deployment_config, branch="main")

        assert success is False, "execute_deployment must fail closed on 'custom' action"
        mock_run.assert_not_called()


def test_unknown_action_is_rejected(tmp_path: Path):
    """Ensure that unknown action strings fail closed and abort deployment."""
    deployment_config = {
        "branch": "main",
        "actions": ["malicious_or_unknown_action"]
    }

    with patch("subprocess.run") as mock_run:
        success = gitops_dispatcher.execute_deployment(tmp_path, deployment_config, branch="main")

        assert success is False, "execute_deployment must reject unknown actions"
        mock_run.assert_not_called()


def test_allowed_actions_use_safe_argument_lists(tmp_path: Path):
    """Ensure that allowed actions execute safely via argument lists without shell=True."""
    deployment_config = {
        "branch": "main",
        "actions": ["git_pull"]
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        success = gitops_dispatcher.execute_deployment(tmp_path, deployment_config, branch="main")

        assert success is True
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert isinstance(args[0], list), "Command must be passed as an argument list"
        assert kwargs.get("shell") is not True, "shell=True must never be set"


def test_resolve_repository_rejects_path_traversal(tmp_path: Path, monkeypatch):
    """Ensure that path traversal payloads are strictly rejected before filesystem lookup."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    traversal_payloads = [
        "../../foo",
        "../",
        "../../../etc/passwd",
        "/etc",
        "/home/foo",
        "foo/bar",
        "foo\\bar",
        "app/../../secret",
    ]

    for payload in traversal_payloads:
        assert gitops_dispatcher.resolve_repository(payload) is None, f"Expected {payload} to be rejected"


def test_resolve_repository_rejects_unknown_repositories(tmp_path: Path, monkeypatch):
    """Ensure that unknown repositories cannot be resolved."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    assert gitops_dispatcher.resolve_repository("unregistered-repo") is None
    assert gitops_dispatcher.resolve_repository("evil-service") is None


def test_resolve_repository_rejects_symlink_escapes(tmp_path: Path, monkeypatch):
    """Ensure that symlinks inside Sites pointing outside are rejected as escapes."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    secret_dir = tmp_path / "outside_root" / "sneaky-app"
    secret_dir.mkdir(parents=True)
    (secret_dir / "app.yaml").write_text("name: sneaky\n", encoding="utf-8")

    # Create a symlink inside Sites pointing outside Sites
    escape_link = sites_dir / "sneaky-link"
    escape_link.symlink_to(secret_dir)

    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    # Resolution should catch the escape and refuse to resolve
    assert gitops_dispatcher.resolve_repository("sneaky") is None
    assert gitops_dispatcher.resolve_repository("sneaky-link") is None


def test_resolve_repository_resolves_valid_manifest(tmp_path: Path, monkeypatch):
    """Ensure valid registered apps resolve by folder name, manifest name, and aliases."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\naliases:\n  - doc2site\n  - notes\n",
        encoding="utf-8"
    )

    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    canonical = app_dir.resolve()
    assert gitops_dispatcher.resolve_repository("homelab-docs") == canonical
    assert gitops_dispatcher.resolve_repository("docs") == canonical
    assert gitops_dispatcher.resolve_repository("doc2site") == canonical
    assert gitops_dispatcher.resolve_repository("notes") == canonical


def test_resolve_repository_resolves_vault(tmp_path: Path, monkeypatch):
    """Ensure authoritative data vault identities resolve strictly to BRAIN_DIR."""
    brain_dir = tmp_path / "Brain"
    brain_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "BRAIN_DIR", brain_dir)

    canonical = brain_dir.resolve()
    assert gitops_dispatcher.resolve_repository("second-brain") == canonical
    assert gitops_dispatcher.resolve_repository("Brain") == canonical
    assert gitops_dispatcher.resolve_repository("brain") == canonical

