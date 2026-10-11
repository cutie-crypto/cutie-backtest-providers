"""Q46：目录声明的执行超时必须对齐 connector 硬上限（300000ms）。

起因：亚洲盘 15m 208 天回测需要超过 120s，connector 取 min(目录 timeout_ms, 300000) 作超时，
目录写 120000 就会提前报 provider timeout。全部工具（含组合轮动、artifact）共用一个常量。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cutie_backtesting_provider as provider  # noqa: E402

CONNECTOR_HARD_CAP_MS = 300000


def _all_catalog_tools():
    symbols = ["BTCUSDT", "ETHUSDT"]
    tools = [provider._catalog_tool(tid, spec, symbols) for tid, spec in provider.TOOL_SPECS.items()]
    tools.append(provider._artifact_catalog_tool(symbols, None))
    return tools


def test_execution_timeout_constant_matches_connector_hard_cap():
    assert provider.EXECUTION_TIMEOUT_MS == CONNECTOR_HARD_CAP_MS


def test_every_catalog_tool_declares_the_shared_timeout():
    tools = _all_catalog_tools()
    assert len(tools) > 10
    bad = {
        t["tool_id"]: t["execution"]["timeout_ms"]
        for t in tools
        if t["execution"]["timeout_ms"] != provider.EXECUTION_TIMEOUT_MS
        or t["execution"]["timeout_ms"] > CONNECTOR_HARD_CAP_MS
    }
    assert not bad, f"这些工具的 timeout_ms 与共享常量不符或超过 connector 上限: {bad}"
