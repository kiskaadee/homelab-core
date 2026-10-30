"""
tests/unit/test_bootstrap_token.py — Unit Tests for Operator Admin Token Bootstrap Helper (WP4)

Verifies:
- 256-bit secure token generation (64 hex characters) and matching SHA-256 digest
- Strict 0600 file mode and 0700 parent directory creation
- TokenDescriptor Pydantic validation (scripts/appctl_auth.py)
- Refusal to overwrite existing token file without --force
- Overwriting existing token file when --force is supplied
- Secrets leakage prevention: raw Bearer token never appears in stdout/stderr
- Machine-readable JSON output via --print-json
"""

from __future__ import annotations

import hashlib
import json
import stat
import string
from pathlib import Path
from typing import Any

import pytest
from appctl_auth import Role, TokenDescriptor
from bootstrap_token import (
    bootstrap_token,
    generate_token,
    main,
    write_token_file,
)


def test_generate_token_produces_64_hex_token_and_valid_sha256() -> None:
    raw_token, digest = generate_token()
    assert len(raw_token) == 64
    assert all(c in string.hexdigits for c in raw_token)
    assert len(digest) == 64
    assert all(c in string.hexdigits for c in digest)
    expected_digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert digest == expected_digest


def test_write_token_file_creates_file_and_enforces_0600(tmp_path: Path) -> None:
    token_file = tmp_path / "subdir" / "token"
    write_token_file(token_file, "secret-token-12345")
    assert token_file.is_file()
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert token_file.read_text(encoding="utf-8") == "secret-token-12345\n"


def test_bootstrap_token_generates_file_with_strict_mode_0600(tmp_path: Path) -> None:
    nested_dir = tmp_path / "nested" / "homelab"
    token_file = nested_dir / "token"

    descriptor = bootstrap_token(output_path=token_file)

    assert token_file.is_file()
    # Check parent directory mode is 0700
    parent_mode = stat.S_IMODE(nested_dir.stat().st_mode)
    assert parent_mode == 0o700, f"Expected parent mode 0700, got {oct(parent_mode)}"

    # Check token file mode is 0600
    file_mode = stat.S_IMODE(token_file.stat().st_mode)
    assert file_mode == 0o600, f"Expected file mode 0600, got {oct(file_mode)}"

    raw_token = token_file.read_text(encoding="utf-8").strip()
    assert len(raw_token) == 64
    assert hashlib.sha256(raw_token.encode("utf-8")).hexdigest() == descriptor["sha256"]


def test_bootstrap_token_descriptor_validates_against_token_descriptor(tmp_path: Path) -> None:
    token_file = tmp_path / "custom_token"
    descriptor = bootstrap_token(
        output_path=token_file,
        token_id="custom-admin",
        identity="operator",
        role="homelab-admin",
    )

    validated = TokenDescriptor.model_validate(descriptor)
    assert validated.token_id == "custom-admin"
    assert validated.identity == "operator"
    assert validated.role == Role.ADMIN
    assert len(validated.sha256) == 64


def test_bootstrap_token_refuses_overwrite_without_force(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    bootstrap_token(output_path=token_file)
    original_token = token_file.read_text(encoding="utf-8")

    with pytest.raises(FileExistsError, match="already exists"):
        bootstrap_token(output_path=token_file, force=False)

    assert token_file.read_text(encoding="utf-8") == original_token


def test_bootstrap_token_overwrites_with_force(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    descriptor1 = bootstrap_token(output_path=token_file)
    first_token = token_file.read_text(encoding="utf-8").strip()

    descriptor2 = bootstrap_token(output_path=token_file, force=True)
    second_token = token_file.read_text(encoding="utf-8").strip()

    assert first_token != second_token
    assert descriptor1["sha256"] != descriptor2["sha256"]
    assert descriptor2["sha256"] == hashlib.sha256(second_token.encode("utf-8")).hexdigest()


def test_bootstrap_cli_never_prints_raw_secret_to_stdout_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    ret = main(["--output", str(token_file)])
    assert ret == 0

    raw_token = token_file.read_text(encoding="utf-8").strip()
    captured = capsys.readouterr()

    # RAW SECRET MUST NEVER APPEAR IN STDOUT OR STDERR
    assert raw_token not in captured.out, "Leak: raw token found in stdout!"
    assert raw_token not in captured.err, "Leak: raw token found in stderr!"

    # Ensure stdout provides helpful human guidance
    assert "operator-admin" in captured.out
    assert "sops nixos/secrets.yaml" in captured.out


def test_bootstrap_cli_print_json_outputs_valid_json_descriptor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    ret = main(["--output", str(token_file), "--print-json"])
    assert ret == 0

    raw_token = token_file.read_text(encoding="utf-8").strip()
    captured = capsys.readouterr()

    assert raw_token not in captured.out
    assert captured.err == ""

    # Must be cleanly parseable as pure JSON
    data: dict[str, Any] = json.loads(captured.out.strip())
    validated = TokenDescriptor.model_validate(data)
    assert validated.token_id == "operator-admin"
    assert validated.identity == "operator"
    assert validated.role == Role.ADMIN
    assert validated.sha256 == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def test_bootstrap_cli_refuses_overwrite_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    assert main(["--output", str(token_file)]) == 0

    ret = main(["--output", str(token_file)])
    assert ret != 0
    captured = capsys.readouterr()
    assert "already exists" in captured.err


def test_bootstrap_cli_force_overwrites(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = tmp_path / "token"
    assert main(["--output", str(token_file)]) == 0
    tok1 = token_file.read_text(encoding="utf-8").strip()

    assert main(["--output", str(token_file), "--force"]) == 0
    tok2 = token_file.read_text(encoding="utf-8").strip()

    assert tok1 != tok2
