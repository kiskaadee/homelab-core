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
