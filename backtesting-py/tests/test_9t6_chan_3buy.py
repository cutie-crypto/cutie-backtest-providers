"""Independent scalar geometry / hand-counted points, causal execution and HTTP contract."""
from pathlib import Path
import sys

import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from strategy_chan import ChanRecognizer
from strategy_time_layer import TimeContext

TOOL = 'local.backtesting_py.chan_3buy'
# Independent endpoints: top 1=111, bottom 3=79, top 5=106,
# bottom 7=89 => ZG106, ZD89; departure top9=131; pullback11=114.
# Right bar12 confirms bottom11. Signal12, actual next open13=125.
CLOSES = [100,110,95,80,95,105,98,90,110,130,120,115,122,125,130,140,141,142]


def frame():
    f = pd.DataFrame(dict(Open=CLOSES, High=[c+1 for c in CLOSES],
        Low=[c-1 for c in CLOSES], Close=CLOSES, Volume=100),
        index=pd.date_range('2026-01-01', periods=len(CLOSES), freq='h'))
    f.loc[f.index[14], 'High'] = 147
    f.loc[f.index[15], 'High'] = 148
    return f.astype(float)


def recognize(data=None, mode='new'):
    data = frame() if data is None else data
    r = ChanRecognizer(bi_mode=mode)
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


def test_hand_counted_center_and_confirmed_pullback():
    signals,r = recognize()
    assert [i for i,s in enumerate(signals) if s] == [12]
    assert signals[12].zg == 106
    assert signals[12].stop == pytest.approx(113.886)
    center = r.report['centers'][0]
    assert (center['zg'], center['zd'], center['confirmed_at']) == (106,89,8)
    assert [(b['start']['raw_index'], b['end']['raw_index'], b['confirmed_at'])
            for b in r.report['strokes'][:5]] == [(1,3,4),(3,5,6),(5,7,8),(7,9,10),(9,11,12)]
    assert signals[11] is None
    assert recognize(frame().iloc[:12])[0] == [None]*12


@pytest.mark.parametrize('end', range(3,19))
def test_prefix_and_future_do_not_rewrite_decisions(end):
    f = frame()
    full,_ = recognize(f)
    assert recognize(f.iloc[:end])[0] == full[:end]
    mutated = f.copy()
    for i in range(end,len(f)):
        mutated.iloc[i, mutated.columns.get_indexer(['Open','High','Low','Close'])] = [1000+i,1001+i,999+i,1000+i]
    assert recognize(mutated)[0][:end] == full[:end]


@pytest.mark.parametrize('direction,values,expected', [
    ('up',[(10,5),(12,7),(11,8),(13,6)],(13,8)),
    ('down',[(12,7),(10,5),(9,6),(11,4)],(9,4)),
])
def test_inclusion_directional_merge(direction, values, expected):
    r=ChanRecognizer()
    for i,(h,l) in enumerate(values): r.push(h,l,i)
    assert len(r.bars)==2
    assert (r.bars[-1]['high'],r.bars[-1]['low'])==expected
    assert r.report['merges'][-1]['direction']==direction


def test_inclusion_changes_fractal_recognition():
    # Up-merge bar2 into bar1 => H12 L8, right bar3 H11 L6 confirms top.
    r=ChanRecognizer()
    for i,(h,l) in enumerate([(10,5),(12,7),(11,8),(11,6)]): r.push(h,l,i)
    assert [(x['kind'],x['raw_index'],x['confirmed_at']) for x in r.report['fractals']]==[('top',2,3)]


@pytest.mark.parametrize('mode,gap,accepted', [('new',1,False),('new',2,True),('old',5,False),('old',6,True)])
def test_independent_bar_spacing(mode,gap,accepted):
    r=ChanRecognizer(bi_mode=mode)
    r._point(point('top',1,110))
    r._point(point('bottom',1+gap,90))
    assert bool(r.report['strokes']) is accepted


@pytest.mark.parametrize('kind,prices', [('top',[110,112,111]),('bottom',[90,88,89])])
def test_same_direction_extreme_replacement(kind,prices):
    r=ChanRecognizer()
    for i,price in enumerate(prices): r._point(point(kind,2*i+1,price))
    assert r.endpoint['price']==prices[1]
    assert len(r.report['replacements'])==1
    assert not r.report['strokes']


@pytest.mark.parametrize('low,valid', [(106,False),(105.99,False),(106.01,True)])
def test_pullback_strictly_above_zg(low,valid):
    r=ChanRecognizer()
    for kind,idx,price in [('top',1,111),('bottom',3,79),('top',5,106),
            ('bottom',7,89),('top',9,131)]: r._point(point(kind,idx,price))
    signal=r._point(point('bottom',11,low))
    assert bool(signal) is valid


def test_no_center_on_touching_overlap():
    r=ChanRecognizer()
    for kind,idx,price in [('top',1,110),('bottom',3,100),('top',5,120),('bottom',7,110)]:
        r._point(point(kind,idx,price))
    assert not r.report['centers']


def test_same_center_has_only_one_opportunity():
    # New center needs three wholly later strokes; second qualifying retrace
    # is evaluated against the consumed original center before any new discovery.
    r=ChanRecognizer()
    signals=[]
    for kind,idx,price in [('top',1,111),('bottom',3,79),('top',5,106),('bottom',7,89),
        ('top',9,131),('bottom',11,114),('top',13,140),('bottom',15,120)]:
        signals.append(r._point(point(kind,idx,price)))
        if idx==11:
            # Isolate consumption arbitration with center discovery held fixed.
            # Actual no-reuse center discovery is covered separately.
            r.center['end_stroke']=99
    assert [i for i,s in enumerate(signals) if s]==[5]
    assert r.report['third_buys'][1]['status']=='invalid_or_consumed'
    assert r.report['third_buys'][1]['center']==0


def test_next_open_and_actual_fill_frozen_r():
    stats=run()
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist()==[[13,16]]
    report=stats['_strategy'].chan_report
    entry=report['entries'][0]
    assert entry['entry_price']==125
    assert entry['frozen_stop']==pytest.approx(113.886)
    assert entry['frozen_target']==pytest.approx(147.228)
    assert report['exits']==[dict(reason='take_profit',decision_bar=15)]


@pytest.mark.parametrize('opening',[113.886,110])
def test_gap_entry_wrong_side_cancelled(opening):
    f=frame()
    f.loc[f.index[13],['Open','High','Low','Close']]=[opening,opening+1,opening-1,opening]
    stats=run(f)
    assert stats['_trades'].empty
    skipped=stats['_strategy'].chan_report['skipped_entries']
    assert skipped[0]['reason']=='entry_open_at_or_below_frozen_stop'


@pytest.mark.parametrize('stop,time,take,expected', [
    (True,True,True,'stop_loss'),(False,True,True,'time_expiry'),
    (False,False,True,'take_profit'),(False,False,False,'close_below_zg')])
def test_exit_priority(stop,time,take,expected):
    f=frame()
    f.loc[f.index[14],['Open','High','Low','Close']]=[130,160 if take else 140,
        110 if stop else 120,130]
    if expected=='close_below_zg':
        # Pullback=106.01 > ZG106; frozen stop105.90399 < ZG106.
        # Close105.99 triggers signal while Low105.95 stays above frozen stop.
        f.loc[f.index[11],['Open','High','Low','Close']]=[108,109,106.01,108]
        f.loc[f.index[14],['Open','High','Low','Close']]=[106,107,105.95,105.99]
    params=dict(time_layer_enabled=True,max_holding_bars=2) if time else {}
    stats=run(f,params)
    assert stats['_strategy'].chan_report['exits'][0]==dict(reason=expected,decision_bar=14)


@pytest.mark.parametrize('params', [
    dict(time_layer_enabled=True,time_session_start='00:00',time_session_end='12:00'),
    dict(filter_layer_enabled=True,filter_ema_enabled=True,filter_ema_period=2),
])
def test_entry_gates_drop_opportunity(params):
    f=frame()
    if 'filter_layer_enabled' in params:
        f.loc[f.index[12],['Open','High','Low','Close']]=[116,123,116,116]
    stats=run(f,params)
    assert stats['_trades'].empty


def test_tail_does_not_backfill_new_trade():
    assert run(frame().iloc[:13])['_trades'].empty


def test_old_mode_actual_merged_geometry():
    assert run(params={'bi_mode':'old'})['_trades'].empty


@pytest.mark.parametrize('params', [dict(direction='short'),dict(direction='both'),dict(direction=None),
    dict(bi_mode='bad'),dict(bi_mode=True),dict(stop_loss_pct=3),dict(take_profit_pct=5),
    dict(risk_layer_enabled=True,stop_loss_pct=3),
    dict(risk_layer_enabled=True,atr_stop_multiplier=2,risk_atr_period=5),
    dict(risk_layer_enabled=True,trailing_stop_pct=3),
    dict(risk_layer_enabled=True,trailing_stop_pct=3,tp1_r=1,tp1_close_pct=100)])
def test_invalid_before_fetch(params,monkeypatch):
    import test_time_layer_compatibility as compat
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a: pytest.fail('invalid params fetched'))
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a: pytest.fail('invalid params warmed'))
    response=TestClient(p.app).post('/cutie/backtest',json=compat.capture.request_body('chan_3buy',params)).json()
    assert response['error_type']=='INVALID_PARAMS',response


def test_http_details_only_in_raw_report_and_result_keys_frozen(monkeypatch,tmp_path):
    import test_time_layer_compatibility as compat
    f=frame()
    body=compat.capture.request_body('chan_3buy',{})
    body['backtest'].update(start_at=int(f.index[0].timestamp()),end_at=int(f.index[-1].timestamp())+3600,
        market='spot',fee_bps='0',slippage_bps='0')
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a:f.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a:f.iloc[:0].copy())
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k:None)
    result=TestClient(p.app).post('/cutie/backtest',json=body).json()
    assert result['result_status']=='success',result
    assert len(result['trades'])==1
    assert set(result['trades'][0])=={'seq','side','opened_at','closed_at','qty','entry_price','exit_price','fee','slippage','pnl'}
    assert set(result['metrics'])=={'total_return','max_drawdown','trade_count'}
    assert set(result['data_manifest'])=={'source','symbol','market','timeframe','start_at','end_at','kline_count','checksum_algo','checksum'}
    assert result['raw_report']['chan']['centers'][0]['zg']==106
    assert result['raw_report']['chan']['entries'][0]['frozen_target']==pytest.approx(147.228)
    assert 'chan' not in result['metrics']
    assert result['assumptions']['chan_execution']['centers'].endswith('no_reuse_extension_or_expansion')


def test_initial_inclusion_defers_merge_until_direction_known():
    r=ChanRecognizer()
    for i,(h,l) in enumerate([(10,5),(9,6),(11,7)]):
        assert r.push(h,l,i) is None
    assert (r.bars[0]['high'],r.bars[0]['low'])==(10,6)
    assert r.report['merges'][0]['reason']=='initial_direction_resolved'


def test_new_centers_never_extend_or_reuse_old_center_strokes():
    r=ChanRecognizer()
    for kind,idx,price in [('top',1,111),('bottom',3,79),('top',5,106),('bottom',7,89),
        ('top',9,131),('bottom',11,114),('top',13,140),('bottom',15,120)]:
        r._point(point(kind,idx,price))
    assert [(c['start_stroke'],c['end_stroke'],c['zg'],c['zd'])
            for c in r.report['centers']]==[(0,2,106,89),(3,5,131,114)]


def test_gap_checked_on_real_adjacent_fractals():
    r=ChanRecognizer()
    for i,c in enumerate([100,110,90,100]): r.push(c+1,c-1,i)
    assert [(x['kind'],x['raw_index']) for x in r.report['fractals']]==[('top',1),('bottom',2)]
    assert not r.report['strokes']
    assert r.report['rejected_strokes'][0]['reason']=='independent_bar_gap'


def test_extreme_replacement_on_real_fractals():
    r=ChanRecognizer()
    for i,c in enumerate([100,110,108,112,111]): r.push(c+1,c-1,i)
    assert len(r.report['replacements'])==1
    assert (r.endpoint['raw_index'],r.endpoint['price'])==(3,113)


def test_initial_resolution_rechecks_containment():
    r=ChanRecognizer()
    for i,(h,l) in enumerate([(10,5),(9,8),(11,6)]): r.push(h,l,i)
    assert len(r.bars)==1
    assert (r.bars[0]['high'],r.bars[0]['low'])==(11,8)
    assert not r.report['fractals']


def test_warmup_points_allow_main_range_entry_without_warmup_orders():
    f=frame()
    cls=p.TOOL_SPECS[TOOL]['build']({})['strategy']
    cls._warmup_bars=10
    cls._warmup_cols={c:f[c].iloc[:10].to_numpy() for c in p._WARMUP_COLUMNS}
    stats=Backtest(f.iloc[10:],cls,cash=10000,exclusive_orders=True,finalize_trades=True).run()
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist()==[[3,6]]
    assert stats['_strategy'].chan_report['third_buys'][0]['confirmed_at']==12


@pytest.mark.parametrize('bad', [(0,0),(1,2),(float('nan'),1),(float('inf'),1)])
def test_recognizer_rejects_invalid_prices(bad):
    with pytest.raises(ValueError,match='INVALID_PARAMS'):
        ChanRecognizer().push(*bad,0)


def test_replacement_cannot_retroactively_change_frozen_center():
    r=ChanRecognizer()
    for kind,idx,price in [('top',1,111),('bottom',3,79),('top',5,106),('bottom',7,89)]:
        r._point(point(kind,idx,price))
    r._point(point('bottom',9,85))
    assert r.report['strokes'][-1]['low']==85
    assert r.center['zd']==89
    assert r.center['strokes'][-1]['low']==89


def test_signal_exits_at_next_open_above_stop_below_zg():
    f=frame()
    f.loc[f.index[11],['Open','High','Low','Close']]=[108,109,106.01,108]
    f.loc[f.index[14],['Open','High','Low','Close']]=[106,107,105.95,105.99]
    stats=run(f)
    assert stats['_trades'][['EntryBar','ExitBar']].values.tolist()==[[13,15]]
    assert stats['_strategy'].chan_report['exits'][0]['reason']=='close_below_zg'


def test_equality_to_zg_close_does_not_exit_signal():
    f=frame()
    f.loc[f.index[11],['Open','High','Low','Close']]=[108,109,106.01,108]
    f.loc[f.index[14],['Open','High','Low','Close']]=[106,107,105.95,106]
    stats=run(f)
    assert all(x['decision_bar']!=14 for x in stats['_strategy'].chan_report['exits'])
