#!/usr/bin/env python3
"""
gitops_dispatcher.py - Dynamic Decentralized GitOps Deployment Engine for Homelab

Receives Gitea/Git webhook payloads, dynamically resolves target repositories
in ~/Sites (via app.yaml) or data vaults (like ~/Brain), and executes the declared
deployment actions with zero Core restarts.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

SITES_DIR = Path(os.environ.get("SITES_DIR", os.path.expanduser("~/Sites")))
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", os.path.expanduser("~/Brain")))
CONFIG_DIR = Path(os.environ.get("HOMELAB_CONFIG_DIR", os.path.expanduser("~/.config/homelab")))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [GitOps] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("gitops_dispatcher")


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


def resolve_target(repo_name: str) -> tuple[Path | None, dict]:
    """
    Dynamically resolve target directory and deployment configuration.
    1. Check ~/Sites/<repo_name> or ~/Sites/homelab-<repo_name>
    2. Check Data Vaults (second-brain -> ~/Brain)
    3. Check ~/.config/homelab/deployments.yaml fallback
    """
    normalized_name = repo_name.strip()
    short_name = normalized_name.replace("homelab-", "")

    # 1. Check Data Vaults
    if normalized_name in ["second-brain", "Brain", "brain"]:
        return BRAIN_DIR, {
            "branch": "main",
            "actions": ["git_pull"]
        }

    # 2. Check ~/Sites directories
    candidates = [
        SITES_DIR / normalized_name,
        SITES_DIR / f"homelab-{short_name}",
        SITES_DIR / short_name,
    ]

    for candidate in candidates:
        if candidate.is_dir():
            manifest_file = candidate / "app.yaml"
            manifest = {}
            if manifest_file.is_file():
                try:
                    manifest = parse_yaml_simple(manifest_file.read_text())
                except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
                    logger.warning(f"Failed to parse {manifest_file}: {e}")

            deployment_config = manifest.get("deployment")
            if not isinstance(deployment_config, dict):
                # Default application deployment strategy
                deployment_config = {
                    "branch": "main",
                    "actions": ["git_pull", "compose_up"]
                }
            return candidate, deployment_config

    # 3. Check custom override registry in ~/.config/homelab/deployments.yaml
    registry_file = CONFIG_DIR / "deployments.yaml"
    if registry_file.is_file():
        try:
            registry = parse_yaml_simple(registry_file.read_text())
            if normalized_name in registry:
                entry = registry[normalized_name]
                target_path = Path(entry.get("path", ""))
                if target_path.is_dir():
                    return target_path, entry.get("deployment", {"branch": "main", "actions": ["git_pull"]})
        except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
            logger.warning(f"Failed to parse custom registry {registry_file}: {e}")

    return None, {}


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
            elif isinstance(action, dict) and "custom" in action:
                cmd = action["custom"]
                logger.info(f"  ↳ [custom] Executing: {cmd}")
                subprocess.run(cmd, shell=True, cwd=str(target_dir), check=True)
            else:
                logger.warning(f"  ↳ Unknown action '{action}'; skipping.")

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
