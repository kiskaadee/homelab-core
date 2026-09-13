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


def test_webhook_verification_valid_signature():
    """Ensure valid HMAC-SHA256 signature passes verification."""
    import hashlib
    import hmac

    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}, "ref": "refs/heads/main"}'
    expected_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert gitops_dispatcher.verify_signature(body, expected_hex, secret) is True
    # Support sha256= prefix as well
    assert gitops_dispatcher.verify_signature(body, f"sha256={expected_hex}", secret) is True


def test_webhook_verification_invalid_signature():
    """Ensure incorrect signature is rejected."""
    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}}'
    bad_sig = "a" * 64

    assert gitops_dispatcher.verify_signature(body, bad_sig, secret) is False


def test_webhook_verification_missing_signature():
    """Ensure missing signature header is rejected."""
    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}}'

    assert gitops_dispatcher.verify_signature(body, None, secret) is False
    assert gitops_dispatcher.verify_signature(body, "", secret) is False


def test_webhook_verification_modified_body():
    """Ensure signature mismatch from tampered body fails verification."""
    import hashlib
    import hmac

    secret = b"supersecrettoken123"
    body_original = b'{"repository": {"name": "docs"}}'
    sig = hmac.new(secret, body_original, hashlib.sha256).hexdigest()

    body_tampered = b'{"repository": {"name": "evil-app"}}'
    assert gitops_dispatcher.verify_signature(body_tampered, sig, secret) is False


def test_webhook_verification_modified_signature():
    """Ensure single-character flip in signature fails verification."""
    import hashlib
    import hmac

    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}}'
    sig = list(hmac.new(secret, body, hashlib.sha256).hexdigest())
    sig[0] = "0" if sig[0] != "0" else "1"
    flipped_sig = "".join(sig)

    assert gitops_dispatcher.verify_signature(body, flipped_sig, secret) is False


def test_webhook_verification_empty_secret_fails_closed():
    """Ensure empty or missing shared secret fails closed."""
    import hashlib
    import hmac

    secret = b""
    body = b'{"repository": {"name": "docs"}}'
    sig = hmac.new(b"somekey", body, hashlib.sha256).hexdigest()

    assert gitops_dispatcher.verify_signature(body, sig, secret) is False


def test_webhook_verification_malformed_signature():
    """Ensure non-hex or invalid length signatures fail closed."""
    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}}'

    assert gitops_dispatcher.verify_signature(body, "not-a-valid-hex-digest", secret) is False
    assert gitops_dispatcher.verify_signature(body, "deadbeef", secret) is False
    assert gitops_dispatcher.verify_signature(body, "z" * 64, secret) is False


def test_admission_rejects_non_push_events(tmp_path: Path):
    """Ensure non-push events such as pull_request or release are rejected."""
    payload_pr = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"},
        "pull_request": {"id": 1}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload_pr, event_type="push")
    assert admitted is False
    assert "non-push event" in reason

    payload_normal = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload_normal, event_type="pull_request")
    assert admitted is False
    assert "Only 'push' is allowed" in reason


def test_admission_rejects_tags_and_invalid_refs():
    """Ensure tags or invalid ref structures are rejected from deployment."""
    tag_payload = {
        "ref": "refs/tags/v1.0.0",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(tag_payload, event_type="push")
    assert admitted is False
    assert "Tag push" in reason

    non_head_payload = {
        "ref": "refs/pull/42/head",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(non_head_payload, event_type="push")
    assert admitted is False
    assert "not a head branch" in reason


def test_admission_rejects_unknown_repository(tmp_path: Path, monkeypatch):
    """Ensure unknown repositories cannot be admitted."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "unregistered-app"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload, event_type="push")
    assert admitted is False
    assert "not recognized" in reason


def test_admission_rejects_branch_mismatch(tmp_path: Path, monkeypatch):
    """Ensure push to branch different from declared deployment branch is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: production\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {
        "ref": "refs/heads/staging",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload, event_type="push")
    assert admitted is False
    assert "Branch mismatch" in reason


def test_admission_rejects_unsupported_strategy(tmp_path: Path, monkeypatch):
    """Ensure unsupported deployment strategy in app.yaml is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  strategy: kubernetes\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload, event_type="push")
    assert admitted is False
    assert "Unsupported deployment strategy" in reason


def test_admission_rejects_unsupported_actions(tmp_path: Path, monkeypatch):
    """Ensure unsupported deployment action in app.yaml is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - evil_action\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"}
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(payload, event_type="push")
    assert admitted is False
    assert "Unsupported deployment action" in reason


def test_admission_admits_valid_push(tmp_path: Path, monkeypatch):
    """Ensure valid push to declared branch on registered repo passes admission."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_up\n",
        encoding="utf-8"
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"}
    }
    admitted, target_dir, config, branch, reason = gitops_dispatcher.admit_deployment(payload, event_type="push")
    assert admitted is True
    assert "Admitted" in reason
    assert target_dir == app_dir.resolve()
    assert branch == "main"
    assert config["actions"] == ["git_pull", "compose_up"]



