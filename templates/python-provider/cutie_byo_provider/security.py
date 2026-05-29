"""Secret / path scrubbing helpers (IMPL §8 check item 4, §12).

The provider must never leak commercial API keys, bearer tokens, exchange
secrets, or local filesystem paths in its ``/health``, ``/catalog`` or
``/cutie/backtest`` responses. These helpers mirror the connector / validator
scrub rules so a wrapper author can sanitize anything they bubble up into
``raw_report`` or report URLs.

Detection rules (IMPL §8.4):

- Key names: normalize to lowercase snake_case, then match by exact name OR by
  sensitive suffix against a fixed denylist. We deliberately avoid arbitrary
  substring matching so普通 param names like ``strategy_name`` are not flagged.
- Values: high-entropy token detection (long, base64/hex-like strings).
- Paths: match known absolute-path prefixes (``/Users/``, ``/home/``,
  ``/root/``, ``/var/``, ``C:\\``, ``\\Users\\``).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List
from urllib.parse import urlsplit

REDACTED = "[redacted]"

# Exact sensitive key names (already normalized to snake_case lowercase).
_SENSITIVE_KEY_NAMES = {
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
    "secret_key",
    "auth",
    "authorization",
}

# Sensitive suffixes — a key like ``binance_api_key`` ends with ``_api_key``.
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_secret",
    "_token",
    "_password",
    "_passwd",
    "_pwd",
    "_private_key",
    "_credential",
    "_credentials",
    "_access_key",
    "_secret_key",
)

# Absolute filesystem path markers (IMPL §8.4).
_PATH_PATTERNS = (
    re.compile(r"/Users/"),
    re.compile(r"/home/"),
    re.compile(r"/root/"),
    re.compile(r"/var/"),
    re.compile(r"[A-Za-z]:\\"),  # C:\  D:\ ...
    re.compile(r"\\Users\\"),
)

# A high-entropy token candidate: long run of base64 / hex characters.
_TOKEN_CANDIDATE = re.compile(r"^[A-Za-z0-9+/=_\-]{24,}$")

# Readable snake_case / kebab-case identifier (e.g. ``openclaw_hermes_local``):
# all-lowercase word segments. Used to suppress entropy false positives.
_READABLE_IDENTIFIER = re.compile(r"^[a-z0-9]+([_\-][a-z0-9]+)+$")

_SNAKE_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_key(key: str) -> str:
    """Normalize a key name to lowercase snake_case (IMPL §8.4)."""
    s = _SNAKE_1.sub(r"\1_\2", key)
    s = _SNAKE_2.sub(r"\1_\2", s)
    s = s.lower()
    s = _NON_ALNUM.sub("_", s)
    return s.strip("_")


def is_sensitive_key(key: str) -> bool:
    """True when a mapping key name looks like it holds a secret."""
    norm = _normalize_key(key)
    if norm in _SENSITIVE_KEY_NAMES:
        return True
    return any(norm.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(value)
    entropy = 0.0
    for count in counts.values():
        p = count / length
        entropy -= p * math.log2(p)
    return entropy


def looks_like_secret_value(value: str) -> bool:
    """Heuristic high-entropy token detection (IMPL §8.4)."""
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not _TOKEN_CANDIDATE.match(candidate):
        return False
    # Readable snake_case / kebab-case enum values (e.g. catalog policy strings)
    # are not secrets, even when long.
    if _READABLE_IDENTIFIER.match(candidate):
        return False
    # Long base64/hex-looking strings with high entropy are likely secrets.
    return _shannon_entropy(candidate) >= 3.5


def contains_local_path(value: str) -> bool:
    """True when a string embeds an absolute local filesystem path."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _PATH_PATTERNS)


def scrub(value: Any) -> Any:
    """Recursively redact secret-like keys, values, and local paths.

    - Mapping entries whose key is sensitive are redacted regardless of value.
    - String values that look like high-entropy secrets are redacted.
    - String values that embed absolute local paths are redacted.
    - Other scalars pass through unchanged.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            # Redact only when a sensitive key actually carries a string value
            # (a credential). Boolean/number flags like ``requires_user_secret``
            # or ``live_trading`` are legitimate non-secret config, not values.
            if isinstance(k, str) and is_sensitive_key(k) and isinstance(v, str) and v:
                out[k] = REDACTED
            else:
                out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        if contains_local_path(value):
            return REDACTED
        if looks_like_secret_value(value):
            return REDACTED
    return value


def scrub_report_url(report_url: Any) -> Any:
    """Reduce a report URL to a relative path / opaque ref (IMPL §7).

    The Cutie Connector only accepts scrubbed relative paths. If a wrapper
    returns an absolute URL we strip scheme / host / port and keep only the
    path (+ query). Local filesystem absolute paths are rejected entirely.
    """
    if report_url is None:
        return None
    if not isinstance(report_url, str) or not report_url.strip():
        return None
    candidate = report_url.strip()

    # Absolute local filesystem path -> not a valid report ref.
    if contains_local_path(candidate):
        return None

    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return path if path.startswith("/") else f"/{path}"

    # Already a relative path/ref.
    return candidate if candidate.startswith("/") else f"/{candidate}"


def scan_for_secrets(value: Any, _path: str = "") -> List[str]:
    """Return human-readable findings for catalog / health self-checks.

    Used by tests and ``adapter`` authors to verify they did not accidentally
    embed secrets or local paths. Returns an empty list when clean.
    """
    findings: List[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            key_path = f"{_path}.{k}" if _path else str(k)
            if isinstance(k, str) and is_sensitive_key(k) and isinstance(v, str) and v:
                findings.append(f"sensitive key name with string value at {key_path}")
            findings.extend(scan_for_secrets(v, key_path))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            findings.extend(scan_for_secrets(v, f"{_path}[{i}]"))
    elif isinstance(value, str):
        if contains_local_path(value):
            findings.append(f"absolute local path at {_path or '<root>'}")
        elif looks_like_secret_value(value):
            findings.append(f"high-entropy secret-like value at {_path or '<root>'}")
    return findings
