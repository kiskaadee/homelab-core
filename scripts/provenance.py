"""
provenance.py — Two-Axis Inspection & Upstream Provenance Resolver

Evaluates workload health along two independent axes:
1. Local Deployment Axis: Local checkout cleanliness (* Dirty vs. ✓ Clean), branch, commit, and container runtime state.
2. Upstream Provenance Axis: Upstream git repository state resolved via bounded `git ls-remote` (10s timeout).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from core_manifest import Manifest, RefType, SourceConfig


class ProvenanceStatus(str, Enum):
    """Evaluation result of local state vs. upstream software provenance."""
    SYNCED = "✓ Synced"
    UPDATE_AVAILABLE = "↓ Update Available"
    IMAGE_ONLY = "— Image-Only (Class B)"
    DIRTY = "* Dirty"
    UNREACHABLE = "⚠ Unreachable"
    TIMEOUT = "⏱ Timed Out (10s)"
    UNKNOWN = "— Not Checked"
    NOT_CLONED = "✕ Missing Checkout"


@dataclass
class CheckoutStatus:
    """Axis 1: Local deployment checkout state."""
    is_git: bool
    is_dirty: bool
    branch: str
    commit_sha: str
    status_label: str

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "is_git": self.is_git,
            "is_dirty": self.is_dirty,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "status_label": self.status_label,
        }


@dataclass
class UpstreamProvenance:
    """Axis 2: Upstream software provenance state."""
    status: ProvenanceStatus
    remote_sha: str = ""
    target_ref: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "status": self.status.value,
            "remote_sha": self.remote_sha,
            "target_ref": self.target_ref,
            "message": self.message,
        }


@dataclass
class TwoAxisStatus:
    """Unified inspection combining local deployment and upstream provenance."""
    app_name: str
    checkout: CheckoutStatus
    provenance: UpstreamProvenance
    docker_status: str
    image_revision: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "checkout": self.checkout.to_dict(),
            "provenance": self.provenance.to_dict(),
            "docker_status": self.docker_status,
            "image_revision": self.image_revision,
        }


def get_local_checkout_status(dir_path: Path | str) -> CheckoutStatus:
    """
    Inspect local git repository checkout state.
    Reports clean/dirty status, active branch/ref, and current commit SHA.
    """
    path = Path(dir_path)
    git_dir = path / ".git"
    if not git_dir.exists():
        return CheckoutStatus(
            is_git=False,
            is_dirty=False,
            branch="",
            commit_sha="",
            status_label="— No Git Repo",
        )

    # Clean git environment so calls are not polluted by outer hooks
    git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

    # 1. Check cleanliness
    is_dirty = False
    try:
        status_res = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
            env=git_env,
        )
        if status_res.returncode == 0 and status_res.stdout.strip():
            is_dirty = True
    except (subprocess.SubprocessError, OSError):
        is_dirty = False

    # 2. Get active branch
    branch = ""
    try:
        branch_res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
            env=git_env,
        )
        if branch_res.returncode == 0:
            branch = branch_res.stdout.strip()
            if branch == "HEAD":
                # Detached HEAD, check describe
                desc_res = subprocess.run(
                    ["git", "-C", str(path), "describe", "--tags", "--exact-match"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5.0,
                    env=git_env,
                )
                if desc_res.returncode == 0 and desc_res.stdout.strip():
                    branch = desc_res.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        branch = "unknown"

    # 3. Get commit SHA
    commit_sha = ""
    try:
        sha_res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
            env=git_env,
        )
        if sha_res.returncode == 0:
            commit_sha = sha_res.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        commit_sha = ""

    # 4. Format status label
    state_symbol = "* Dirty" if is_dirty else "✓ Clean"
    ref_desc = f"{branch}@{commit_sha}" if commit_sha else branch
    status_label = f"{state_symbol} ({ref_desc})" if ref_desc else state_symbol

    return CheckoutStatus(
        is_git=True,
        is_dirty=is_dirty,
        branch=branch,
        commit_sha=commit_sha,
        status_label=status_label,
    )


def resolve_upstream_provenance(
    source: SourceConfig | None,
    local_commit: str = "",
    timeout: float = 10.0,
) -> UpstreamProvenance:
    """
    Resolve upstream provenance via bounded `git ls-remote`.
    Compares the declared upstream ref with the deployed local commit.
    Bounded by a 10-second timeout to prevent hanging on unresponsive remotes.
    """
    if source is None:
        return UpstreamProvenance(
            status=ProvenanceStatus.IMAGE_ONLY,
            message="Class B workload deploys prebuilt container images directly",
        )

    target_ref = source.ref
    remote_url = source.url

    # Commits pinned directly in manifest cannot be checked via ls-remote ref matching
    if source.ref_type == RefType.COMMIT:
        is_match = (
            local_commit and (
                source.ref.startswith(local_commit) or local_commit.startswith(source.ref)
            )
        )
        status = ProvenanceStatus.SYNCED if is_match else ProvenanceStatus.UPDATE_AVAILABLE
        return UpstreamProvenance(
            status=status,
            remote_sha=source.ref,
            target_ref=source.ref[:7],
            message="Pinned immutable commit SHA",
        )

    cmd = ["git", "ls-remote", "--exit-code", remote_url, target_ref]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return UpstreamProvenance(
            status=ProvenanceStatus.TIMEOUT,
            target_ref=target_ref,
            message=f"Upstream check timed out after {int(timeout)}s ({remote_url})",
        )
    except (subprocess.SubprocessError, OSError) as e:
        return UpstreamProvenance(
            status=ProvenanceStatus.UNREACHABLE,
            target_ref=target_ref,
            message=f"Network error querying upstream: {e}",
        )

    if proc.returncode != 0:
        # Check if ref was not found on remote (exit code 2 for git ls-remote)
        err_msg = proc.stderr.strip() or f"Ref '{target_ref}' not found on remote."
        return UpstreamProvenance(
            status=ProvenanceStatus.UNREACHABLE,
            target_ref=target_ref,
            message=f"Failed to query remote ({remote_url}): {err_msg}",
        )

    # Parse remote SHA from ls-remote output (Format: <SHA>\t<ref_name>)
    remote_sha = ""
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and (
            parts[1] == target_ref or
            parts[1] == f"refs/heads/{target_ref}" or
            parts[1] == f"refs/tags/{target_ref}" or
            parts[1] == f"refs/tags/{target_ref}^{{}}"
        ):
            remote_sha = parts[0]
            # Peaked tags (^{}) take precedence over lightweight tag refs
            if parts[1].endswith("^{}"):
                break

    if not remote_sha and proc.stdout.strip():
        # Fallback: take first match SHA
        remote_sha = proc.stdout.strip().split()[0]

    if not remote_sha:
        return UpstreamProvenance(
            status=ProvenanceStatus.UNREACHABLE,
            target_ref=target_ref,
            message=f"No matching ref found for '{target_ref}' at {remote_url}",
        )

    # Compare local commit with remote SHA
    is_synced = False
    if local_commit and (remote_sha.startswith(local_commit) or local_commit.startswith(remote_sha[:7])):
        is_synced = True

    status = ProvenanceStatus.SYNCED if is_synced else ProvenanceStatus.UPDATE_AVAILABLE
    short_remote = remote_sha[:7]
    msg = f"Upstream at {short_remote}" if not is_synced else "Up to date with upstream"

    return UpstreamProvenance(
        status=status,
        remote_sha=remote_sha,
        target_ref=target_ref,
        message=msg,
    )


def inspect_workload_two_axis(
    app_dir: Path | str,
    manifest: Manifest,
    fetch_upstream: bool = False,
    docker_status: str = "⚪ Not Stack",
    image_revision: str | None = None,
    timeout: float = 10.0,
) -> TwoAxisStatus:
    """
    Perform two-axis status inspection of a single workload.
    """
    path = Path(app_dir)

    # Axis 1: Local Deployment Checkout
    checkout = get_local_checkout_status(path)

    # Axis 2: Upstream Provenance
    if manifest.is_class_b:
        provenance = UpstreamProvenance(
            status=ProvenanceStatus.IMAGE_ONLY,
            message="Class B (Image-Only)",
        )
    elif not fetch_upstream:
        provenance = UpstreamProvenance(
            status=ProvenanceStatus.UNKNOWN,
            target_ref=manifest.source.ref if manifest.source else "",
            message="Pass --fetch to query upstream repository",
        )
    else:
        provenance = resolve_upstream_provenance(
            manifest.source,
            local_commit=checkout.commit_sha,
            timeout=timeout,
        )

    return TwoAxisStatus(
        app_name=manifest.name,
        checkout=checkout,
        provenance=provenance,
        docker_status=docker_status,
        image_revision=image_revision,
    )
