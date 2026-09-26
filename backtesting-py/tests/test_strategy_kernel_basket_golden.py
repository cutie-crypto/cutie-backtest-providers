"""123 组合策略：生产实跑 golden 用例（真实数据端到端复现）。

用 2026-09-26 生产实跑成功的组合回测（run_id 362234588539392000，
``basket_ratio_sma_cross``，ETHUSDT 多 / BTCUSDT 空，4h，17 笔）的真实 K 线与真实
结果，断言 provider ``/cutie/backtest`` 在本地能逐笔复现出相同的 17 笔交易与相同
指标。数据与裁窗规则的来源见 ``tests/fixtures/basket_sma_cross_prod_golden_0926.json``
顶部 ``_note``。

本文件只做「真实数据能否复现生产结果」这一件事，不测组合内核语义本身（语义已由
``test_strategy_kernel_conformance_v3.py``/``test_basket_signal_http.py`` 的手算
fixture 覆盖）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import cutie_backtesting_provider as provider  # noqa: E402

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "basket_sma_cross_prod_golden_0926.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _install_fetch(monkeypatch, klines_by_symbol: dict[str, list[dict]]):
    def fetch(exchange_id, market, symbol, timeframe, start_sec, end_sec):
        rows = klines_by_symbol[symbol]
        ohlcv = [
            [row["open_time"] * 1000, row["open"], row["high"], row["low"], row["close"], row["volume"]]
            for row in rows
        ]
        return ohlcv, "cutie_central_market_data", True, False

    monkeypatch.setattr(provider, "_fetch_ohlcv_raw", fetch)


def _request_body() -> dict:
    req = FIXTURE["request"]
    return {
        "backtest": {
            "run_id": "prod-golden-0926",
            "provider_tool_id": req["provider_tool_id"],
            "provider_params": req["provider_params"],
            "symbol": req["symbol"],
            "market": req["market"],
            "timeframe": req["timeframe"],
            "start_at": req["start_at"],
            "end_at": req["end_at"],
            "initial_capital": req["initial_capital"],
            "fee_bps": req["fee_bps"],
            "slippage_bps": req["slippage_bps"],
            "instrument_rules": FIXTURE["instrument_rules"],
        }
    }


def _post(request: dict) -> dict:
    return TestClient(provider.app).post(
        "/cutie/backtest", content=json.dumps(request), headers={"Content-Type": "application/json"},
    ).json()


def _klines_by_symbol() -> dict[str, list[dict]]:
    symbol_by_leg = FIXTURE["symbol_by_leg"]
    return {symbol_by_leg[leg_id]: rows for leg_id, rows in FIXTURE["klines"].items()}


def test_basket_sma_cross_reproduces_production_run(monkeypatch):
    """真实 K 线喂进 provider，逐笔复现生产 17 笔交易与全部指标。"""
    _install_fetch(monkeypatch, _klines_by_symbol())
    body = _post(_request_body())
    assert body["result_status"] == "success", body

    expected = FIXTURE["expected"]
    assert body["trades"] == expected["trades"]
    assert body["metrics"] == expected["metrics"]
    assert len(body["trades"]) == expected["trade_count"] == 17
    assert body["strategy_spec_hash"] == expected["strategy_spec_v3_hash"]

    # 两腿 data manifest：kline_count 与 checksum 都要与生产 integrity_evidence 一致——
    # 这一条证明喂进内核的就是生产当时用的那份数据，不只是「跑得动」。
    got_manifests = {m["leg_id"]: m for m in body["data_manifests"]}
    for expected_manifest in expected["data_manifests"]:
        leg_id = expected_manifest["leg_id"]
        got = got_manifests[leg_id]
        assert got["kline_count"] == expected_manifest["kline_count"] == 539
        assert got["checksum"] == expected_manifest["checksum"]
        assert got["checksum_algo"] == expected_manifest["checksum_algo"]


def test_basket_sma_cross_golden_is_sensitive_to_kline_data(monkeypatch):
    """变异自证：改掉窗口内某一根 close，trades/metrics 必须随之变化——证明上面那条
    用例真的在比对喂进去的真实数据，不是碰巧总能对上一份写死的 expected。"""
    mutated = copy.deepcopy(FIXTURE["klines"])
    # 选窗口内（非预热段）中间某一根，把收盘价改成那一根的最高价——扰动足够大到会
    # 改变均线交叉信号，同时仍满足 low<=close<=high，不会被 OHLC 合法性校验拒掉。
    target = mutated["a"][300]
    assert FIXTURE["request"]["start_at"] <= target["open_time"] < FIXTURE["request"]["end_at"]
    assert target["close"] != target["high"]
    target["close"] = target["high"]

    symbol_by_leg = FIXTURE["symbol_by_leg"]
    _install_fetch(monkeypatch, {symbol_by_leg[leg_id]: rows for leg_id, rows in mutated.items()})
    body = _post(_request_body())
    assert body["result_status"] == "success", body

    expected = FIXTURE["expected"]
    assert body["trades"] != expected["trades"] or body["metrics"] != expected["metrics"]
