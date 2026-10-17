import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cutie_backtesting_provider as provider


def test_longwin_annual_range_and_structured_refusal_before_fetch(monkeypatch):
    start = 1700000000
    assert provider._exchange_range_failure("15m", start, start + 365 * 86400) is None
    assert provider._exchange_range_failure("1h", start, start + 365 * 86400) is None
    assert provider.CATALOG_MAX_BARS_EXCHANGE == 35040
    assert provider.CATALOG_MAX_BARS_CENTRAL == 5000
    monkeypatch.setattr(provider, "_verify_bearer", lambda auth: None)
    def forbidden(*args, **kwargs):
        raise AssertionError("over-limit request fetched market data")
    monkeypatch.setattr(provider, "_fetch_ohlcv", forbidden)
    class Request:
        async def json(self):
            return {"backtest": {"run_id": "longwin", "symbol": "BTCUSDT", "market": "spot",
                                  "timeframe": "15m", "start_at": start,
                                  "end_at": start + 365 * 86400 + 900}}
    response = asyncio.run(provider.run_backtest(Request(), authorization=None))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["error_type"] == "TIME_RANGE_TOO_LONG"
    assert body["result_status"] == "failed"
    assert body["limitations"] == {"reason": "time_range_too_long", "interval": "15m",
                                   "max_bars": 35040, "suggested_max_days": 365}
    assert provider._exchange_range_failure("1m", start, start + 25 * 86400) is not None
