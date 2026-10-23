"""
core_manifest.py — Authoritative Workload Manifest Schema (v1.0) & Parser Library

Governs the contract for all ~/Sites workloads:
- Strongly-typed Pydantic v2 representations (Manifest, SourceConfig, DeploymentConfig, HomepageConfig)
- Strict source transport scheme validation (https://, ssh://, git@)
- Explicit ref classification (moving branches vs. pinned tags vs. commit SHAs)
- Workload classification: Class A (Source-Backed) vs. Class B (Image-Only)
- Fail-closed schema enforcement with ConfigDict(extra="forbid")
"""

from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


class ManifestValidationError(Exception):
    """Raised when an application manifest violates schema or security constraints."""


class RefType(str, Enum):
    """Semantic classification of an upstream git reference."""

    BRANCH = "branch"  # Moving branch (e.g. main, master, develop, release/v1)
    TAG = "tag"  # Pinned release tag (e.g. v1.0.0, 2.3.1)
    COMMIT = "commit"  # Immutable commit SHA (40-character hex)
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
SCP_URL_RE = re.compile(
    r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+:([0-9]+/)?([a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+(\.git)?$"
)
ENV_KEY_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
RESERVED_ENV_KEYS = {
    "PATH",
    "LD_PRELOAD",
    "IFS",
    "SHELL",
    "USER",
    "CORE_DIR",
    "SITES_DIR",
    "ENV_FILE",
    "HOMESERVER_ENV",
}

ALLOWED_STRATEGIES = {"compose"}
ALLOWED_ACTIONS = {"git_pull", "compose_pull", "compose_up", "compose_build", "compose_restart"}


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
        raise ManifestValidationError(
            f"Prohibited filesystem path or traversal in source URL: '{clean_url}'"
        )

    # 1. HTTPS transport
    if clean_url.startswith("https://"):
        parsed = urlparse(clean_url)
        if not parsed.netloc:
            raise ManifestValidationError(
                f"Malformed HTTPS source URL (missing host): '{clean_url}'"
            )
        if not parsed.path or parsed.path == "/":
            raise ManifestValidationError(
                f"Malformed HTTPS source URL (missing repository path): '{clean_url}'"
            )
        return TransportScheme.HTTPS, clean_url

    # 2. Standard SSH transport (ssh://[user@]host[:port]/path)
    if clean_url.startswith("ssh://"):
        parsed = urlparse(clean_url)
        if not parsed.netloc:
            raise ManifestValidationError(
                f"Malformed SSH source URL (missing host): '{clean_url}'"
            )
        if not parsed.path or parsed.path == "/":
            raise ManifestValidationError(
                f"Malformed SSH source URL (missing repository path): '{clean_url}'"
            )
        return TransportScheme.SSH, clean_url

    # 3. SCP-style SSH transport (git@host:owner/repo.git)
    if clean_url.startswith("git@") or (
        "@" in clean_url and ":" in clean_url and not clean_url.startswith("http")
    ):
        if SCP_URL_RE.match(clean_url):
            return TransportScheme.GIT_SCP, clean_url
        raise ManifestValidationError(f"Malformed SCP-style source URL: '{clean_url}'")

    # Reject plaintext HTTP or unsupported schemes
    if clean_url.startswith("http://"):
        raise ManifestValidationError(
            f"Insecure HTTP source URL rejected (HTTPS or SSH required): '{clean_url}'"
        )

    raise ManifestValidationError(
        f"Invalid source.url scheme: '{clean_url}'. "
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

    # Non-versioned arbitrary string treated as branch
    return RefType.BRANCH, ref



class SourceConfig(BaseModel):
    """Upstream source provenance descriptor."""

    model_config = ConfigDict(extra="forbid", ignored_types=(dict,))


    url: str
    ref: str = "main"
    type: str = "git"
    ref_type: RefType = RefType.BRANCH
    transport: TransportScheme = TransportScheme.HTTPS

    @model_validator(mode="before")
    @classmethod
    def validate_source(cls, data: Any) -> Any:
        if isinstance(data, dict):
            src_type = str(data.get("type", "git")).strip().lower()
            if src_type != "git":
                raise ManifestValidationError(
                    f"Unsupported source type '{src_type}'. Only 'git' is supported."
                )
            data["type"] = src_type

            raw_url = data.get("url")
            if raw_url is not None:
                transport, validated_url = validate_transport(str(raw_url))
                data["url"] = validated_url
                if "transport" not in data or data["transport"] is None:
                    data["transport"] = transport

            raw_ref = data.get("ref", "main")
            if raw_ref is not None:
                ref_type, clean_ref = classify_ref(str(raw_ref))
                data["ref"] = clean_ref
                if "ref_type" not in data or data["ref_type"] is None:
                    data["ref_type"] = ref_type

        return data

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "url": self.url,
            "ref": self.ref,
            "ref_type": self.ref_type.value,
            "transport": self.transport.value,
        }


class DeploymentConfig(BaseModel):
    """Continuous deployment strategy and action policy."""

    model_config = ConfigDict(extra="forbid", ignored_types=(dict,))


    strategy: str = "compose"
    branch: str = "main"
    actions: list[str] = Field(default_factory=lambda: ["git_pull", "compose_up"])

    @field_validator("strategy", mode="before")
    @classmethod
    def validate_strategy(cls, v: Any) -> str:
        clean = str(v).strip().lower()
        if clean not in ALLOWED_STRATEGIES:
            raise ManifestValidationError(
                f"Unsupported deployment strategy '{clean}'. Allowed: {sorted(ALLOWED_STRATEGIES)}"
            )
        return clean

    @field_validator("actions", mode="before")
    @classmethod
    def validate_actions(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            actions: list[str] = []
            for act in v:
                act_str = str(act).strip().lower()
                if act_str not in ALLOWED_ACTIONS:
                    raise ManifestValidationError(
                        f"Unsupported deployment action '{act_str}' (Prohibited deployment action '{act_str}'). Allowed: {sorted(ALLOWED_ACTIONS)}"
                    )
                actions.append(act_str)
            if not actions:
                return ["compose_up"]
            return actions
        return ["compose_up"]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class HomepageConfig(BaseModel):
    """Dashboard card metadata for Homepage."""

    model_config = ConfigDict(extra="forbid", ignored_types=(dict,))


    title: str
    group: str = "Applications"
    icon: str = "default.png"
    container: str = ""
    weight: int = 50

    @field_validator("weight", mode="before")
    @classmethod
    def normalize_weight(cls, v: Any) -> int:
        try:
            return int(v)
        except (ValueError, TypeError):
            return 50

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="python")


class Manifest(BaseModel):
    """
    Authoritative Workload Manifest Model (Schema v1.0).
    Represents the full contract declared in a workload's app.yaml.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        ignored_types=(dict,),
    )


    name: str
    schema_version: str = Field(default="1.0", alias="schemaVersion")
    domain: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    visible: bool = True
    auth: bool = True
    networks: list[str] = Field(default_factory=lambda: ["proxy-net"])
    env: dict[str, str] = Field(default_factory=dict)
    deployment: DeploymentConfig = Field(default_factory=DeploymentConfig)
    source: SourceConfig | None = None
    homepage: HomepageConfig | None = None

    @model_validator(mode="before")
    @classmethod
    def prepare_manifest(cls, data: Any) -> Any:
        if isinstance(data, dict):
            raw_name = data.get("name")
            if (
                raw_name is None
                or not isinstance(raw_name, str)
                or not raw_name.strip()
            ):
                raise ManifestValidationError(
                    "Manifest is missing mandatory string field 'name'."
                )
            clean_name = raw_name.strip()
            if not NAME_RE.match(clean_name):
                raise ManifestValidationError(
                    f"Invalid workload name '{clean_name}'. Must be alphanumeric with optional '.' '_' '-', starting with alphanumeric."
                )
            data["name"] = clean_name

            # Source: treat empty dict or missing url as None
            src = data.get("source")
            if isinstance(src, dict) and not src.get("url"):
                data["source"] = None

            # Homepage default propagation
            hp = data.get("homepage")
            if isinstance(hp, dict):
                hp.setdefault("title", clean_name)
                hp.setdefault("container", clean_name)

        return data

    @field_validator("visible", "auth", mode="before")
    @classmethod
    def cast_bool(cls, v: Any) -> bool:
        if v is None:
            return True
        return bool(v)

    @field_validator("aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, v: Any) -> list[str]:

        if isinstance(v, str):
            return [v.strip()] if v.strip() else []
        if isinstance(v, list):
            return [str(a).strip() for a in v if isinstance(a, str) and str(a).strip()]
        return []

    @field_validator("networks", mode="before")
    @classmethod
    def normalize_networks(cls, v: Any) -> list[str]:
        if isinstance(v, list):
            clean = [str(n).strip() for n in v if str(n).strip()]
            if clean:
                return clean
        return ["proxy-net"]

    @field_validator("env", mode="before")
    @classmethod
    def validate_env(cls, v: Any) -> dict[str, str]:
        if not isinstance(v, dict):
            return {}
        env: dict[str, str] = {}
        for raw_k, raw_v in v.items():
            k_str = str(raw_k).strip()
            if not ENV_KEY_RE.match(k_str) or k_str in RESERVED_ENV_KEYS:
                raise ManifestValidationError(
                    f"Invalid or reserved environment variable key '{k_str}'. "
                    f"Keys must match ^[a-zA-Z_][a-zA-Z0-9_]*$ and cannot be reserved."
                )
            env[k_str] = str(raw_v) if raw_v is not None else ""
        return env

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
        return (
            not self.domain
            or self.domain == "internal"
            or self.domain.endswith(".local")
            or ":" in self.domain
        )

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
            raise ManifestValidationError(
                f"Failed to read manifest {raw_input}: {e}"
            ) from e
        try:
            data = (
                yaml.safe_load(content) if yaml else _parse_yaml_basic(content)
            ) or {}
        except Exception as e:
            raise ManifestValidationError(
                f"YAML parsing error in {raw_input}: {e}"
            ) from e
    elif isinstance(raw_input, str):
        try:
            data = (
                yaml.safe_load(raw_input) if yaml else _parse_yaml_basic(raw_input)
            ) or {}
        except Exception as e:
            raise ManifestValidationError(f"YAML parsing error: {e}") from e
    elif isinstance(raw_input, dict):
        data = raw_input
    else:
        raise ManifestValidationError(
            f"Invalid input type for manifest parsing: {type(raw_input)}"
        )

    if not isinstance(data, dict):
        raise ManifestValidationError(
            "Manifest root must be a YAML dictionary mapping."
        )

    try:
        return Manifest.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            ctx = err.get("ctx", {})
            orig_err = ctx.get("error")
            if isinstance(orig_err, ManifestValidationError):
                raise orig_err from e
        first_error = e.errors()[0]
        msg = first_error.get("msg", str(e))
        if msg.startswith("Value error, "):
            msg = msg.removeprefix("Value error, ")
        loc = ".".join(str(p) for p in first_error.get("loc", []))
        if loc and first_error.get("type") == "extra_forbidden":
            raise ManifestValidationError(
                f"Manifest contains unrecognized or forbidden field '{loc}'."
            ) from e
        raise ManifestValidationError(msg) from e


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
