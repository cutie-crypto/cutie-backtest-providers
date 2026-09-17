"""目录声明的周期必须与真实能力对齐（2026-09-18 加）。

起因：目录里写死 ["1h","4h","1d"]，执行侧却接受 1m–1M。前端按目录声明决定回测
按钮能不能点，于是用户写的 15m 策略被自己的门禁挡在门外，而同一台机器经别的入口
跑同一份草稿的 15m 回测是成功的。这类"声明与能力脱节"肉眼看不出来，用测试钉住。

这里只钉三件事：
1. 对外声明的每一档，执行侧必须真的接受（否则用户点了必失败）。
2. 走平台中心行情的那份，中心行情必须真的提供该档（否则拿不到 K 线）。
3. catalog 输出用的就是这两份常量，不是又写回字面量。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402

# cutie-server `services/backtest_data_source.py` 的 _TIMEFRAME_TO_INTERVAL。
# 中心行情只映射这七档，多写一档就会在取数时报 Unsupported timeframe。
CENTRAL_MARKET_DATA_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1d", "1w"}


def test_catalog_timeframes_are_executable():
    for name, declared in (
        ("exchange", provider.CATALOG_TIMEFRAMES_EXCHANGE),
        ("central", provider.CATALOG_TIMEFRAMES_CENTRAL),
    ):
        unsupported = [tf for tf in declared if tf not in provider.EXECUTION_SUPPORTED_TIMEFRAMES]
        assert not unsupported, f"{name} 目录声明了执行侧不接受的周期: {unsupported}"


def test_central_catalog_timeframes_have_central_market_data():
    missing = [tf for tf in provider.CATALOG_TIMEFRAMES_CENTRAL if tf not in CENTRAL_MARKET_DATA_TIMEFRAMES]
    assert not missing, f"中心行情没有这些周期的 K 线，不能对外声明: {missing}"


def test_catalog_declares_15m_for_copy_trading():
    # 15m 是跟单能承受的最短周期，两条路径都必须支持，别再被目录声明挡回去。
    assert "15m" in provider.CATALOG_TIMEFRAMES_EXCHANGE
    assert "15m" in provider.CATALOG_TIMEFRAMES_CENTRAL


def test_catalog_tools_use_the_shared_constants():
    symbols = ["BTCUSDT", "ETHUSDT"]

    artifact_tool = provider._artifact_catalog_tool(symbols, None)
    assert artifact_tool["timeframes"] == list(provider.CATALOG_TIMEFRAMES_CENTRAL)

    tool_id, spec = next(iter(provider.TOOL_SPECS.items()))
    exchange_tool = provider._catalog_tool(tool_id, spec, symbols)
    assert exchange_tool["timeframes"] == list(provider.CATALOG_TIMEFRAMES_EXCHANGE)
