"""10B: hand-calculated next-open fills; no generated expected values."""
import sys
from pathlib import Path
from decimal import Decimal as D
import json
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_position_sizing import risk_quantity, SizingRejected, POSITION_SIZE_KEYS
from test_leverage_params import request
from test_risk_overlay_compatibility import fingerprint, fixture_cases, enumerate_mixin_cases

PARAMS = dict(position_size_risk_pct=1, stop_loss_pct=2, risk_layer_enabled=True,
              position_size_qty_step=.5)


def frame(opens):
    return pd.DataFrame(dict(Open=opens, Close=opens, High=[v+.1 for v in opens],
                            Low=[v-.1 for v in opens], Volume=1),
                        index=pd.date_range('2026-01-01', periods=len(opens), freq='h'))


def manual(params, *, second=False, side='long'):
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(params)
        _initial_capital = 10000
        def init(self):
            self._risk_init()
            self.states = []
        def next(self):
            bar = len(self.data)-1
            if self.position:
                if self._risk_check_exit():
                    return
                if self._risk.get('risk_layer_enabled'):
                    self.states.append(self._risk_state)
                if second and bar == 3:
                    self.position.close()
                    return
            if not self.position and bar in ((1, 5) if second else (1,)):
                self._risk_buy() if side == 'long' else self._risk_sell()
    return Manual


def run(params=None, opens=None, *, second=False, side='long', fee=0, slip=0):
    params = {**PARAMS, **(params or {})}
    params = {key:value for key,value in params.items() if value is not None}
    cls = manual(params, second=second, side=side)
    scale = D(str(params['position_size_qty_step']))
    cls._sizing_scale = cls._isolated_equity_scale = scale
    cls._isolated_initial_capital = D(10000)
    cls._isolated_fee_bps = D(fee)
    cls._isolated_slippage_bps = D(slip)
    data = frame(opens or [100]*8)
    return Backtest(data, cls, cash=float(D(10000)/scale), commission=fee/10000,
                    exclusive_orders=True, finalize_trades=True,
                    **p._leverage_backtest_kwargs(params.get('leverage', 1))).run()


@pytest.mark.parametrize('step,expected', [('.5', '50'), ('3', '48')])
def test_hand_calculated_quantity(step, expected):
    assert risk_quantity(capital=D(10000), risk_pct=D('.01'), fill_price=D(100),
                         initial_stop=D(98), qty_step=D(step)) == D(expected)
    result = run(dict(position_size_qty_step=float(step)))
    trade = result['_trades'].iloc[0]
    assert D(str(trade.Size)) * D(step) == D(expected)
    assert trade.EntryBar == 2 and trade.EntryPrice == 100
    state = result['_strategy'].states[0]
    assert state.initial_stop == D(98) and state.initial_distance == D(2)
    assert state is result['_strategy']._sizing_states[result['_strategy']._risk_trade]


@pytest.mark.parametrize('side,stop', [('long', '117.6'), ('short', '122.4')])
def test_gap_uses_actual_fill_not_signal(side, stop):
    result = run(opens=[100,100,120,120,120,120,120,120], side=side)
    # 100 / (120 * 2%) = 41.666... -> floor to .5 = 41.5, not signal-price 50.
    fill = result['_strategy']._sizing_report['fills'][0]
    assert D(fill['qty']) == D('41.5') and D(fill['fill_price']) == D(120)
    assert D(fill['initial_stop']) == D(stop)
    assert abs(result['_trades'].iloc[0].Size)*.5 == 41.5


@pytest.mark.parametrize('compound,second_base,second_qty', [(False,'10000','50'), (True,'10500','52.5')])
def test_compound_two_trades(compound, second_base, second_qty):
    result = run(dict(compound=compound), [100,100,100,110,110,100,100,100], second=True)
    fills = result['_strategy']._sizing_report['fills']
    assert len(result['_trades']) == len(fills) == 2
    assert D(fills[0]['qty']) == D(50)
    assert D(fills[1]['capital_base']) == D(second_base)
    assert D(fills[1]['qty']) == D(second_qty)


@pytest.mark.parametrize('compound,base,qty', [(False,'10000','50'), (True,'10447.5','52')])
def test_compound_net_of_entry_exit_fee_and_slip(compound, base, qty):
    # 50*(110-100) - 50*(100+110)*(.001 fee+.004 slip) = 447.5.
    result = run(dict(compound=compound), [100,100,100,110,110,100,100,100], second=True, fee=10, slip=40)
    fills = result['_strategy']._sizing_report['fills']
    assert D(fills[1]['capital_base']) == D(base)
    assert D(fills[1]['qty']) == D(qty)


@pytest.mark.parametrize('leverage,margin', [(1,'5000'),(5,'1000')])
def test_leverage_only_changes_margin(leverage, margin):
    result = run(dict(leverage=leverage))
    fill = result['_strategy']._sizing_report['fills'][0]
    assert D(fill['qty']) == D(50) and D(fill['margin']) == D(margin)


@pytest.mark.parametrize('stop,step,reason', [(None,'1','missing_initial_stop'),
    (D('-1'),'1','invalid_initial_stop'),(D(100),'1','non_positive_stop_distance'),
    (D(98),'51','quantity_below_step')])
def test_four_quantity_rejections(stop, step, reason):
    with pytest.raises(SizingRejected, match=reason):
        risk_quantity(capital=D(10000), risk_pct=D('.01'), fill_price=D(100),
                      initial_stop=stop, qty_step=D(step))


def test_fifth_rejection_cannot_afford_fee():
    # qty=100, margin=10000, entry fee=10 -> 10010 > 10000.
    result = run(dict(position_size_risk_pct=2), fee=10)
    assert result['_trades'].empty
    assert result['_strategy']._sizing_report['rejections'][0]['reason'] == 'insufficient_funds_after_fees'


@pytest.mark.parametrize('leverage,risk', [(1,2),(5,10)])
def test_fee_reserved_outside_leveraged_margin(leverage, risk):
    result = run(dict(leverage=leverage, position_size_risk_pct=risk), fee=10)
    assert result['_trades'].empty
    assert result['_strategy']._sizing_report['rejections'][0]['reason'] == 'insufficient_funds_after_fees'


def test_invalid_atr_stop_rejected_at_fill():
    result = run(dict(atr_stop_multiplier=100, risk_atr_period=2, stop_loss_pct=None),
                 [100,100,1,1,1,1,1,1])
    assert result['_trades'].empty
    assert result['_strategy']._sizing_report['rejections'][0]['reason'] == 'invalid_initial_stop'


@pytest.mark.parametrize('tool', sorted(p.POSITION_SIZING_UNWIRED_TOOLS))
@pytest.mark.parametrize('key,value', [('position_size_risk_pct',1),('compound',False),('position_size_qty_step',.1)])
def test_unwired_runner_rejected_before_fetch(monkeypatch, tool, key, value):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('data fetched'))
    result = TestClient(p.app).post('/cutie/backtest', json=request({key:value}, name=tool.split('.')[-1])).json()
    assert result['error_type'] == 'INVALID_PARAMS'
    assert 'not wired' in result['raw_report']['position_sizing']['rejections'][0]['reason']


@pytest.mark.parametrize('params', [dict(position_size_risk_pct=1), dict(position_size_risk_pct=0,stop_loss_pct=2),
    dict(position_size_risk_pct=1,position_size_pct=20,stop_loss_pct=2),
    dict(position_size_risk_pct=1,position_size_notional=200,stop_loss_pct=2),
    dict(compound=False), dict(position_size_risk_pct=1,stop_loss_pct=2,compound=0),
    dict(position_size_risk_pct=1,stop_loss_pct=2,position_size_qty_step=0)])
def test_invalid_sizing_rejected_before_fetch_with_reason(monkeypatch, params):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('data fetched'))
    result = TestClient(p.app).post('/cutie/backtest', json=request(params)).json()
    assert result['error_type'] == 'INVALID_PARAMS'
    assert result['raw_report']['position_sizing']['rejections']


@pytest.mark.parametrize('tool', sorted(p.POSITION_SIZING_PENDING_TOOLS))
@pytest.mark.parametrize('key,value', [('position_size_risk_pct',1),('compound',False),('position_size_qty_step',.1)])
def test_pending_template_rejected_before_fetch(monkeypatch, tool, key, value):
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **k: pytest.fail('data fetched'))
    result = TestClient(p.app).post('/cutie/backtest', json=request({key:value}, name=tool.split('.')[-1])).json()
    assert result['error_type'] == 'INVALID_PARAMS'
    assert result['raw_report']['position_sizing']['rejections'] == [
        {'reason': 'position sizing is not wired to this template yet'}]


# INTEG-C: single-position templates merged after the 10-B start b42210b. The list may only shrink.
FROZEN_PENDING_INTEG_C = frozenset('local.backtesting_py.' + n for n in (
    'opening_range_breakout asia_range_breakout calendar_schedule red_streak_rsi vwap_reversion '
    'bullish_engulfing hammer_pin_bar morning_star three_white_soldiers bullish_doji_reversal inside_bar_breakout '
    'double_bottom inverse_head_shoulders macd_bullish_divergence rsi_bullish_divergence chan_3buy '
    'fibonacci_retracement us_open_momentum cme_weekend_gap').split())


# 集成 D：做空形态一 FROZEN_PENDING_SHORT_PAT1、做空形态二 FROZEN_PENDING_SHORT_PAT2 合并为一份；只比 INTEG-C 多 4 个做空 id。
FROZEN_PENDING_SHORT_PAT12 = frozenset({
    "local.backtesting_py.opening_range_breakout",
    "local.backtesting_py.asia_range_breakout",
    "local.backtesting_py.calendar_schedule",
    "local.backtesting_py.red_streak_rsi",
    "local.backtesting_py.vwap_reversion",
    "local.backtesting_py.bullish_engulfing",
    "local.backtesting_py.hammer_pin_bar",
    "local.backtesting_py.morning_star",
    "local.backtesting_py.three_white_soldiers",
    "local.backtesting_py.bullish_doji_reversal",
    "local.backtesting_py.inside_bar_breakout",
    "local.backtesting_py.double_bottom",
    "local.backtesting_py.inverse_head_shoulders",
    "local.backtesting_py.macd_bullish_divergence",
    "local.backtesting_py.rsi_bullish_divergence",
    "local.backtesting_py.chan_3buy",
    "local.backtesting_py.fibonacci_retracement",
    "local.backtesting_py.us_open_momentum",
    "local.backtesting_py.cme_weekend_gap",
    "local.backtesting_py.macd_bearish_divergence",
    "local.backtesting_py.rsi_bearish_divergence",
    "local.backtesting_py.double_top",
    "local.backtesting_py.head_shoulders",
})


def test_pending_list_only_shrinks_and_is_disjoint_from_runner_list():
    """Pending tools may only shrink; tools with sizing keys must leave this list.

    At 10-B2 close-out POSITION_SIZING_PENDING_TOOLS must be empty; both frozen
    sets expire together. Replace the subset assertion with
    `assert not p.POSITION_SIZING_PENDING_TOOLS`.
    """
    assert len(FROZEN_PENDING_SHORT_PAT12) == 23
    assert FROZEN_PENDING_SHORT_PAT12 - FROZEN_PENDING_INTEG_C == {
        "local.backtesting_py.macd_bearish_divergence",
        "local.backtesting_py.rsi_bearish_divergence",
        "local.backtesting_py.double_top",
        "local.backtesting_py.head_shoulders",
    }
    assert p.POSITION_SIZING_PENDING_TOOLS <= FROZEN_PENDING_SHORT_PAT12
    assert p.POSITION_SIZING_PENDING_TOOLS.isdisjoint(p.POSITION_SIZING_UNWIRED_TOOLS)
    for tool in p.POSITION_SIZING_PENDING_TOOLS:
        assert POSITION_SIZE_KEYS.isdisjoint(p.TOOL_SPECS[tool]['param_schema_properties']), tool
        assert issubclass(p.TOOL_SPECS[tool]['build']({})['strategy'], p._FixedRiskMixin), tool


def test_catalog_and_builders_cover_actual_mixins():
    for tool, spec in p.TOOL_SPECS.items():
        wired = tool not in p.POSITION_SIZING_UNWIRED_TOOLS | p.POSITION_SIZING_PENDING_TOOLS
        assert POSITION_SIZE_KEYS <= set(spec['param_schema_properties']) if wired else POSITION_SIZE_KEYS.isdisjoint(spec['param_schema_properties'])
        if wired:
            # 10-B2a: template-stop tools size against their own frozen stop (divergence rejects user stops).
            params = ({k: v for k, v in PARAMS.items() if k != 'stop_loss_pct'}
                      if tool in p.POSITION_SIZING_TEMPLATE_STOP_TOOLS else PARAMS)
            cls = spec['build'](params)['strategy']
            assert issubclass(cls, p._FixedRiskMixin)
            assert cls._risk['compound'] is False
    assert p.POSITION_SIZING_UNWIRED_TOOLS == {tool for tool,spec in p.TOOL_SPECS.items()
        if spec.get('runner') in (p.TURTLE_RUNNER,p.SCALE_IN_OUT_RUNNER,'kernel_v3',p.ROTATION_RUNNER)}


@pytest.mark.parametrize('risk_case', ['pct','notional'])
def test_omitted_new_keys_match_immutable_bytes(risk_case):
    expected = fixture_cases()[f'ema_cross/{risk_case}/0']
    assert fingerprint('ema_cross',risk_case,False) == expected
    assert not p._parse_fixed_risk_params({'position_size_pct':20}).get('position_sizing_enabled')


@pytest.mark.parametrize('compound,base,qty', [(False,'10000','50'),(True,'10500','52.5')])
def test_existing_percentage_opt_in_compound(compound,base,qty):
    result = run(dict(position_size_risk_pct=None, position_size_pct=50, compound=compound),
                 [100,100,100,110,110,100,100,100], second=True)
    fills = result['_strategy']._sizing_report['fills']
    assert D(fills[1]['capital_base']) == D(base) and D(fills[1]['qty']) == D(qty)


def test_fixed_notional_remains_fixed_in_new_mode():
    result = run(dict(position_size_risk_pct=None, position_size_notional=5000, compound=True),
                 [100,100,100,110,110,100,100,100], second=True)
    assert [D(f['qty']) for f in result['_strategy']._sizing_report['fills']] == [D(50),D(50)]


def test_default_compound_is_off_and_no_final_bar_entry():
    result = run(second=True, opens=[100,100,100,110,110,100])
    assert result['_strategy']._sizing_report['compound'] is False
    assert len(result['_trades']) == 1
    assert result['_strategy']._sizing_report['rejections'][-1]['reason'] == 'no_next_open'


def test_fill_candle_close_does_not_enter_compound_base():
    cls = manual({**PARAMS,'compound':True})
    cls._sizing_scale = cls._isolated_equity_scale = D('.5')
    cls._isolated_initial_capital = D(10000)
    data = frame([100]*8)
    data.iloc[2,data.columns.get_loc('Close')] = 105
    data.iloc[2,data.columns.get_loc('High')] = 106
    result = Backtest(data, cls, cash=20000,finalize_trades=True).run()
    fill = result['_strategy']._sizing_report['fills'][0]
    assert D(fill['capital_base']) == D(10000) and D(fill['qty']) == D(50)


@pytest.mark.parametrize('params,fee,expected_qty,reason', [
    (PARAMS,0,'50',None), ({**PARAMS,'position_size_risk_pct':2},10,None,'insufficient_funds_after_fees'),
    ({**PARAMS,'position_size_qty_step':51},0,None,'quantity_below_step')])
def test_http_registered_route_quantity_and_report(monkeypatch,tmp_path,params,fee,expected_qty,reason):
    def build(values, **kwargs):
        return dict(strategy=manual(values), min_bars=2, executed_name='EMA Cross')
    monkeypatch.setitem(p.TOOL_SPECS['local.backtesting_py.ema_cross'],'build',build)
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**k: frame([100]*8))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a,**k: pd.DataFrame())
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k: None)
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    body = request(params)
    body['backtest']['end_at'] = 1767254400
    body['backtest']['fee_bps'] = str(fee)
    result = TestClient(p.app).post('/cutie/backtest',json=body).json()
    assert result['result_status'] == 'success', result
    report = result['raw_report']['position_sizing']
    if reason:
        assert result['trades'] == [] and report['rejections'][0]['reason'] == reason
    else:
        assert D(result['trades'][0]['qty']) == D(expected_qty)
        assert D(report['fills'][0]['qty']) == D(expected_qty)


def test_real_ema_builder_http_next_open_quantity(monkeypatch,tmp_path):
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**k: frame([100,100,100,99,99,102,100,100,100,100]))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a,**k: pd.DataFrame())
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k: None)
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    body = request({**PARAMS,'ema_fast':2,'ema_slow':3})
    body['backtest']['end_at'] = 1767261600
    result = TestClient(p.app).post('/cutie/backtest',json=body).json()
    assert result['result_status'] == 'success', result
    assert len(result['trades']) == 1
    assert D(result['trades'][0]['qty']) == D(50)
    assert D(result['raw_report']['position_sizing']['fills'][0]['fill_price']) == D(100)


def test_frozen_R_targets_share_fill_stop():
    result = run(dict(take_profit_r=2))
    state = result['_strategy'].states[0]
    assert state.initial_stop == D(98) and state.initial_distance == D(2) and state.take_price == D(104)


def test_trailing_state_uses_fill_timestamp_and_keeps_initial_R():
    result = run(dict(trailing_stop_pct=2, stop_loss_pct=None), opens=[100]*8)
    state = result['_strategy'].states[0]
    assert state.stop_state.entry_at == int(pd.Timestamp('2026-01-01 02:00:00').value)
    assert state.initial_stop == D(98) and state.initial_distance == D(2)


@pytest.mark.parametrize('params', [dict(position_size_risk_pct=True),dict(compound=None),
    dict(position_size_qty_step=1e-12),dict(position_size_qty_step=True),dict(position_size_qty_step=float('nan'))])
def test_new_field_invalid_types_and_precision(params):
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params({**PARAMS,**params})


def test_atr_unavailable_rejected_before_fill_and_recorded():
    result = run(dict(atr_stop_multiplier=2,risk_atr_period=14,stop_loss_pct=None))
    assert result['_trades'].empty
    assert result['_strategy']._sizing_report['rejections'][0]['reason'] == 'invalid_initial_stop'
