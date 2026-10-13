"""Six EMA families keep 10x EMA budgets and reject incomplete recursive seeds."""
from __future__ import annotations
import math
import pandas as pd
import pytest
import cutie_backtesting_provider as provider
from test_ema_backtest_warmup import START, STEP, client, fetcher, frame, post, V2_KEYS
from test_warmup_explicit import request
from warmup_rules import wilder_bars

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
def test_builder_rejects_request_above_maximum(monkeypatch, name, params, period, minimum):
    monkeypatch.setattr(provider, 'EMA_WARMUP_MULTIPLIER', 2000)
    with pytest.raises(ValueError, match='INVALID_PARAMS:required_warmup exceeds 20000'):
        provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)


def market_frame(period, warm_count):
    warm = [200.]*max(0, warm_count-period)+[100.]*min(warm_count, period)
    main = [100+20*math.sin(i/12)+i*.02 for i in range(360)]
    return frame(warm, main)

@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_sufficient_history_seeds_indicators_without_warmup_signals(client, monkeypatch, name, params, period, minimum):
    target = period*10
    required = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)['required_warmup']
    full = market_frame(period, required+2)
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
    assert calls == [(START, START+360*STEP), (START-(required+2)*STEP, START)]
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=target,
        target_bars=target, actual_bars=required, truncated=False, tenfold_reached=True)
    assert evaluated and min(evaluated) >= pd.to_datetime(START, unit='s')
    # Independent recurrence proves actual indicators use the tenfold prefix,
    # not just the report. Earlier unavailable/unused history is excluded.
    closes = list(full['Close'].iloc[-(required+360):])
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
def test_short_history_is_rejected(client, monkeypatch, name, params, period, minimum, warm_count):
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(period, warm_count)))
    body = request(client, params=params, name=name, count=360)
    required = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)['required_warmup']
    assert body['error_type'] == 'WARMUP_INSUFFICIENT'
    assert body['limitations']['missing_bars'] == required-warm_count


@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_unavailable_history_is_fetch_failure(client, monkeypatch, name, params, period, minimum):
    good = fetcher(market_frame(period, 0))
    def flaky(*args):
        if args[-2] < START:
            raise RuntimeError('history unavailable')
        return good(*args)
    monkeypatch.setattr(provider, '_fetch_ohlcv', flaky)
    body = request(client, params=params, name=name, count=360)
    assert body['error_type'] == 'WARMUP_FETCH_FAILED'
    assert 'actual_bars' not in body['limitations']


@pytest.mark.parametrize('name,params,period,minimum', CASES, ids=IDS)
def test_http_over_limit_rejected_without_fetch(client, monkeypatch, name, params, period, minimum):
    monkeypatch.setattr(provider, 'EMA_WARMUP_MULTIPLIER', 2000)
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(period,0), calls))
    body = request(client, params=params, name=name, count=360)
    assert body['error_type'] == 'INVALID_PARAMS'
    assert not calls


def test_trend_rsi_long_rsi_uses_own_alpha_budget(client, monkeypatch):
    params = {'ema_fast':2, 'ema_slow':3, 'rsi_period':100}
    built = provider.TOOL_SPECS['local.backtesting_py.ema_trend_rsi']['build'](params)
    required = wilder_bars(100)+1
    assert built['min_bars'] == 101 and built['ema_warmup_target_bars'] == 30
    assert built['required_warmup'] == required
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(3,required+2),calls))
    body = post(client,params,'ema_trend_rsi',count=360)
    assert calls == [(START,START+360*STEP),(START-(required+2)*STEP,START)]
    assert body['raw_report']['ema_warmup']['actual_bars'] == required
    assert body['raw_report']['ema_warmup']['tenfold_reached'] is True


DEFAULTS = [
    ('ema_triple_alignment', {'ema_short':20, 'ema_mid':60, 'ema_long':120}, 1200),
    ('ema_trend_rsi', {'ema_fast':50, 'ema_slow':200, 'rsi_period':14}, 2000),
    ('ema_rsi_pullback', {'ema_period':200, 'rsi_period':14}, 2000),
    ('bias_reversion', {'ema_period':20, 'bias_entry_pct':3}, 200),
    ('ema_pullback', {'ema_fast':20, 'ema_slow':60}, 600),
]

@pytest.mark.parametrize('name,params,target', DEFAULTS, ids=[d[0] for d in DEFAULTS])
def test_default_parameters_fetch_depth_unchanged(client, monkeypatch, name, params, target):
    # params mirror each builder's defaults; min_bars <= tenfold target, so the
    # shared max(target, min_bars) floor is a no-op and depth stays at target.
    built = provider.TOOL_SPECS['local.backtesting_py.'+name]['build']({})
    assert built['ema_warmup_target_bars'] == target
    assert built['min_bars'] <= target
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(market_frame(built['min_bars'], target+2), calls))
    body = post(client, params, name, count=360)
    assert calls == [(START, START+360*STEP), (START-(target+2)*STEP, START)]
    assert body['raw_report']['ema_warmup'] == dict(requested_bars=target,
        target_bars=target, actual_bars=target, truncated=False, tenfold_reached=True)


@pytest.mark.parametrize('warm_count', [15,30,50,101])
def test_trend_rsi_short_rsi_seed_is_rejected_even_if_ema_target_met(client, monkeypatch, warm_count):
    params = {'ema_fast':2,'ema_slow':3,'rsi_period':100}
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(market_frame(3,warm_count)))
    body = request(client,params=params,name='ema_trend_rsi',count=360)
    assert body['error_type'] == 'WARMUP_INSUFFICIENT'
    assert body['limitations']['required_bars'] == wilder_bars(100)+1
