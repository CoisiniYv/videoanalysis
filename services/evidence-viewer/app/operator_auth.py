"""Fail-closed Basic authentication helpers for the 8090 operator portal."""

from __future__ import annotations

import base64
import binascii
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


DEFAULT_AUTH_FILE = "/evidence/.operator-auth"


@dataclass(frozen=True)
class OperatorCredentials:
    username: str
    password: str


def auth_required() -> bool:
    value = os.getenv("OPERATOR_AUTH_REQUIRED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def auth_file_path() -> Path:
    return Path(os.getenv("OPERATOR_AUTH_FILE", DEFAULT_AUTH_FILE))


def load_credentials(path: Path | None = None) -> OperatorCredentials | None:
    """Load the two-line credential file without treating it as shell syntax.

    Line 1 is the username and line 2 is the password. Extra lines are rejected
    so a partially edited or accidentally concatenated secret never becomes
    active silently.
    """

    target = path or auth_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None

    lines = text.splitlines()
    if len(lines) != 2:
        return None
    username = lines[0].strip()
    password = lines[1]
    if not username or not password or ":" in username:
        return None
    return OperatorCredentials(username=username, password=password)


def parse_basic_authorization(header: str | None) -> OperatorCredentials | None:
    if not header:
        return None
    scheme, separator, encoded = header.partition(" ")
    if separator != " " or scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    username, separator, password = decoded.partition(":")
    if separator != ":" or not username or not password:
        return None
    return OperatorCredentials(username=username, password=password)


def credentials_match(
    supplied: OperatorCredentials | None,
    expected: OperatorCredentials | None,
) -> bool:
    if supplied is None or expected is None:
        return False
    return secrets.compare_digest(supplied.username, expected.username) and secrets.compare_digest(
        supplied.password,
        expected.password,
    )


def authorization_valid(header: str | None, expected: OperatorCredentials | None) -> bool:
    return credentials_match(parse_basic_authorization(header), expected)
