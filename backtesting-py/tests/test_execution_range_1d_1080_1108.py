"""JES-T15: 日线回测上限放开到 1080 天，日内周期仍限 365 天。

Owner 结论：日线（timeframe >= 1d）最长 1080 天（12 个 90 天分片 / 36 个整月；
1095 会被前端切片规则减到 1080）。日内（< 1d）必须留在 365 天：1d 的 1h 根数
是 24，1080 天 1h 就是 25920 根，远超目录声明的 max_bars=5000。

JES-T15-R1（本文件第二轮）：区间上限必须按**每个数据流自己的 interval** 取，
不能把主周期的额度借给别的流；月线 ``1M`` 归日线及以上；1080 天**包含预热**，
与服务端「超限时建议减少天数」同一口径（K 线库存从 2023-11 起，不另采预热余量）。

修前（origin/main，全局 365）各用例红绿：
- 1d/1080、1d/1080+真实预热：红（365 天闸门误拒）。
- 1M/1080：红（``1M`` 未识别成 >= 1d，落进 365）。
- 1d 带 1h 流、1h 带 1d 流：修前红/绿方向都错——前者 1h 流借了 1080 额度被放行
  （断言「必须被拒」所以红），后者 1d 流继承 365 额度被误拒（断言「必须通过」所以红）。
- 1h/366、1h/1080、1h/365：修前也是绿（全局 365 本来就会拒/放行同样的边界），
  它们是「不许把常量直接放宽到 1080」的变异锚点，不是本轮的新红点。

校验入口唯一：``strategy_execution.validate_execution_request``（请求跨度与
预热撑开的 adapter 窗口两处判定）。provider catalog 的 ``max_range_days`` 只是
同一个常量 ``EXECUTION_MAX_RANGE_DAYS`` 的声明。
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
HOUR = 60 * 60
# 真实正时间戳（2023-01-01 起），不用 0：``max(0, start_at - warmup)`` 会把
# start_at=0 的预热截断成 0，那是审查点名的 happy-path 假象来源。
BASE = 1672531200


def _feature(interval: str) -> tuple[dict, dict]:
    key = f"cvd_{interval}"
    feature = {
        "key": key,
        "primitive": "rolling_sum",
        "primitive_version": "1",
        "source_stream": "coinglass.futures_cvd",
        "interval": interval,
        "value_kind": "flow",
        "output_type": "decimal",
        "params": {"window_bars": 1},
        "required": True,
    }
    condition = {
        "node": "compare",
        "op": "gt",
        "left": {"node": "feature", "key": key, "lag_bars": 0},
        "right": {"node": "literal", "value_type": "decimal", "value": "1.5"},
    }
    return feature, condition


def _request(
    timeframe: str,
    start_at: int,
    end_at: int,
    *,
    primary_warmup: int = 0,
    feature_interval: str | None = None,
    feature_warmup: int = 2,
) -> dict:
    """A one-symbol request whose primary K-line is ``timeframe``.

    ``tsk.make_spec``/``tsk.make_manifest`` are hardcoded to a 1h primary and a
    1h feature stream; this retargets the spec market, the primary requirement
    and (optionally) the feature requirement so the manifest really declares the
    intervals under test (the primary identity must equal the spec's, asserted
    by the compiler).
    """
    if feature_interval is None:
        spec = tsk.make_spec()
    else:
        feature, condition = _feature(feature_interval)
        spec = tsk.make_spec(condition=condition, features=[feature])
    spec["market"]["timeframe"] = timeframe
    manifest = tsk.make_manifest(spec)
    feature_stream_id = (
        f"coinglass.futures_cvd.{feature_interval}"
        if feature_interval is not None
        else None
    )
    for requirement in manifest["data_requirements"]:
        if requirement["execution_role"] == "primary_execution_kline":
            requirement["stream_id"] = f"binance.futures.kline.{timeframe}"
            requirement["interval"] = timeframe
            requirement["warmup_bars"] = primary_warmup
        elif (
            feature_stream_id is not None
            and requirement["execution_role"] == "feature_input"
        ):
            requirement["stream_id"] = feature_stream_id
            requirement["interval"] = feature_interval
            requirement["warmup_bars"] = feature_warmup
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


def _no_fetch(monkeypatch) -> None:
    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    monkeypatch.setattr(provider, "_fetch_from_central", fail_if_fetched)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fail_if_fetched)


def _stub_fetches(monkeypatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def fetch_klines(exchange, market, symbol, timeframe, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        return [[0, 100, 101, 99, 100, 1]]

    def fetch_metric_chunk(**kwargs):
        return []

    monkeypatch.setattr(provider, "_fetch_from_central", fetch_klines)
    monkeypatch.setattr(provider, "_fetch_artifact_metric_chunk", fetch_metric_chunk)
    return calls


def test_execution_range_catalog_advertises_per_timeframe_budget():
    """目录声明与校验共用常量：日线 1080，日内 365，月线归日线。"""
    assert EXECUTION_MAX_RANGE_DAYS == 1080
    assert EXECUTION_INTRADAY_MAX_RANGE_DAYS == 365
    assert max_range_days_for_timeframe("1d") == 1080
    assert max_range_days_for_timeframe("3d") == 1080
    assert max_range_days_for_timeframe("1w") == 1080
    # 修前红：``1M`` 不在单位表里，落到日内 365。
    assert max_range_days_for_timeframe("1M") == 1080
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
    """1d、1080 天通过。修前红：全局上限是 365，1080 天被拒。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls = _stub_fetches(monkeypatch)
    body = _post(_request("1d", BASE, BASE + 1080 * DAY))

    assert calls, "1080 天日线请求必须越过区间闸门走到取数"
    # 桩数据只覆盖一根 1d K 线，因此停在覆盖不足——但绝不是区间超限。
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE, body


def test_execution_range_1d_1081_rejected(monkeypatch):
    """1d、1081 天被拒；修前已绿（365 更严），钉住 1080 不是无上限。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(_request("1d", BASE, BASE + 1081 * DAY))

    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == "$.execution_params"
    assert body["error_detail"]["required"] == {"max_range_days": 1080}
    assert body["error_detail"]["actual"] == {"range_seconds": 1081 * DAY}


@pytest.mark.parametrize("range_days", [366, 1080])
def test_execution_range_1h_over_365_rejected(range_days, monkeypatch):
    """1h、366 天（以及一路到 1080 天）必须被拒。

    修前也绿——那是「所有周期共用 365」的结果，所以它不构成本轮的新红点；
    它的价值是变异锚点：若只把常量放宽到 1080 而不按周期分支，它就会失败。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(_request("1h", BASE, BASE + range_days * DAY))

    assert body["error_type"] == ERR_SPEC_INVALID
    assert body["error_detail"]["path"] == "$.execution_params"
    assert body["error_detail"]["required"] == {"max_range_days": 365}
    assert body["error_detail"]["actual"] == {"range_seconds": range_days * DAY}


def test_execution_range_1h_365_accepted(monkeypatch):
    """1h、365 天通过：既有行为不退化（修前亦绿，回归锚点）。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls = _stub_fetches(monkeypatch)
    body = _post(_request("1h", BASE, BASE + 365 * DAY))

    assert calls == [(BASE * 1000, (BASE + 365 * DAY) * 1000)]
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE, body


def test_execution_range_1M_1080_accepted(monkeypatch):
    """1M、1080 天不得被区间闸门拒。修前红：``1M`` 被当成日内，365 天拒掉。

    本用例只钉区间额度这一层：1080 天跨度必须越过 ``$.execution_params`` 的
    区间闸门。月线 spec 本身是否可执行由 ``compile_strategy`` 的周期白名单
    （``_INTERVAL_RE``）判定，那是既有契约，本轮不改——所以断言的是「失败点
    不是区间闸门」，而不是「整条请求成功」。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(_request("1M", BASE, BASE + 1080 * DAY))

    assert body["error_type"] == ERR_SPEC_INVALID, body
    detail = body["error_detail"]
    assert detail["path"] != "$.execution_params", detail
    assert detail.get("required") != {"max_range_days": 365}, detail


def test_execution_range_1d_with_1h_stream_over_365_rejected(monkeypatch):
    """1d 主周期 + 1h 特征流：1h 流自己的窗口仍不得超过 365 天。

    修前红：循环里所有流共用主周期的 1080 天额度，366 天 + 2 根 1h 预热的
    1h 流被放行（断言「必须被拒」因此失败）。
    归因：请求跨度 366 天本身就超过 1h 流的 365 天，所以错误路径指 ``interval``
    而不是 ``warmup_bars``（JES-T15-S2 第 2 项）。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(
        _request(
            "1d",
            BASE,
            BASE + 366 * DAY,
            feature_interval="1h",
            feature_warmup=2,
        )
    )

    assert body["error_type"] == ERR_SPEC_INVALID, body
    # data_requirements 按 stream_id 排序：binance...kline.1d 在 [0]，
    # coinglass...cvd.1h 在 [1]。
    assert (
        body["error_detail"]["path"]
        == "$.artifact_manifest.data_requirements[1].interval"
    )
    assert body["error_detail"]["required"] == {"max_range_days": 365}, body
    assert body["error_detail"]["actual"] == {"range_seconds": 366 * DAY}, body


def test_execution_range_1d_with_1h_stream_error_points_interval(monkeypatch):
    """JES-T15-S2：请求区间本身超限时，错误必须指向 ``interval``。

    修前红：同一场景一律抛 ``warmup_bars`` + "warmup expands adapter data
    window ..."，把「含日内流的策略整体区间不得超 365 天」误报成预热太深——
    即使 warmup_bars=0 也照抛。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(
        _request("1d", BASE, BASE + 500 * DAY, feature_interval="1h", feature_warmup=1)
    )

    assert body["error_type"] == ERR_SPEC_INVALID, body
    detail = body["error_detail"]
    assert detail["path"] == "$.artifact_manifest.data_requirements[1].interval", detail
    assert detail["required"] == {"max_range_days": 365}, detail
    assert detail["actual"] == {"range_seconds": 500 * DAY}, detail


def test_execution_range_warmup_overflow_keeps_warmup_path(monkeypatch):
    """只有预热撑开窗口时才保留 warmup_bars 归因（1d/1080 + 20 根预热）。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(_request("1d", BASE, BASE + 1080 * DAY, primary_warmup=20))

    assert body["error_type"] == ERR_SPEC_INVALID, body
    detail = body["error_detail"]
    assert detail["path"] == "$.artifact_manifest.data_requirements[0].warmup_bars", detail
    assert detail["required"] == {"max_range_days": 1080}, detail
    assert detail["actual"]["effective_start"] == BASE - 20 * DAY, detail


def test_execution_range_1h_with_1d_stream_365_accepted(monkeypatch):
    """1h 主周期 + 1d 特征流：合法的 365 天回测不得被 1d 流的预热误拒。

    修前红：1d 流继承了主周期的 365 天额度，365 天 + 2 根 1d 预热 = 367 天
    被判超限（断言「必须通过」因此失败）。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls = _stub_fetches(monkeypatch)
    body = _post(
        _request(
            "1h",
            BASE,
            BASE + 365 * DAY,
            feature_interval="1d",
            feature_warmup=2,
        )
    )

    assert calls, "1h/365 天带 1d 特征流必须越过区间闸门走到取数"
    assert body["error_type"] != ERR_SPEC_INVALID, body


def test_execution_range_1d_880_with_real_warmup_accepted(monkeypatch):
    """1080 天包含预热：880 天 + 20 根真实日线预热 = 900 天，通过。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls = _stub_fetches(monkeypatch)
    body = _post(
        _request("1d", BASE, BASE + 880 * DAY, primary_warmup=20)
    )

    assert calls == [((BASE - 20 * DAY) * 1000, (BASE + 880 * DAY) * 1000)]
    assert body["error_type"] == ERR_COVERAGE_INCOMPLETE, body


def test_execution_range_1d_1080_with_real_warmup_rejected(monkeypatch):
    """1080 天包含预热：1080 天 + 20 根真实日线预热 = 1100 天，被拒。

    修前也红（365 天闸门），但拒的理由不同；本轮钉住的是「1080 含预热」的
    口径：错误信息里的 ``max_range_days`` 必须是 1080。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _no_fetch(monkeypatch)
    body = _post(
        _request("1d", BASE, BASE + 1080 * DAY, primary_warmup=20)
    )

    assert body["error_type"] == ERR_SPEC_INVALID, body
    assert (
        body["error_detail"]["path"]
        == "$.artifact_manifest.data_requirements[0].warmup_bars"
    )
    assert body["error_detail"]["required"] == {"max_range_days": 1080}


# --- JES-T15-S2: template (exchange-param) entry has its own range gate ------
# The artifact path above validates via strategy_execution; template tools take
# ``_run_backtest_sync``'s exchange-param branch, which used to check only
# ``start_at < end_at``. The catalog has to advertise a timeframe-agnostic 1080,
# so the run entry narrows it per timeframe (intraday 365).

TEMPLATE_TOOL = "local.backtesting_py.turtle"


def _template_body(
    timeframe: str,
    start_at: int,
    end_at: int,
    *,
    tool: str = TEMPLATE_TOOL,
    params: dict | None = None,
) -> dict:
    return {
        "backtest": {
            "run_id": "range_template",
            "provider_tool_id": tool,
            "provider_params": {} if params is None else params,
            "symbol": "BTCUSDT",
            "market": "spot",
            "timeframe": timeframe,
            "start_at": start_at,
            "end_at": end_at,
            "initial_capital": "100000",
            "fee_bps": "0",
            "slippage_bps": "0",
        }
    }


def _template_no_fetch(monkeypatch) -> None:
    def fail_if_fetched(*args, **kwargs):
        raise AssertionError("range validation must happen before data access")

    monkeypatch.setattr(provider, "_fetch_ohlcv", fail_if_fetched)


def _template_stub_fetch(monkeypatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def fetch(exchange, market, symbol, timeframe, start_ms, end_ms):
        calls.append((start_ms, end_ms))
        import pandas as pd

        return pd.DataFrame(
            {"Open": [100.0], "High": [101.0], "Low": [99.0], "Close": [100.0], "Volume": [1.0]},
            index=pd.DatetimeIndex([pd.Timestamp(start_ms, unit="s")]),
        )

    monkeypatch.setattr(provider, "_fetch_ohlcv", fetch)
    return calls


def test_execution_range_template_1h_366_rejected(monkeypatch):
    """模板入口 1h/366 天必须被拒。修前红：模板入口只校验 start_at < end_at。"""
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    _template_no_fetch(monkeypatch)
    body = _post(_template_body("1h", BASE, BASE + 366 * DAY))

    assert body["result_status"] == "failed", body
    assert body["error_type"] == "INVALID_PARAMS", body
    assert "365" in body["error_message"], body
    assert "366.0 days" in body["error_message"], body


def _assert_template_accepted(monkeypatch, timeframe: str, range_days: int) -> dict:
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    calls = _template_stub_fetch(monkeypatch)
    body = _post(_template_body(timeframe, BASE, BASE + range_days * DAY))

    # 模板路径的 _fetch_ohlcv 收秒级时间戳（artifact 路径是毫秒）。
    assert calls == [(BASE, BASE + range_days * DAY)]
    assert not (
        body.get("error_type") == "INVALID_PARAMS"
        and "Requested range is" in body.get("error_message", "")
    ), body
    return body


def test_execution_range_template_1d_1080_accepted(monkeypatch):
    """模板入口 1d/1080 天必须通过（回归锚点，修前亦绿）。"""
    _assert_template_accepted(monkeypatch, "1d", 1080)


def test_execution_range_template_1h_365_accepted(monkeypatch):
    """模板入口 1h/365 天必须通过（边界，修前亦绿）。"""
    _assert_template_accepted(monkeypatch, "1h", 365)


def test_execution_range_template_self_declared_limit_not_rejected(monkeypatch):
    """自己声明了更大 max_range_days 的工具（轮动 5000）不得被周期上限误拒。

    变异点：若闸门对所有工具一律套 max_range_days_for_timeframe（忽略
    TOOL_SPEC 的 max_range_days 声明），1h/366 天会在这里被拒 -> 本用例如。
    """
    monkeypatch.setattr(provider, "PROVIDER_REVISION", tsk.REVISION)
    wide_spec = dict(provider.TOOL_SPECS[TEMPLATE_TOOL])
    wide_spec["max_range_days"] = 5000
    monkeypatch.setitem(provider.TOOL_SPECS, TEMPLATE_TOOL, wide_spec)
    calls = _template_stub_fetch(monkeypatch)
    body = _post(_template_body("1h", BASE, BASE + 366 * DAY))

    assert provider._template_max_range_days(TEMPLATE_TOOL, "1h") == 5000
    assert calls, "声明 5000 天的工具不得被 365 天周期上限误拒"
    assert not (
        body["error_type"] == "INVALID_PARAMS"
        and "Requested range is" in body["error_message"]
    ), body


def test_execution_range_template_rotation_catalog_keeps_5000():
    """轮动目录声明仍是 5000，且运行期闸门读同一个声明值。"""
    import portfolio_rotation_http as rotation

    assert rotation.TOOL_SPEC["max_range_days"] == 5000
    entry = provider._catalog_tool(
        rotation.TOOL_ID, provider.TOOL_SPECS[rotation.TOOL_ID], []
    )
    assert entry["execution"]["max_range_days"] == 5000
    assert provider._template_max_range_days(rotation.TOOL_ID, "1d") == 5000
