"""
Unit test suite for scripts/core_manifest.py (Schema v1.0 & Parser Library).
"""

import sys
from pathlib import Path

import pytest

# Add scripts directory to path
CORE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(CORE_ROOT / "scripts"))

from core_manifest import (
    ManifestValidationError,
    RefType,
    TransportScheme,
    classify_ref,
    parse_manifest,
    validate_transport,
)


def test_validate_transport_https():
    scheme, url = validate_transport("https://gitea.roadtotech.me/kiskaadee/homelab-snappymail.git")
    assert scheme == TransportScheme.HTTPS
    assert url == "https://gitea.roadtotech.me/kiskaadee/homelab-snappymail.git"


def test_validate_transport_ssh():
    scheme, url = validate_transport("ssh://git@gitea.roadtotech.me:2223/kiskaadee/homelab-docs.git")
    assert scheme == TransportScheme.SSH
    assert url == "ssh://git@gitea.roadtotech.me:2223/kiskaadee/homelab-docs.git"


def test_validate_transport_git_scp():
    scheme, url = validate_transport("git@github.com:kiskaadee/custom-workload.git")
    assert scheme == TransportScheme.GIT_SCP
    assert url == "git@github.com:kiskaadee/custom-workload.git"


@pytest.mark.parametrize("insecure_url", [
    "http://insecure.roadtotech.me/repo.git",
    "http://github.com/repo.git",
])
def test_validate_transport_rejects_http(insecure_url: str):
    with pytest.raises(ManifestValidationError, match="Insecure HTTP source URL rejected"):
        validate_transport(insecure_url)


@pytest.mark.parametrize("traversal_url", [
    "../../etc/shadow",
    "/home/kiskaadee/secret",
    "~/vault",
    "file:///etc/passwd",
])
def test_validate_transport_rejects_paths_and_traversal(traversal_url: str):
    with pytest.raises(ManifestValidationError):
        validate_transport(traversal_url)


def test_classify_ref_branches():
    assert classify_ref("main") == (RefType.BRANCH, "main")
    assert classify_ref("refs/heads/develop") == (RefType.BRANCH, "develop")
    assert classify_ref("feat/manifest-engine") == (RefType.BRANCH, "feat/manifest-engine")
    assert classify_ref("release/v1.0") == (RefType.BRANCH, "release/v1.0")


def test_classify_ref_tags():
    assert classify_ref("v1.0.0") == (RefType.TAG, "v1.0.0")
    assert classify_ref("refs/tags/2.4.1") == (RefType.TAG, "2.4.1")
    assert classify_ref("v0.1.0-alpha") == (RefType.TAG, "v0.1.0-alpha")


def test_classify_ref_commit_sha():
    sha = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert classify_ref(sha) == (RefType.COMMIT, sha)


def test_parse_manifest_class_a_source_backed():
    raw_yaml = """
schemaVersion: "1.0"
name: "bitetrack"
aliases:
  - "bt"
domain: "bitetrack.roadtotech.me"
description: "Nutrition and macro tracking application"
visible: true
auth: true
networks:
  - proxy-net
env:
  PORT: 8080
source:
  type: git
  url: "https://git.roadtotech.me/kiskaadee/bitetrack.git"
  ref: "v1.2.0"
deployment:
  strategy: compose
  branch: main
  actions:
    - git_pull
    - compose_build
    - compose_up
homepage:
  title: "BiteTrack"
  group: "Productivity"
  icon: "food.png"
  weight: 25
"""
    manifest = parse_manifest(raw_yaml)
    assert manifest.name == "bitetrack"
    assert manifest.schema_version == "1.0"
    assert manifest.aliases == ["bt"]
    assert manifest.is_class_a is True
    assert manifest.is_class_b is False
    assert manifest.is_internal is False
    assert manifest.source is not None
    assert manifest.source.url == "https://git.roadtotech.me/kiskaadee/bitetrack.git"
    assert manifest.source.ref == "v1.2.0"
    assert manifest.source.ref_type == RefType.TAG
    assert manifest.source.transport == TransportScheme.HTTPS
    assert manifest.deployment.actions == ["git_pull", "compose_build", "compose_up"]
    assert manifest.homepage is not None
    assert manifest.homepage.weight == 25


def test_parse_manifest_class_b_image_only():
    raw_yaml = """
name: "snappymail"
domain: "mail.roadtotech.me"
description: "Webmail Client"
deployment:
  actions:
    - compose_up
"""
    manifest = parse_manifest(raw_yaml)
    assert manifest.name == "snappymail"
    assert manifest.is_class_a is False
    assert manifest.is_class_b is True
    assert manifest.source is None
    assert manifest.deployment.actions == ["compose_up"]


def test_parse_manifest_rejects_missing_name():
    with pytest.raises(ManifestValidationError, match="missing mandatory string field 'name'"):
        parse_manifest("domain: test.roadtotech.me\n")


def test_parse_manifest_rejects_invalid_name():
    with pytest.raises(ManifestValidationError, match="Invalid workload name"):
        parse_manifest("name: 'invalid/name'\n")


def test_parse_manifest_rejects_prohibited_deployment_actions():
    raw_yaml = """
name: "app"
deployment:
  actions:
    - compose_up
    - rm_rf_root
"""
    with pytest.raises(ManifestValidationError, match="Prohibited deployment action 'rm_rf_root'"):
        parse_manifest(raw_yaml)


def test_parse_manifest_rejects_prohibited_strategy():
    raw_yaml = """
name: "app"
deployment:
  strategy: "swarm"
"""
    with pytest.raises(ManifestValidationError, match="Unsupported deployment strategy 'swarm'"):
        parse_manifest(raw_yaml)


def test_parse_manifest_from_path(tmp_path: Path):
    manifest_file = tmp_path / "app.yaml"
    manifest_file.write_text("name: file-app\ndomain: internal\n", encoding="utf-8")

    manifest = parse_manifest(manifest_file)
    assert manifest.name == "file-app"
    assert manifest.is_internal is True
