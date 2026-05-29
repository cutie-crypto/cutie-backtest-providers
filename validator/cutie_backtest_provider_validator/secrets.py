"""Secret-like / path / high-entropy detection (IMPL W3.9 §8 check 4, §12).

Mirrors the connector-side scrub contract in
cutie-connector/packages/connector/src/backtest.ts so the validator rejects
exactly the catalog content the connector would refuse to forward to the server.

Detection has three independent signals:
  - sensitive key name: key normalized to lower snake_case, then matched against
    an exact-name set or a sensitive suffix (avoids false positives like
    ``tokens_per_minute``);
  - high-entropy token value: long random-looking strings (API keys / bearer
    tokens), excluding dotted readable identifiers (schema names, reverse-DNS);
  - local path value: ``/Users/`` ``/home/`` ``/root/`` ``/var/`` ``C:\\`` ``\\Users\\``.
"""

from __future__ import annotations

import re
from typing import Any, List, Tuple

# Exact sensitive key names (after normalize_key_name). Matches IMPL §8 check 4
# plus the connector's SENSITIVE_KEY_NAMES (which also blocks endpoint/url keys
# since those must never appear in a catalog snapshot).
SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "token",
        "password",
        "passwd",
        "pwd",
        "private_key",
        "credential",
        "credentials",
        "bearer",
        "access_key",
        "endpoint",
        "catalog_url",
        "backtest_url",
        "base_url",
    }
)

# Sensitive suffixes (e.g. user_api_key / vendor_secret / xxx_token).
_SENSITIVE_SUFFIX_RE = re.compile(
    r"(_api_key|_secret|_token|_password|_passwd|_pwd|_private_key|_credential|_bearer|_access_key)$"
)

# Local absolute path / home dir / Windows path patterns (IMPL §8 check 4).
PATH_PATTERN = re.compile(
    r"(^|[^a-z])(/Users/|/home/|/root/|/var/|[A-Za-z]:\\|\\Users\\)", re.IGNORECASE
)


def normalize_key_name(key: str) -> str:
    """Normalize a key to lower snake_case (camelCase / kebab-case / spaces)."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key.strip())
    s = re.sub(r"[\s-]+", "_", s)
    return s.lower()


def is_sensitive_key(key: str) -> bool:
    normalized = normalize_key_name(key)
    if normalized in SENSITIVE_KEY_NAMES:
        return True
    return bool(_SENSITIVE_SUFFIX_RE.search(normalized))


def looks_like_high_entropy_token(value: str) -> bool:
    """Heuristic for API-key / bearer-token shaped strings.

    Excludes dotted readable identifiers (schema names / reverse-DNS / versions)
    so ``cutie.backtest_tool_catalog.v1`` is not flagged.
    """
    if re.search(r"\s", value):
        return False
    segments = value.split(".")
    looks_dotted_identifier = len(segments) >= 2 and all(
        re.fullmatch(r"[A-Za-z0-9_-]+", seg) and len(seg) <= 40 for seg in segments
    )
    has_upper = bool(re.search(r"[A-Z]", value))
    has_lower = bool(re.search(r"[a-z]", value))
    has_digit = bool(re.search(r"[0-9]", value))
    if looks_dotted_identifier and not has_upper:
        # all-lowercase dotted identifier (typical schema name) -> allow
        return False
    if (
        len(value) >= 24
        and has_upper
        and has_lower
        and has_digit
        and re.fullmatch(r"[A-Za-z0-9._\-+/=]+", value)
    ):
        return True
    if len(value) >= 32 and re.fullmatch(r"[A-Fa-f0-9]+", value):
        return True  # pure hex (sha256 etc.)
    if len(value) >= 32 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", value) and has_digit:
        return True  # base64 / base64url shape
    return False


def value_looks_sensitive(value: str) -> bool:
    if PATH_PATTERN.search(value):
        return True
    if looks_like_high_entropy_token(value):
        return True
    return False


def value_is_path(value: str) -> bool:
    return bool(PATH_PATTERN.search(value))


def scan_for_secrets(value: Any, path: str = "$") -> List[Tuple[str, str, str]]:
    """Recursively scan a JSON value for secret-like content.

    Returns a list of ``(json_path, reason, detail)`` findings. Empty list = clean.
    ``reason`` is one of ``sensitive_key`` / ``high_entropy_value`` / ``local_path_value``.
    """
    findings: List[Tuple[str, str, str]] = []
    if isinstance(value, dict):
        for key, raw in value.items():
            child_path = f"{path}.{key}"
            # A sensitive key name is only a real leak when it actually carries a
            # non-empty string secret. Boolean/number flags such as the declared
            # security.requires_user_secret catalog field are not secrets — this
            # matches the connector, which keeps the security block by allowlist
            # rather than scrubbing those boolean flags as values (backtest.ts
            # buildSnapshotSecurity).
            if is_sensitive_key(str(key)) and isinstance(raw, str) and raw.strip():
                findings.append(
                    (child_path, "sensitive_key", f"key '{key}' carries a secret-like value")
                )
            findings.extend(scan_for_secrets(raw, child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            findings.extend(scan_for_secrets(item, f"{path}[{idx}]"))
    elif isinstance(value, str):
        if value_is_path(value):
            findings.append(
                (path, "local_path_value", f"value exposes a local path: {value!r}")
            )
        elif looks_like_high_entropy_token(value):
            findings.append(
                (path, "high_entropy_value", "value looks like a secret token")
            )
    return findings
