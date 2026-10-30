#!/usr/bin/env python3
"""
bootstrap_token.py — Operator Admin Token Bootstrap & Rotation Helper (WP4)

Generates a 256-bit cryptographically secure Bearer token, saves it locally
with strict mode 0600 permissions (~/.config/homelab/token), and prints the
corresponding JSON descriptor for insertion into the SOPS tokens store
(nixos/secrets.yaml -> appctl/tokens_json).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

# Ensure scripts directory is in sys.path when executed directly
_scripts_dir = Path(__file__).resolve().parent
if str(_scripts_dir) not in sys.path:
    sys.path.insert(0, str(_scripts_dir))

from appctl_auth import Role, TokenDescriptor

DEFAULT_TOKEN_PATH = Path.home() / ".config/homelab/token"
DEFAULT_TOKEN_ID = "operator-admin"
DEFAULT_IDENTITY = "operator"
DEFAULT_ROLE = Role.ADMIN.value


def generate_token() -> tuple[str, str]:
    """Generate a 256-bit cryptographically secure Bearer token and its SHA-256 digest."""
    raw_token = secrets.token_hex(32)
    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    return raw_token, digest


def write_token_file(path: Path, raw_token: str, force: bool = False) -> None:
    """
    Write raw Bearer token to path with mode 0600.
    Ensures parent directory exists with mode 0700.
    Fails closed if the destination exists and force is False.
    """
    path = path.expanduser().resolve()
    if path.exists() and not force:
        raise FileExistsError(
            f"Token file '{path}' already exists. Use --force / -f to overwrite."
        )

    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
    elif stat.S_IMODE(parent.stat().st_mode) != 0o700:
        try:
            parent.chmod(0o700)
        except OSError:
            pass

    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        content = f"{raw_token}\n".encode()
        os.write(fd, content)
    finally:
        os.close(fd)

    path.chmod(0o600)


def bootstrap_token(
    output_path: Path | str = DEFAULT_TOKEN_PATH,
    token_id: str = DEFAULT_TOKEN_ID,
    identity: str = DEFAULT_IDENTITY,
    role: str = DEFAULT_ROLE,
    force: bool = False,
) -> dict[str, Any]:
    """
    Generate Bearer token, persist to file with strict permissions,
    and return the validated TokenDescriptor dictionary.
    """
    path = Path(output_path).expanduser().resolve()
    raw_token, digest = generate_token()

    write_token_file(path, raw_token, force=force)

    # Validate against TokenDescriptor model
    validated = TokenDescriptor(
        token_id=token_id,
        identity=identity,
        role=Role(role),
        sha256=digest,
    )
    return validated.model_dump(mode="json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap or rotate an operator Bearer token for appctl."
    )
    parser.add_argument(
        "-o",
        "--output",
        default=str(DEFAULT_TOKEN_PATH),
        help=f"Destination token file path (default: {DEFAULT_TOKEN_PATH})",
    )
    parser.add_argument(
        "--token-id",
        default=DEFAULT_TOKEN_ID,
        help=f"Token descriptor ID (default: {DEFAULT_TOKEN_ID})",
    )
    parser.add_argument(
        "--identity",
        default=DEFAULT_IDENTITY,
        help=f"Caller identity (default: {DEFAULT_IDENTITY})",
    )
    parser.add_argument(
        "--role",
        default=DEFAULT_ROLE,
        choices=[r.value for r in Role],
        help=f"Role assignment (default: {DEFAULT_ROLE})",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite existing token file if present",
    )
    parser.add_argument(
        "--print-json",
        action="store_true",
        help="Output only the JSON descriptor snippet for piping/automation",
    )

    args = parser.parse_args(argv)

    try:
        descriptor = bootstrap_token(
            output_path=args.output,
            token_id=args.token_id,
            identity=args.identity,
            role=args.role,
            force=args.force,
        )
    except FileExistsError as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1

    json_snippet = json.dumps(descriptor, indent=2)

    if args.print_json:
        sys.stdout.write(f"{json_snippet}\n")
    else:
        sys.stdout.write(
            f"Generated Bearer token and saved to {args.output} (mode 0600).\n\n"
            f"Add the following descriptor to your SOPS tokens store (nixos/secrets.yaml -> appctl/tokens_json):\n\n"
            f"{json_snippet}\n\n"
            f"After updating nixos/secrets.yaml with `sops nixos/secrets.yaml`, apply changes with:\n"
            f"  sudo nixos-rebuild switch --flake ~/Core#server\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
