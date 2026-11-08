"""JES-T15: 日线回测上限放开到 1080 天，日内周期仍限 365 天。

Owner 结论：日线（timeframe >= 1d）最长 1080 天（12 个 90 天分片 / 36 个整月；
1095 会被前端切片规则减到 1080）。日内（< 1d）必须留在 365 天：1d 的 1h 根数
是 24，1080 天 1h 就是 25920 根，远超目录声明的 max_bars=5000。

修前这些用例红的原因：
- 上限常量是 365，1d/1080 天与 1h/365 天都在同一个 365 天闸门上（前者被误拒）。
- 1h/366 天在修前也红——但红的方式不同：它的上限是 365，所以修前是被拒的；
  把常量直接放宽到 1080 而不加按周期的分支，这条用例才真正变成「不该过的过了」。
  即 1h/366 是防回归锚点，1d/1080 是本次放开的目标。

校验入口唯一：``strategy_execution.validate_execution_request``（两处区间判定共用
``max_range_days_for_timeframe`` 解析出的同一个 max_range_days），provider 端
catalog 的 ``max_range_days`` 只是同一个常量 ``EXECUTION_MAX_RANGE_DAYS`` 的声明。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cutie_backtesting_provider as provider  # noqa: E402
import test_strategy_kernel as tsk  # noqa: E402
from canonical_json import canonical_json_sha256  # noqa: E402
from strategy_execution import (  # noqa: E402
    EXECUTION_INTRADAY_MAX_RANGE_DAYS,
    EXECUTION_MAX_RANGE_DAYS,
    max_range_days_for_timeframe,
)
from strategy_kernel import (  # noqa: E402
    ERR_COVERAGE_INCOMPLETE,
    ERR_SPEC_INVALID,
)

DAY = 24 * 60 * 60


def _request(timeframe: str, start_at: int, end_at: int, *, primary_warmup: int = 0) -> dict:
    """A one-symbol request whose primary K-line is ``timeframe``.

    ``tsk.make_spec``/``tsk.make_manifest`` are hardcoded to a 1h primary; both
    the spec market and the primary data requirement are retargeted here so the
    request really is a 1d run (the manifest's primary identity must equal the
    spec's, asserted by the compiler).
    """
    spec = tsk.make_spec()
    spec["market"]["timeframe"] = timeframe
    manifest = tsk.make_manifest(spec)
    for requirement in manifest["data_requirements"]:
        if requirement["execution_role"] == "primary_execution_kline":
            requirement["stream_id"] = f"binance.futures.kline.{timeframe}"
            requirement["interval"] = timeframe
            requirement["warmup_bars"] = primary_warmup
    request = tsk.build_request(spec)
    request["artifact_manifest"] = manifest
    request["artifact"]["manifest_hash"] = canonical_json_sha256(manifest)
    request["artifact"]["artifact_hash"] = canonical_json_sha256(
        {
            "schema": "cutie.strategy_artifact_digest.v1",
            "spec_hash": request["artifact"]["spec_hash"],
            "manifest_hash": request["artifact"]["manifest_hash"],
        }
    )
    request["execution_params"]["timeframe"] = timeframe
    request["execution_params"]["start_at"] = start_at
    request["execution_params"]["end_at"] = end_at
    return request


def _post(request: dict) -> dict:
    response = TestClient(provider.app).post(
        "/cutie/backtest",
        json=request,
        headers={"X-Cutie-Connector-Version": "1.2.3"},
    )
    assert response.status_code == 200
    return response.json()


def test_execution_range_catalog_advertises_per_timeframe_budget():
    """目录声明与校验共用常量：日线 1080，日内 365。"""
    assert EXECUTION_MAX_RANGE_DAYS == 1080
    assert EXECUTION_INTRADAY_MAX_RANGE_DAYS == 365
    assert max_range_days_for_timeframe("1d") == 1080
    assert max_range_days_for_timeframe("3d") == 1080
    assert max_range_days_for_timeframe("1w") == 1080
    assert max_range_days_for_timeframe("1h") == 365
    assert max_range_days_for_timeframe("4h") == 365
    assert max_range_days_for_timeframe("15m") == 365
    # 垃圾周期不被这个 helper 放宽——仍按日内 365 处理，周期本身的非法性由
    # compile_strategy 报。
    assert max_range_days_for_timeframe("nonsense") == 365

    artifact_tool = provider._artifact_catalog_tool(["BTCUSDT"], None)
    assert artifact_tool["execution"]["max_range_days"] == 1080
    tool_id, spec = next(iter(provider.TOOL_SPECS.items()))
    exchange_tool = provider._catalog_tool(tool_id, spec, ["BTCUSDT"])
    assert exchange_tool["execution"]["max_range_days"] == 1080


def test_execution_range_1d_1080_accepted(monkeypatch):
    """1d、1080 天通过。修前红：上限是 365，1080 天被拒。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [[0, 100, 101, 99, 100, 1]]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    body = _post(_request("1d", 0, 1080 * DAY))

    assert calls, "1080 天日线请求必须越过区间闸门走到取数"
    # 桩数据只覆盖一根 1d K 线，因此停在覆盖不足——但绝不是区间超限。
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE, body


def test_execution_range_1d_1081_rejected(monkeypatch):
    """1d、1081 天被拒，错误码/结构与既有区间超限一致。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    body = _post(_request("1d", 0, 1081 * DAY))

    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == "$.execution_params"
    assert body["error_detail"]["required"] == {"max_range_days": 1080}
    assert body["error_detail"]["actual"] == {"range_seconds": 1081 * DAY}


@pytest.mark.parametrize("range_days", [366, 1080])
def test_execution_range_1h_over_365_rejected(range_days, monkeypatch):
    """1h、366 天（以及一路到 1080 天）被拒：常量放开后靠这条守住。

    修前 1h/366 也被拒，但那是「所有周期共用 365」的结果；把常量直接改成
    1080 而不按周期分支，这条会变绿——所以它是本次改动的回归锚点。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    body = _post(_request("1h", 0, range_days * DAY))

    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == "$.execution_params"
    assert body["error_detail"]["required"] == {"max_range_days": 365}
    assert body["error_detail"]["actual"] == {"range_seconds": range_days * DAY}


def test_execution_range_1h_365_accepted(monkeypatch):
    """1h、365 天通过：既有行为不退化（与 test_artifact_range_limit_* 同口径）。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [[0, 100, 101, 99, 100, 1]]

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    body = _post(_request("1h", 0, 365 * DAY))

    assert calls == [(0, 365 * DAY * 1000)]
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE, body


def test_execution_range_1d_1080_with_warmup_still_capped(monkeypatch):
    """第二处区间判定（预热撑开的 adapter 窗口）也吃按周期的上限。

    1d、1080 天 + 1 根预热 = 1081 天 > 1080，被拒；错误仍是既有的
    warmup_bars 路径与结构，只是 required 里的数字来自日线档。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)

    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    body = _post(
        _request("1d", 1 * DAY, 1081 * DAY, primary_warmup=1)
    )

    assert body["error_type"] == ERR_SPEC_INVALID
    assert (
        body["error_detail"]["path"]
        == "$.artifact_manifest.data_requirements[0].warmup_bars"
    )
    assert body["error_detail"]["required"] == {"max_range_days": 1080}
