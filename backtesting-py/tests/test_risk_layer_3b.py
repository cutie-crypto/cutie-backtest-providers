"""3b contracts: strict parameters, closed-bar execution and actual integer fills."""
from decimal import Decimal as D
import pytest
from test_risk_layer import p, bars_frame, http_request

KEYS = ['trailing_stop_pct', 'breakeven_stop', 'max_holding_bars',
        *[f'tp{n}_{s}' for n in (1, 2, 3) for s in ('r', 'close_pct')]]
BASE = dict(risk_layer_enabled=True, stop_loss_pct=10)
LEVELS = dict(trailing_stop_pct=20, tp1_r=1, tp1_close_pct=30, tp2_r=2, tp2_close_pct=30,
              tp3_r=3, tp3_close_pct=40)

def valid(key, value):
    params = dict(BASE, **{key: value})
    if key.startswith('tp') and value:
        n = int(key[2])
        params.update(trailing_stop_pct=20)
        for i in range(1, n+1):
            params[f'tp{i}_r'] = i
            params[f'tp{i}_close_pct'] = 10
        params[key] = value
        if key.endswith('_r'):
            for i in range(1, n):
                params[f'tp{i}_r'] = value * i / n
    return params

@pytest.mark.parametrize('key', KEYS)
def test_parameter_defaults_normalize_away(key):
    default = False if key == 'breakeven_stop' else 0
    assert p._parse_fixed_risk_params({key: default}) == {}
    assert p._parse_fixed_risk_params(dict(BASE, **{key: default})) == p._parse_fixed_risk_params(BASE)

@pytest.mark.parametrize('key', KEYS)
@pytest.mark.parametrize('bad', [None, '1', float('nan'), float('inf'), -float('inf'), 10**400, [], {}])
def test_parameter_bad_types_and_nonfinite(key, bad):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **{key: bad}))

@pytest.mark.parametrize('key', [k for k in KEYS if k != 'breakeven_stop'])
@pytest.mark.parametrize('bad', [True, False, -1])
def test_numeric_bool_and_negative_rejected(key, bad):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **{key: bad}))

@pytest.mark.parametrize('key,value', [('trailing_stop_pct', 99.999), ('breakeven_stop', True),
    ('max_holding_bars', 1000000), *[(k,100) for k in KEYS if k.startswith('tp')]])
def test_parameter_upper_bound_accepted(key, value):
    assert p._validate_params_against_schema({key: value}, p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES) is None
    if key.endswith('_close_pct'):
        value -= 10 * (int(key[2]) - 1)
    assert p._parse_fixed_risk_params(valid(key, value))[key] == value

@pytest.mark.parametrize('key,value', [('trailing_stop_pct',100), ('trailing_stop_pct',101),
    ('breakeven_stop',1), ('breakeven_stop',0), ('max_holding_bars',1000001),
    ('max_holding_bars',1.0), ('max_holding_bars',0.0),
    *[(k,101) for k in KEYS if k.startswith('tp')]])
def test_parameter_upper_bounds_and_strict_integer(key, value):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(valid(key, value))

@pytest.mark.parametrize('key', KEYS)
def test_nondefault_requires_explicit_risk_gate(key):
    params = valid(key, True if key == 'breakeven_stop' else 1)
    params['risk_layer_enabled'] = False
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(params)

@pytest.mark.parametrize('extra', [dict(tp1_r=1), dict(tp1_close_pct=50),
    dict(tp2_r=2,tp2_close_pct=100), dict(tp1_r=2,tp1_close_pct=50,tp2_r=1,tp2_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=60,tp2_r=2,tp2_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=50),
    dict(tp1_r=1,tp1_close_pct=100,take_profit_pct=5),
    dict(tp1_r=1,tp1_close_pct=100,take_profit_r=2),
    dict(tp1_r=1,tp1_close_pct=50,tp3_r=3,tp3_close_pct=50)])
def test_cross_field_levels_rejected(extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(BASE, **extra))

@pytest.mark.parametrize('extra', [dict(breakeven_stop=True), dict(take_profit_r=1),
                                  dict(tp1_r=1,tp1_close_pct=100)])
def test_r_based_features_require_initial_stop(extra):
    with pytest.raises(ValueError, match='INVALID_PARAMS:'):
        p._parse_fixed_risk_params(dict(risk_layer_enabled=True, **extra))
    assert p._parse_fixed_risk_params(dict(risk_layer_enabled=True, trailing_stop_pct=5, **extra))

from backtesting import Backtest, Strategy
from backtesting.backtesting import Trade
from strategy_risk_overlay import initial_risk_state, advance_risk_state, risk_assumptions

FLAT = [100,101,99,100]

def mirror(rows):
    return [[200-o,200-lo,200-hi,200-c] for o,hi,lo,c in rows]

def run(rows, extra, side='long', size=11, repeat=False, signal=False, finalize=True, commission=0):
    rows = mirror(rows) if side == 'short' else rows
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(dict(BASE, **extra))
        def init(self):
            self._risk_init()
            self.snapshots = []
            self.signal_orders = 0
            self.close_orders = []
        def next(self):
            i = len(self.data)-1
            if i == 1:
                self._risk_buy() if side == 'long' else self._risk_sell()
            if not self.position:
                return
            exited = self._risk_check_exit()
            self.snapshots.append((i, self._risk_state, abs(self.trades[-1].size), self._risk_exit_reason))
            self.close_orders.append((i, len([o for o in self.orders if o.parent_trade is self.trades[-1]])))
            if repeat and exited:
                assert self._risk_check_exit()
                assert len([o for o in self.orders if o.parent_trade is self.trades[-1]]) == 1
            if exited:
                return
            if signal and i >= 3:
                self.signal_orders += 1
                self.position.close()
        def _risk_entry_size(self):
            return size
    return Backtest(bars_frame(rows), Manual, cash=100000, commission=commission,
                    exclusive_orders=True, finalize_trades=finalize).run()

def snap(stats, bar):
    return next(s for i,s,_,_ in stats['_strategy'].snapshots if i == bar)

@pytest.mark.parametrize('side', ['long','short'])
def test_trailing_prior_stop_next_bar_entry_extreme_and_ratchet(side):
    rows = [FLAT, FLAT, [100,150,99,100], [100,120,100,115],
            [115,119,113,115], [110,111,107,109], [104,105,103,104], FLAT]
    result = run(rows, dict(trailing_stop_pct=10), side)
    assert list(result['_trades'].ExitBar) == [6]
    s2,s3,s4 = [snap(result,i).stop_state for i in (2,3,4)]
    assert s2.extreme == 100  # entry fill bar's extreme is excluded
    assert s3.extreme == (120 if side == 'long' else 80)
    assert s3.effective_stop == (108 if side == 'long' else 88)
    assert s4.extreme == s3.extreme and s4.effective_stop == s3.effective_stop
    assert result['_trades'].iloc[0].ExitPrice == (104 if side == 'long' else 96)

@pytest.mark.parametrize('side', ['long','short'])
def test_breakeven_wick_exact_close_and_following_bar(side):
    rows = [FLAT]*3 + [[100,110,99,109], [109,111,99,110], [101,102,99,101], [103,104,102,103], FLAT]
    result = run(rows, dict(breakeven_stop=True), side)
    assert not snap(result,3).stop_state.breakeven_active
    assert snap(result,4).stop_state.breakeven_active
    assert snap(result,4).stop_state.effective_stop == 100
    assert list(result['_trades'].ExitBar) == [6]

@pytest.mark.parametrize('side', ['long','short'])
def test_breakeven_never_loosens_tighter_trailing_and_uses_initial_R(side):
    rows = [FLAT]*3 + [[100,109,99,105], [107,111,107,109], [109,112,109,110], [110,111,109,110]]
    result = run(rows, dict(trailing_stop_pct=3, breakeven_stop=True), side)
    assert not snap(result,4).stop_state.breakeven_active  # moving-stop distance is NOT 1R
    assert snap(result,5).stop_state.breakeven_active
    assert snap(result,5).stop_state.effective_stop != 100
    assert snap(result,5).stop_state.effective_stop == (D('108.64') if side == 'long' else D('90.64'))

@pytest.mark.parametrize('n', [1,2,3,5])
@pytest.mark.parametrize('side', ['long','short'])
def test_holding_fill_bar_is_one_next_open_fill(n, side):
    result = run([FLAT]*10, dict(max_holding_bars=n), side)
    assert list(result['_trades'].EntryBar) == [2]
    assert list(result['_trades'].ExitBar) == [2+n]
    snapshots = result['_strategy'].snapshots
    assert all(reason is None for i,_,_,reason in snapshots if i < 1+n)
    assert snapshots[-1][0] == 1+n and snapshots[-1][3] == 'time_expiry'

@pytest.mark.parametrize('side', ['long','short'])
def test_levels_original_units_floor_actual_fills_and_frozen_state(side):
    rows = [FLAT]*3 + [[100,111,99,110], [111,121,100,120], [121,131,110,130], [131,132,130,131], FLAT]
    result = run(rows, LEVELS, side)
    assert list(abs(result['_trades'].Size)) == [3,3,5]  # remaining-based would give [3,1,...]
    assert list(result['_trades'].ExitBar) == [4,5,6]
    assert all(s.initial_distance == 10 and s.entry_price == 100 and s.original_units == 11
               for _,s,_,_ in result['_strategy'].snapshots)
    assert snap(result,4).stop_state.extreme == (121 if side == 'long' else 79)
    assert snap(result,5).stop_state.extreme == (131 if side == 'long' else 69)

@pytest.mark.parametrize('side', ['long','short'])
def test_same_bar_multiple_levels_merge_one_order(side):
    rows = [FLAT]*3 + [[100,121,99,120], [120,121,119,120], FLAT]
    result = run(rows, LEVELS, side, repeat=True)
    assert abs(result['_trades'].iloc[0].Size) == 6
    assert result['_strategy'].close_orders[1] == (3,1)
    assert snap(result,3).reached_levels == 2

@pytest.mark.parametrize('side', ['long','short'])
def test_floor_zero_marks_level_without_minimum_one_order(side):
    params = dict(trailing_stop_pct=20,tp1_r=1,tp1_close_pct=10,tp2_r=2,tp2_close_pct=20,
                  tp3_r=3,tp3_close_pct=70)
    rows = [FLAT]*3 + [[100,111,99,110], [110,121,100,120], [120,131,110,130], [130,131,129,130], FLAT]
    result = run(rows, params, side, size=3)
    assert snap(result,3).reached_levels == 1 and snap(result,4).reached_levels == 2
    assert result['_strategy'].close_orders[1:3] == [(3,0),(4,0)]
    assert list(abs(result['_trades'].Size)) == [3] and list(result['_trades'].ExitBar) == [6]

@pytest.mark.parametrize('side', ['long','short'])
def test_submitted_level_never_retriggers_pending_or_after_fill(side):
    rows = [FLAT]*3 + [[100,111,99,110], [110,111,100,110], [110,111,100,110], FLAT]
    result = run(rows, LEVELS, side, repeat=True)
    assert list(abs(result['_trades'].Size)) == [3,8]
    assert result['_strategy'].close_orders[1:4] == [(3,1),(4,0),(5,0)]

@pytest.mark.parametrize('side', ['long','short'])
def test_partial_exit_does_not_reset_extreme_or_initial_R(side):
    rows = [FLAT]*3 + [[100,120,99,115], [115,119,105,115], [115,117,107,110], [104,105,103,104], FLAT]
    params = dict(trailing_stop_pct=10,tp1_r=2,tp1_close_pct=30)
    result = run(rows, params, side)
    assert snap(result,4).initial_distance == 10
    assert snap(result,4).stop_state.extreme == (120 if side == 'long' else 80)
    assert list(result['_trades'].ExitBar) == [4,5]
    assert list(abs(result['_trades'].Size)) == [3,8]

@pytest.mark.parametrize('side', ['long','short'])
def test_partial_exit_keeps_holding_clock(side):
    rows = [FLAT]*3 + [[100,111,99,110], [111,112,100,110], [112,113,101,112], FLAT]
    result = run(rows, dict(LEVELS, max_holding_bars=3), side)
    assert list(result['_trades'].ExitBar) == [4,5]
    assert result['_strategy'].snapshots[-1][3] == 'time_expiry'

@pytest.mark.parametrize('mode,expected', [('stop','stop_loss'), ('expiry','time_expiry')])
@pytest.mark.parametrize('side', ['long','short'])
def test_priority_and_template_signal_suppression(mode, expected, side):
    row = [100,121,89 if mode == 'stop' else 99,100]
    result = run([FLAT]*3+[row,FLAT,FLAT], dict(LEVELS,max_holding_bars=2), side, signal=True)
    assert result['_strategy'].snapshots[-1][3] == expected
    assert result['_strategy'].signal_orders == 0
    assert list(abs(result['_trades'].Size)) == [11]
    assert list(result['_trades'].ExitBar) == [4]

@pytest.mark.parametrize('side', ['long','short'])
def test_partial_order_suppresses_template_signal(side):
    result = run([FLAT]*3+[[100,111,99,110],[110,111,100,110],FLAT], LEVELS, side, signal=True)
    # The signal is suppressed on the partial-trigger bar and may run after its fill.
    assert result['_strategy'].signal_orders == 1
    assert list(result['_trades'].ExitBar) == [4,5]

@pytest.mark.parametrize('side', ['long','short'])
def test_final_bar_settlement_is_separate_from_next_open(side):
    result = run([FLAT]*4, dict(max_holding_bars=2), side)
    assert list(result['_trades'].ExitBar) == [3]  # engine-finalize exception; no bar 4 exists
    assert result['_strategy'].snapshots[-1][3] == 'time_expiry'

@pytest.mark.parametrize('side', ['long','short'])
def test_trailing_only_seeds_positive_initial_stop_and_R(side):
    risk = p._parse_fixed_risk_params(dict(risk_layer_enabled=True,trailing_stop_pct=5,take_profit_r=2,
                                         breakeven_stop=True))
    state = initial_risk_state(risk=risk,entry_price=100,direction=side)
    assert state.initial_stop == (95 if side == 'long' else 105)
    assert state.initial_distance == 5 and state.take_price == (110 if side == 'long' else 90)


def test_adjacent_bars_advance_without_overlap_and_final_period():
    result = run([FLAT]*7, dict(trailing_stop_pct=20))
    for i in (3,4,5,6):
        state = snap(result,i).stop_state
        assert state.last_bar_close == int(bars_frame([FLAT]*7).index[i].value) + 3600*10**9-1

@pytest.mark.parametrize('feature,key', [({'trailing_stop_pct':5},'trailing_basis'),
    ({'breakeven_stop':True},'breakeven_trigger'), ({'max_holding_bars':2},'holding_bar_count_from'),
    ({'tp1_r':1,'tp1_close_pct':100},'take_profit_levels_basis')])
def test_assumptions_feature_specific(feature,key):
    base = risk_assumptions(BASE)['risk_layer']
    layer = risk_assumptions(dict(BASE,**feature))['risk_layer']
    assert key not in base and key in layer
    assert all(layer[k] == value for k,value in base.items() if k != 'same_bar_priority')

@pytest.mark.parametrize('size,pct,expected', [(3,10,0),(7,50,3),(11,30,3),(11,60,6)])
def test_trade_close_rounding_boundary_uses_precomputed_integer_units(size,pct,expected,monkeypatch):
    calls=[]
    original=Trade.close
    def observe(self,portion=1):
        calls.append((abs(self.size),portion))
        return original(self,portion)
    monkeypatch.setattr(Trade,'close',observe)
    result=run([FLAT]*3+[[100,111,99,110],[110,111,100,110],FLAT],
               dict(trailing_stop_pct=20,tp1_r=1,tp1_close_pct=pct),size=size)
    level_calls=[(s,portion) for s,portion in calls if portion != 1]
    assert level_calls == ([(size,expected/size)] if expected else [])
    if expected:
        assert abs(result['_trades'].iloc[0].Size) == expected
    assert sum(abs(result['_trades'].Size)) == size

@pytest.mark.parametrize('side', ['long','short'])
def test_level_targets_keep_initial_R_after_trailing_tightens(side):
    rows=[FLAT]*3+[[100,111,99,110],[110,115,110,113],[113,121,113,120],
                   [120,122,119,121],FLAT]
    result=run(rows,dict(trailing_stop_pct=10,tp1_r=1,tp1_close_pct=30,tp2_r=2,tp2_close_pct=70),side)
    assert snap(result,4).reached_levels == 1
    assert snap(result,4).initial_distance == 10
    assert list(result['_trades'].ExitBar) == [4,6]
    assert list(abs(result['_trades'].Size)) == [3,8]

@pytest.mark.parametrize('extra', [dict(trailing_stop_pct=1e-320),
    dict(trailing_stop_pct=10,tp1_r=1e-320,tp1_close_pct=100)])
def test_unrepresentable_dynamic_levels_fail_closed(extra):
    with pytest.raises(ValueError,match='INVALID_PARAMS:'):
        initial_risk_state(risk=p._parse_fixed_risk_params(dict(risk_layer_enabled=True,**extra)),
                           entry_price=100,direction='long')

from test_risk_overlay_compatibility import enumerate_mixin_cases, frame, PARAMS, tool_name
from fastapi.testclient import TestClient

FEATURES = {
    'trailing': dict(stop_loss_pct=50,trailing_stop_pct=.1),
    'breakeven': dict(stop_loss_pct=.25,breakeven_stop=True),
    'holding': dict(max_holding_bars=1),
    'levels': dict(stop_loss_pct=20,tp1_r=.01,tp1_close_pct=50,tp2_r=.02,tp2_close_pct=50),
}

FEATURE_CASES = [(name, feature) for name in enumerate_mixin_cases() for feature, values in FEATURES.items()
                 if set(values) <= set(p.TOOL_SPECS['local.backtesting_py.'+tool_name(name)]['param_schema_properties'])]

@pytest.mark.parametrize('name,feature', FEATURE_CASES)
def test_every_runtime_mixin_feature_real_backtest(name,feature,monkeypatch):
    if name in (*p._CANDLE_TOOL_NAMES.values(), 'double_bottom', 'inverse_head_shoulders'):
        build = p.TOOL_SPECS['local.backtesting_py.'+name]['build']
        params = dict(risk_layer_enabled=True, **FEATURES[feature])
        if feature != 'holding':
            # Template-owned exits deliberately reject the generic overlay modes.
            with pytest.raises(ValueError, match='INVALID_PARAMS:.*mutually exclusive'):
                build(params)
            return
        if name in ('bullish_engulfing', 'hammer_pin_bar'):
            from test_9t1_engulf_pin import compatibility_frame
        elif name in ('double_bottom', 'inverse_head_shoulders'):
            from test_9t3_patterns import compatibility_frame
        else:
            from test_9t2_patterns import compatibility_frame
        cls = build(params)['strategy']
        reasons = []
        original = cls._record_holding_expiry
        def record(self, fact):
            reasons.append('time_expiry')
            original(self, fact)
        cls._record_holding_expiry = record
        trades = Backtest(compatibility_frame(name), cls, cash=100000,
                          exclusive_orders=True, finalize_trades=True).run()['_trades']
        assert len(trades) == 1 and reasons == ['time_expiry']
        assert trades.iloc[0].ExitBar - trades.iloc[0].EntryBar == 1
        return
    observations=[]
    original=p._FixedRiskMixin._risk_layer_check_exit
    def observe(self):
        exited=original(self)
        observations.append((self._risk_state,self._risk_exit_reason,exited))
        return exited
    monkeypatch.setattr(p._FixedRiskMixin,'_risk_layer_check_exit',observe)
    params=dict(PARAMS.get(name,{}),risk_layer_enabled=True,**FEATURES[feature])
    if name == 'opening_range_breakout':
        params['flatten_at'] = '23:00'  # 1h test grid
    if name == 'calendar_schedule':
        params.update(time_entry_at='02:00', time_max_holding_minutes=60*24, calendar_stop_enabled=False)
    if name == 'ema_rsi_pullback':
        params['rsi_exit'] = 100
    data=frame()
    if feature == 'breakeven':
        # Narrow wicks let close-based 1R activate before initial-stop touches.
        data['Open']=data.Close
        data['High']=data.Close+.01
        data['Low']=data.Close-.01
        if name == 'red_streak_rsi':
            # Equality intentionally is NOT red. Provide four real red bars
            # through the trough, followed by rebound bars for 1R activation.
            data.loc[data.index[43:47], 'Open'] = data.Close.iloc[43:47] + .005
    cls=p.TOOL_SPECS['local.backtesting_py.'+tool_name(name)]['build'](params)['strategy']
    stats=Backtest(data,cls,cash=100000,exclusive_orders=True,finalize_trades=True).run()
    assert not stats['_trades'].empty and observations
    assert any(exited or (feature == 'breakeven' and s.stop_state.breakeven_active)
               for s,_,exited in observations)
    if feature in ('trailing','breakeven'):
        assert any(s.stop_state is not None and s.stop_state.last_bar_close is not None for s,_,_ in observations)
    elif feature == 'holding':
        assert any(reason == 'time_expiry' for _,reason,_ in observations)
    else:
        assert any(reason == 'take_profit_levels' for _,reason,_ in observations)

@pytest.mark.parametrize('template', ['rsi_scale_in_out','grid','dca'])
@pytest.mark.parametrize('key', [key for key in KEYS if key != 'max_holding_bars'])
def test_ledger_templates_reject_each_new_key_even_default(template,key,monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('ledger rejection must precede fetch')
    monkeypatch.setattr(p,'_fetch_ohlcv',forbidden)
    request=http_request({})
    request['backtest'].update(provider_tool_id='local.backtesting_py.'+template,
                               provider_params={key:False if key == 'breakeven_stop' else 0})
    body=TestClient(p.app).post('/cutie/backtest',json=request).json()
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS',body
    assert key in body['error_message']


def test_http_all_3b_parameters_success(monkeypatch,tmp_path):
    data=frame()
    def fetch(exchange,market,symbol,timeframe,start,end):
        lo,hi=p.pd.to_datetime(start,unit='s'),p.pd.to_datetime(end,unit='s')
        return data.loc[(data.index>=lo)&(data.index<hi)].copy()
    monkeypatch.setattr(p,'_fetch_ohlcv',fetch)
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(Backtest,'plot',lambda self,**kwargs:None)
    params=dict(risk_layer_enabled=True,stop_loss_pct=2,trailing_stop_pct=2,breakeven_stop=True,
                max_holding_bars=5,tp1_r=.25,tp1_close_pct=30,tp2_r=.5,tp2_close_pct=30,
                tp3_r=.75,tp3_close_pct=40)
    response=TestClient(p.app).post('/cutie/backtest',json=http_request(params))
    assert response.status_code == 200
    body=response.json()
    assert body['result_status'] == 'success' and body['trades'],body
    assert all(k in body['assumptions']['risk_layer'] for k in
               ('trailing_basis','breakeven_trigger','holding_bar_count_from','take_profit_levels_basis'))


def test_http_invalid_combination_before_market_fetch(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('invalid combination fetched market data')
    monkeypatch.setattr(p,'_fetch_ohlcv',forbidden)
    params=dict(BASE,tp1_r=1,tp1_close_pct=60,tp2_r=2,tp2_close_pct=50)
    response=TestClient(p.app).post('/cutie/backtest',json=http_request(params))
    body=response.json()
    assert body['result_status'] == 'failed' and body['error_type'] == 'INVALID_PARAMS'

@pytest.mark.parametrize('key',KEYS)
def test_new_schema_consumed_only_by_runtime_mixins(key):
    assert p._FIXED_RISK_PARAM_SCHEMA_PROPERTIES[key]['default'] == (False if key=='breakeven_stop' else 0)
    for tool_id, spec in p.TOOL_SPECS.items():
        runner = spec.get('runner')
        included = (
            runner not in ('kernel_v3', 'scale_in_out_ledger', 'turtle_group') or
            (runner in ('scale_in_out_ledger', p.TURTLE_RUNNER) and key == 'max_holding_bars'))
        if tool_id in ('local.backtesting_py.opening_range_breakout', 'local.backtesting_py.asia_range_breakout', 'local.backtesting_py.calendar_schedule'):
            included = key == 'max_holding_bars'
        assert (key in spec['param_schema_properties']) == included

@pytest.mark.parametrize('side',['long','short'])
def test_partial_fee_accounting_preserves_quantity_and_equity(side):
    rows=[FLAT]*3+[[100,111,99,110],[111,121,100,120],[121,131,110,130],[131,132,130,131],FLAT]
    stats=run(rows,LEVELS,side,commission=.001)
    trades=stats['_trades']
    assert list(abs(trades.Size)) == [3,3,5]
    assert stats['Equity Final [$]'] == pytest.approx(100000+trades.PnL.sum())
    assert trades.Commission.sum() == pytest.approx(sum(abs(t.Size)*(t.EntryPrice+t.ExitPrice)*.001
                                                       for t in trades.itertuples()))
