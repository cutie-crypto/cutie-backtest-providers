"""report_url validation (IMPL W3.9 §8 check 8, §7).

Validator policy on the *provider response* layer (looser than the
connector/server contract, which only accepts scrubbed relative path/ref):

  - relative path / opaque ref (no scheme/host)            -> OK
  - absolute http/https URL whose host is loopback or in an RFC1918 private
    range (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)      -> OK (local/private)
  - absolute URL with a public IP or public domain          -> BLOCKED
  - local absolute file path / file: URL                    -> BLOCKED
"""

from __future__ import annotations

import ipaddress
from typing import Optional, Tuple
from urllib.parse import urlsplit

from .secrets import value_is_path


def _host_is_local_or_private(host: str) -> bool:
    """True if host is loopback or an RFC1918 private IP, or 'localhost'."""
    host = host.strip().lower()
    if not host:
        return False
    if host == "localhost":
        return True
    # Strip IPv6 brackets if present.
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # not an IP -> a domain name
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return (
            ip in ipaddress.ip_network("10.0.0.0/8")
            or ip in ipaddress.ip_network("172.16.0.0/12")
            or ip in ipaddress.ip_network("192.168.0.0/16")
        )
    # IPv6 unique-local (fc00::/7) treated as private; everything else public.
    return ip.is_private


def classify_report_url(report_url: Optional[str]) -> Tuple[str, str]:
    """Classify a provider-returned report_url.

    Returns ``(status, detail)`` where ``status`` is one of:
      - ``ok_relative``       relative path/ref, ideal for connector scrub
      - ``ok_local_url``      absolute URL with loopback/RFC1918 host (warn-worthy)
      - ``blocked_public``    public IP or domain -> reject
      - ``blocked_path``      local absolute file path / file: URL -> reject
    """
    if report_url is None or report_url == "":
        return "ok_relative", "no report_url present"

    raw = report_url.strip()

    # Local absolute file path or file: URL must never be a report ref (§7).
    if raw.startswith("file:") or value_is_path(raw):
        return "blocked_path", f"report_url is a local absolute path: {raw!r}"

    parts = urlsplit(raw)
    if not parts.scheme and not parts.netloc:
        # Relative path/ref -> ideal.
        return "ok_relative", f"relative report ref: {raw!r}"

    if parts.scheme not in ("http", "https"):
        return "blocked_path", f"report_url uses unsupported scheme: {parts.scheme!r}"

    host = parts.hostname or ""
    if _host_is_local_or_private(host):
        return (
            "ok_local_url",
            f"report_url host {host!r} is loopback/RFC1918 (local-only)",
        )
    return "blocked_public", f"report_url host {host!r} is a public IP/domain"
