#!/usr/bin/env python3
"""
appctl_cli.py — Thin HTTP/1.1 Unix Domain Socket Client for homelab-appctl

Dispatches CLI commands to the control plane daemon over /run/homelab/appctl.sock.
Supports local peercred viewer fallback, Bearer token authentication, and formatted output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from appctl_contracts import (
    OperationRequest,
    OperationResponse,
    OperationType,
)
from appctl_daemon import (
    DEFAULT_SOCKET_PATH,
    send_daemon_request,
)


def resolve_token(cli_token: str | None = None) -> str | None:
    """Resolve Bearer token from CLI flag, environment, or user token files."""
    if cli_token:
        return cli_token.strip()

    if "APPCTL_TOKEN" in os.environ:
        return os.environ["APPCTL_TOKEN"].strip()

    token_candidates = [
        Path.home() / ".config/homelab/token",
        Path("/run/secrets/appctl/token"),
    ]
    for path in token_candidates:
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                continue

    return None


def _format_workload_table(workloads: list[dict[str, Any]]) -> str:
    """Format a list of workload dictionaries into a human-readable tabular display."""
    if not workloads:
        return "ℹ️  No workloads found."

    has_ssl = any("ssl" in w and isinstance(w["ssl"], dict) for w in workloads)
    lines: list[str] = []

    if has_ssl:
        lines.append(
            f"{'SERVICE':<18} {'TYPE':<14} {'DOMAIN':<30} {'CERT':<24} {'DESCRIPTION'}"
        )
        lines.append(
            f"{'-------':<18} {'----':<14} {'------':<30} {'----':<24} {'-----------'}"
        )
    else:
        lines.append(
            f"{'SERVICE':<18} {'TYPE':<14} {'DOMAIN':<30} {'DESCRIPTION'}"
        )
        lines.append(
            f"{'-------':<18} {'----':<14} {'------':<30} {'-----------'}"
        )

    for w in workloads:
        name = str(w.get("name", "-"))
        w_type = str(w.get("type", w.get("app_type", "app")))
        domain = str(w.get("domain", "-"))
        desc = str(w.get("description", ""))
        if w.get("manifest_error"):
            desc = f"⚠️ {w['manifest_error']}"
        elif w.get("deployment_status"):
            desc = f"{w['deployment_status']} ({w.get('provenance_status', '-')})"

        if has_ssl:
            ssl_info = w.get("ssl")
            cert_status = (
                str(ssl_info.get("status", "-"))
                if isinstance(ssl_info, dict)
                else "-"
            )
            lines.append(
                f"{name:<18} {w_type:<14} {domain:<30} {cert_status:<24} {desc}"
            )
        else:
            lines.append(f"{name:<18} {w_type:<14} {domain:<30} {desc}")

    return "\n".join(lines)


def _format_workload_info(workload: dict[str, Any]) -> str:
    """Format a single workload metadata dictionary for `appctl info`."""
    name = workload.get("name", "unknown")
    w_type = workload.get("type", workload.get("app_type", "app"))
    header = (
        f"ℹ️  Core Infrastructure Service: {name}"
        if w_type in ("core", "core_service")
        else f"ℹ️  Workload Overview: {name}"
    )
    lines = [
        header,
        "-" * 50,
        f"Directory:       {workload.get('dir_path', '-')}",
        f"Domain:          {workload.get('domain', '-')}",
        f"Type:            {w_type}",
        f"Description:     {workload.get('description', '-')}",
    ]
    if workload.get("container"):
        lines.append(f"Container:       {workload['container']}")
    aliases = workload.get("aliases")
    if isinstance(aliases, list) and aliases:
        lines.append(f"Aliases:         {', '.join(str(a) for a in aliases)}")
    networks = workload.get("networks")
    if isinstance(networks, list) and networks:
        lines.append(f"Networks:        {', '.join(str(n) for n in networks)}")
    if "auth" in workload:
        lines.append(f"Auth Protected:  {workload['auth']}")
    if "visible" in workload:
        lines.append(f"Dashboard:       {'Visible' if workload['visible'] else 'Hidden'}")
    source = workload.get("source")
    if isinstance(source, dict):
        lines.append(f"Source URL:      {source.get('url', '-')}")
        lines.append(f"Target Ref:      {source.get('ref', '-')}")
    if workload.get("manifest_error"):
        lines.append(f"\n⚠️  Manifest Validation Error:\n  {workload['manifest_error']}")
    ssl_info = workload.get("ssl")
    if isinstance(ssl_info, dict):
        lines.append("\n🔒 SSL/TLS Certificate:")
        lines.append(f"  Status:        {ssl_info.get('status', '-')}")
        lines.append(f"  Issuer:        {ssl_info.get('issuer', '-')}")
        lines.append(f"  Expiration:    {ssl_info.get('formatted_expiry', '-')}")
    return "\n".join(lines)


def _format_ssl_info(ssl_info: dict[str, Any]) -> str:
    """Format SSL certificate details for `appctl ssl`."""
    domain = ssl_info.get("domain", "-")
    lines = [
        f"🔒 SSL/TLS Certificate: {domain}",
        "-" * 50,
        f"Domain:          {domain}",
        f"Status:          {ssl_info.get('status', '-')}",
        f"Issuer:          {ssl_info.get('issuer', '-')}",
        f"Subject:         {ssl_info.get('subject', '-')}",
        f"Expiration:      {ssl_info.get('not_after', '-')}",
    ]
    if ssl_info.get("days_remaining") is not None:
        lines.append(
            f"Days Remaining:  {ssl_info['days_remaining']} days ({ssl_info.get('formatted_expiry', '-')})"
        )
    if ssl_info.get("error"):
        lines.append(f"Details/Error:   {ssl_info['error']}")
    return "\n".join(lines)


def format_response(resp: OperationResponse, raw_json: bool = False) -> str:
    """Format an OperationResponse for human-friendly terminal display or JSON output."""
    if raw_json:
        return resp.model_dump_json(indent=2, exclude_none=True)

    if not resp.success:
        err_msg = resp.error.message if resp.error else "Unknown error"
        code = f" [{resp.error.code.value}]" if resp.error else ""
        return f"❌ Error{code}: {err_msg}"

    # If the engine produced terminal output (e.g. logs, compose config, status table), show it
    if resp.output:
        return resp.output

    if resp.result:
        if "workloads" in resp.result and isinstance(resp.result["workloads"], list):
            return _format_workload_table(resp.result["workloads"])
        if resp.operation == OperationType.WORKLOAD_RESOLVE and "workload" in resp.result:
            return json.dumps(resp.result["workload"])
        if "workload" in resp.result and isinstance(resp.result["workload"], dict):
            return _format_workload_info(resp.result["workload"])
        if "ssl" in resp.result and isinstance(resp.result["ssl"], dict):
            return _format_ssl_info(resp.result["ssl"])
        if "message" in resp.result and len(resp.result) <= 3:
            return f"✨ {resp.result['message']}"
        return json.dumps(resp.result, indent=2)

    return f"✨ Operation '{resp.operation}' succeeded."


def run_cli(argv: list[str] | None = None) -> int:
    """Main CLI command parser and dispatcher."""
    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="Path to appctl Unix domain socket",
    )
    common_parser.add_argument(
        "--token",
        type=str,
        default=None,
        help="Bearer token secret (overrides APPCTL_TOKEN and token files)",
    )
    common_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON response",
    )

    parser = argparse.ArgumentParser(
        prog="appctl",
        description="Homelab Unified Control Plane Client",
        parents=[common_parser],
    )

    subparsers = parser.add_subparsers(dest="command", help="Operational commands")

    # list / status
    list_p = subparsers.add_parser("list", aliases=["status"], parents=[common_parser], help="List workloads")
    list_p.add_argument("--core", "-c", "--all", "-a", action="store_true", dest="core", help="Include core platform services")
    list_p.add_argument("--fetch", "-f", action="store_true", help="Fetch remote git revisions")
    list_p.add_argument("--ssl", "-s", action="store_true", help="Check SSL certificate status")

    # info
    info_p = subparsers.add_parser("info", parents=[common_parser], help="Get workload metadata")
    info_p.add_argument("target", help="Workload name")
    info_p.add_argument("--ssl", "-s", action="store_true", help="Check SSL certificate status")

    # resolve
    resolve_p = subparsers.add_parser("resolve", parents=[common_parser], help="Resolve target metadata")
    resolve_p.add_argument("target", help="Workload or core target")

    # ssl
    ssl_p = subparsers.add_parser("ssl", parents=[common_parser], help="Inspect platform SSL certificates")
    ssl_p.add_argument("target", nargs="?", default=None, help="Target workload or platform")

    # health
    subparsers.add_parser("health", parents=[common_parser], help="Check control plane daemon health")

    # up / deploy
    up_p = subparsers.add_parser("up", aliases=["deploy"], parents=[common_parser], help="Deploy workload")
    up_p.add_argument("target", help="Workload or core target")
    up_p.add_argument("--build", action="store_true", help="Rebuild container images")

    # down / stop
    down_p = subparsers.add_parser("down", aliases=["stop"], parents=[common_parser], help="Stop workload")
    down_p.add_argument("target", help="Workload or core target")

    # restart
    restart_p = subparsers.add_parser("restart", parents=[common_parser], help="Restart workload")
    restart_p.add_argument("target", help="Workload or core target")

    # update
    update_p = subparsers.add_parser("update", parents=[common_parser], help="Update workload revision")
    update_p.add_argument("target", help="Workload target")
    update_p.add_argument("--branch", default=None, help="Expected git branch")

    # pull
    pull_p = subparsers.add_parser("pull", parents=[common_parser], help="Pull workload images")
    pull_p.add_argument("target", help="Workload target")

    # logs
    logs_p = subparsers.add_parser("logs", parents=[common_parser], help="View workload logs")
    logs_p.add_argument("target", help="Workload target")
    logs_p.add_argument("--tail", type=int, default=100, help="Number of log lines")

    # config
    config_p = subparsers.add_parser("config", parents=[common_parser], help="View workload compose config")
    config_p.add_argument("target", help="Workload target")

    # sync
    subparsers.add_parser("sync", parents=[common_parser], help="Synchronize Homepage services.yaml")

    parsed, _ = parser.parse_known_args(argv)

    if not parsed.command:
        parser.print_help()
        return 1

    token = resolve_token(parsed.token)
    socket_path = parsed.socket or Path(os.environ.get("APPCTL_SOCKET_PATH", DEFAULT_SOCKET_PATH))

    # Map command to OperationRequest
    op_req: OperationRequest
    cmd = parsed.command

    if cmd in ("list", "status"):
        params: dict[str, Any] = {
            "core": getattr(parsed, "core", False),
            "fetch": getattr(parsed, "fetch", False),
            "ssl": getattr(parsed, "ssl", False),
        }
        op_req = OperationRequest(operation=OperationType.WORKLOAD_LIST, parameters=params)
    elif cmd == "info":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_GET,
            target=parsed.target,
            parameters={"ssl": getattr(parsed, "ssl", False)},
        )
    elif cmd == "resolve":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_RESOLVE,
            target=parsed.target,
        )
    elif cmd == "ssl":
        op_req = OperationRequest(
            operation=OperationType.PLATFORM_SSL,
            target=parsed.target,
        )
    elif cmd == "health":
        op_req = OperationRequest(operation=OperationType.PLATFORM_HEALTH)
    elif cmd in ("up", "deploy"):
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_DEPLOY,
            target=parsed.target,
            parameters={"build": getattr(parsed, "build", False)},
        )
    elif cmd in ("down", "stop"):
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_STOP,
            target=parsed.target,
        )
    elif cmd == "restart":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_RESTART,
            target=parsed.target,
        )
    elif cmd == "update":
        params = {}
        if getattr(parsed, "branch", None):
            params["expected_branch"] = parsed.branch
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_UPDATE,
            target=parsed.target,
            parameters=params,
        )
    elif cmd == "pull":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_PULL,
            target=parsed.target,
        )
    elif cmd == "logs":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_LOGS,
            target=parsed.target,
            parameters={"tail": getattr(parsed, "tail", 100)},
        )
    elif cmd == "config":
        op_req = OperationRequest(
            operation=OperationType.WORKLOAD_CONFIG,
            target=parsed.target,
        )
    elif cmd == "sync":
        op_req = OperationRequest(operation=OperationType.PLATFORM_SYNC_HOMEPAGE)
    else:
        print(f"❌ Unknown command '{cmd}'", file=sys.stderr)
        return 1

    try:
        resp = send_daemon_request(
            endpoint=socket_path,
            operation_request=op_req,
            token=token,
            timeout=330.0,
        )
        print(format_response(resp, raw_json=parsed.json))
        return 0 if resp.success else 1
    except FileNotFoundError:
        print(f"❌ Error: appctl daemon socket not found at {socket_path}", file=sys.stderr)
        return 1
    except ConnectionRefusedError:
        print(f"❌ Error: appctl daemon is not accepting connections at {socket_path}", file=sys.stderr)
        return 1
    except (
        OSError,
        json.JSONDecodeError,
        TimeoutError,
        ValueError,
    ) as e:
        print(f"❌ Error: Failed to communicate with appctl daemon: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run_cli())
