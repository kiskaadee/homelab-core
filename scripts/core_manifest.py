"""
core_manifest.py — Authoritative Workload Manifest Schema (v1.0) & Parser Library

Governs the contract for all ~/Sites workloads:
- Strongly-typed dataclass representations (Manifest, SourceConfig, DeploymentConfig, HomepageConfig)
- Strict source transport scheme validation (https://, ssh://, git@)
- Explicit ref classification (moving branches vs. pinned tags vs. commit SHAs)
- Workload classification: Class A (Source-Backed) vs. Class B (Image-Only)
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


class ManifestValidationError(Exception):
    """Raised when an application manifest violates schema or security constraints."""



class RefType(str, Enum):
    """Semantic classification of an upstream git reference."""
    BRANCH = "branch"      # Moving branch (e.g. main, master, develop, release/v1)
    TAG = "tag"            # Pinned release tag (e.g. v1.0.0, 2.3.1)
    COMMIT = "commit"      # Immutable commit SHA (40-character hex)
    UNKNOWN = "unknown"


class TransportScheme(str, Enum):
    """Supported secure git network transports."""
    HTTPS = "https"
    SSH = "ssh"
    GIT_SCP = "git_scp"


# Security regexes
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SHORT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,12}$")
SEMVER_TAG_RE = re.compile(r"^v?[0-9]+(\.[0-9]+)+([a-zA-Z0-9._-]*)$")
SCP_URL_RE = re.compile(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+:([0-9]+/)?([a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+(\.git)?$")

ALLOWED_STRATEGIES = {"compose"}
ALLOWED_ACTIONS = {"git_pull", "compose_up", "compose_build", "compose_restart"}


def validate_transport(url: str) -> tuple[TransportScheme, str]:
    """
    Validate that an upstream source URL uses an approved secure transport.
    Rejects raw host paths, traversal attempts, and insecure protocols (e.g. http://, file://).
    """
    if not isinstance(url, str) or not url.strip():
        raise ManifestValidationError("Source URL cannot be empty.")

    clean_url = url.strip()

    # Reject path traversal / directory escapes
    if ".." in clean_url or "\\" in clean_url or clean_url.startswith(("/", "~")):
        raise ManifestValidationError(f"Prohibited filesystem path or traversal in source URL: '{clean_url}'")

    # 1. HTTPS transport
    if clean_url.startswith("https://"):
        parsed = urlparse(clean_url)
        if not parsed.netloc:
            raise ManifestValidationError(f"Malformed HTTPS source URL (missing host): '{clean_url}'")
        if not parsed.path or parsed.path == "/":
            raise ManifestValidationError(f"Malformed HTTPS source URL (missing repository path): '{clean_url}'")
        return TransportScheme.HTTPS, clean_url

    # 2. Standard SSH transport (ssh://[user@]host[:port]/path)
    if clean_url.startswith("ssh://"):
        parsed = urlparse(clean_url)
        if not parsed.netloc:
            raise ManifestValidationError(f"Malformed SSH source URL (missing host): '{clean_url}'")
        if not parsed.path or parsed.path == "/":
            raise ManifestValidationError(f"Malformed SSH source URL (missing repository path): '{clean_url}'")
        return TransportScheme.SSH, clean_url

    # 3. SCP-style SSH transport (git@host:owner/repo.git)
    if clean_url.startswith("git@") or ("@" in clean_url and ":" in clean_url and not clean_url.startswith("http")):
        if SCP_URL_RE.match(clean_url):
            return TransportScheme.GIT_SCP, clean_url
        raise ManifestValidationError(f"Malformed SCP-style source URL: '{clean_url}'")

    # Reject plaintext HTTP or unsupported schemes
    if clean_url.startswith("http://"):
        raise ManifestValidationError(f"Insecure HTTP source URL rejected (HTTPS or SSH required): '{clean_url}'")

    raise ManifestValidationError(
        f"Unsupported source URL scheme: '{clean_url}'. "
        "Allowed transports are https://, ssh://, or git@<host>:<path>."
    )


def classify_ref(ref_str: str) -> tuple[RefType, str]:
    """
    Classify a git ref into semantic categories (BRANCH, TAG, COMMIT)
    and return the normalized identifier.
    """
    if not isinstance(ref_str, str) or not ref_str.strip():
        return RefType.BRANCH, "main"

    ref = ref_str.strip()

    # Explicit git ref namespace
    if ref.startswith("refs/heads/"):
        return RefType.BRANCH, ref.removeprefix("refs/heads/")
    if ref.startswith("refs/tags/"):
        return RefType.TAG, ref.removeprefix("refs/tags/")

    # Full 40-character commit SHA
    if COMMIT_SHA_RE.match(ref):
        return RefType.COMMIT, ref

    # Version tags (e.g. v1.0.0, 2.4.1, v0.2-alpha)
    if SEMVER_TAG_RE.match(ref):
        return RefType.TAG, ref

    # Standard branch naming patterns
    if ref in {"main", "master", "develop", "dev", "trunk", "staging", "prod", "production"}:
        return RefType.BRANCH, ref

    if ref.startswith(("feat/", "fix/", "release/", "chore/", "refactor/")):
        return RefType.BRANCH, ref

    # Fallback default: non-versioned arbitrary string treated as branch
    return RefType.BRANCH, ref


@dataclass
class SourceConfig:
    """Upstream source provenance descriptor."""
    url: str
    ref: str = "main"
    type: str = "git"
    ref_type: RefType = RefType.BRANCH
    transport: TransportScheme = TransportScheme.HTTPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "url": self.url,
            "ref": self.ref,
            "ref_type": self.ref_type.value,
            "transport": self.transport.value,
        }


@dataclass
class DeploymentConfig:
    """Continuous deployment strategy and action policy."""
    strategy: str = "compose"
    branch: str = "main"
    actions: list[str] = field(default_factory=lambda: ["git_pull", "compose_up"])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HomepageConfig:
    """Dashboard card metadata for Homepage."""
    title: str
    group: str = "Applications"
    icon: str = "default.png"
    container: str = ""
    weight: int = 50

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Manifest:
    """
    Authoritative Workload Manifest Model (Schema v1.0).
    Represents the full contract declared in a workload's app.yaml.
    """
    name: str
    schema_version: str = "1.0"
    domain: str = ""
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    visible: bool = True
    auth: bool = True
    networks: list[str] = field(default_factory=lambda: ["proxy-net"])
    env: dict[str, Any] = field(default_factory=dict)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    source: SourceConfig | None = None
    homepage: HomepageConfig | None = None

    @property
    def is_class_a(self) -> bool:
        """Class A: Source-backed workload requiring upstream repository checkout & build."""
        return self.source is not None

    @property
    def is_class_b(self) -> bool:
        """Class B: Image-only workload deploying directly from container registries."""
        return self.source is None

    @property
    def is_internal(self) -> bool:
        """True if the workload does not expose a public TLS endpoint."""
        return not self.domain or self.domain == "internal" or self.domain.endswith(".local") or ":" in self.domain

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schemaVersion": self.schema_version,
            "name": self.name,
            "domain": self.domain,
            "description": self.description,
            "aliases": list(self.aliases),
            "visible": self.visible,
            "auth": self.auth,
            "networks": list(self.networks),
            "env": dict(self.env),
            "deployment": self.deployment.to_dict(),
            "is_class_a": self.is_class_a,
            "is_class_b": self.is_class_b,
            "is_internal": self.is_internal,
        }
        if self.source:
            data["source"] = self.source.to_dict()
        if self.homepage:
            data["homepage"] = self.homepage.to_dict()
        return data


def parse_manifest(raw_input: str | Path | dict[str, Any]) -> Manifest:
    """
    Parse and strictly validate an app.yaml manifest into an authoritative Manifest object.
    
    Accepts:
    - Path object pointing to an app.yaml
    - Raw YAML string content
    - Pre-parsed dictionary
    """
    data: dict[str, Any] = {}

    if isinstance(raw_input, Path):
        if not raw_input.is_file():
            raise ManifestValidationError(f"Manifest file not found: {raw_input}")
        try:
            content = raw_input.read_text(encoding="utf-8")
        except OSError as e:
            raise ManifestValidationError(f"Failed to read manifest {raw_input}: {e}") from e
        try:
            data = (yaml.safe_load(content) if yaml else _parse_yaml_basic(content)) or {}
        except Exception as e:
            raise ManifestValidationError(f"YAML parsing error in {raw_input}: {e}") from e
    elif isinstance(raw_input, str):
        try:
            data = (yaml.safe_load(raw_input) if yaml else _parse_yaml_basic(raw_input)) or {}
        except Exception as e:
            raise ManifestValidationError(f"YAML parsing error: {e}") from e
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise ManifestValidationError(f"Invalid input type for manifest parsing: {type(raw_input)}")

    if not isinstance(data, dict):
        raise ManifestValidationError("Manifest root must be a YAML dictionary mapping.")

    # 1. Name validation
    raw_name = data.get("name")
    if not raw_name or not isinstance(raw_name, str):
        raise ManifestValidationError("Manifest is missing mandatory string field 'name'.")
    name = raw_name.strip()
    if not NAME_RE.match(name):
        raise ManifestValidationError(
            f"Invalid workload name '{name}'. Must be alphanumeric with optional '.' '_' '-', starting with alphanumeric."
        )

    # 2. Schema Version
    schema_version = str(data.get("schemaVersion", "1.0")).strip()

    # 3. Domain & Description
    domain = str(data.get("domain", "")).strip()
    description = str(data.get("description", "")).strip()

    # 4. Aliases
    aliases_raw = data.get("aliases", [])
    aliases: list[str] = []
    if isinstance(aliases_raw, str):
        aliases = [aliases_raw.strip()]
    elif isinstance(aliases_raw, list):
        for a in aliases_raw:
            if isinstance(a, str) and a.strip():
                aliases.append(a.strip())

    # 5. Flags (visible, auth)
    visible = bool(data.get("visible", True))
    auth = bool(data.get("auth", True))

    # 6. Networks
    networks_raw = data.get("networks", ["proxy-net"])
    networks: list[str] = []
    if isinstance(networks_raw, list):
        networks = [str(n).strip() for n in networks_raw if str(n).strip()]
    if not networks:
        networks = ["proxy-net"]

    # 7. Environment overrides
    env_raw = data.get("env", {})
    env = dict(env_raw) if isinstance(env_raw, dict) else {}

    # 8. Deployment Policy
    deployment_raw = data.get("deployment")
    deployment = DeploymentConfig()
    if isinstance(deployment_raw, dict):
        strategy = str(deployment_raw.get("strategy", "compose")).strip().lower()
        if strategy not in ALLOWED_STRATEGIES:
            raise ManifestValidationError(
                f"Unsupported deployment strategy '{strategy}'. Allowed: {sorted(ALLOWED_STRATEGIES)}"
            )

        branch = str(deployment_raw.get("branch", "main")).strip()
        actions_raw = deployment_raw.get("actions", ["compose_up"])
        actions: list[str] = []
        if isinstance(actions_raw, list):
            for act in actions_raw:
                act_str = str(act).strip().lower()
                if act_str not in ALLOWED_ACTIONS:
                    raise ManifestValidationError(
                        f"Prohibited deployment action '{act_str}'. Allowed: {sorted(ALLOWED_ACTIONS)}"
                    )
                actions.append(act_str)
        if not actions:
            actions = ["compose_up"]

        deployment = DeploymentConfig(strategy=strategy, branch=branch, actions=actions)

    # 9. Source Provenance (Class A vs Class B)
    source_raw = data.get("source")
    source: SourceConfig | None = None
    if isinstance(source_raw, dict) and source_raw.get("url"):
        raw_url = str(source_raw.get("url", "")).strip()
        transport, validated_url = validate_transport(raw_url)

        raw_ref = str(source_raw.get("ref", "main")).strip()
        ref_type, clean_ref = classify_ref(raw_ref)

        src_type = str(source_raw.get("type", "git")).strip().lower()
        if src_type != "git":
            raise ManifestValidationError(f"Unsupported source type '{src_type}'. Only 'git' is supported.")

        source = SourceConfig(
            url=validated_url,
            ref=clean_ref,
            type=src_type,
            ref_type=ref_type,
            transport=transport,
        )

    # 10. Homepage configuration
    homepage_raw = data.get("homepage")
    homepage: HomepageConfig | None = None
    if isinstance(homepage_raw, dict):
        title = str(homepage_raw.get("title", name)).strip()
        group = str(homepage_raw.get("group", "Applications")).strip()
        icon = str(homepage_raw.get("icon", "default.png")).strip()
        container = str(homepage_raw.get("container", name)).strip()
        try:
            weight = int(homepage_raw.get("weight", 50))
        except (ValueError, TypeError):
            weight = 50

        homepage = HomepageConfig(
            title=title,
            group=group,
            icon=icon,
            container=container,
            weight=weight,
        )

    return Manifest(
        name=name,
        schema_version=schema_version,
        domain=domain,
        description=description,
        aliases=aliases,
        visible=visible,
        auth=auth,
        networks=networks,
        env=env,
        deployment=deployment,
        source=source,
        homepage=homepage,
    )


def _parse_yaml_basic(content: str) -> dict[str, Any]:
    """Lightweight fallback parser when PyYAML is unavailable."""
    result: dict[str, Any] = {}
    for line in content.splitlines():
        trimmed = line.strip()
        if not trimmed or trimmed.startswith("#") or ":" not in trimmed:
            continue
        k, v = trimmed.split(":", 1)
        k = k.strip()
        v = v.strip().strip("\"'")
        result[k] = v
    return result
