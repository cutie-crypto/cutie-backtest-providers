"""SHORT-PAT-3 Chan third sell: hand-counted mirror geometry, causal confirmation,
futures-only short execution, isolated risk and chan_3buy byte invariance."""
from decimal import Decimal
from pathlib import Path
import json
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_chan import ChanRecognizer
from strategy_time_layer import TimeContext
from test_time_layer import DEFAULTS as TIME_OFF
from _exit_kinds_strip import without_exit_kinds

TOOL = 'local.backtesting_py.chan_3sell'
# Hand count (High=c+1, Low=c-1, Low14=93, Low15=92), no inclusion before bar15:
# bottom1=129 (conf2), top3=161 (conf4), bottom5=134 (conf6), top7=151 (conf8)
# => strokes 1-3 [129,161], 3-5 [134,161], 5-7 [134,151]: ZG=min(161,161,151)=151,
# ZD=max(129,134,134)=134, confirmed at 8. Departure down 7-9 low 109 < ZD.
# Pullback up 9-11 high 126 < ZD; top11 confirmed only by right bar12 => signal12.
# Stop 126*1.001=126.126; fill next open13=115; target 115-2*(126.126-115)=92.748.
# Bar14 Low93 > 92.748; bar15 Low92 <= 92.748 => take_profit decision15, exit open16=99.
CLOSES = [140,130,145,160,145,135,142,150,130,110,120,125,118,115,110,100,99,98]


def frame():
    f = pd.DataFrame(dict(Open=CLOSES, High=[c+1 for c in CLOSES],
        Low=[c-1 for c in CLOSES], Close=CLOSES, Volume=100),
        index=pd.date_range('2026-01-01', periods=len(CLOSES), freq='h'))
    f.loc[f.index[14], 'Low'] = 93
    f.loc[f.index[15], 'Low'] = 92
    return f.astype(float)


def recognize(data=None, mode='new'):
    data = frame() if data is None else data
    r = ChanRecognizer(bi_mode=mode, direction='short')
    signals = [r.push(float(row.High), float(row.Low), i) for i,row in enumerate(data.itertuples())]
    return signals, r


def run(data=None, params=None):
    data = frame() if data is None else data
    cls = p.TOOL_SPECS[TOOL]['build'](params or {})['strategy']
    if cls._time_config:
        cls._time_context = TimeContext.build(cls._time_config, '1h', data.index)
    return Backtest(data, cls, cash=10000, exclusive_orders=True, finalize_trades=True).run()


def point(kind, index, price):
    return dict(kind=kind, merged_index=index, raw_index=index, price=price, confirmed_at=index+1)


def short_recognizer(points):
    r = ChanRecognizer(direction='short')
    return r, [r._point(point(*x)) for x in points]


CENTER = [('bottom',1,129),('top',3,161),('bottom',5,134),('top',7,151)]


def test_hand_counted_center_and_confirmed_pullback_top():
    signals,r = recognize()
    assert [i for i,s in enumerate(signals) if s] == [12]
    assert (signals[12].zd, signals[12].center) == (134, 0)
    assert signals[12].stop == pytest.approx(126.126)
    center = r.report['centers'][0]
    assert (center['zg'], center['zd'], center['confirmed_at']) == (151,134,8)
    assert [(b['start']['raw_index'], b['end']['raw_index'], b['confirmed_at'], b['direction'])
            for b in r.report['strokes'][:5]] == [(1,3,4,'up'),(3,5,6,'down'),(5,7,8,'up'),(7,9,10,'down'),(9,11,12,'up')]
    fact = r.report['third_sells'][0]
    assert (fact['zd'], fact['pullback_high'], fact['confirmed_at'], fact['status']) == (134,126,12,'signal')
    assert fact['departure']['low'] == 109 and fact['departure']['end']['kind'] == 'bottom'
    assert 'third_buys' not in r.report


def test_top_fractal_cannot_trigger_before_right_confirmation_bar_closes():
    # Top11 (High126) is a fractal only after bar12 (High119 < 126) closes.
    signals,r = recognize()
    assert signals[11] is None
    assert [x['confirmed_at'] for x in r.report['fractals'] if x['raw_index'] == 11] == [12]
    assert recognize(frame().iloc[:12])[0] == [None]*12
    # Bar12 replaced by a higher bar: top11 never forms, no sell at 11 or 12.
    f = frame()
    f.loc[f.index[12], ['Open','High','Low','Close']] = [128,130,127,129]
    assert recognize(f.iloc[:13])[0] == [None]*13
    # Signal on the last bar never back-fills an entry.
    assert run(frame().iloc[:13])['_trades'].empty


@pytest.mark.parametrize('end', range(3,19))
def test_prefix_and_future_do_not_rewrite_decisions(end):
    f = frame()
    full,_ = recognize(f)
    assert recognize(f.iloc[:end])[0] == full[:end]
    mutated = f.copy()
    for i in range(end,len(f)):
        mutated.iloc[i, mutated.columns.get_indexer(['Open','High','Low','Close'])] = [10+i,11+i,9+i,10+i]
    assert recognize(mutated)[0][:end] == full[:end]


@pytest.mark.parametrize('high,valid', [(134,False),(134.01,False),(133.99,True)])
def test_pullback_high_strictly_below_zd(high,valid):
    r,_ = short_recognizer(CENTER+[('bottom',9,109)])
    signal = r._point(point('top',11,high))
    assert bool(signal) is valid
    assert r.report['third_sells'][0]['status'] == ('signal' if valid else 'invalid_or_consumed')


@pytest.mark.parametrize('low,departs', [(134,False),(133.99,True)])
def test_departure_must_close_strictly_below_zd(low,departs):
    # Down stroke low must leave below ZD134; a stroke ending at ZD is not a departure.
    r,signals = short_recognizer(CENTER+[('bottom',9,low),('top',11,low+0.005)])
    assert bool(signals[-1]) is departs
    assert len(r.report['third_sells']) == int(departs)


def test_up_departure_above_zg_is_not_a_short_setup():
    # Long-side geometry (up leaves above ZG151, down pullback stays above) never sells.
    r,signals = short_recognizer(CENTER+[('bottom',9,140),('top',11,170),('bottom',13,155)])
    assert not any(signals)
    assert not r.report['third_sells']


def test_lower_bottom_replacement_moves_departure():
    r,_ = short_recognizer(CENTER+[('bottom',9,120),('bottom',11,110)])
    assert r.departure['low'] == 110 and r.departure['end']['raw_index'] == 11
    signal = r._point(point('top',13,125))
    assert r.report['third_sells'][0]['departure']['low'] == 110
    assert signal.stop == pytest.approx(125.125)


def test_same_center_has_only_one_sell_opportunity():
    r = ChanRecognizer(direction='short')
    signals = []
    for kind,idx,price in CENTER+[('bottom',9,109),('top',11,126),('bottom',13,100),('top',15,120)]:
        signals.append(r._point(point(kind,idx,price)))
        if idx == 11:
            r.center['end_stroke'] = 99
    assert [i for i,s in enumerate(signals) if s] == [5]
    assert [x['status'] for x in r.report['third_sells']] == ['signal','invalid_or_consumed']
    assert r.report['third_sells'][1]['center'] == 0


def test_next_open_and_actual_fill_frozen_r():
    stats = run()
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist() == [[13,16]]
    assert stats['_trades'].Size.iloc[0] < 0
    report = stats['_strategy'].chan_report
    entry = report['entries'][0]
    assert entry['entry_price'] == 115
    assert entry['frozen_stop'] == pytest.approx(126.126)
    assert entry['frozen_target'] == pytest.approx(92.748)
    assert report['exits'] == [dict(reason='take_profit',decision_bar=15)]
    assert stats['_trades'].ExitPrice.iloc[0] == 99


@pytest.mark.parametrize('opening',[126.126,130])
def test_gap_entry_wrong_side_cancelled(opening):
    f = frame()
    f.loc[f.index[13],['Open','High','Low','Close']] = [opening,opening+1,opening-1,opening]
    stats = run(f)
    assert stats['_trades'].empty
    skipped = stats['_strategy'].chan_report['skipped_entries']
    assert skipped == [dict(reason='entry_open_at_or_above_frozen_stop', signal_bar=12, entry_bar=13,
                            entry_open=opening, frozen_stop=pytest.approx(126.126))]


def test_gap_just_below_stop_still_enters():
    f = frame()
    f.loc[f.index[13],['Open','High','Low','Close']] = [126.12,126.12,114,115]
    assert run(f)['_trades'].EntryBar.tolist() == [13]


def pullback_signal_frame(close14):
    # Pullback top11 High133.99 < ZD134 => stop 133.99*1.001=134.12399; fill115,
    # target 115-2*19.12399=76.75202. Bar14 High134.05 < stop, Low133 > target.
    f = frame()
    f.loc[f.index[11],['Open','High','Low','Close']] = [132,133.99,131,132]
    f.loc[f.index[14],['Open','High','Low','Close']] = [134,134.05,133,close14]
    return f


@pytest.mark.parametrize('stop,time,take,expected', [
    (True,True,True,'stop_loss'),(False,True,True,'time_expiry'),
    (False,False,True,'take_profit'),(False,False,False,'close_above_zd')])
def test_exit_priority(stop,time,take,expected):
    f = frame()
    f.loc[f.index[14],['Open','High','Low','Close']] = [110,130 if stop else 120,80 if take else 100,110]
    if expected == 'close_above_zd':
        f = pullback_signal_frame(134.01)
    params = dict(time_layer_enabled=True,max_holding_bars=2) if time else {}
    stats = run(f,params)
    assert stats['_strategy'].chan_report['exits'][0] == dict(reason=expected,decision_bar=14)


def test_signal_exit_at_next_open_and_equality_does_not_exit():
    stats = run(pullback_signal_frame(134.01))
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist() == [[13,15]]
    stats = run(pullback_signal_frame(134))
    assert all(x['decision_bar'] != 14 for x in stats['_strategy'].chan_report['exits'])


def test_time_gate_drops_opportunity():
    assert run(params=dict(time_layer_enabled=True,time_session_start='00:00',time_session_end='12:00'))['_trades'].empty


def test_old_mode_actual_merged_geometry():
    # Old strokes need 5 independent bars between endpoints; 1->3 etc. are rejected.
    stats = run(params={'bi_mode':'old'})
    assert stats['_trades'].empty
    assert not stats['_strategy'].chan_report['third_sells']


def test_warmup_points_allow_main_range_entry():
    f = frame()
    cls = p.TOOL_SPECS[TOOL]['build']({})['strategy']
    cls._warmup_bars = 10
    cls._warmup_cols = {c:f[c].iloc[:10].to_numpy() for c in p._WARMUP_COLUMNS}
    stats = Backtest(f.iloc[10:],cls,cash=10000,exclusive_orders=True,finalize_trades=True).run()
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist() == [[3,6]]
    assert stats['_strategy'].chan_report['third_sells'][0]['confirmed_at'] == 12


def request(params,data=None,market='futures'):
    data = frame() if data is None else data
    body = dict(backtest=dict(run_id='shortpat3',provider_tool_id=TOOL,provider_params=params,
        symbol='BTCUSDT',market=market,timeframe='1h',
        start_at=int(data.index[0].timestamp()),end_at=int(data.index[-1].timestamp())+3600,
        initial_capital='10000',fee_bps='0',slippage_bps='0'))
    if market is None:
        body['backtest'].pop('market')
    return body


@pytest.mark.parametrize('params', [dict(direction='long'),dict(direction='both'),dict(direction=None),
    dict(bi_mode='bad'),dict(bi_mode=True),dict(stop_loss_pct=3),dict(take_profit_pct=5),
    dict(risk_layer_enabled=True,stop_loss_pct=3),
    dict(risk_layer_enabled=True,atr_stop_multiplier=2,risk_atr_period=5),
    dict(risk_layer_enabled=True,trailing_stop_pct=3),
    dict(risk_layer_enabled=True,trailing_stop_pct=3,tp1_r=1,tp1_close_pct=100),
    dict(filter_layer_enabled=True),dict(position_sizing_enabled=True)])
def test_invalid_before_fetch(params,monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a: pytest.fail('invalid params fetched'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a: pytest.fail('invalid params warmed'))
    response = TestClient(p.app).post('/cutie/backtest',json=request(params)).json()
    assert response['error_type'] == 'INVALID_PARAMS',response


@pytest.mark.parametrize('market', ['spot', None])
@pytest.mark.parametrize('direction', [None, 'short', 'long', 'both'])
def test_futures_only_before_any_fetch(market,direction,monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a: pytest.fail('market gate fetched'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a: pytest.fail('market gate fetched warmup'))
    body = request({} if direction is None else {'direction':direction},market=market)
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type'] == 'INVALID_PARAMS'


# 7-P4：缠论三卖接入单仓入场过滤层（镜像规则与开态手算见 test_7p4_short_filter.py）。
def test_filter_layer_wired_as_short():
    properties = p.TOOL_SPECS[TOOL]['param_schema_properties']
    assert 'filter_layer_enabled' in properties
    assert getattr(p.TOOL_SPECS[TOOL]['build'],'_supports_entry_filters',False)
    cls = p.TOOL_SPECS[TOOL]['build']({'filter_layer_enabled':True,'filter_ema_enabled':True})['strategy']
    assert cls._filter_direction == 'short' and cls._filter_config.ema_enabled
    assert run()['_strategy'].chan_report['entries']


def http_response(params=None,data=None,warm=0,tool=TOOL,market='futures'):
    from unittest.mock import patch
    import tempfile
    data = frame() if data is None else data
    main = data.iloc[warm:].copy()
    body = request(params or {},main,market)
    body['backtest']['provider_tool_id'] = tool
    with tempfile.TemporaryDirectory(prefix='shortpat3-') as reports, \
         patch.object(p,'AUTH_TOKEN',''), patch.object(p,'REPORTS_DIR',Path(reports)), \
         patch.object(p,'_fetch_ohlcv',lambda *a: main.copy()), \
         patch.object(p,'_fetch_template_warmup',lambda *a: data.iloc[:warm].copy()), \
         patch.object(Backtest,'plot',return_value=None):
        result = TestClient(p.app).post('/cutie/backtest',json=body).json()
    assert result['result_status'] == 'success',result
    return result


def test_http_details_only_in_raw_report_and_result_keys_frozen():
    result = http_response()
    assert len(result['trades']) == 1 and result['trades'][0]['side'] == 'short'
    assert set(result['trades'][0]) == {'seq','side','opened_at','closed_at','qty','entry_price','exit_price','fee','slippage','pnl'}
    assert set(result['metrics']) == {'total_return','max_drawdown','trade_count'}
    assert set(result['data_manifest']) == {'source','symbol','market','timeframe','start_at','end_at','kline_count','checksum_algo','checksum'}
    chan = result['raw_report']['chan']
    assert (chan['centers'][0]['zg'], chan['centers'][0]['zd']) == (151,134)
    # CHANSLIM: segments keep direction/high/low/confirmed_at; top11 confirmed by bar12, bottom9 by bar10.
    assert chan['third_sells'][0]['pullback'] == dict(direction='up', high=126, low=109, confirmed_at=12)
    assert chan['third_sells'][0]['departure']['confirmed_at'] == 10
    assert chan['entries'][0]['frozen_target'] == pytest.approx(92.748)
    assert 'chan' not in result['metrics']
    execution = result['assumptions']['chan_execution']
    assert execution['direction'] == 'short_only' and execution['stop'] == 'pullback_high_times_1.001'


@pytest.mark.parametrize('case', ['liquidation','closer_stop','gap_liquidation'])
def test_short_isolated_risk_arbitration_and_accounting(case):
    # Fill115: L20 => liquidation 115*21/20=120.75 inside frozen stop126.126;
    # L10 => liquidation126.5 beyond the closer stop126.126 (intrabar stop wins).
    data = frame()
    if case == 'gap_liquidation':
        data.iloc[14,:4] = [130,131,129,130]
    else:
        data.iloc[14,:4] = [110,130 if case == 'closer_stop' else 125,105,110]
    leverage = 10 if case == 'closer_stop' else 20
    result = http_response(dict(leverage=leverage,position_size_pct=10),data)
    risk = result['raw_report']['isolated_risk']
    trade = result['trades'][0]
    assert trade['side'] == 'short'
    if case == 'closer_stop':
        assert risk['liquidation_count'] == 0
        assert result['raw_report']['chan']['exits'][0] == dict(reason='stop_loss',decision_bar=14)
        assert trade['exit_price'] == '100'
    else:
        assert risk['liquidation_count'] == 1
        assert trade['exit_price'] == ('130' if case == 'gap_liquidation' else '120.75')
        assert risk['liquidations'][0]['liquidation_gap'] is (case == 'gap_liquidation')
    entry,exit,qty = (Decimal(trade[k]) for k in ('entry_price','exit_price','qty'))
    assert entry == 115
    assert Decimal(trade['fee']) == 0 and Decimal(trade['slippage']) == 0
    assert Decimal(trade['pnl']) == (entry-exit)*qty
    assert Decimal(result['equity_curve'][-1]['equity']) == Decimal('10000')+Decimal(trade['pnl'])
    for price_key,time_key in (('entry_price','opened_at'),('exit_price','closed_at')):
        bar = data.loc[pd.Timestamp(trade[time_key],unit='s')]
        assert Decimal(str(bar.Low)) <= Decimal(trade[price_key]) <= Decimal(str(bar.High))
    assert 'funding' in str(result['assumptions']).lower()


@pytest.mark.parametrize('warm', [0,10])
@pytest.mark.parametrize('explicit', [False,True])
def test_branch_head_isolated_off_golden(warm,explicit,monkeypatch):
    monkeypatch.setenv('CUTIE_BACKTEST_CHAN_DEBUG', '1')  # CHANSLIM: frozen bytes predate the slim chan evidence
    from canonical_json import canonical_json
    import hashlib
    fixture = json.loads((Path(__file__).parent/'fixtures/shortpat3_isolated_off.json').read_text())
    body = http_response({'leverage':1,**TIME_OFF} if explicit else {},warm=warm)
    assert body['trades']
    body = without_exit_kinds(body)
    assert 'isolated_risk' not in body['raw_report']
    assert 'isolated_margin' not in body['assumptions']
    v2 = {key:body[key] for key in ('schema_version','metrics','trades','equity_curve','data_manifest')}
    actual = {'v2':hashlib.sha256(canonical_json(v2).encode()).hexdigest()}
    for key in ('assumptions','raw_report'):
        actual[key] = hashlib.sha256(json.dumps(body[key],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert actual == fixture['cases']['chan_3sell/'+str(warm)]


def test_catalog_count_and_default_params():
    # main c72c4a1 has 53 tools; SHORT-PAT-3 adds chan_3sell => 54.
    assert len(p.TOOL_SPECS) == 67  # SHORT-PAT-4 +3, SHORT-PAT-5 +2, P-EVENT0 +1, Q18 +3, P1 +1, P2 +1, S3 +1, S4 +1
    spec = p.TOOL_SPECS[TOOL]
    assert spec['markets'] == ['futures']
    props = spec['param_schema_properties']
    assert props['direction'] == dict(type='string',default='short',enum=['short'])
    assert props['bi_mode'] == dict(type='string',default='new',enum=['new','old'])
    assert getattr(spec['build'],'_supports_time_config',True)
    assert 'time_layer_enabled' in props


CHAN_BASE = 'df7b17c'


@pytest.mark.parametrize('market,params', [('spot',{}),('futures',{}),('futures',{'bi_mode':'old'}),
    ('futures',{'leverage':3,'position_size_pct':10}),('spot',dict(time_layer_enabled=True,max_holding_bars=2))])
@pytest.mark.parametrize('warm', [0,10])
def test_chan_3buy_response_bytes_match_original_engine(market,params,warm,monkeypatch):
    import subprocess
    import types
    import strategy_chan as engine
    from test_9t6_chan_3buy import frame as long_frame
    tool = 'local.backtesting_py.chan_3buy'
    # CHANSLIM: the original engine has no slim projection; compare its bytes with the debug path
    # (test_chan_slim_evidence pins default == slim projection of this debug body).
    monkeypatch.setenv('CUTIE_BACKTEST_CHAN_DEBUG', '1')
    current = http_response(params,long_frame(),warm,tool,market)
    if not params:
        assert current['trades']
    source = subprocess.check_output(['git','show',CHAN_BASE+':backtesting-py/strategy_chan.py'],text=True)
    original = types.ModuleType('shortpat3_original_chan')
    monkeypatch.setitem(sys.modules,original.__name__,original)
    exec(compile(source,engine.__file__,'exec'),original.__dict__)
    monkeypatch.setattr(engine,'make_chan_strategy',
                        lambda *a, chan_debug=False, **k: original.make_chan_strategy(*a, **k))
    before = http_response(params,long_frame(),warm,tool,market)
    for key in ('schema_version','metrics','trades','equity_curve','data_manifest','assumptions','raw_report'):
        assert json.dumps(current[key],sort_keys=True,separators=(',',':')) == json.dumps(before[key],sort_keys=True,separators=(',',':')),key


def test_chan_3buy_recognizer_report_matches_original_engine(monkeypatch):
    import subprocess
    import types
    from test_9t6_chan_3buy import frame as long_frame
    source = subprocess.check_output(['git','show',CHAN_BASE+':backtesting-py/strategy_chan.py'],text=True)
    original = types.ModuleType('shortpat3_original_chan_recognizer')
    monkeypatch.setitem(sys.modules,original.__name__,original)
    exec(compile(source,'strategy_chan_original','exec'),original.__dict__)
    for mode in ('new','old'):
        for data in (long_frame(), frame()):
            a,b = ChanRecognizer(bi_mode=mode), original.ChanRecognizer(bi_mode=mode)
            rows = [(float(r.High),float(r.Low),i) for i,r in enumerate(data.itertuples())]
            assert [repr(a.push(*x)) for x in rows] == [repr(b.push(*x)) for x in rows]
            assert json.dumps(a.report,sort_keys=True) == json.dumps(b.report,sort_keys=True)


def test_chan_3buy_builder_and_catalog_source_bytes_unchanged():
    import subprocess
    import ast
    original = subprocess.check_output(['git','show',CHAN_BASE+':backtesting-py/cutie_backtesting_provider.py'],text=True)
    current = Path(p.__file__).read_text()
    def sections(source):
        result = {}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node,ast.FunctionDef) and node.name == '_build_chan_3buy':
                start = min([node.lineno]+[d.lineno for d in node.decorator_list])
                result[node.name] = ''.join(source.splitlines(keepends=True)[start-1:node.end_lineno])
            if isinstance(node,ast.Dict):
                for key,value in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value == 'local.backtesting_py.chan_3buy':
                        result[key.value] = ast.get_source_segment(source,value)
        return result
    assert len(sections(current)) == 2
    # 10-B2b: the only sanctioned chan_3buy delta wires risk sizing to the frozen pullback stop.
    expected = sections(original)
    for old,new in (
        ("    risk = _parse_fixed_risk_params(params)\n",
         "    # Chan rejects user stops; its pullback stop is frozen at the signal (10-B2b).\n"
         "    risk = _parse_fixed_risk_params(params, template_initial_stop=True)\n"),
        ("    cls = make_chan_strategy(_FixedRiskMixin, bi_mode=bi_mode, risk=risk, initial_capital=initial_capital)\n",
         "    cls = make_chan_strategy(_FixedRiskMixin, bi_mode=bi_mode, risk=risk, initial_capital=initial_capital,\n"
         "                             chan_debug=_chan_debug_enabled())\n"  # CHANSLIM: debug-only chan traces
         "    # 10-B2b: risk distance = |actual fill - pullback Low * 0.999| frozen in the order tag.\n"
         "    cls._sizing_template_stop = lambda self, order: Decimal(str(order.tag.stop))\n")):
        assert expected['_build_chan_3buy'].count(old) == 1
        expected['_build_chan_3buy'] = expected['_build_chan_3buy'].replace(old,new)
    # P-PATCONF-2b2: chan_3buy opts into pattern confirmation (decorator argument only).
    old, new = '@_with_filter_config\n', '@_with_filter_config(pattern_confirm=True)\n'
    assert expected['_build_chan_3buy'].count(old) == 1
    expected['_build_chan_3buy'] = expected['_build_chan_3buy'].replace(old, new)
    assert sections(current) == expected
