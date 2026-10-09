"""10-B2a: risk sizing for MACD/RSI bullish divergence, Fibonacci retracement and VWAP reversion.

Risk distance = |actual next-open fill - the template's own initial stop frozen at the signal|.
Expected quantities below are hand arithmetic (capital 10000, fee/slippage 0), never generated.
"""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import _10b2a_cases as c
import cutie_backtesting_provider as p
from strategy_position_sizing import POSITION_SIZE_KEYS

TOOLS = tuple(c.CASES)
RISK = dict(position_size_risk_pct=1, position_size_qty_step=0.001)


def fills(body):
    return body['raw_report']['position_sizing']['fills']


@pytest.mark.parametrize('name,label', [(n, v) for n in c.GOLDEN_VARIANTS for v in c.GOLDEN_VARIANTS[n]])
def test_omitted_sizing_keys_match_c72c4a1_bytes(name, label, monkeypatch, tmp_path):
    golden = (c.GOLDEN_DIR / f'{name}.{label}.json').read_text()
    body = c.post(monkeypatch, tmp_path, name, c.GOLDEN_VARIANTS[name][label])
    assert body['result_status'] == 'success'
    assert c.canonical(body) + '\n' == golden


# name -> (fill, frozen stop, qty per trade). Hand arithmetic, 1% of 10000 = 100 risked:
#  divergence: L2 low 78 * 0.999 = 77.922; fill bar24 open 100; 100/22.078 = 4.5293.. -> 4.529
#  fibonacci:  wave stop 104.17572; fill bar9 open 109; 100/4.82428 = 20.7285.. -> 20.728
#  vwap:       signal close 94 * 0.98 = 92.12; fill 95; 100/2.88 = 34.7222.. -> 34.722
#              second: signal close 190 * 0.98 = 186.2; fill 190; 100/3.8 = 26.3157.. -> 26.315
HAND = {
    'macd_bullish_divergence': [('100.0', '77.922', '4.529')],
    'rsi_bullish_divergence': [('100.0', '77.922', '4.529')],
    'fibonacci_retracement': [('109.0', '104.1757200', '20.728')],
    'vwap_reversion': [('95.0', '92.120', '34.722'), ('190.0', '186.200', '26.315')],
}


@pytest.mark.parametrize('name', TOOLS)
def test_risk_quantity_hand_calculated(name, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, RISK)
    assert body['result_status'] == 'success', body
    got = [(f['fill_price'], f['initial_stop'], f['qty']) for f in fills(body)]
    assert got == HAND[name]
    assert [float(t['qty']) for t in body['trades']] == [float(q) for _, _, q in HAND[name]]
    assert [float(t['entry_price']) for t in body['trades']] == [float(f) for f, _, _ in HAND[name]]
    assert body['raw_report']['position_sizing']['rejections'] == []
    assert body['assumptions']['position_sizing']['initial_stop'] == 'template_frozen_signal_stop_actual_fill_distance'


@pytest.mark.parametrize('name,params,expected', [
    # User stop frozen at the signal close 108: 108 * 0.97 = 104.76; fill 109;
    # 100 / 4.24 = 23.5849.. -> 23.584 (a fill-based stop 109*0.97 would give 30.581).
    ('fibonacci_retracement', dict(stop_loss_pct=3), ('109.0', '104.760', '23.584')),
    # 94 * 0.97 = 91.18; fill 95; 100 / 3.82 = 26.1780.. -> 26.178
    ('vwap_reversion', dict(stop_loss_pct=3), ('95.0', '91.180', '26.178')),
])
def test_user_stop_is_frozen_at_signal_close(name, params, expected, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, {**RISK, **params})
    assert body['result_status'] == 'success', body
    first = fills(body)[0]
    assert (first['fill_price'], first['initial_stop'], first['qty']) == expected


def doubled(name):
    first = c.CASES[name][0]()
    second = first.copy()
    second.index = second.index + (first.index[-1] - first.index[0]) + (first.index[1] - first.index[0])
    return pd.concat([first, second])


# Second fill uses pre-fill equity = 10000 + first trade pnl (fee 0):
#  divergence: 10000 + 4.529*(110-100) = 10045.29; 100.4529/22.078 = 4.5499.. -> 4.549
#  fibonacci:  10000 + 20.728*(107-109) = 9958.544; 99.58544/4.82428 = 20.6425.. -> 20.642
#  vwap:       10000 + 34.722*(200-95) = 13645.81; 136.4581/3.8 = 35.9100.. -> 35.910
COMPOUND = {
    'macd_bullish_divergence': ('doubled', ['10000.0000', '10045.2900'], ['4.529', '4.549']),
    'rsi_bullish_divergence': ('doubled', ['10000.0000', '10045.2900'], ['4.529', '4.549']),
    'fibonacci_retracement': ('doubled', ['10000.0000', '9958.5440'], ['20.728', '20.642']),
    'vwap_reversion': ('single', ['10000.0000', '13645.8100'], ['34.722', '35.910']),
}


@pytest.mark.parametrize('name', TOOLS)
def test_compound_uses_pre_fill_equity(name, monkeypatch, tmp_path):
    shape, bases, qtys = COMPOUND[name]
    data = doubled(name) if shape == 'doubled' else None
    body = c.post(monkeypatch, tmp_path, name, {**RISK, 'compound': True}, data=data)
    assert body['result_status'] == 'success', body
    assert [f['capital_base'] for f in fills(body)] == bases
    assert [f['qty'] for f in fills(body)] == qtys
    simple = c.post(monkeypatch, tmp_path, name, RISK, data=data)
    assert [f['qty'] for f in fills(simple)][1] != qtys[1]


def gapped(name):
    data = c.CASES[name][0]()
    if name.endswith('divergence'):
        data.iloc[24, 0] = 77.9   # below 77.922
    elif name == 'fibonacci_retracement':
        data = data.iloc[:10].copy()
        data.loc[data.index[9], ['Open', 'Low', 'Close']] = [100, 99, 100]   # below 104.17572
    else:
        data.loc[data.index[3], 'Open'] = 92.   # below 92.12
    return data


@pytest.mark.parametrize('name', TOOLS)
def test_gap_past_frozen_stop_still_skips_before_sizing(name, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, RISK, data=gapped(name))
    assert body['result_status'] == 'success', body
    report = body['raw_report']
    skipped = (report['divergence'] if name.endswith('divergence') else report[name])['skipped_entries']
    assert skipped[0]['reason'] in ('entry_open_at_or_below_frozen_stop', 'frozen_stop_wrong_side_of_entry_open')
    sized = report['position_sizing']
    if name == 'vwap_reversion':   # the second day's signal is unaffected
        assert [f['fill_price'] for f in sized['fills']] == ['190.0']
    else:
        assert body['trades'] == [] and sized['fills'] == []
    assert sized['rejections'] == []


@pytest.mark.parametrize('name', TOOLS)
def test_quantity_below_step_fails_closed(name, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, dict(position_size_risk_pct=0.0001, position_size_qty_step=1))
    assert body['result_status'] == 'success', body
    assert body['trades'] == []
    reasons = [r['reason'] for r in body['raw_report']['position_sizing']['rejections']]
    assert reasons and set(reasons) == {'quantity_below_step'}


@pytest.mark.parametrize('name,params,message', [
    ('vwap_reversion', dict(take_profit_pct=3), 'missing_initial_stop'),
    ('fibonacci_retracement', dict(take_profit_pct=3), 'missing_initial_stop'),
    ('macd_bullish_divergence', dict(stop_loss_pct=3), 'divergence frozen exits conflict with risk exit overrides'),
])
def test_rejected_before_fetch_without_frozen_stop(name, params, message, monkeypatch, tmp_path):
    body = c.post(monkeypatch, tmp_path, name, {**RISK, **params},
                  fetch=lambda *a: pytest.fail('rejected request fetched data'))
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'
    assert message in body['error_message']


def test_four_templates_left_pending_list_and_pending_list_empty(monkeypatch):
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a: pytest.fail('pending tool fetched data'))
    wired = {'local.backtesting_py.' + n for n in TOOLS}
    assert wired.isdisjoint(p.POSITION_SIZING_PENDING_TOOLS)
    # 10-B2b 又接入 6 个（做空 5 个 + chan_3buy），10-B2c 再接 8 个（K 线六 + 双底 + 头肩底），模板止损名单只比本批多这 14 个。
    assert p.POSITION_SIZING_TEMPLATE_STOP_TOOLS - wired == {'local.backtesting_py.' + n for n in (
        'macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell', 'chan_3buy',
        'bullish_engulfing', 'hammer_pin_bar', 'morning_star', 'three_white_soldiers', 'bullish_doji_reversal',
        'inside_bar_breakout', 'double_bottom', 'inverse_head_shoulders',
        # 10-B2d：ORB、亚洲区间、日历定时、red_streak_rsi、美股开盘按模板冻结止损定仓（CME 走共享路径不在内）。
        'opening_range_breakout', 'asia_range_breakout', 'calendar_schedule', 'red_streak_rsi', 'us_open_momentum')}
    for tool in wired:
        assert POSITION_SIZE_KEYS <= set(p.TOOL_SPECS[tool]['param_schema_properties']), tool
    # 集成 E：10-B2a 移出 4 个后 19，SHORT-PAT-3 新增 chan_3sell 进待接名单，合并后 20；10-B2b 移出 6 个后 14；10-B2c 移出 8 个后 6
    # 10-B2d 移出最后 6 个，名单清空。
    assert not p.POSITION_SIZING_PENDING_TOOLS
