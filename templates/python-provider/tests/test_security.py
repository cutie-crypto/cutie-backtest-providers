"""Secret / path scrub tests (IMPL §8 check item 4, §7, §12)."""

from __future__ import annotations

from cutie_byo_provider import security


def test_sensitive_key_with_secret_value_redacted():
    out = security.scrub({"api_key": "AKIAIOSFODNN7EXAMPLEKEY1234", "exchange": "binance"})
    assert out["api_key"] == security.REDACTED
    assert out["exchange"] == "binance"


def test_suffix_sensitive_key_redacted():
    out = security.scrub({"binance_api_key": "abcDEF123ghiJKL456mnoPQR789"})
    assert out["binance_api_key"] == security.REDACTED


def test_boolean_flag_keys_not_redacted():
    # requires_user_secret / live_trading are config flags, not secrets.
    out = security.scrub(
        {"requires_user_secret": True, "secrets_stay_local": True, "live_trading": False}
    )
    assert out["requires_user_secret"] is True
    assert out["secrets_stay_local"] is True
    assert out["live_trading"] is False


def test_readable_enum_values_not_flagged():
    payload = {
        "network_scope": "openclaw_hermes_local_or_private",
        "working_dir_policy": "ephemeral_or_provider_managed",
    }
    assert security.scan_for_secrets(payload) == []
    assert security.scrub(payload) == payload


def test_local_paths_redacted():
    for path in (
        "/Users/alice/secret/config.yml",
        "/home/kol/data",
        "/root/.cutie/token",
        "/var/lib/provider",
        "C:\\Users\\bob\\key.txt",
    ):
        assert security.contains_local_path(path)
        assert security.scrub({"x": path})["x"] == security.REDACTED


def test_high_entropy_value_redacted():
    secret = "s3cR3t+B4se64/Token==ABCdef0987654321"
    assert security.looks_like_secret_value(secret)
    assert security.scrub({"opaque": secret})["opaque"] == security.REDACTED


def test_scrub_report_url_strips_host_and_port():
    assert security.scrub_report_url("http://127.0.0.1:8767/reports/r.html") == "/reports/r.html"
    assert security.scrub_report_url("https://192.168.1.5/reports/r.html?x=1") == "/reports/r.html?x=1"
    assert security.scrub_report_url("/reports/r.html") == "/reports/r.html"
    assert security.scrub_report_url("reports/r.html") == "/reports/r.html"


def test_scrub_report_url_rejects_local_path():
    assert security.scrub_report_url("/Users/alice/reports/r.html") is None
    assert security.scrub_report_url("") is None
    assert security.scrub_report_url(None) is None
