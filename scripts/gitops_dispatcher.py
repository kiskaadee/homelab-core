#!/usr/bin/env python3
"""
gitops_dispatcher.py - Dynamic Decentralized GitOps Deployment Engine for Homelab

Receives Gitea/Git webhook payloads, dynamically resolves target repositories
in ~/Sites (via app.yaml) or data vaults (like ~/Brain), and executes the declared
deployment actions with zero Core restarts.
"""

import contextlib
import datetime
import fcntl
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
STATE_DIR = Path(os.environ.get("GITOPS_STATE_DIR", Path.home() / ".local/state/homelab/gitops"))

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
    Primary Gitea path: X-Gitea-Signature provides the bare 64-character hex digest of the raw body.
    Compatibility path: X-Hub-Signature-256 provides 'sha256=<hex>'.
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
    current_subsection = None
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
                if current_subsection and isinstance(data.get(current_section), dict):
                    if not isinstance(data[current_section].get(current_subsection), list):
                        data[current_section][current_subsection] = []
                    data[current_section][current_subsection].append(item)
                else:
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
                current_subsection = None
                if v == "":
                    data[k] = {}
                else:
                    data[k] = v
            elif indent > 0 and current_section:
                if not isinstance(data.get(current_section), dict):
                    data[current_section] = {}
                current_subsection = k
                if v == "":
                    data[current_section][k] = []
                else:
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


ALLOWED_ACTIONS = {"git_pull", "compose_up", "compose_build", "compose_restart"}
ALLOWED_STRATEGIES = {"compose"}


def validate_manifest_deployment(manifest: dict) -> tuple[bool, str]:
    """
    Validate deployment policy inside app.yaml.
    Rejects unsupported deployment strategies or unknown actions.
    """
    deployment = manifest.get("deployment")
    if deployment is None:
        return True, ""

    if not isinstance(deployment, dict):
        return False, "Field 'deployment' must be a mapping/dictionary."

    # Validate strategy if specified
    strategy = deployment.get("strategy")
    if strategy is not None and strategy not in ALLOWED_STRATEGIES:
        return False, f"Unsupported deployment strategy '{strategy}'. Allowed strategies: {sorted(ALLOWED_STRATEGIES)}"

    # Validate actions if specified
    actions = deployment.get("actions")
    if actions is not None:
        if not isinstance(actions, list):
            return False, "Field 'deployment.actions' must be a list of strings."
        for action in actions:
            if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
                return False, f"Unsupported deployment action '{action}'. Allowed actions: {sorted(ALLOWED_ACTIONS)}"

    return True, ""


def execute_deployment(target_dir: Path, deployment_config: dict, branch: str) -> bool:
    """
    Execute declared deployment actions sequentially.
    Enforces revision-consistent manifest validation:
    After pulling the target revision, app.yaml is re-read and re-validated
    from the freshly updated tree, ensuring deployment policy is derived
    from the exact revision being deployed.
    """
    expected_branch = deployment_config.get("branch", "main")
    if branch and branch != expected_branch:
        logger.info(f"Skipping deployment: pushed branch '{branch}' != target branch '{expected_branch}'")
        return True

    logger.info(f"🚀 Deploying '{target_dir.name}' at {target_dir} (branch: {expected_branch})...")
    actions = deployment_config.get("actions", ["git_pull"])

    try:
        idx = 0
        while idx < len(actions):
            action = actions[idx]
            if action == "git_pull":
                logger.info(f"  ↳ [git_pull] Pulling latest '{expected_branch}'...")
                subprocess.run(
                    ["git", "-C", str(target_dir), "pull", "--ff-only", "origin", expected_branch],
                    check=True
                )
                # Revision-consistent manifest validation:
                # The working tree has now advanced to the deployed revision.
                # Re-read and re-validate app.yaml from the exact deployed revision.
                if not (BRAIN_DIR.is_dir() and target_dir == BRAIN_DIR.resolve()):
                    manifest_file = target_dir / "app.yaml"
                    if manifest_file.is_file():
                        try:
                            fresh_manifest = parse_yaml_simple(manifest_file.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
                            logger.error(f"❌ Post-pull manifest parse failure in {manifest_file}: {e}")
                            return False

                        valid, err_msg = validate_manifest_deployment(fresh_manifest)
                        if not valid:
                            logger.error(f"❌ Post-pull manifest validation failed for '{target_dir.name}': {err_msg}")
                            return False

                        fresh_config = fresh_manifest.get("deployment")
                        if isinstance(fresh_config, dict):
                            fresh_branch = fresh_config.get("branch", "main")
                            if branch and branch != fresh_branch:
                                logger.error(
                                    f"❌ Post-pull branch policy violation: revision declared branch '{fresh_branch}', "
                                    f"but pushed branch was '{branch}'. Aborting deployment."
                                )
                                return False
                            # Adopt the freshly validated actions from the deployed revision
                            fresh_actions = fresh_config.get("actions", actions)
                            actions = [a for a in fresh_actions if a != "git_pull"]
                            idx = 0
                            continue
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

            idx += 1

        logger.info(f"✨ Deployment of '{target_dir.name}' completed successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Deployment step failed with exit code {e.returncode}: {e}")
        return False
    except (subprocess.SubprocessError, OSError, KeyError, TypeError, ValueError, RuntimeError) as e:
        logger.error(f"❌ Unexpected deployment error: {e}")
        return False


def admit_deployment(
    payload: dict,
    event_type: str | None = None
) -> tuple[bool, Path | None, dict, str, str]:
    """
    Evaluate webhook deployment admission against strict policy gates:
    1. Event Admission (must be a push event, rejects PRs, tags, releases)
    2. Ref Format (must be 'refs/heads/<branch>')
    3. Repository Admission (must resolve against trusted mapping)
    4. Manifest & Strategy Validation (app.yaml must be valid)
    5. Branch Policy Admission (pushed branch must match declared deployment branch)

    Returns: (admitted: bool, target_dir: Path | None, deployment_config: dict, branch: str, reason: str)
    """
    # 1. Event Admission
    if event_type and event_type.strip().lower() != "push":
        return False, None, {}, "", f"Event '{event_type}' is not eligible for deployment. Only 'push' is allowed."

    if "pull_request" in payload or "issue" in payload or "release" in payload:
        return False, None, {}, "", "Payload represents non-push event (pull_request/issue/release). Rejected."

    # 2. Ref Admission
    ref = payload.get("ref", "")
    if not isinstance(ref, str) or not ref:
        return False, None, {}, "", "Missing or invalid 'ref' in payload."

    if ref.startswith("refs/tags/"):
        return False, None, {}, "", f"Tag push '{ref}' is not deployable."

    if not ref.startswith("refs/heads/"):
        return False, None, {}, "", f"Ref '{ref}' is not a head branch. Only 'refs/heads/*' is supported."

    pushed_branch = ref.removeprefix("refs/heads/")
    if not pushed_branch:
        return False, None, {}, "", "Empty branch name extracted from ref."

    # 3. Repository Admission
    repo_field = payload.get("repository")
    if not isinstance(repo_field, dict):
        return False, None, {}, pushed_branch, "Missing or invalid 'repository' object in payload."

    repo_name = repo_field.get("name")
    if not isinstance(repo_name, str) or not repo_name.strip():
        return False, None, {}, pushed_branch, "Missing or empty repository name in payload."

    repo_name = repo_name.strip()
    target_dir = resolve_repository(repo_name)
    if not target_dir:
        return False, None, {}, pushed_branch, f"Repository '{repo_name}' is not recognized in trusted registry."

    # 4. Manifest & Strategy Validation
    if BRAIN_DIR.is_dir() and target_dir == BRAIN_DIR.resolve():
        deployment_config = {"branch": "main", "actions": ["git_pull"]}
    else:
        manifest_file = target_dir / "app.yaml"
        manifest = {}
        if manifest_file.is_file():
            try:
                manifest = parse_yaml_simple(manifest_file.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError, KeyError, AttributeError) as e:
                return False, None, {}, pushed_branch, f"Failed to parse manifest {manifest_file}: {e}"

            valid, err_msg = validate_manifest_deployment(manifest)
            if not valid:
                return False, None, {}, pushed_branch, f"Manifest validation error: {err_msg}"

        deployment_config = manifest.get("deployment")
        if not isinstance(deployment_config, dict):
            deployment_config = {
                "branch": "main",
                "actions": ["git_pull", "compose_up"]
            }

    # 5. Branch Policy Admission
    expected_branch = deployment_config.get("branch", "main")
    if pushed_branch != expected_branch:
        return False, target_dir, deployment_config, pushed_branch, (
            f"Branch mismatch: pushed branch '{pushed_branch}' does not match "
            f"declared target branch '{expected_branch}'."
        )

    return True, target_dir, deployment_config, pushed_branch, f"Admitted for deployment on branch '{pushed_branch}'."


def get_target_lock(target_name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{target_name}.lock"


def get_pending_file(target_name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{target_name}.pending.json"


def enqueue_deployment(
    target_dir: Path,
    deployment_config: dict,
    branch: str,
    commit_sha: str = ""
) -> Path:
    """
    Atomically enqueue a deployment request.
    Supersedes any existing pending request for this target.
    """
    target_name = target_dir.name
    pending_file = get_pending_file(target_name)
    temp_file = STATE_DIR / f"{target_name}.pending.tmp.{os.getpid()}"

    data = {
        "target_dir": str(target_dir),
        "deployment_config": deployment_config,
        "branch": branch,
        "commit_sha": commit_sha,
        "queued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    temp_file.write_text(json.dumps(data), encoding="utf-8")
    temp_file.replace(pending_file)
    logger.info(f"📥 Enqueued deployment for '{target_name}' (branch: {branch}, superseding previous pending).")
    return pending_file


def run_target_worker(target_name: str) -> bool:
    """
    Serialized worker for a specific repository.
    Acquires exclusive flock and processes pending deployments until queue is empty.
    Records intentional execution status in the state directory.
    """
    lock_file_path = get_target_lock(target_name)
    pending_file_path = get_pending_file(target_name)

    with open(lock_file_path, "w", encoding="utf-8") as lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            logger.info(f"Deployment worker for '{target_name}' is already active. Pending update will be picked up.")
            return True

        overall_success = True
        try:
            while True:
                if not pending_file_path.is_file():
                    break

                try:
                    content = pending_file_path.read_text(encoding="utf-8")
                    pending_file_path.unlink(missing_ok=True)
                    item = json.loads(content)
                except (OSError, json.JSONDecodeError) as e:
                    logger.error(f"Failed to read pending deployment file: {e}")
                    break

                target_dir = Path(item["target_dir"])
                deployment_config = item["deployment_config"]
                branch = item["branch"]
                commit_sha = item.get("commit_sha", "")

                success = execute_deployment(target_dir, deployment_config, branch)

                # Record intentional execution status
                status_file = STATE_DIR / f"{target_name}.status.json"
                try:
                    status_data = {
                        "target": target_name,
                        "branch": branch,
                        "commit_sha": commit_sha,
                        "success": success,
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    }
                    status_file.write_text(json.dumps(status_data, indent=2), encoding="utf-8")
                except OSError as e:
                    logger.warning(f"Failed to write deployment status file {status_file}: {e}")

                if not success:
                    overall_success = False

            return overall_success
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)


def spawn_worker_async(target_name: str):
    """Spawn a detached worker process to execute deployments asynchronously."""
    dispatcher_script = Path(__file__).resolve()
    cmd = [sys.executable, str(dispatcher_script), "--worker", target_name]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )


def main():
    payload_raw = ""
    repo_name = ""
    branch = "main"
    signature = None
    event_type = None

    if "--worker" in sys.argv:
        w_idx = sys.argv.index("--worker")
        if w_idx + 1 < len(sys.argv):
            worker_target = sys.argv[w_idx + 1]
            success = run_target_worker(worker_target)
            sys.exit(0 if success else 1)
        else:
            logger.error("--worker requires a repository/target name.")
            sys.exit(1)

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

    if "--event" in sys.argv:
        ev_idx = sys.argv.index("--event")
        if ev_idx + 1 < len(sys.argv):
            event_type = sys.argv[ev_idx + 1]
    elif len(sys.argv) > 3 and not sys.argv[3].startswith("-"):
        event_type = sys.argv[3]
    elif os.environ.get("HTTP_X_GITEA_EVENT"):
        event_type = os.environ.get("HTTP_X_GITEA_EVENT")
    elif os.environ.get("X_GITEA_EVENT"):
        event_type = os.environ.get("X_GITEA_EVENT")

    # Support CLI arguments for manual trigger: --repo <name> [--branch <branch>]
    if "--repo" in sys.argv:
        idx = sys.argv.index("--repo")
        if idx + 1 < len(sys.argv):
            repo_name = sys.argv[idx + 1]
        if "--branch" in sys.argv:
            b_idx = sys.argv.index("--branch")
            if b_idx + 1 < len(sys.argv):
                branch = sys.argv[b_idx + 1].replace("refs/heads/", "")
        logger.info(f"Local manual trigger: repository='{repo_name}', branch='{branch}'")
        target_dir, deployment_config = resolve_target(repo_name)
        if not target_dir or not target_dir.is_dir():
            logger.error(f"Target repository '{repo_name}' could not be resolved.")
            sys.exit(1)
        success = execute_deployment(target_dir, deployment_config, branch)
        sys.exit(0 if success else 1)
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
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON payload: {e}")
            sys.exit(1)

        admitted, target_dir, deployment_config, branch, reason = admit_deployment(payload, event_type)
        if not admitted:
            logger.warning(f"🚫 Webhook admission rejected: {reason}")
            # Branch mismatch on valid push is benign skip; other violations fail closed
            sys.exit(0 if "Branch mismatch" in reason else 1)

        logger.info(f"✅ Webhook admission passed: {reason}")
        commit_sha = payload.get("after", "")
        if not commit_sha and isinstance(payload.get("head_commit"), dict):
            commit_sha = payload.get("head_commit", {}).get("id", "")

        enqueue_deployment(target_dir, deployment_config, branch, commit_sha=commit_sha)
        spawn_worker_async(target_dir.name)
        logger.info(f"🚀 Asynchronous deployment scheduled for '{target_dir.name}'. Returning HTTP 200.")
        sys.exit(0)


if __name__ == "__main__":
    main()
