"""SHORT-T2-2a：独立标量期望与服务端复核仿真，不打开公开杠杆门。"""
from __future__ import annotations
import copy
import sys
from decimal import Decimal as D
from pathlib import Path
import pandas as pd
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cutie_backtesting_provider as p
from canonical_json import canonical_json

START, STEP = 1800000000, 3600
ENTRY, LIQ, NEXT, END = (START + i * STEP for i in range(1, 5))
TRADE_KEYS = {'seq', 'opened_at', 'closed_at', 'side', 'qty', 'entry_price',
              'exit_price', 'fee', 'slippage', 'pnl'}


def frame():
    return pd.DataFrame({'Open': [100, 100, 100, 100, 110],
                         'High': [101, 101, 101, 111, 111],
                         'Low': [99, 99, 99, 99, 100],
                         'Close': [100, 100, 100, 110, 110], 'Volume': [1]*5},
                        index=pd.to_datetime([START+i*STEP for i in range(5)], unit='s'))


def row(side='long', opened=ENTRY, closed=NEXT, exit_price=100):
    return {'Size': 1 if side == 'long' else -1, 'EntryPrice': 100,
            'ExitPrice': exit_price, 'EntryTime': pd.Timestamp(opened, unit='s'),
            'ExitTime': pd.Timestamp(closed, unit='s')}


def record(opened=ENTRY, bar=LIQ):
    return {'opened_at': opened, 'liquidation_bar_open_time': bar}


def set_bar(df, ts, *, open=100, low=49, high=101, close=50):
    df.loc[pd.Timestamp(ts, unit='s'), ['Open', 'Low', 'High', 'Close']] = [open, low, high, close]


def build(df, rows=None, **kwargs):
    return p._build_result_v2(stats_trades=pd.DataFrame(rows if rows is not None else [row()]),
        equity_scale_dec=D('.5'), fee_bps=D(10), slippage_bps=D(5), initial_capital=D(1000),
        start_at=START, end_at=START+5*STEP, symbol='BTCUSDT',
        market=kwargs.pop('market', 'futures'), timeframe='1h', exchange_id='binance', df=df, **kwargs)


def report(result, df, records, leverage=2, market='futures'):
    return p._build_isolated_risk_report(result['trades'], leverage=leverage, market=market,
                                         df=df, step=STEP, liquidations=records)


def server_recompute(result, df):
    # 独立照 TokenBeep cutie-server/services/strategy_backtest_validation_service.py
    # :1773-1825 写：桶区间、closed_at 不倒退、冻结费用/滑点/pnl、逐笔权益。
    # 曲线规则出自 strategy_backtest_service.py:2282-2312（及后续末点检查）。
    bars = {int(ts.value // 10**9): bar for ts, bar in df.iterrows()}
    equity, previous_close, checkpoints = D(1000), 0, {}
    for seq, trade in enumerate(result['trades'], 1):
        assert set(trade) == TRADE_KEYS
        assert trade['seq'] == seq
        assert trade['closed_at'] >= previous_close
        previous_close = trade['closed_at']
        for label, time_key in [('entry_price', 'opened_at'), ('exit_price', 'closed_at')]:
            ts = trade[time_key]
            bar = bars[ts-ts % STEP]
            assert D(str(bar['Low'])) <= D(trade[label]) <= D(str(bar['High'])), label
        entry, exit, qty = (D(trade[k]) for k in ['entry_price', 'exit_price', 'qty'])
        fee = (entry+exit)*qty*D(10)/D(10000)
        slip = (entry+exit)*qty*D(5)/D(10000)
        gross = (exit-entry if trade['side'] == 'long' else entry-exit)*qty
        assert D(trade['fee']) == fee
        assert D(trade['slippage']) == slip
        assert D(trade['pnl']) == gross-fee-slip
        equity += gross-fee-slip
        checkpoints[trade['closed_at']] = equity
    assert D(result['metrics']['total_return']) == (equity-D(1000))/D(1000)
    curve = result['equity_curve']
    assert curve[0] == {'ts': START, 'equity': '1000'}
    assert all(a['ts'] < b['ts'] for a, b in zip(curve, curve[1:]))
    by_ts = {point['ts']: D(point['equity']) for point in curve}
    for ts, expected in checkpoints.items():
        assert ts in by_ts
        assert by_ts[ts] == expected
    assert curve[-1]['ts'] == result['trades'][-1]['closed_at']
    assert D(curve[-1]['equity']) == equity
    peak, drawdown = D(0), D(0)
    for point in curve:
        amount = D(point['equity'])
        peak = max(peak, amount)
        drawdown = max(drawdown, (peak-amount)/peak)
    assert D(result['metrics']['max_drawdown']) == drawdown


# E=100、qty=.5、fee=10bps、slip=5bps；全部期望是独立手算字面值。
@pytest.mark.parametrize('side,leverage,price,fee,slip,pnl,margin', [
    ('long', 2, '50', '0.075', '0.0375', '-25.1125', '25'),
    ('short', 2, '150', '0.125', '0.0625', '-25.1875', '25'),
    ('long', 5, '80', '0.09', '0.045', '-10.135', '10'),
    ('short', 5, '120', '0.11', '0.055', '-10.165', '10'),
    ('long', 20, '95', '0.0975', '0.04875', '-2.64625', '2.5'),
    ('short', 20, '105', '0.1025', '0.05125', '-2.65375', '2.5'),
])
def test_non_gap_scalar_settlement(side, leverage, price, fee, slip, pnl, margin):
    df = frame()
    set_bar(df, LIQ, low=int(price)-1 if side == 'long' else 99,
            high=int(price)+1 if side == 'short' else 101, close=int(price))
    result = build(df, [row(side)], leverage=leverage, liquidations=[record()])
    assert result['trades'] == [{'seq': 1, 'opened_at': ENTRY, 'closed_at': LIQ, 'side': side,
        'qty': '0.5', 'entry_price': '100', 'exit_price': price, 'fee': fee, 'slippage': slip, 'pnl': pnl}]
    assert report(result, df, [record()], leverage)['isolated_risk']['liquidations'] == [{
        'seq': 1, 'liquidation_price': price, 'fill_price': price, 'liquidation_gap': False,
        'margin_lost': margin, 'loss_beyond_margin': '0'}]
    server_recompute(result, df)


@pytest.mark.parametrize('side,open,low,high,price,fee,slip,pnl', [
    ('long', 40, 39, 41, '50', '0.07', '0.035', '-30.105'),
    ('short', 160, 159, 161, '150', '0.13', '0.065', '-30.195'),
])
def test_gap_scalar_settlement(side, open, low, high, price, fee, slip, pnl):
    df = frame()
    set_bar(df, LIQ, open=open, low=low, high=high, close=open)
    result = build(df, [row(side)], leverage=2, liquidations=[record()])
    trade = result['trades'][0]
    assert [trade[k] for k in ['exit_price', 'fee', 'slippage', 'pnl']] == [str(open), fee, slip, pnl]
    assert D(trade['pnl']) < D('-25')
    assert report(result, df, [record()])['isolated_risk']['liquidations'] == [{
        'seq': 1, 'liquidation_price': price, 'fill_price': str(open), 'liquidation_gap': True,
        'margin_lost': '25', 'loss_beyond_margin': '5'}]
    server_recompute(result, df)


@pytest.mark.parametrize('side,open,price', [('long', 50, '50'), ('short', 150, '150')])
def test_gap_open_equal_boundary(side, open, price):
    df = frame()
    set_bar(df, LIQ, open=open, low=open-1, high=open+1, close=open)
    result = build(df, [row(side)], leverage=2, liquidations=[record()])
    detail = report(result, df, [record()])['isolated_risk']['liquidations'][0]
    assert detail['fill_price'] == price
    assert detail['liquidation_gap'] is True
    assert detail['loss_beyond_margin'] == '0'


def test_entry_bar_liquidation():
    df = frame()
    set_bar(df, ENTRY)
    result = build(df, leverage=2, liquidations=[record(bar=ENTRY)])
    assert result['trades'][0]['closed_at'] == result['trades'][0]['opened_at'] == ENTRY
    assert result['equity_curve'] == [{'ts': START, 'equity': '1000'}, {'ts': ENTRY, 'equity': '974.8875'}]
    server_recompute(result, df)  # 本地仿真，服务端真库往返待服务端批确认。


def test_server_recompute_liquidated_result():
    df = frame()
    set_bar(df, LIQ)
    # 原库次根 NEXT 的 [99,111] 不容纳爆仓价 50，延迟时间必被区间复核抓住。
    result = build(df, leverage=2, liquidations=[record()])
    server_recompute(result, df)


def test_next_entry_after_liquidation():
    df = frame()
    set_bar(df, LIQ)
    result = build(df, [row(), row(opened=NEXT, closed=END, exit_price=110)],
                   leverage=2, liquidations=[record()])
    assert [t['seq'] for t in result['trades']] == [1, 2]
    assert result['equity_curve'] == [{'ts': START, 'equity': '1000'},
        {'ts': LIQ, 'equity': '974.8875'}, {'ts': END, 'equity': '979.73'}]
    assert result['metrics'] == {'total_return': '-0.02027', 'max_drawdown': '0.0251125', 'trade_count': 2}
    server_recompute(result, df)


def test_report_two_liquidations_and_server_recompute():
    df = frame()
    set_bar(df, LIQ)
    set_bar(df, END, open=160, low=159, high=161, close=160)
    records = [record(opened=NEXT, bar=END), record()]  # 输入记录逆序，报告必须对齐 seq。
    result = build(df, [row(), row('short', opened=NEXT, closed=END)], leverage=2, liquidations=records)
    assert report(result, df, records) == {'isolated_risk': {
        'leverage': 2, 'liquidation_count': 2, 'liquidated_margin_total': '50',
        'liquidation_gap_count': 1, 'loss_beyond_margin_total': '5', 'liquidations': [
            {'seq': 1, 'liquidation_price': '50', 'fill_price': '50', 'liquidation_gap': False,
             'margin_lost': '25', 'loss_beyond_margin': '0'},
            {'seq': 2, 'liquidation_price': '150', 'fill_price': '160', 'liquidation_gap': True,
             'margin_lost': '25', 'loss_beyond_margin': '5'}]}}
    assert [d['seq'] for d in report(result, df, records)['isolated_risk']['liquidations']] == [t['seq'] for t in result['trades']]
    assert result['equity_curve'][-1] == {'ts': END, 'equity': '944.6925'}
    assert result['metrics'] == {'total_return': '-0.0553075', 'max_drawdown': '0.0553075', 'trade_count': 2}
    server_recompute(result, df)


@pytest.mark.parametrize('case,records,error', [
    ('untouched', [record()], 'outside'), ('unknown', [record(opened=ENTRY+1)], 'exactly one trade'),
    ('earlier', [record(bar=START)], 'precedes'), ('duplicate', [record(), record()], 'duplicate'),
    ('missing_bar', [record(bar=END+STEP)], 'exist exactly once'),
    ('unaligned', [record(bar=LIQ+1)], 'unaligned'),
    ('extra_key', [dict(record(), reason='liquidation')], 'record keys'),
])
def test_fail_closed(case, records, error):
    df = frame()
    if case != 'untouched':
        set_bar(df, LIQ)
    with pytest.raises(ValueError, match=error):
        build(df, leverage=2, liquidations=records)


def test_reordering_fails_closed():
    df = frame()
    set_bar(df, LIQ)
    with pytest.raises(ValueError, match='trade order'):
        build(df, [row(), row(opened=LIQ, closed=END)], leverage=2, liquidations=[record(opened=LIQ)])


def test_duplicate_entry_identity_fails_closed():
    df = frame()
    set_bar(df, LIQ)
    with pytest.raises(ValueError, match='exactly one trade'):
        build(df, [row(), row(closed=END)], leverage=2, liquidations=[record()])


def test_report_requires_settled_trades():
    df = frame()
    set_bar(df, LIQ)
    with pytest.raises(ValueError, match='settled result'):
        report(build(df), df, [record()])


def test_settlement_and_report_do_not_mutate_inputs():
    df = frame()
    set_bar(df, LIQ)
    trades = p._build_result_v2_trades(pd.DataFrame([row()]), D('.5'), D(10), D(5))
    records = [record()]
    old_trades, old_records, old_df = copy.deepcopy(trades), copy.deepcopy(records), df.copy(deep=True)
    settled = p._settle_isolated_liquidations(trades, leverage=2, df=df, step=STEP,
        liquidations=records, fee_bps=D(10), slippage_bps=D(5))
    p._build_isolated_risk_report(settled, leverage=2, market='futures', df=df, step=STEP, liquidations=records)
    assert trades == old_trades and records == old_records
    pd.testing.assert_frame_equal(df, old_df)


def test_no_liquidations_futures_report():
    df = frame()
    assert report(build(df), df, []) == {'isolated_risk': {'leverage': 2, 'liquidation_count': 0,
        'liquidated_margin_total': '0', 'liquidation_gap_count': 0, 'loss_beyond_margin_total': '0', 'liquidations': []}}


@pytest.mark.parametrize('market,leverage', [('spot', 1), ('spot', 5), ('futures', 1)])
@pytest.mark.parametrize('records', [None, []])
def test_inactive_report_and_assumptions_add_no_keys(market, leverage, records):
    df = frame()
    original_report, original_assumptions = {'existing': 'report'}, {'existing': 'assumptions'}
    assert canonical_json({**original_report, **report(build(df), df, records, leverage, market)}) == canonical_json(original_report)
    assert canonical_json({**original_assumptions, **p._build_isolated_margin_assumptions(
        leverage=leverage, market=market, stop_loss_pct=50)}) == canonical_json(original_assumptions)


@pytest.mark.parametrize('leverage,stop,expected', [
    (2, None, False), (2, 0, False), (2, '49.999', False), (2, 50, True),
    (2, 51, True), (5, 20, True), (20, 5, True),
])
def test_assumptions_fixed_stop_boundary(leverage, stop, expected):
    assert p._build_isolated_margin_assumptions(leverage=leverage, market='futures', stop_loss_pct=stop) == {
        'isolated_margin': {'leverage': leverage, 'mmr': '0', 'funding_rate_included': False,
            'fees_in_liquidation_price': False,
            'liquidation_price_formula': 'long: E*(1-1/L); short: E*(1+1/L)',
            'gap_fill': '跳空按开盘价成交、result.v2 按冻结公式可超保证金、逐仓封顶见 raw_report',
            'stop_beyond_liquidation': expected}}


# cfc42a2 开工前直接采集的规范 JSON，没有改动任何既有 golden/fixture。
BASELINE_JSON = '{"data_manifest":{"checksum":"b65ca6dd8dc19dbc35c79566f8320b5240cec25edb3848413223e91859ba7cc2","checksum_algo":"sha256","end_at":1800018000,"kline_count":5,"market":"futures","source":"ccxt:binance","start_at":1800000000,"symbol":"BTCUSDT","timeframe":"1h"},"equity_curve":[{"equity":"1000","ts":1800000000},{"equity":"999.925","ts":1800007200},{"equity":"999.925","ts":1800010800},{"equity":"1004.8425","ts":1800014400}],"metrics":{"max_drawdown":"0.000075","total_return":"0.0048425","trade_count":1},"schema_version":"cutie.backtest_result.v2","trades":[{"closed_at":1800014400,"entry_price":"100","exit_price":"110","fee":"0.105","opened_at":1800003600,"pnl":"4.8425","qty":"0.5","seq":1,"side":"long","slippage":"0.0525"}]}'


@pytest.mark.parametrize('records', [None, []])
@pytest.mark.parametrize('leverage', [1, 5])
def test_none_and_empty_liquidations_match_prechange_bytes(records, leverage):
    df, rows = frame(), [row(closed=END, exit_price=110)]
    assert canonical_json(build(df, rows)) == BASELINE_JSON
    result = build(df, rows, leverage=leverage, liquidations=records)
    assert canonical_json(result) == BASELINE_JSON
    assert set(result) == {'schema_version', 'trades', 'equity_curve', 'metrics', 'data_manifest'}


def test_public_leverage_gate_futures_open_spot_closed(monkeypatch):
    from fastapi.testclient import TestClient
    from test_leverage_params import request
    assert p._single_leverage_rejection(5, 'futures') is None
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('spot leverage reached market data'))
    response = TestClient(p.app).post('/cutie/backtest', json=request({'leverage': 5})).json()
    assert response['error_type'] == 'INVALID_PARAMS'
    assert response['error_message'] == 'leverage above 1 requires futures market'
