"""7P-2 independent close-boundary goldens and registered HTTP proof."""
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pytest
from backtesting import Backtest
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_entry_filters as single
from strategy_entry_filters import FilterConfig, HigherTimeframeContext, FilterHistoryError


def coarse_frame(closes=None, start='2026-01-01', freq='4h'):
    closes = closes if closes is not None else [100]*10+[110,90,10000]
    data = single.frame(closes)
    data.index = pd.date_range(start, periods=len(data), freq=freq)
    return data


def context(kind, opens, source=None):
    source = coarse_frame() if source is None else source
    config = FilterConfig.parse({**single.params(kind), 'filter_timeframe':'4h'})
    return HigherTimeframeContext.build(config, '15m', opens, lambda *args:source.copy(), p._supertrend_arrays)


@pytest.mark.parametrize('kind',['ema','macd','supertrend'])
def test_boundary_before_equal_after_independent_golden(kind):
    # Coarse up candle opens Jan 2 16:00, closes 20:00.
    # EMA2=106 2/3; DIF(2,3)=5/3; ST(5,1) crosses old upper=102.
    opens = pd.to_datetime(['2026-01-02 19:30','2026-01-02 19:45','2026-01-02 20:00'])
    assert context(kind,opens).mask.tolist()==[False,True,True]


@pytest.mark.parametrize('kind',['ema','macd','supertrend'])
def test_unclosed_coarse_close_cannot_change_earlier_decisions(kind):
    source=coarse_frame()
    opens=pd.date_range('2026-01-02 16:00',periods=32,freq='15min')
    before=context(kind,opens,source).mask
    changed=source.copy()
    changed.iloc[10,changed.columns.get_loc('Close')]=90
    after=context(kind,opens,changed).mask
    # 16:15..19:45 flat; 20:00..23:45 up; midnight down.
    assert before.tolist()==[False]*15+[True]*16+[False]
    np.testing.assert_array_equal(before[:15],after[:15])
    assert not after[15]


@pytest.mark.parametrize('kind',['ema','macd','supertrend'])
def test_future_source_tail_ignored(kind):
    opens=pd.date_range('2026-01-02 16:00',periods=16,freq='15min')
    source=coarse_frame()
    first=context(kind,opens,source)
    source.iloc[11:,source.columns.get_loc('Close')]=np.nan
    second=context(kind,opens,source)
    assert first.mask.tolist()==second.mask.tolist()==[False]*15+[True]


@pytest.mark.parametrize('gap',['first','interior','last','shift','nan','empty','fetch_error'])
def test_strict_history_never_fills_or_falls_back(gap):
    source=coarse_frame()
    opens=pd.date_range('2026-01-02 16:00',periods=32,freq='15min')
    if gap in ('first','interior','last'):
        source=source.drop(source.index[{'first':8,'interior':10,'last':11}[gap]])
    elif gap=='shift': source.index=source.index+pd.Timedelta(minutes=1)
    elif gap=='nan': source.iloc[10,source.columns.get_loc('Close')]=np.nan
    elif gap=='empty': source=source.iloc[:0]
    def fetch(*args):
        if gap=='fetch_error': raise RuntimeError('NO_DATA')
        return source
    config=FilterConfig.parse({**single.params('ema'),'filter_timeframe':'4h'})
    with pytest.raises(FilterHistoryError):
        HigherTimeframeContext.build(config,'15m',opens,fetch,p._supertrend_arrays)


def test_weekly_monday_grid_and_boundary():
    source=coarse_frame([100]*10+[110,90],start='2025-10-27',freq='7D')
    # Jan 5 up week closes Monday Jan 12, not epoch's Thursday grid.
    opens=pd.to_datetime(['2026-01-11 23:30','2026-01-11 23:45','2026-01-12 00:00'])
    config=FilterConfig.parse({**single.params('ema'),'filter_timeframe':'1w'})
    result=HigherTimeframeContext.build(config,'15m',opens,lambda *args:source,
        p._supertrend_arrays,p._timeframe_grid_offset_ms('1w'))
    assert result.mask.tolist()==[False,True,True]
    assert result.report['fetched_bars']==3


@pytest.mark.parametrize('timeframe',['1m','5m','15m','1M','3h',None,4,False])
def test_invalid_timeframe_rejected_before_fetch(timeframe,monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *args:pytest.fail('invalid request fetched data'))
    body=single.capture.capture.request_body('ema_cross',{**single.params('ema'),'filter_timeframe':timeframe})
    body['backtest']['timeframe']='15m'
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type']=='INVALID_PARAMS'


# 7-P4：short 放行（多周期镜像见 test_7p4_short_filter.py），both 仍拒。
@pytest.mark.parametrize('name',['ema_pullback','breakout','cci_rsi'])
@pytest.mark.parametrize('direction',['both'])
def test_mtf_short_both_rejected_before_fetch(name,direction,monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *args:pytest.fail('direction rejection fetched data'))
    body=single.capture.capture.request_body(name,{**single.params('ema'),'filter_timeframe':'4h','direction':direction})
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type']=='INVALID_PARAMS'


@pytest.mark.parametrize('name',single.EXCLUDED)
def test_ledger_kernel_reject_mtf_even_default(name,monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *args:pytest.fail('excluded tool fetched data'))
    body=single.capture.capture.request_body(name,{'filter_timeframe':''})
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type']=='INVALID_PARAMS'


def http_run(monkeypatch,tmp_path,kind='ema',name='ema_cross',gap=None,enabled=True,warm=True,cutoff=None):
    closes=[100]*10+[90,110,120,80,70,130,90,100]*64
    data=single.frame(closes)
    data.index=pd.date_range('2026-01-02 16:00',periods=len(data),freq='15min')
    source=coarse_frame([100]*10+[110,90,100,110,90,120,80,110,90]*9)
    if gap=='missing': source=source.drop(source.index[10])
    if gap=='short': source=source.iloc[9:]
    if gap=='empty': source=source.iloc[:0]
    values={**single.params(kind),'ema_fast':2,'ema_slow':3} if name=='ema_cross' else {**single.compat.PARAMS.get(name,{}),**single.params(kind)}
    if name=='ema_rsi_pullback': values['ema_period']=50
    if name=='volume_breakout': values['lookback']=10; values['volume_avg_period']=10; values['volume_multiple']=1.2
    if enabled: values['filter_timeframe']='4h'
    else: values={**single.compat.PARAMS.get(name,{}),'filter_timeframe':''}
    if 'direction' in p.TOOL_SPECS['local.backtesting_py.'+name]['param_schema_properties']: values['direction']='long'
    values['position_size_pct']=10
    calls=[]
    def fetch(exchange,market,symbol,timeframe,since,until):
        calls.append((timeframe,since,until))
        if timeframe=='4h':
            if gap=='error': raise ValueError('NO_DATA')
            return source.copy()
        return data.copy()
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',fetch)
    prefix=single.frame([100]*10)
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *args:prefix if warm else prefix.iloc[:0])
    monkeypatch.setattr(Backtest,'plot',lambda *args,**kwargs:None)
    body=single.capture.capture.request_body(name,values)
    start=int(data.index[0].timestamp())
    body['backtest'].update(timeframe='15m',start_at=start,end_at=start+len(data)*900)
    if cutoff is not None: body['backtest']['end_at']=cutoff
    return TestClient(p.app).post('/cutie/backtest',json=body).json(),calls


@pytest.mark.parametrize('kind',['ema','macd','supertrend'])
@pytest.mark.parametrize('warm',[False,True])
def test_http_gate_uses_coarse_mask_at_primary_decision(kind,warm,monkeypatch,tmp_path):
    seen=[]
    original=p._FilterLayerMixin._filter_allow_entry
    def observe(self):
        result=original(self)
        seen.append((len(self.data)-1,result))
        return result
    monkeypatch.setattr(p._FilterLayerMixin,'_filter_allow_entry',observe)
    result,calls=http_run(monkeypatch,tmp_path,kind=kind,warm=warm)
    assert result['result_status']=='success',result
    # Main bar15 decision=20:00; 3 coarse filters bullish. Warmup cannot shift it.
    assert (15,True) in seen
    assert all(not allowed for index,allowed in seen if index<15)
    assert all(not allowed for index,allowed in seen if 31<=index<47)
    assert sum(call[0]=='4h' for call in calls)==1
    assert result['raw_report']['entry_filters']['timeframe']=='4h'
    assert result['raw_report']['entry_filters']['boundary']=='inclusive'
    assert result['trades']
    for trade in result['trades']:
        assert trade['opened_at']>=int(pd.Timestamp('2026-01-02 20:00').timestamp())
    assert 'entry_filters' not in result['metrics'] and 'entry_filters' not in result['data_manifest']


@pytest.mark.parametrize('gap',['missing','short','empty','error'])
def test_http_insufficient_history(gap,monkeypatch,tmp_path):
    result,calls=http_run(monkeypatch,tmp_path,gap=gap)
    assert result['error_type']=='INSUFFICIENT_DATA',result
    assert result['limitations']['reason']=='filter_history_insufficient'
    assert sum(call[0]=='4h' for call in calls)==1


@pytest.mark.parametrize('name',single.SINGLE_NAMES)
def test_each_registered_template_consumes_mtf(name,monkeypatch,tmp_path):
    result,calls=http_run(monkeypatch,tmp_path,name=name)
    assert result['result_status']=='success',result
    assert result['raw_report']['entry_filters']['timeframe']=='4h'
    assert sum(c[0]=='4h' for c in calls)==1


@pytest.mark.parametrize('warm',[False,True])
def test_http_off_state_does_not_fetch_coarse(monkeypatch,tmp_path,warm):
    result,calls=http_run(monkeypatch,tmp_path,enabled=False,warm=warm)
    assert result['result_status']=='success',result
    assert [call[0] for call in calls]==['15m']
    assert 'entry_filters' not in result['raw_report']


@pytest.mark.parametrize('case',single.CASES)
@pytest.mark.parametrize('warm',[False,True])
def test_explicit_default_timeframe_preserves_frozen_off_bytes(case,warm,monkeypatch):
    monkeypatch.setenv('CUTIE_BACKTEST_CHAN_DEBUG', '1')  # CHANSLIM: frozen bytes predate the slim chan evidence
    name,values=single.CASES[case]
    monkeypatch.setattr(p.HigherTimeframeContext,'build',lambda *args:pytest.fail('off state fetched coarse'))
    assert single.capture.snapshot(name,{**values,'filter_timeframe':''},warm)==single.BASELINE['cases'][case+'/'+str(int(warm))]


def test_off_nondefault_timeframe_rejected_before_fetch(monkeypatch):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *args:pytest.fail('off invalid request fetched data'))
    body=single.capture.capture.request_body('ema_cross',{'filter_timeframe':'4h'})
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type']=='INVALID_PARAMS'


def test_mtf_and_requires_every_coarse_predicate():
    source=coarse_frame([100]*10+[80,81,90])
    opens=pd.to_datetime(['2026-01-03 03:45'])
    # Last closed candle=90: EMA2 on its minimum prefix [81,90]=87, so passes; DIF still negative.
    assert context('ema',opens,source).mask.tolist()==[True]
    config=FilterConfig.parse({**single.params('ema'),**single.params('macd'),
        **single.params('supertrend'),'filter_timeframe':'4h'})
    result=HigherTimeframeContext.build(config,'15m',opens,lambda *args:source,p._supertrend_arrays)
    assert result.mask.tolist()==[False]


@pytest.mark.parametrize('kind',['ema','macd','supertrend'])
def test_http_end_boundary_excludes_unclosed_coarse(kind,monkeypatch,tmp_path):
    cutoff=int(pd.Timestamp('2026-01-02 19:45').timestamp())
    result,calls=http_run(monkeypatch,tmp_path,kind=kind,cutoff=cutoff)
    assert result['result_status']=='success',result
    assert result['trades']==[]
    assert result['raw_report']['entry_filters']['history_end_at']==int(pd.Timestamp('2026-01-02 16:00').timestamp())
    assert calls[1][2]==int(pd.Timestamp('2026-01-02 16:00').timestamp())


def test_request_context_does_not_leak_between_runs(monkeypatch,tmp_path):
    on,_=http_run(monkeypatch,tmp_path)
    off,calls=http_run(monkeypatch,tmp_path,enabled=False)
    assert on['result_status']==off['result_status']=='success'
    assert 'entry_filters' in on['raw_report'] and 'entry_filters' not in off['raw_report']
    assert [c[0] for c in calls]==['15m']


def test_year_fetch_volume_is_one_strict_coarse_span():
    opens=pd.date_range('2025-01-01',periods=365*24*4,freq='15min')
    config=FilterConfig.parse({'filter_layer_enabled':True,'filter_ema_enabled':True,'filter_timeframe':'4h'})
    calls=[]
    def fetch(since,until):
        calls.append((since,until))
        count=(until-since)//(4*3600)
        return coarse_frame([100]*count,start=pd.Timestamp(since,unit='s'))
    result=HigherTimeframeContext.build(config,'15m',opens,fetch,p._supertrend_arrays)
    assert len(calls)==1 and result.report['fetched_bars']==2390
    assert len(result.mask)==35040 and not result.mask.any()
    start=int(opens[0].timestamp());end=int(opens[-1].timestamp())+900
    chunks=p._split_central_range(start*1000,end*1000,'15m',p.CENTRAL_MAX_CHUNK_MS)
    coarse_chunks=p._split_central_range(calls[0][0]*1000,calls[0][1]*1000,'4h',p.CENTRAL_MAX_CHUNK_MS)
    assert len(chunks)==5 and len(coarse_chunks)==5
