"""ARM-C0-P mutation targets: premature availability, wrong family/interval, zero-fill,
deduplicate/null-fill, open chunk boundary, changed daily semantics."""
import json
from pathlib import Path
import pytest
import test_s3_top_long_short_reversal as s3
import test_s4_liquidation_reversal as s4
p = s3.p
H, T0 = 3600, s3.T0


def hourly(monkeypatch, tmp_path, kind, damage=None, mode='percentile', hours=48):
    calls = []
    first = T0 if kind == 'top' else T0 - (720 if mode == 'percentile' else 1) * H
    last = T0 + (hours - 1 if kind == 'top' else hours - 2) * H
    store = [{'ts': ts, 'value': '0.8' if kind == 'top' else '100'} for ts in range(first, last + H, H)]
    if damage == 'gap': del store[len(store)//2]
    elif damage == 'early_start': store = store[1:]
    elif damage == 'duplicate': store.insert(1, dict(store[0]))
    elif damage == 'null': store[1]['value'] = None
    next(r for r in store if r['ts'] == T0 + 3 * H)['value'] = '0.6' if kind == 'top' else '1000'
    frame = s3.flat_frame(hours, 'h')
    if kind == 'liq':
        frame.iloc[3] = [100, 100.5, 95, 96, 1]
        frame.iloc[4:] = [96, 96.5, 95.5, 96, 1]
    monkeypatch.setattr(p, 'AUTH_TOKEN', '')
    monkeypatch.setattr(p, 'REPORTS_DIR', tmp_path)
    monkeypatch.setattr(p, '_fetch_ohlcv', lambda *a, **kw: frame.copy())
    monkeypatch.setattr(s3.Backtest, 'plot', lambda *a, **kw: None)
    labels = ('BTC', 'Binance', 'top_long_short_ratio', '1h') if kind == 'top' else ('BTC', 'AGGREGATED', 'liquidation_long', '1h')
    def fetch(**kw):
        calls.append(kw)
        if tuple(kw[k] for k in ('symbol','exchange','metric','interval')) != labels: return []
        return [dict(r) for r in store if kw['start_at'] <= r['ts'] <= kw['end_at']][:5000]
    monkeypatch.setattr(p, '_fetch_artifact_metric_chunk', fetch)
    params = {} if mode == 'percentile' else {'threshold_mode':'fixed','fixed_threshold_usd':500}
    post = (lambda: s3.post(timeframe='1h',bars=hours)) if kind == 'top' else (lambda: s4.post(params,timeframe='1h',t0=T0,n=hours))
    return post,calls,first,last,labels


def block(body,kind):
    return body['assumptions']['long_short_ratio_series'] if kind == 'top' else body['assumptions']['liquidation_reversal']['liquidation_series']


@pytest.mark.parametrize('kind',['top','liq'])
def test_hourly_metric_available_at_H_plus_1(monkeypatch,tmp_path,kind):
    post,_,first,last,labels = hourly(monkeypatch,tmp_path,kind)
    rows = p._fetch_metric_series(symbol=labels[0],exchange=labels[1],metric=labels[2],interval='1h',start_at=first,end_at=last+H)
    assert all(r['available_at'] == r['ts'] + H for r in rows)
    body = post()
    assert body['result_status'] == 'success',body
    if kind == 'top':
        e = next(r for r in s3.decisions(body) if r['action']=='entry')
        assert e['value_ts'] == T0+3*H and e['decision_bar_open']==T0+3*H
        assert body['trades'][0]['opened_at']==T0+4*H
    else:
        e = next(r for r in s4.events(body) if r['day_utc']==s4.lr._iso(T0+3*H))
        assert e['ts_utc']==s4.lr._iso(T0+4*H) and e['decision_bar_utc']==s4.lr._iso(T0+4*H)
        assert body['trades'][0]['opened_at']==T0+5*H


@pytest.mark.parametrize('kind,mode',[('top','percentile'),('liq','percentile'),('liq','fixed')])
def test_hourly_bind_uses_exact_family_interval(monkeypatch,tmp_path,kind,mode):
    post,calls,first,last,labels = hourly(monkeypatch,tmp_path,kind,mode=mode)
    body=post()
    assert body['result_status']=='success',body
    assert calls and all(tuple(c[k] for k in ('symbol','exchange','metric','interval'))==labels for c in calls)
    assert calls[0]['start_at']==first and calls[-1]['end_at']==last+H-1
    assert block(body,kind)['interval']=='1h' and block(body,kind)['count']==(last-first)//H+1
    if kind=='liq':
        a=body['assumptions']['liquidation_reversal']
        assert a['lookback_bars']==(720 if mode=='percentile' else 0)
        e=next(r for r in s4.events(body) if r['day_utc']==s4.lr._iso(T0+3*H))
        assert e['threshold']==('100' if mode=='percentile' else '500')


@pytest.mark.parametrize('kind',['top','liq'])
@pytest.mark.parametrize('damage',['gap','early_start'])
def test_hourly_metric_gap_rejects(monkeypatch,tmp_path,kind,damage):
    post,calls,*_=hourly(monkeypatch,tmp_path,kind,damage)
    body=post()
    assert body['result_status']=='failed' and body['error_type']=='TIME_DATA_GAP'
    assert body['limitations']['reason']==('long_short_ratio_data_gap' if kind=='top' else 'liquidation_data_gap')
    assert all(c['interval']=='1h' for c in calls)


@pytest.mark.parametrize('kind',['top','liq'])
@pytest.mark.parametrize('damage',['duplicate','null'])
def test_hourly_metric_duplicate_or_null_rejects(monkeypatch,tmp_path,kind,damage):
    post,*_=hourly(monkeypatch,tmp_path,kind,damage)
    body=post()
    assert body['result_status']=='failed' and body['error_type']=='TIME_DATA_GAP'
    assert body['limitations']['reason']==('long_short_ratio_data_gap' if kind=='top' else 'liquidation_data_gap')


@pytest.mark.parametrize('kind',['top','liq'])
def test_chunked_fetch_keeps_boundary_bar(monkeypatch,tmp_path,kind):
    post,calls,first,last,_=hourly(monkeypatch,tmp_path,kind,hours=5101)
    # Large hourly event reports can exceed the existing signed-report size cap.
    # Exercise the production bind/fetch path directly, without serialising an oversized report.
    if kind == 'top':
        config = s3.p.TopLongShortReversalConfig.parse({})
        failure = p._bind_top_lsr('chunk', config, 'BTCUSDT', '1h', s3.flat_frame(5101, 'h'))
        series = config.series_block
    else:
        config = s4.lr.LiquidationReversalConfig.parse({})
        config.bind_timeframe('1h')
        failure = p._bind_liquidation_reversal('chunk', config, 'BTCUSDT', T0, T0 + 5101 * H)
        series = config.assumptions['liquidation_series']
    assert failure is None, failure
    assert len(calls)>=2 and calls[0]['start_at']==first and calls[-1]['end_at']==last+H-1
    assert all(b['start_at']==a['end_at']+1 for a,b in zip(calls,calls[1:]))
    assert all((c['end_at']-c['start_at'])//H+1<=5000 for c in calls)
    assert series['count']==(last-first)//H+1


def test_daily_and_4h_unchanged(monkeypatch,tmp_path):
    expected=(Path(__file__).parent/'fixtures/arm_c0_p_daily_4h.json').read_bytes()
    out={}
    monkeypatch.setattr(p.time,'time',lambda:1800000000)
    for tf in ('4h','1d'):
        bars=48 if tf=='4h' else 8
        data=s3.flat_frame(bars,'4h') if tf=='4h' else s3.daily_frame(bars)
        s3.install(monkeypatch,tmp_path,{1:'0.65',3:'1.1'},data)
        body=s3.post(timeframe=tf,bars=bars)
        assert body['result_status']=='success',body
        out[f'top-{tf}']={k:body[k] for k in ('trades','metrics','assumptions','raw_report') if k in body}
        body,_=s4.run(monkeypatch,tmp_path,s4.SPIKE,rows=s4.ROWS,timeframe=tf)
        out[f'liq-{tf}']={k:body[k] for k in ('trades','metrics','assumptions','raw_report') if k in body}
    assert (json.dumps(out,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()==expected
