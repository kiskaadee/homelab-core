"""
Security invariant tests for homelab GitOps execution and admission.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import gitops_dispatcher
import pytest

# Add Core to path so scripts can be imported
# Comment-out reason: testing pyproject-level pythonpath definition
# CORE_ROOT = Path(__file__).resolve().parent.parent.parent
# sys.path.insert(0, str(CORE_ROOT / "scripts"))


def test_custom_shell_action_is_rejected_without_execution(tmp_path: Path):
    """Ensure that arbitrary 'custom' actions fail closed and never invoke a shell."""
    deployment_config = {"branch": "main", "actions": [{"custom": "touch /tmp/pwned"}]}

    with patch("subprocess.run") as mock_run:
        success = gitops_dispatcher.execute_deployment(
            tmp_path, deployment_config, branch="main"
        )

        assert success is False, (
            "execute_deployment must fail closed on 'custom' action"
        )
        mock_run.assert_not_called()


def test_unknown_action_is_rejected(tmp_path: Path):
    """Ensure that unknown action strings fail closed and abort deployment."""
    deployment_config = {"branch": "main", "actions": ["malicious_or_unknown_action"]}

    with patch("subprocess.run") as mock_run:
        success = gitops_dispatcher.execute_deployment(
            tmp_path, deployment_config, branch="main"
        )

        assert success is False, "execute_deployment must reject unknown actions"
        mock_run.assert_not_called()


def test_allowed_actions_use_safe_argument_lists(tmp_path: Path):
    """Ensure that allowed actions execute safely via argument lists without shell=True."""
    deployment_config = {"branch": "main", "actions": ["git_pull"]}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        success = gitops_dispatcher.execute_deployment(
            tmp_path, deployment_config, branch="main"
        )

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
        assert gitops_dispatcher.resolve_repository(payload) is None, (
            f"Expected {payload} to be rejected"
        )


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
        "name: docs\naliases:\n  - doc2site\n  - notes\n", encoding="utf-8"
    )

    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  homelab-docs:\n    path: "{app_dir}"\n    aliases:\n      - docs\n      - doc2site\n      - notes\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    canonical = app_dir.resolve()
    assert gitops_dispatcher.resolve_repository("homelab-docs") == canonical
    assert gitops_dispatcher.resolve_repository("docs") == canonical
    assert gitops_dispatcher.resolve_repository("doc2site") == canonical
    assert gitops_dispatcher.resolve_repository("notes") == canonical


def test_resolve_repository_rejects_vault(tmp_path: Path, monkeypatch):
    """Ensure data vault identities (Brain / second-brain) are not recognized by GitOps."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    brain_dir = tmp_path / "Brain"
    brain_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    assert gitops_dispatcher.resolve_repository("second-brain") is None
    assert gitops_dispatcher.resolve_repository("Brain") is None
    assert gitops_dispatcher.resolve_repository("brain") is None


def test_get_trusted_repository_mapping_from_registry(tmp_path: Path, monkeypatch):
    """Ensure authoritative Core registry (deployments.yaml) is loaded with containment checks."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    app_dir = sites_dir / "my-app"
    app_dir.mkdir()

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    deployments_content = f"""
schemaVersion: "1.0"
repositories:
  my-app:
    path: "{app_dir}"
    aliases:
      - "app-alias"
  escaped-app:
    path: "{outside_dir}"
"""
    deployments_file = tmp_path / "deployments.yaml"
    deployments_file.write_text(deployments_content, encoding="utf-8")

    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", deployments_file)

    mapping = gitops_dispatcher.get_trusted_repository_mapping()
    assert "my-app" in mapping
    assert mapping["my-app"] == app_dir.resolve()
    assert "app-alias" in mapping
    assert mapping["app-alias"] == app_dir.resolve()
    # Out-of-bounds path must be rejected
    assert "escaped-app" not in mapping


def test_unregistered_sites_directory_rejected_when_registry_active(
    tmp_path: Path, monkeypatch
):
    """Ensure unregistered directories in ~/Sites cannot be resolved when deployments.yaml is active."""
    sites_dir = tmp_path / "Sites"
    registered_dir = sites_dir / "homelab-docs"
    unregistered_dir = sites_dir / "rogue-workload"
    registered_dir.mkdir(parents=True)
    unregistered_dir.mkdir(parents=True)
    (registered_dir / "app.yaml").write_text("name: docs\n", encoding="utf-8")
    (unregistered_dir / "app.yaml").write_text("name: rogue\n", encoding="utf-8")

    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  docs:\n    path: "{registered_dir}"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    assert gitops_dispatcher.resolve_repository("docs") == registered_dir.resolve()
    # Unregistered workload must be rejected despite having app.yaml in Sites
    assert gitops_dispatcher.resolve_repository("rogue") is None
    assert gitops_dispatcher.resolve_repository("rogue-workload") is None


def test_missing_or_corrupt_registry_fails_closed(tmp_path: Path, monkeypatch):
    """Ensure missing or corrupted deployments.yaml fails closed without falling back to directory scanning."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "valid-app"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text("name: valid\n", encoding="utf-8")

    missing_registry = tmp_path / "nonexistent-deployments.yaml"
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", missing_registry)
    monkeypatch.setattr(
        gitops_dispatcher, "CONFIG_DIR", tmp_path / "nonexistent-config"
    )

    # Missing registry must fail closed: empty mapping, no resolution
    assert gitops_dispatcher.get_trusted_repository_mapping() == {}
    assert gitops_dispatcher.resolve_repository("valid") is None

    # Corrupt YAML registry must also fail closed
    corrupt_registry = tmp_path / "corrupt-deployments.yaml"
    corrupt_registry.write_text(
        "schemaVersion: 1.0\nrepositories: [bad list syntax", encoding="utf-8"
    )
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", corrupt_registry)

    assert gitops_dispatcher.get_trusted_repository_mapping() == {}
    assert gitops_dispatcher.resolve_repository("valid") is None


def test_webhook_verification_valid_signature():
    """Ensure valid HMAC-SHA256 signature passes verification."""
    import hashlib
    import hmac

    secret = b"supersecrettoken123"
    body = b'{"repository": {"name": "docs"}, "ref": "refs/heads/main"}'
    expected_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()

    assert gitops_dispatcher.verify_signature(body, expected_hex, secret) is True
    # Support sha256= prefix as well
    assert (
        gitops_dispatcher.verify_signature(body, f"sha256={expected_hex}", secret)
        is True
    )


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

    assert (
        gitops_dispatcher.verify_signature(body, "not-a-valid-hex-digest", secret)
        is False
    )
    assert gitops_dispatcher.verify_signature(body, "deadbeef", secret) is False
    assert gitops_dispatcher.verify_signature(body, "z" * 64, secret) is False


def test_admission_rejects_non_push_events(tmp_path: Path):
    """Ensure non-push events such as pull_request or release are rejected."""
    payload_pr = {
        "ref": "refs/heads/main",
        "repository": {"name": "docs"},
        "pull_request": {"id": 1},
    }
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload_pr, event_type="push"
    )
    assert admitted is False
    assert "non-push event" in reason

    payload_normal = {"ref": "refs/heads/main", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload_normal, event_type="pull_request"
    )
    assert admitted is False
    assert "Only 'push' is allowed" in reason


def test_admission_rejects_tags_and_invalid_refs():
    """Ensure tags or invalid ref structures are rejected from deployment."""
    tag_payload = {"ref": "refs/tags/v1.0.0", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        tag_payload, event_type="push"
    )
    assert admitted is False
    assert "Tag push" in reason

    non_head_payload = {"ref": "refs/pull/42/head", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        non_head_payload, event_type="push"
    )
    assert admitted is False
    assert "not a head branch" in reason


def test_admission_rejects_unknown_repository(tmp_path: Path, monkeypatch):
    """Ensure unknown repositories cannot be admitted."""
    sites_dir = tmp_path / "Sites"
    sites_dir.mkdir()
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)

    payload = {"ref": "refs/heads/main", "repository": {"name": "unregistered-app"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload, event_type="push"
    )
    assert admitted is False
    assert "not recognized" in reason


def test_admission_rejects_branch_mismatch(tmp_path: Path, monkeypatch):
    """Ensure push to branch different from declared deployment branch is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: production\n", encoding="utf-8"
    )
    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  docs:\n    path: "{app_dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    payload = {"ref": "refs/heads/staging", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload, event_type="push"
    )
    assert admitted is False
    assert "Branch mismatch" in reason


def test_admission_rejects_unsupported_strategy(tmp_path: Path, monkeypatch):
    """Ensure unsupported deployment strategy in app.yaml is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  strategy: kubernetes\n",
        encoding="utf-8",
    )
    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  docs:\n    path: "{app_dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    payload = {"ref": "refs/heads/main", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload, event_type="push"
    )
    assert admitted is False
    assert "Unsupported deployment strategy" in reason


def test_admission_rejects_unsupported_actions(tmp_path: Path, monkeypatch):
    """Ensure unsupported deployment action in app.yaml is rejected."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - evil_action\n",
        encoding="utf-8",
    )
    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  docs:\n    path: "{app_dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    payload = {"ref": "refs/heads/main", "repository": {"name": "docs"}}
    admitted, _, _, _, reason = gitops_dispatcher.admit_deployment(
        payload, event_type="push"
    )
    assert admitted is False
    assert "Unsupported deployment action" in reason


def test_admission_admits_valid_push(tmp_path: Path, monkeypatch):
    """Ensure valid push to declared branch on registered repo passes admission."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "homelab-docs"
    app_dir.mkdir(parents=True)
    (app_dir / "app.yaml").write_text(
        "name: docs\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_up\n",
        encoding="utf-8",
    )
    registry = tmp_path / "deployments.yaml"
    registry.write_text(
        f'schemaVersion: "1.0"\nrepositories:\n  docs:\n    path: "{app_dir}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(gitops_dispatcher, "SITES_DIR", sites_dir)
    monkeypatch.setattr(gitops_dispatcher, "DEPLOYMENTS_FILE", registry)

    payload = {"ref": "refs/heads/main", "repository": {"name": "docs"}}
    admitted, target_dir, config, branch, reason = gitops_dispatcher.admit_deployment(
        payload, event_type="push"
    )
    assert admitted is True

    assert "Admitted" in reason
    assert target_dir == app_dir.resolve()
    assert branch == "main"
    assert config["actions"] == ["git_pull", "compose_up"]


def test_enqueue_supersedes_pending_revision(tmp_path: Path, monkeypatch):
    """Ensure newer pending deployment overwrites older pending deployment."""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(gitops_dispatcher, "STATE_DIR", state_dir)

    target_dir = tmp_path / "app"
    target_dir.mkdir()

    # Enqueue revision A
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "A"}, "main"
    )
    pending_file = gitops_dispatcher.get_pending_file("app")
    assert json.loads(pending_file.read_text())["deployment_config"]["rev"] == "A"

    # Enqueue revision B (supersedes A)
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "B"}, "main"
    )
    assert json.loads(pending_file.read_text())["deployment_config"]["rev"] == "B"

    # Enqueue revision C (supersedes B)
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "C"}, "main"
    )
    assert json.loads(pending_file.read_text())["deployment_config"]["rev"] == "C"


def test_serialized_worker_race_superseding(tmp_path: Path, monkeypatch):
    """
    Simulate race condition:
    A starts
    B arrives (enqueued)
    C arrives (supersedes B)
    A finishes
    Verify: B is skipped, C is deployed.
    """
    import threading

    state_dir = tmp_path / "state"
    monkeypatch.setattr(gitops_dispatcher, "STATE_DIR", state_dir)

    target_dir = tmp_path / "app"
    target_dir.mkdir()

    executed_revisions = []
    a_started_event = threading.Event()
    b_and_c_enqueued_event = threading.Event()

    def mock_execute(target, config, branch):
        rev = config.get("rev", "unknown")
        executed_revisions.append(rev)
        if rev == "A":
            a_started_event.set()
            # Wait until B and C are enqueued
            b_and_c_enqueued_event.wait(timeout=2.0)
        return True

    monkeypatch.setattr(gitops_dispatcher, "execute_deployment", mock_execute)

    # 1. Enqueue A
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "A"}, "main"
    )

    # Start worker in thread (takes A)
    worker_thread = threading.Thread(
        target=gitops_dispatcher.run_target_worker, args=("app",)
    )
    worker_thread.start()

    # Wait for A to start executing
    assert a_started_event.wait(timeout=2.0) is True

    # 2. B arrives while A is running
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "B"}, "main"
    )

    # 3. C arrives while A is running (supersedes B)
    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main", "rev": "C"}, "main"
    )

    # Let A complete
    b_and_c_enqueued_event.set()

    worker_thread.join(timeout=3.0)

    # Verification: A was executed, B was superseded/skipped, C was executed!
    assert executed_revisions == ["A", "C"], (
        f"Expected ['A', 'C'] but got {executed_revisions}"
    )


def test_post_pull_manifest_validation_rejects_unsupported_action(tmp_path: Path):
    """
    Ensure that if a pulled revision introduces an unsupported action in app.yaml,
    the post-pull validation detects it and fails closed before any compose actions run.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    manifest_file = app_dir / "app.yaml"
    manifest_file.write_text(
        "name: app\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_up\n"
    )
    (app_dir / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  web:\n    image: nginx\n"
    )

    initial_config = {"branch": "main", "actions": ["git_pull", "compose_up"]}

    def mock_pull(cmd, check=True):
        if "pull" in cmd:
            # Simulate git pull bringing in an invalid action in app.yaml
            manifest_file.write_text(
                "name: app\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - evil_action\n"
            )
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_pull) as mock_run:
        success = gitops_dispatcher.execute_deployment(
            app_dir, initial_config, branch="main"
        )

        assert success is False, (
            "Deployment must fail closed when pulled revision has invalid actions"
        )
        # Only git pull was called; compose_up was never reached!
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert len(calls) == 1
        assert "pull" in calls[0]


def test_post_pull_manifest_validation_rejects_branch_policy_mismatch(tmp_path: Path):
    """
    Ensure that if a pulled revision modifies app.yaml to target a different branch,
    post-pull branch validation catches the mismatch and aborts deployment.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    manifest_file = app_dir / "app.yaml"
    manifest_file.write_text(
        "name: app\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_up\n"
    )
    (app_dir / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  web:\n    image: nginx\n"
    )

    initial_config = {"branch": "main", "actions": ["git_pull", "compose_up"]}

    def mock_pull(cmd, check=True):
        if "pull" in cmd:
            # Simulate git pull bringing in an app.yaml that targets 'staging', not 'main'
            manifest_file.write_text(
                "name: app\ndeployment:\n  branch: staging\n  actions:\n    - git_pull\n    - compose_up\n"
            )
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_pull) as mock_run:
        success = gitops_dispatcher.execute_deployment(
            app_dir, initial_config, branch="main"
        )

        assert success is False, (
            "Deployment must fail closed on post-pull branch mismatch"
        )
        calls = [c[0][0] for c in mock_run.call_args_list]
        assert len(calls) == 1
        assert "pull" in calls[0]


def test_post_pull_manifest_adopts_updated_actions(tmp_path: Path):
    """
    Ensure that when a pulled revision declares different valid actions (e.g. compose_build),
    post-pull manifest validation adopts and executes the new revision's actions.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    manifest_file = app_dir / "app.yaml"
    manifest_file.write_text(
        "name: app\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_up\n"
    )
    (app_dir / "docker-compose.yml").write_text(
        "version: '3'\nservices:\n  web:\n    image: nginx\n"
    )

    initial_config = {"branch": "main", "actions": ["git_pull", "compose_up"]}

    def mock_pull(cmd, check=True):
        if "pull" in cmd:
            # Revision updates action to compose_build
            manifest_file.write_text(
                "name: app\ndeployment:\n  branch: main\n  actions:\n    - git_pull\n    - compose_build\n"
            )
        return MagicMock(returncode=0)

    with patch("subprocess.run", side_effect=mock_pull) as mock_run:
        success = gitops_dispatcher.execute_deployment(
            app_dir, initial_config, branch="main"
        )

        assert success is True
        calls = [c[0][0] for c in mock_run.call_args_list]
        # Should call pull, then build, then up
        assert any("pull" in c for c in calls)
        assert any("build" in c for c in calls)


def test_worker_records_status_file(tmp_path: Path, monkeypatch):
    """Ensure worker writes intentional execution status file on completion."""
    state_dir = tmp_path / "state"
    monkeypatch.setattr(gitops_dispatcher, "STATE_DIR", state_dir)

    target_dir = tmp_path / "app"
    target_dir.mkdir()

    monkeypatch.setattr(gitops_dispatcher, "execute_deployment", lambda t, c, b: True)

    gitops_dispatcher.enqueue_deployment(
        target_dir, {"branch": "main"}, "main", commit_sha="abcdef123456"
    )

    success = gitops_dispatcher.run_target_worker("app")
    assert success is True

    status_file = state_dir / "app.status.json"
    assert status_file.is_file()
    status = json.loads(status_file.read_text())
    assert status["target"] == "app"
    assert status["branch"] == "main"
    assert status["commit_sha"] == "abcdef123456"
    assert status["success"] is True
    assert "timestamp" in status


def test_validate_compose_security_rejects_privileged(tmp_path: Path):
    """Ensure docker-compose with privileged: true fails pre-flight validation."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine:latest
    privileged: true
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is False
    assert err is not None and "privileged: true" in err


def test_validate_compose_security_rejects_docker_socket_mount(tmp_path: Path):
    """Ensure docker-compose with /var/run/docker.sock mount fails validation."""
    # Test string syntax
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is False
    assert err is not None and "Docker socket" in err

    # Test dict / long syntax
    compose_file.write_text(
        """
services:
  app:
    image: alpine:latest
    volumes:
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is False
    assert err is not None and "Docker socket" in err


def test_validate_compose_security_rejects_host_network(tmp_path: Path):
    """Ensure docker-compose with network_mode: host fails pre-flight validation."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine:latest
    network_mode: host
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is False
    assert err is not None and "network_mode: host" in err


def test_validate_compose_security_rejects_out_of_bounds_host_bind(tmp_path: Path):
    """Ensure host bind mounts outside workload directory or state root are rejected."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine:latest
    volumes:
      - /etc/shadow:/etc/shadow:ro
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is False
    assert err is not None and "prohibited host bind mount" in err


def test_validate_compose_security_allows_safe_compose(tmp_path: Path):
    """Ensure safe compose definitions with relative binds and named volumes pass validation."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        f"""
services:
  app:
    image: alpine:latest
    volumes:
      - {data_dir}:/data
      - named_vol:/var/log
volumes:
  named_vol:
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is True
    assert err is None


def test_validate_compose_security_nonexistent_file(tmp_path: Path):
    """Ensure validation passes when compose file is absent."""
    compose_file = tmp_path / "nonexistent.yml"
    valid, err = gitops_dispatcher.validate_compose_security(compose_file, tmp_path)
    assert valid is True
    assert err is None


@pytest.mark.parametrize(
    "malicious_volume",
    [
        "../../etc/passwd:/mnt/passwd:ro",
        "./../../Core/nixos/secrets.yaml:/secrets.yaml",
        "../other-workload/data:/data",
    ],
)
def test_validate_compose_security_blocks_relative_traversal(
    tmp_path: Path, malicious_volume: str
):
    """Ensure relative path traversals in volume mounts cannot escape workload root."""
    app_dir = tmp_path / "my-app"
    app_dir.mkdir(parents=True)

    compose_file = app_dir / "docker-compose.yml"
    compose_file.write_text(
        f"services:\n  web:\n    image: nginx\n    volumes:\n      - '{malicious_volume}'\n",
        encoding="utf-8",
    )

    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid is False, (
        f"Expected relative traversal '{malicious_volume}' to be rejected!"
    )
    assert err is not None and "prohibited host bind mount" in err


def test_validate_compose_security_blocks_cross_tenant_state_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ensure workload 'my-app' cannot mount another workload's state directory ('other-app')."""
    sites_dir = tmp_path / "Sites"
    app_dir = sites_dir / "my-app"
    app_dir.mkdir(parents=True)

    state_root = tmp_path / "var/lib/homelab"
    (state_root / "my-app").mkdir(parents=True)
    (state_root / "other-app").mkdir(parents=True)
    monkeypatch.setenv("HOMELAB_STATE_ROOT", str(state_root))

    # Cross-tenant state mount
    compose_file = app_dir / "docker-compose.yml"
    compose_file.write_text(
        f"services:\n  web:\n    image: nginx\n    volumes:\n      - '{state_root}/other-app:/data'\n",
        encoding="utf-8",
    )

    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid is False, "Expected cross-tenant state mount to be rejected!"
    assert err is not None and (
        "prohibited host bind mount" in err or "cross-tenant" in err.lower()
    )

    # Authorized own state mount must pass
    compose_file.write_text(
        f"services:\n  web:\n    image: nginx\n    volumes:\n      - '{state_root}/my-app:/data'\n",
        encoding="utf-8",
    )
    valid_own, err_own = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid_own is True, (
        f"Own state mount should be permitted, got error: {err_own}"
    )


@pytest.mark.parametrize(
    "unsafe_directive",
    [
        "cap_add:\n      - SYS_ADMIN",
        "pid: host",
        "ipc: host",
        "userns_mode: host",
        "devices:\n      - /dev/sda:/dev/sda",
        "security_opt:\n      - apparmor:unconfined",
    ],
)
def test_validate_compose_security_blocks_privilege_escalation_primitives(
    tmp_path: Path, unsafe_directive: str
):
    """Ensure dangerous container breakout capabilities and host namespaces are blocked."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        f"services:\n  app:\n    image: alpine\n    {unsafe_directive}\n",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, tmp_path, app_name="app"
    )
    assert valid is False, f"Directive should be blocked:\n{unsafe_directive}"
    assert err is not None


def test_validate_compose_security_rejects_top_level_include(tmp_path: Path):
    """Ensure top-level Compose include directives are rejected to prevent out-of-tree includes."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
include:
  - /etc/shadow
services:
  app:
    image: alpine
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, tmp_path, app_name="app"
    )
    assert valid is False
    assert err is not None and "include" in err.lower()


def test_validate_compose_security_rejects_symlink_escape(tmp_path: Path):
    """Ensure docker-compose.yml cannot be a symlink escaping the workload directory."""
    app_dir = tmp_path / "my-app"
    app_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside-compose.yml"
    outside_file.write_text("services:\n  app:\n    image: alpine\n", encoding="utf-8")

    compose_link = app_dir / "docker-compose.yml"
    compose_link.symlink_to(outside_file)

    valid, err = gitops_dispatcher.validate_compose_security(
        compose_link, app_dir, app_name="my-app"
    )
    assert valid is False
    assert err is not None and "symlink" in err.lower()


def test_validate_compose_security_rejects_escaping_env_file(tmp_path: Path):
    """Ensure services cannot use env_file to read secrets outside the workload root."""
    app_dir = tmp_path / "my-app"
    app_dir.mkdir(parents=True)
    (app_dir / "valid.env").write_text("A=1\n", encoding="utf-8")

    # 1. Escaping absolute path
    compose_file = app_dir / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine
    env_file: /run/secrets/homeserver.env
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid is False
    assert err is not None and "env_file" in err.lower()

    # 2. Escaping relative path in list
    compose_file.write_text(
        """
services:
  app:
    image: alpine
    env_file:
      - valid.env
      - ../../Core/.env
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid is False
    assert err is not None and "env_file" in err.lower()

    # 3. Valid local env_file
    compose_file.write_text(
        """
services:
  app:
    image: alpine
    env_file: valid.env
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, app_dir, app_name="my-app"
    )
    assert valid is True
    assert err is None


def test_validate_compose_security_rejects_undeclared_named_volumes(tmp_path: Path):
    """Ensure ambiguous volume paths not declared in top-level volumes are rejected."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine
    volumes:
      - rogue_named_volume:/data
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, tmp_path, app_name="my-app"
    )
    assert valid is False
    assert err is not None and "undeclared" in err.lower()


def test_validate_compose_security_handles_string_security_opt(tmp_path: Path):
    """Ensure security_opt specified as string instead of list is strictly checked."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(
        """
services:
  app:
    image: alpine
    security_opt: "apparmor:unconfined"
""",
        encoding="utf-8",
    )
    valid, err = gitops_dispatcher.validate_compose_security(
        compose_file, tmp_path, app_name="my-app"
    )
    assert valid is False
    assert err is not None and "security_opt" in err.lower()
