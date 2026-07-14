from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[2]
INSTALL_LIB = REPO_DIR / "scripts" / "lib" / "cutie-provider-install.sh"


def _run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    clean_env = {
        "HOME": env["HOME"],
        "PATH": os.environ["PATH"],
        "PYTHON_BIN": os.environ.get("PYTHON", "python3"),
        **{key: value for key, value in env.items() if key != "HOME"},
    }
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=clean_env, check=False)


def _setup(provider_dir: Path) -> str:
    return f"""
. {INSTALL_LIB!s}
REPO_DIR={REPO_DIR!s}
PROVIDER_LABEL=test
PROVIDER_DIR={provider_dir!s}
PROVIDER_MODULE=test_provider:app
DEFAULT_PORT=8765
DEFAULT_SOURCE_ID=test-provider
DEFAULT_SERVICE_NAME=cutie-test-provider.service
DEFAULT_SUPPORTED_SYMBOLS=BTCUSDT,ETHUSDT
PROVIDER_PERSISTED_ENV_NAMES='CUTIE_BACKTEST_SUPPORTED_SYMBOLS CUTIE_CENTRAL_MARKET_DATA_URL CUTIE_CENTRAL_MARKET_DATA_TOKEN CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC'
mkdir -p "$PROVIDER_DIR"
"""


def _read_env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, raw = line.split("=", 1)
        values[name] = json.loads(raw)
    return values


def test_managed_config_is_mode_0600_atomic_and_preserved_on_rerun(tmp_path: Path):
    home = tmp_path / "home"
    provider_dir = tmp_path / "provider"
    home.mkdir()
    provider_token = 'provider-$-"-\\-token'
    central_token = 'central-$-"-\\-token'
    first_env = {
        "HOME": str(home),
        "CUTIE_BACKTEST_MANAGED_INSTALL": "1",
        "CUTIE_BACKTEST_PROVIDER_TOKEN": provider_token,
        "CUTIE_CENTRAL_MARKET_DATA_URL": "https://cutie.example.test/v1/internal/market-data",
        "CUTIE_CENTRAL_MARKET_DATA_TOKEN": central_token,
        "CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC": "7.5",
    }
    first = _run_bash(
        _setup(provider_dir)
        + "provider_init_config && provider_write_persistent_config && printf '%s\\n' \"$CUTIE_PROVIDER_ENV_FILE\"",
        first_env,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    assert provider_token not in first.stdout + first.stderr
    assert central_token not in first.stdout + first.stderr

    env_file = Path(first.stdout.strip())
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700
    values = _read_env_file(env_file)
    assert values["CUTIE_BACKTEST_PROVIDER_TOKEN"] == provider_token
    assert values["CUTIE_CENTRAL_MARKET_DATA_TOKEN"] == central_token
    assert values["CUTIE_CENTRAL_MARKET_DATA_TIMEOUT_SEC"] == "7.5"

    second = _run_bash(
        _setup(provider_dir)
        + "provider_init_config && provider_write_persistent_config && "
        + "test \"$TOKEN\" = 'provider-$-\"-\\-token' && "
        + "test \"$CUTIE_CENTRAL_MARKET_DATA_TOKEN\" = 'central-$-\"-\\-token'",
        {"HOME": str(home), "CUTIE_BACKTEST_MANAGED_INSTALL": "1"},
    )
    assert second.returncode == 0, second.stderr + second.stdout
    assert _read_env_file(env_file)["CUTIE_CENTRAL_MARKET_DATA_TOKEN"] == central_token
    assert list(env_file.parent.glob(".provider-env.*")) == []


def test_managed_install_fails_closed_without_production_token(tmp_path: Path):
    home = tmp_path / "home"
    provider_dir = tmp_path / "provider"
    home.mkdir()
    result = _run_bash(
        _setup(provider_dir) + "provider_init_config",
        {"HOME": str(home), "CUTIE_BACKTEST_MANAGED_INSTALL": "1"},
    )
    assert result.returncode != 0
    assert "Managed provider token is missing" in result.stdout
    assert "local-dev-token" not in result.stdout + result.stderr


def test_managed_install_rejects_http_central_url_for_bearer_token(tmp_path: Path):
    home = tmp_path / "home"
    provider_dir = tmp_path / "provider"
    home.mkdir()
    result = _run_bash(
        _setup(provider_dir) + "provider_init_config",
        {
            "HOME": str(home),
            "CUTIE_BACKTEST_MANAGED_INSTALL": "1",
            "CUTIE_BACKTEST_PROVIDER_TOKEN": "provider-secret",
            "CUTIE_CENTRAL_MARKET_DATA_URL": "http://cutie.example.test/v1/internal/market-data",
            "CUTIE_CENTRAL_MARKET_DATA_TOKEN": "market-data-secret",
        },
    )
    assert result.returncode != 0
    assert "not safe for a managed Bearer token" in result.stdout
    assert "market-data-secret" not in result.stdout + result.stderr


def test_systemd_unit_references_env_file_without_embedding_secrets(tmp_path: Path):
    home = tmp_path / "home"
    provider_dir = tmp_path / "provider"
    home.mkdir()
    provider_token = "provider-secret"
    central_token = "central-secret"
    result = _run_bash(
        _setup(provider_dir)
        + """
systemctl() { return 0; }
loginctl() { return 0; }
provider_init_config && provider_write_persistent_config && _start_via_systemd
""",
        {
            "HOME": str(home),
            "CUTIE_BACKTEST_MANAGED_INSTALL": "1",
            "CUTIE_BACKTEST_PROVIDER_TOKEN": provider_token,
            "CUTIE_CENTRAL_MARKET_DATA_URL": "https://cutie.example.test/v1/internal/market-data",
            "CUTIE_CENTRAL_MARKET_DATA_TOKEN": central_token,
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    unit = (home / ".config/systemd/user/cutie-test-provider.service").read_text(encoding="utf-8")
    assert "EnvironmentFile=" in unit
    assert provider_token not in unit
    assert central_token not in unit


def test_nohup_receives_central_config_from_safe_shell_environment(tmp_path: Path):
    home = tmp_path / "home"
    provider_dir = tmp_path / "provider"
    capture = tmp_path / "captured.json"
    home.mkdir()
    (provider_dir / ".venv/bin").mkdir(parents=True)
    fake_uvicorn = provider_dir / ".venv/bin/uvicorn"
    fake_uvicorn.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import json,os; json.dump({k: os.environ.get(k) for k in "
        "[\"CUTIE_CENTRAL_MARKET_DATA_URL\",\"CUTIE_CENTRAL_MARKET_DATA_TOKEN\",\"CUTIE_PROVIDER_REVISION\"]}, "
        "open(os.environ[\"CAPTURE_ENV_PATH\"], \"w\"))'\n",
        encoding="utf-8",
    )
    fake_uvicorn.chmod(0o755)
    result = _run_bash(
        _setup(provider_dir) + "provider_init_config && provider_write_persistent_config && _start_via_nohup",
        {
            "HOME": str(home),
            "CAPTURE_ENV_PATH": str(capture),
            "CUTIE_BACKTEST_MANAGED_INSTALL": "1",
            "CUTIE_BACKTEST_PROVIDER_TOKEN": "provider-secret",
            "CUTIE_CENTRAL_MARKET_DATA_URL": "https://cutie.example.test/v1/internal/market-data",
            "CUTIE_CENTRAL_MARKET_DATA_TOKEN": "central-secret",
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    for _ in range(250):
        if capture.exists():
            break
        time.sleep(0.02)
    assert capture.exists(), (provider_dir / ".runtime/install.log").read_text(encoding="utf-8")
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["CUTIE_CENTRAL_MARKET_DATA_URL"].startswith("https://cutie.example.test/")
    assert captured["CUTIE_CENTRAL_MARKET_DATA_TOKEN"] == "central-secret"
    assert captured["CUTIE_PROVIDER_REVISION"] != ""
