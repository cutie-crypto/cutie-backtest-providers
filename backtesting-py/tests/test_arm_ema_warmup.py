"""Five builders share EMA cross's best-effort tenfold seed."""
from __future__ import annotations
import math
import pandas as pd
import pytest
import cutie_backtesting_provider as provider
from test_ema_backtest_warmup import START, STEP, client, fetcher, frame, post, V2_KEYS

CASES = [
    ('ema_triple_alignment', {'ema_short':5, 'ema_mid':20, 'ema_long':60}, 60, 60),
    # RSI > slow EMA changes min_bars, never EMA seed depth.
    ('ema_trend_rsi', {'ema_fast':5, 'ema_slow':20, 'rsi_period':30,
        'rsi_entry_below':80, 'rsi_exit_above':90}, 20, 31),
    ('ema_rsi_pullback', {'ema_period':50, 'rsi_period':30, 'rsi_entry':40, 'rsi_exit':70}, 50, 50),
    ('bias_reversion', {'ema_period':20, 'bias_entry_pct':3}, 20, 20),
    ('ema_pullback', {'ema_fast':5, 'ema_slow':30}, 30, 30),
]
IDS = [c[0] for c in CASES]

@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_builder_tenfold_depth_and_original_minimum(name, params, period, minimum):
    built = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)
    assert built['ema_warmup_requested_bars'] == period*10
    assert built['ema_warmup_target_bars'] == period*10
    assert built['min_bars'] == minimum

@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_builder_caps_request_above_maximum(monkeypatch, name, params, period, minimum):
    # Public bounded EMA schemas cannot exceed 20000. Simulate a larger request
    # through the multiplier without changing the public parameter domains.
    monkeypatch.setattr(provider, 'EMA_WARMUP_MULTIPLIER', 2000)
    built = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)
    assert built['ema_warmup_requested_bars'] == period*2000
    assert built['ema_warmup_target_bars'] == 20000
    assert built['min_bars'] == minimum


def market_frame(period, warm_count):
    warm = [200.]*max(0, warm_count-period)+[100.]*min(warm_count, period)
    main = [100+20*math.sin(i/12)+i*.02 for i in range(360)]
    return frame(warm, main)

@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_sufficient_history_seeds_indicators_without_warmup_signals(client, monkeypatch, name, params, period, minimum):
    target = period*10
    full = market_frame(period, target+2)
    calls, evaluated, indicators = [], [], {}
    tool = provider.TOOL_SPECS['local.backtesting_py.'+name]
    original = tool['build']
    def capture(supplied, **kwargs):
        built = original(supplied, **kwargs)
        strategy = built['strategy']
        original_init = strategy.init
        def init_with_indicators(self):
            original_init(self)
            for attr in ('ema_short','ema_mid','ema_long','ema_fast','ema_slow','ema','bias'):
                if hasattr(self, attr):
                    indicators[attr] = list(getattr(self, attr))
        strategy.init = init_with_indicators
        original_next = strategy.next
        def next_with_clock(self):
            evaluated.append(self.data.index[-1])
            return original_next(self)
        strategy.next = next_with_clock
        return built
    monkeypatch.setitem(tool, 'build', capture)
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full, calls))
    body = post(client, params, name, count=360)
    assert calls == [(START, START+360*STEP), (START-(target+2)*STEP, START)]
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=target,
        target_bars=target, actual_bars=target, truncated=False, tenfold_reached=True)
    assert evaluated and min(evaluated) >= pd.to_datetime(START, unit='s')
    # Independent recurrence proves actual indicators use the tenfold prefix,
    # not just the report. Earlier unavailable/unused history is excluded.
    closes = list(full['Close'].iloc[-(target+360):])
    spans = {'ema_short':params.get('ema_short'), 'ema_mid':params.get('ema_mid'),
             'ema_long':params.get('ema_long'), 'ema_fast':params.get('ema_fast'),
             'ema_slow':params.get('ema_slow'), 'ema':params.get('ema_period'),
             'bias':params.get('ema_period')}
    assert indicators
    for attr, values in indicators.items():
        span = spans[attr]
        alpha, current, expected = 2/(span+1), closes[0], []
        for close in closes:
            current = alpha*close+(1-alpha)*current
            expected.append((close-current)/current*100 if attr == 'bias' else current)
        assert values == pytest.approx(expected[-360:], rel=0, abs=1e-10)
    assert body['trades'], 'main range must exercise real entries'
    assert all(t['opened_at'] >= START and t['closed_at'] <= START+360*STEP for t in body['trades'])
    assert body['data_manifest']['kline_count'] == 360
    assert body['equity_curve'][0]['ts'] == START
    assert all('ema_warmup' not in body[key] for key in ('metrics','data_manifest'))
    assert all('ema_warmup' not in trade for trade in body['trades'])
    assert all(key in body for key in V2_KEYS)

@pytest.mark.parametrize('warm_count', [0,15,50])
@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_short_history_matches_ema_cross_best_effort(client, monkeypatch, name, params, period, minimum, warm_count):
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(period, warm_count)))
    body = post(client, params, name, count=360)
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=period*10,
        target_bars=period*10, actual_bars=warm_count, truncated=False, tenfold_reached=False)
    assert body['assumptions']['ema_warmup'] == f'EMA 预热取 10×最长周期（目标 {period*10} 根，实得 {warm_count} 根）'
    assert body['data_manifest']['kline_count'] == 360
    assert all(t['opened_at'] >= START for t in body['trades'])

@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_unavailable_history_matches_ema_cross_best_effort(client, monkeypatch, name, params, period, minimum):
    good = fetcher(market_frame(period, 0))
    def flaky(*args):
        if args[-2] < START:
            raise RuntimeError('history unavailable')
        return good(*args)
    monkeypatch.setattr(provider, '_fetch_ohlcv', flaky)
    body = post(client, params, name, count=360)
    assert body['raw_report']['ema_warmup']['actual_bars'] == 0
    assert body['raw_report']['ema_warmup']['tenfold_reached'] is False


@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_http_cap_reports_and_fetches_at_most_maximum(client, monkeypatch, name, params, period, minimum):
    monkeypatch.setattr(provider, 'EMA_WARMUP_MULTIPLIER', 2000)
    full = market_frame(period, 20002)
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full, calls))
    body = post(client, params, name, count=360)
    assert calls == [(START, START+360*STEP), (START-20002*STEP, START)]
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=period*2000,
        target_bars=20000, actual_bars=20000, truncated=True, tenfold_reached=False)
    assert '回测已把预热截到 20000 根' in body['assumptions']['ema_warmup']
    assert f'需 {period*2000} 根' in body['assumptions']['ema_warmup']
    assert '无法布防自动信号' in body['assumptions']['ema_warmup']


def test_trend_rsi_long_rsi_retains_taskbook_ema_only_target(client, monkeypatch):
    # Public schema permits this boundary. The task explicitly keeps RSI out of
    # the target; min_bars still prevents premature main-range evaluation.
    params = {'ema_fast':2, 'ema_slow':3, 'rsi_period':100}
    built = provider.TOOL_SPECS['local.backtesting_py.ema_trend_rsi']['build'](params)
    assert built['min_bars'] == 101
    assert built['ema_warmup_requested_bars'] == 30
    assert built['ema_warmup_target_bars'] == 30
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(3, 200), calls))
    body = post(client, params, 'ema_trend_rsi', count=360)
    assert calls == [(START, START+360*STEP), (START-32*STEP, START)]
    assert body['raw_report']['ema_warmup']['actual_bars'] == 30
    assert body['raw_report']['ema_warmup']['tenfold_reached'] is True
    assert all(t['opened_at'] >= START+70*STEP for t in body['trades'])
