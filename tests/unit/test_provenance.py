"""
Unit test suite for scripts/provenance.py (Two-Axis Inspection & Provenance Resolver).
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

# # Add scripts directory to path
# CORE_ROOT = Path(__file__).resolve().parent.parent.parent
# sys.path.insert(0, str(CORE_ROOT / "scripts"))
from core_manifest import Manifest, RefType, SourceConfig, TransportScheme
from provenance import (
    ProvenanceStatus,
    get_local_checkout_status,
    inspect_workload_two_axis,
    resolve_upstream_provenance,
)


def _clean_git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_AUTHOR_NAME"] = "Test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    return env


def test_get_local_checkout_status_non_git(tmp_path: Path):
    non_git = tmp_path / "app"
    non_git.mkdir()
    status = get_local_checkout_status(non_git)
    assert status.is_git is False
    assert status.is_dirty is False
    assert "No Git Repo" in status.status_label


def test_get_local_checkout_status_clean_repo(tmp_path: Path):
    app_dir = tmp_path / "git_app"
    app_dir.mkdir()
    env = _clean_git_env()
    subprocess.run(
        ["git", "-C", str(app_dir), "init", "-b", "main"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(app_dir), "config", "user.email", "test@example.com"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(app_dir), "config", "user.name", "Test"], check=True, env=env
    )
    (app_dir / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(app_dir), "add", "file.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(app_dir), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        env=env,
    )

    status = get_local_checkout_status(app_dir)
    assert status.is_git is True
    assert status.is_dirty is False
    assert status.branch == "main"
    assert status.commit_sha != ""
    assert "✓ Clean" in status.status_label


def test_get_local_checkout_status_dirty_repo(tmp_path: Path):
    app_dir = tmp_path / "git_dirty_app"
    app_dir.mkdir()
    env = _clean_git_env()
    subprocess.run(
        ["git", "-C", str(app_dir), "init", "-b", "main"],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(app_dir), "config", "user.email", "test@example.com"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(app_dir), "config", "user.name", "Test"], check=True, env=env
    )
    (app_dir / "file.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "-C", str(app_dir), "add", "file.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(app_dir), "commit", "-m", "init"],
        check=True,
        capture_output=True,
        env=env,
    )

    # Modify file to create dirty state
    (app_dir / "file.txt").write_text("modified", encoding="utf-8")

    status = get_local_checkout_status(app_dir)
    assert status.is_git is True
    assert status.is_dirty is True
    assert "* Dirty" in status.status_label


def test_resolve_upstream_provenance_image_only():
    prov = resolve_upstream_provenance(None)
    assert prov.status == ProvenanceStatus.IMAGE_ONLY


def test_resolve_upstream_provenance_synced():
    source = SourceConfig(
        url="https://git.example.com/operator/app.git",
        ref="main",
        ref_type=RefType.BRANCH,
        transport=TransportScheme.HTTPS,
    )
    mock_sha = "abc1234567890abcdef1234567890abcdef12345"
    mock_output = f"{mock_sha}\trefs/heads/main\n"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
        prov = resolve_upstream_provenance(source, local_commit="abc1234", timeout=10.0)

        assert prov.status == ProvenanceStatus.SYNCED
        assert prov.remote_sha == mock_sha
        assert "Up to date" in prov.message
        # Verify 10s timeout was passed to subprocess.run
        assert mock_run.call_args[1]["timeout"] == 10.0


def test_resolve_upstream_provenance_update_available():
    source = SourceConfig(
        url="https://git.example.com/operator/app.git",
        ref="main",
        ref_type=RefType.BRANCH,
        transport=TransportScheme.HTTPS,
    )
    mock_sha = "fff9999999990abcdef1234567890abcdef12345"
    mock_output = f"{mock_sha}\trefs/heads/main\n"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
        prov = resolve_upstream_provenance(source, local_commit="abc1234", timeout=10.0)

        assert prov.status == ProvenanceStatus.UPDATE_AVAILABLE
        assert prov.remote_sha == mock_sha
        assert "Upstream at fff9999" in prov.message


def test_resolve_upstream_provenance_timeout():
    source = SourceConfig(
        url="https://slow-remote.test/repo.git",
        ref="main",
        ref_type=RefType.BRANCH,
        transport=TransportScheme.HTTPS,
    )

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=10.0),
    ):
        prov = resolve_upstream_provenance(source, local_commit="abc1234", timeout=10.0)

        assert prov.status == ProvenanceStatus.TIMEOUT
        assert "timed out after 10s" in prov.message


def test_resolve_upstream_provenance_unreachable():
    source = SourceConfig(
        url="https://bad-remote.test/repo.git",
        ref="main",
        ref_type=RefType.BRANCH,
        transport=TransportScheme.HTTPS,
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stderr="fatal: remote error")
        prov = resolve_upstream_provenance(source, local_commit="abc1234", timeout=10.0)

        assert prov.status == ProvenanceStatus.UNREACHABLE
        assert "Failed to query remote" in prov.message


def test_resolve_upstream_provenance_pinned_commit():
    sha = "1234567890abcdef1234567890abcdef12345678"
    source = SourceConfig(
        url="https://git.example.com/app.git",
        ref=sha,
        ref_type=RefType.COMMIT,
        transport=TransportScheme.HTTPS,
    )
    # Matched local commit
    prov = resolve_upstream_provenance(source, local_commit="1234567")
    assert prov.status == ProvenanceStatus.SYNCED

    # Differing local commit
    prov_diff = resolve_upstream_provenance(source, local_commit="9999999")
    assert prov_diff.status == ProvenanceStatus.UPDATE_AVAILABLE


def test_inspect_workload_two_axis(tmp_path: Path):
    manifest = Manifest(
        name="test-app",
        source=SourceConfig(
            url="https://git.test/app.git",
            ref="main",
            ref_type=RefType.BRANCH,
            transport=TransportScheme.HTTPS,
        ),
    )

    # Without fetch_upstream: Axis 2 returns UNKNOWN
    status_no_fetch = inspect_workload_two_axis(
        tmp_path, manifest, fetch_upstream=False
    )
    assert status_no_fetch.provenance.status == ProvenanceStatus.UNKNOWN
    assert "Pass --fetch" in status_no_fetch.provenance.message

    # With fetch_upstream: Axis 2 queries remote
    with patch("provenance.resolve_upstream_provenance") as mock_resolve:
        mock_resolve.return_value = MagicMock(status=ProvenanceStatus.SYNCED)
        status_fetch = inspect_workload_two_axis(
            tmp_path, manifest, fetch_upstream=True
        )
        assert status_fetch.provenance.status == ProvenanceStatus.SYNCED
        mock_resolve.assert_called_once()


def test_resolve_upstream_provenance_class_b_and_default_timeout():
    # 1. Class B workload (source is None)
    prov_class_b = resolve_upstream_provenance(None)
    assert prov_class_b.status == ProvenanceStatus.IMAGE_ONLY
    assert prov_class_b.message == "Class B workload deploys prebuilt container images directly"

    # 2. Default timeout (10.0s) verification
    source = SourceConfig(
        url="https://git.example.com/operator/app.git",
        ref="main",
        ref_type=RefType.BRANCH,
        transport=TransportScheme.HTTPS,
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="abc1234567890abcdef1234567890abcdef12345\trefs/heads/main\n")
        prov = resolve_upstream_provenance(source, local_commit="abc1234")
        assert prov.status == ProvenanceStatus.SYNCED
        assert mock_run.call_args[1]["timeout"] == 10.0
