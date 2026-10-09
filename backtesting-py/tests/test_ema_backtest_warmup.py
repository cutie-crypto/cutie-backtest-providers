"""S4b: hand-calculated EMA convergence, routed fetch and frozen result.v2 bytes."""
from __future__ import annotations
import json
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as provider
from canonical_json import canonical_json
import test_risk_overlay_compatibility as compat

START = 1767225600
STEP = 3600
TOOL = 'local.backtesting_py.ema_cross'
V2_KEYS = ('schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest')
FIXTURE = Path(__file__).parent / 'fixtures/ema_warmup_result_v2_32ae030.json'


def frame(warm, main):
    values = warm + main
    return pd.DataFrame(dict(Open=values, High=[v+1 for v in values],
        Low=[v-1 for v in values], Close=values, Volume=[10.]*len(values)),
        index=pd.date_range(pd.to_datetime(START-len(warm)*STEP, unit='s'), periods=len(values), freq='h'))


def fetcher(full, calls=None):
    def fetch(exchange, market, symbol, timeframe, start, end):
        if calls is not None:
            calls.append((start, end))
        return full.loc[(full.index >= pd.to_datetime(start, unit='s')) &
                        (full.index < pd.to_datetime(end, unit='s'))].copy()
    return fetch


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(provider, 'AUTH_TOKEN', '')
    monkeypatch.setattr(provider, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(Backtest, 'plot', lambda *a, **kw: None)
    return TestClient(provider.app)


def post(client, params=None, name='ema_cross', start=START, count=21):
    res = client.post('/cutie/backtest', json={'backtest': dict(run_id='s4bp',
        provider_tool_id='local.backtesting_py.'+name, provider_params=params or {'ema_fast':5,'ema_slow':20},
        symbol='BTCUSDT', market='spot', timeframe='1h', start_at=start, end_at=start+count*STEP,
        initial_capital='10000', fee_bps='10', slippage_bps='5')})
    assert res.status_code == 200
    body = res.json()
    assert body['result_status'] == 'success', body
    return body


@pytest.mark.parametrize('fast,slow,requested,target', [(5,20,200,200),(60,120,1200,1200),(5,3000,30000,20000)])
def test_target_bars_and_original_minimum(fast, slow, requested, target):
    built = provider.TOOL_SPECS[TOOL]['build']({'ema_fast':fast,'ema_slow':slow})
    assert built['ema_warmup_requested_bars'] == requested
    assert built['ema_warmup_target_bars'] == target
    assert built['min_bars'] == slow + 1


@pytest.mark.parametrize('fast,slow,target', [(5,20,200),(60,120,1200),(5,3000,20000)])
def test_http_fetch_depth_and_cap(client, monkeypatch, fast, slow, target):
    full = frame([100.]*(target+2), [100.]*(slow+1))
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full, calls))
    params = {'ema_fast':fast,'ema_slow':slow}
    if slow == 3000:
        # Public schema currently rejects slow>300. Simulate a future expanded
        # parameter domain at the build boundary; do not relax the actual schema.
        original = provider.TOOL_SPECS[TOOL]['build']
        future_params = params.copy()
        monkeypatch.setitem(provider.TOOL_SPECS[TOOL], 'build',
            lambda supplied, **kwargs: original(future_params, **kwargs))
        params = {'ema_fast':5,'ema_slow':20}
    body = post(client, params, count=slow+1)
    assert calls == [(START, START+(slow+1)*STEP), (START-(target+2)*STEP, START)]
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=slow*10,
        target_bars=target, actual_bars=target, truncated=(slow==3000), tenfold_reached=(slow!=3000))
    expected = f'EMA 预热取 10×最长周期（目标 {target} 根，实得 {target} 根）'
    if slow == 3000:
        # Frozen customer-facing cap disclosure; requested=30000 is handwritten.
        expected += ('回测已把预热截到 20000 根；实盘自动信号要求 10×最长周期 ≤ 20000 根，'
                     '本参数（需 30000 根）无法布防自动信号，请调小慢线周期')
        assert '需 30000 根' in body['assumptions']['ema_warmup']
    else:
        assert '回测已把预热截到' not in body['assumptions']['ema_warmup']
        assert '无法布防自动信号' not in body['assumptions']['ema_warmup']
    assert body['assumptions']['ema_warmup'] == expected


def test_tenfold_suppresses_seed_cross_at_first_evaluated_bar(client, monkeypatch):
    # 179×200, 20×110, 100; main 100,120,19×100. EMA recurrence: a=2/(N+1).
    # Legacy 21-bar seed (slow+1): main[0] fast=104.444444 slow=108.185941; main[1]
    # fast=109.629630 slow=109.311090 -> one cross, next-open buy main[2], sell main[3].
    # 200-bar seed stays below slow at main[0] and main[1].
    # Remains fast<slow on the subsequent flat closes -> zero trades.
    # backtesting.py first evaluates next() on main[1], not main[0].
    full = frame([200.]*179+[110.]*20+[100.], [100.,120.]+[100.]*19)
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
    tenfold = post(client)
    assert tenfold['trades'] == []
    assert tenfold['raw_report']['ema_warmup']['actual_bars'] == 200
    original = provider.TOOL_SPECS[TOOL]['build']
    def onefold(params, **kwargs):
        built = original(params, **kwargs)
        built['ema_warmup_target_bars'] = 21
        return built
    monkeypatch.setitem(provider.TOOL_SPECS[TOOL], 'build', onefold)
    old = post(client)
    assert len(old['trades']) == 1
    assert old['trades'][0]['opened_at'] == START+2*STEP
    assert old['trades'][0]['closed_at'] == START+3*STEP


def test_short_history_succeeds_and_discloses_shortfall(client, monkeypatch):
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(frame([100.]*50, [100.]*21)))
    body = post(client)
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=200, target_bars=200,
        actual_bars=50, truncated=False, tenfold_reached=False)
    assert body['assumptions']['ema_warmup'] == 'EMA 预热取 10×最长周期（目标 200 根，实得 50 根）'
    assert body['data_manifest']['kline_count'] == 21


def test_fetch_failure_stays_best_effort(client, monkeypatch):
    good = fetcher(frame([], [100.]*21))
    def flaky(*args):
        if args[-2] < START:
            raise RuntimeError('history unavailable')
        return good(*args)
    monkeypatch.setattr(provider, '_fetch_ohlcv', flaky)
    body = post(client)
    assert body['raw_report']['ema_warmup']['actual_bars'] == 0
    assert body['raw_report']['ema_warmup']['tenfold_reached'] is False


def test_warmup_excludes_main_and_future_rows(monkeypatch):
    full = frame([100.]*200, [100.]*21)
    main = full.loc[full.index >= pd.to_datetime(START,unit='s')]
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *a: full.copy())
    before = provider._fetch_template_warmup('binance','spot','BTCUSDT','1h',START,200,main)
    assert len(before) == 200
    assert (before.index < main.index[0]).all()
    assert (before.index + pd.Timedelta(hours=1) <= pd.to_datetime(START,unit='s')).all()
    full.loc[main.index, 'Close'] = 1e9
    after = provider._fetch_template_warmup('binance','spot','BTCUSDT','1h',START,200,main)
    pd.testing.assert_frame_equal(before, after)


@pytest.mark.parametrize('name', ['rsi_reversal','macd','supertrend','ema_cross'])
def test_routed_result_v2_matches_frozen_bytes(client, monkeypatch, name):
    full = compat.frame()
    main = full.iloc[60:]
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
    body = post(client, compat.PARAMS[name], name, int(main.index[0].timestamp()), len(main))
    expected = json.loads(FIXTURE.read_text())
    encoded = canonical_json({k:body[k] for k in V2_KEYS})
    assert encoded.encode() == expected['cases'][name].encode()
    if name != 'ema_cross':
        assert 'ema_warmup' not in body['raw_report']
        assert 'ema_warmup' not in body['assumptions']
