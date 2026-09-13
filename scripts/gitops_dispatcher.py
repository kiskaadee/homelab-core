#!/usr/bin/env python3
"""
gitops_dispatcher.py - Dynamic Decentralized GitOps Deployment Engine for Homelab

Receives Gitea/Git webhook payloads, dynamically resolves target repositories
in ~/Sites (via app.yaml) or data vaults (like ~/Brain), and executes the declared
deployment actions with zero Core restarts.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

SITES_DIR = Path(os.environ.get("SITES_DIR", os.path.expanduser("~/Sites")))
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", os.path.expanduser("~/Brain")))
CONFIG_DIR = Path(os.environ.get("HOMELAB_CONFIG_DIR", os.path.expanduser("~/.config/homelab")))
DEFAULT_SECRET_PATH = Path(os.environ.get("GITOPS_SECRET_FILE", "/run/secrets/gitops/webhook_secret"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [GitOps] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("gitops_dispatcher")


def load_webhook_secret(secret_file: Path | None = None) -> bytes:
    """
    Load webhook shared secret from projected secret file or environment.
    Never hardcoded in code or repository manifests.
    """
    path = secret_file or Path(os.environ.get("GITOPS_SECRET_FILE", DEFAULT_SECRET_PATH))
    if path.is_file():
        try:
            return path.read_bytes().strip()
        except OSError as e:
            logger.error(f"Failed to read webhook secret from {path}: {e}")
            return b""

    env_secret = os.environ.get("GITOPS_WEBHOOK_SECRET")
    if env_secret:
        return env_secret.strip().encode("utf-8")

    return b""


def verify_signature(raw_body: bytes, signature: str | None, secret: bytes) -> bool:
    """
    Verify incoming webhook HMAC-SHA256 signature using constant-time comparison.
    Fail-closed: missing signature, malformed signature, empty secret, or mismatch returns False.
    """
    if not secret:
        logger.error("Authentication failed: Webhook shared secret is empty or missing.")
        return False

    if not signature or not isinstance(signature, str):
        logger.error("Authentication failed: Missing signature header.")
        return False

    cleaned_signature = signature.strip()
    cleaned_signature = cleaned_signature.removeprefix("sha256=")

    # SHA256 hex digest is exactly 64 hexadecimal characters
    if len(cleaned_signature) != 64 or not all(c in "0123456789abcdefABCDEF" for c in cleaned_signature):
        logger.error("Authentication failed: Malformed signature format.")
        return False

    expected_mac = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_mac.lower(), cleaned_signature.lower()):
        logger.error("Authentication failed: Signature mismatch.")
        return False

    return True


def parse_yaml_simple(text: str) -> dict:
    """Lightweight, zero-dependency YAML parser tailored for app.yaml manifests."""
    data = {}
    current_section = None
    lines = text.splitlines()

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))

        # List item
        if stripped.startswith("- "):
            item = stripped[2:].strip().strip("\"'")
            if current_section:
                if not isinstance(data.get(current_section), list):
                    data[current_section] = []
                data[current_section].append(item)
            continue

        if ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip().strip("\"'")

            if v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            elif v.isdigit():
                v = int(v)

            if indent == 0:
                current_section = k
                if v == "":
                    data[k] = {}
                else:
                    data[k] = v
            elif indent > 0 and current_section:
                if not isinstance(data.get(current_section), dict):
                    data[current_section] = {}
                data[current_section][k] = v

    return data


REPO_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def get_trusted_repository_mapping() -> dict[str, Path]:
    """
    Build authoritative mapping of logical repository identities to local paths.
    Webhook payloads can only select repositories defined in this mapping.
    """
    mapping: dict[str, Path] = {}

    # 1. Authoritative Data Vaults
    if BRAIN_DIR.is_dir():
        brain_canonical = BRAIN_DIR.resolve()
        for alias in ["second-brain", "Brain", "brain"]:
            mapping[alias] = brain_canonical

    # 2. Workload Plane Applications in SITES_DIR
    if SITES_DIR.is_dir():
        sites_canonical = SITES_DIR.resolve()
        for entry in SITES_DIR.iterdir():
            if not entry.is_dir():
                continue

            # Defense-in-depth: resolve symlinks and ensure target is strictly inside SITES_DIR
            resolved_entry = entry.resolve()
            if not resolved_entry.is_relative_to(sites_canonical):
                logger.warning(f"Rejecting repository symlink escape: {entry} -> {resolved_entry}")
                continue

            manifest_file = resolved_entry / "app.yaml"
            if not manifest_file.is_file():
                continue

            # Register entry directory name
            dir_name = entry.name
            mapping[dir_name] = resolved_entry

            # Parse app.yaml for canonical name and aliases
            manifest = {}
            try:
                manifest = parse_yaml_simple(manifest_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
                logger.warning(f"Failed to parse {manifest_file}: {e}")

            canonical_name = manifest.get("name")
            if canonical_name and isinstance(canonical_name, str):
                canonical_name = canonical_name.strip()
                mapping[canonical_name] = resolved_entry
                if not canonical_name.startswith("homelab-"):
                    mapping[f"homelab-{canonical_name}"] = resolved_entry
                else:
                    mapping[canonical_name.replace("homelab-", "", 1)] = resolved_entry

            aliases = manifest.get("aliases", [])
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(aliases, list):
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip():
                        mapping[alias.strip()] = resolved_entry

    # 3. Optional local override registry with strict containment
    registry_file = CONFIG_DIR / "deployments.yaml"
    if registry_file.is_file():
        try:
            registry = parse_yaml_simple(registry_file.read_text(encoding="utf-8"))
            for reg_name, entry in registry.items():
                if not isinstance(entry, dict):
                    continue
                raw_path = entry.get("path")
                if not raw_path:
                    continue
                p = Path(raw_path).resolve()
                # Must be strictly relative to SITES_DIR or BRAIN_DIR
                if (SITES_DIR.is_dir() and p.is_relative_to(SITES_DIR.resolve())) or \
                   (BRAIN_DIR.is_dir() and p.is_relative_to(BRAIN_DIR.resolve())):
                    mapping[reg_name] = p
                else:
                    logger.warning(f"Rejecting out-of-bounds path in deployments.yaml for '{reg_name}': {p}")
        except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.warning(f"Failed to parse custom registry {registry_file}: {e}")

    return mapping


def resolve_repository(repo_name: str) -> Path | None:
    """
    Resolve a logical repository identity to a trusted local directory.
    Rejects directory traversal, unknown repositories, and path escapes.
    """
    if not repo_name or not isinstance(repo_name, str):
        return None

    cleaned_name = repo_name.strip()
    if not REPO_NAME_RE.match(cleaned_name):
        logger.warning(f"Invalid repository name format rejected: '{repo_name}'")
        return None

    if ".." in cleaned_name or "/" in cleaned_name or "\\" in cleaned_name:
        logger.warning(f"Path traversal characters detected in repository name: '{repo_name}'")
        return None

    trusted_mapping = get_trusted_repository_mapping()
    target = trusted_mapping.get(cleaned_name)

    if not target:
        logger.warning(f"Repository '{cleaned_name}' is not in trusted repository registry.")
        return None

    try:
        canonical_target = target.resolve()
    except (OSError, RuntimeError) as e:
        logger.warning(f"Failed to resolve target path for '{cleaned_name}': {e}")
        return None

    if not canonical_target.is_dir():
        logger.warning(f"Trusted repository target '{canonical_target}' does not exist or is not a directory.")
        return None

    # Defense-in-depth: check allowed root containment
    allowed = False
    if SITES_DIR.is_dir() and canonical_target.is_relative_to(SITES_DIR.resolve()) or BRAIN_DIR.is_dir() and canonical_target.is_relative_to(BRAIN_DIR.resolve()):
        allowed = True

    if not allowed:
        logger.warning(f"Resolved path '{canonical_target}' escapes allowed roots for repository '{cleaned_name}'.")
        return None

    return canonical_target


def resolve_target(repo_name: str) -> tuple[Path | None, dict]:
    """
    Dynamically resolve target directory and deployment configuration.
    Uses resolve_repository() as the single authoritative resolver.
    """
    target_dir = resolve_repository(repo_name)
    if not target_dir:
        return None, {}

    # Data vault default deployment config
    if BRAIN_DIR.is_dir() and target_dir == BRAIN_DIR.resolve():
        return target_dir, {
            "branch": "main",
            "actions": ["git_pull"]
        }

    manifest_file = target_dir / "app.yaml"
    manifest = {}
    if manifest_file.is_file():
        try:
            manifest = parse_yaml_simple(manifest_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.warning(f"Failed to parse {manifest_file}: {e}")

    deployment_config = manifest.get("deployment")
    if not isinstance(deployment_config, dict):
        deployment_config = {
            "branch": "main",
            "actions": ["git_pull", "compose_up"]
        }

    return target_dir, deployment_config


def execute_deployment(target_dir: Path, deployment_config: dict, branch: str) -> bool:
    """Execute declared deployment actions sequentially."""
    expected_branch = deployment_config.get("branch", "main")
    if branch and branch != expected_branch:
        logger.info(f"Skipping deployment: pushed branch '{branch}' != target branch '{expected_branch}'")
        return True

    logger.info(f"🚀 Deploying '{target_dir.name}' at {target_dir} (branch: {expected_branch})...")
    actions = deployment_config.get("actions", ["git_pull"])

    try:
        for action in actions:
            if action == "git_pull":
                logger.info(f"  ↳ [git_pull] Pulling latest '{expected_branch}'...")
                subprocess.run(
                    ["git", "-C", str(target_dir), "pull", "--ff-only", "origin", expected_branch],
                    check=True
                )
            elif action == "compose_up":
                compose_file = target_dir / "docker-compose.yml"
                if compose_file.is_file():
                    logger.info("  ↳ [compose_up] Recreating containers (docker compose up -d)...")
                    subprocess.run(
                        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"],
                        check=True
                    )
            elif action == "compose_build":
                compose_file = target_dir / "docker-compose.yml"
                if compose_file.is_file():
                    logger.info("  ↳ [compose_build] Building & recreating containers...")
                    subprocess.run(
                        ["docker", "compose", "-f", str(compose_file), "build"],
                        check=True
                    )
                    subprocess.run(
                        ["docker", "compose", "-f", str(compose_file), "up", "-d", "--remove-orphans"],
                        check=True
                    )
            elif action == "compose_restart":
                compose_file = target_dir / "docker-compose.yml"
                if compose_file.is_file():
                    logger.info("  ↳ [compose_restart] Restarting containers...")
                    subprocess.run(
                        ["docker", "compose", "-f", str(compose_file), "restart"],
                        check=True
                    )
            else:
                logger.error(f"❌ Unsupported or rejected deployment action '{action}'. Aborting deployment.")
                return False

        logger.info(f"✨ Deployment of '{target_dir.name}' completed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Deployment step failed with exit code {e.returncode}: {e}")
        return False
    except (subprocess.SubprocessError, OSError, KeyError, TypeError, ValueError, RuntimeError) as e:
        logger.error(f"❌ Unexpected deployment error: {e}")
        return False


def main():
    payload_raw = ""
    repo_name = ""
    branch = "main"
    signature = None

    if "--signature" in sys.argv:
        sig_idx = sys.argv.index("--signature")
        if sig_idx + 1 < len(sys.argv):
            signature = sys.argv[sig_idx + 1]
    elif len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
        signature = sys.argv[2]
    elif os.environ.get("HTTP_X_GITEA_SIGNATURE"):
        signature = os.environ.get("HTTP_X_GITEA_SIGNATURE")
    elif os.environ.get("X_GITEA_SIGNATURE"):
        signature = os.environ.get("X_GITEA_SIGNATURE")

    # Support CLI arguments for manual trigger: --repo <name> [--branch <branch>]
    if "--repo" in sys.argv:
        idx = sys.argv.index("--repo")
        if idx + 1 < len(sys.argv):
            repo_name = sys.argv[idx + 1]
        if "--branch" in sys.argv:
            b_idx = sys.argv.index("--branch")
            if b_idx + 1 < len(sys.argv):
                branch = sys.argv[b_idx + 1].replace("refs/heads/", "")
    else:
        # 1. Check if first argument is a raw JSON payload string
        if len(sys.argv) > 1 and sys.argv[1].strip().startswith("{"):
            payload_raw = sys.argv[1].strip()
        # 2. Fallback to reading from stdin
        elif not sys.stdin.isatty():
            payload_raw = sys.stdin.read().strip()

        if not payload_raw:
            logger.error("No webhook payload provided (checked sys.argv and stdin).")
            sys.exit(1)

        raw_body = payload_raw.encode("utf-8")
        secret = load_webhook_secret()
        if not verify_signature(raw_body, signature, secret):
            logger.error("❌ Webhook authentication failed. Rejecting request.")
            sys.exit(1)

        try:
            payload = json.loads(payload_raw)
            repo_name = payload.get("repository", {}).get("name", "")
            ref = payload.get("ref", "refs/heads/main")
            branch = ref.replace("refs/heads/", "")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON payload: {e}")
            sys.exit(1)

    if not repo_name:
        logger.error("No repository name found in payload.")
        sys.exit(1)

    logger.info(f"Incoming webhook event: repository='{repo_name}', branch='{branch}'")
    target_dir, deployment_config = resolve_target(repo_name)

    if not target_dir or not target_dir.is_dir():
        logger.warning(f"No valid deployment target directory found on host for '{repo_name}'. Ignoring.")
        sys.exit(0)

    success = execute_deployment(target_dir, deployment_config, branch)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
