"""WARMUP-B: complete seed, safe hints and independently bound OHLCV."""
import json
import math
import pandas as pd
import pytest
import cutie_backtesting_provider as provider
from canonical_json import canonical_json_sha256
from test_ema_backtest_warmup import START, STEP, client, fetcher, frame, post, V2_KEYS


def request(client, start=START, count=500, params=None, name='ema_cross', **seed):
    response = client.post('/cutie/backtest', json={'backtest': dict(run_id='warmup-b',
        provider_tool_id='local.backtesting_py.'+name,
        provider_params={'ema_fast':5,'ema_slow':20} if params is None else params, symbol='BTCUSDT', market='spot',
        timeframe='1h', start_at=start, end_at=START+count*STEP,
        initial_capital='10000', fee_bps='10', slippage_bps='5', **seed)})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize('count,gap', [(199,False),(0,False),(200,True)])
def test_short_or_gapped_warmup_fails_with_valid_start_hint(client, monkeypatch, count, gap):
    full = frame([100.]*count, [100.+20*math.sin(i/12) for i in range(500)])
    if gap:
        full = full.drop(full.index[100])
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
    failed = request(client)
    assert failed['result_status'] == 'failed'
    assert failed['error_type'] == 'WARMUP_INSUFFICIENT'
    details = failed['limitations']
    expected = 99 if gap else count
    assert details['required_bars'] == 200
    assert details['actual_bars'] == expected
    assert details['missing_bars'] == 200-expected
    assert details['warmup_rule_version'] == 'template-warmup.v1'
    assert 'trades' not in failed
    hint = details['suggested_start_at']
    assert hint == START + (200-expected)*STEP
    assert request(client, start=hint)['result_status'] == 'success'


def test_no_hint_for_short_or_gapped_residual(client, monkeypatch):
    for full in (frame([], [100.]*220), frame([], [100.]*500).drop(pd.to_datetime(START+450*STEP,unit='s'))):
        monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
        failed = request(client, count=500 if len(full)>220 else 220)
        assert failed['error_type'] == 'WARMUP_INSUFFICIENT'
        assert failed['limitations']['suggested_start_at'] is None


@pytest.mark.parametrize('exception', [RuntimeError('private-fetch-error'), provider.MarketDataFetchError('ENGINE_ERROR','private-error', central_failure_reason=None, central_attempted=False)])
def test_warmup_fetch_failure_is_not_history_shortage(client, monkeypatch, exception):
    good = fetcher(frame([], [100.]*500))
    def fetch(*args):
        if args[-2] < START:
            raise exception
        return good(*args)
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetch)
    failed = request(client)
    assert failed['result_status'] == 'failed'
    assert failed['error_type'] == 'WARMUP_FETCH_FAILED'
    assert not {'missing_bars','actual_bars','suggested_start_at'} & failed['limitations'].keys()
    assert 'private' not in failed['error_message']


def test_warmup_exact_history_repeat_is_identical(client, monkeypatch, tmp_path):
    full = frame([180.]*180+[110.]*20, [100.+20*math.sin(i/12) for i in range(500)])
    monkeypatch.setattr(provider, 'WARMUP_SNAPSHOT_DIR', tmp_path/'snapshots')
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
    first, second = request(client), request(client)
    assert first['result_status'] == second['result_status'] == 'success'
    assert first['trades'] and first['trades'] == second['trades']
    assert canonical_json_sha256({k:first[k] for k in V2_KEYS}) == canonical_json_sha256({k:second[k] for k in V2_KEYS})
    evidence = first['assumptions']['warmup_manifest']
    assert evidence['source'] == first['data_manifest']['source']
    assert len(evidence) == 14
    assert evidence['actual_bars'] == evidence['required_bars'] == 200
    assert evidence['start_at'] == START-200*STEP and evidence['end_at'] == START
    assert evidence['schema_version'] == 'cutie.backtest_warmup.v1'
    assert evidence['seed_rule_version'] == 'adjacent-first-observation.v1'
    assert first['assumptions']['warmup_manifest_hash'] == canonical_json_sha256(evidence)
    assert first['assumptions']['warmup_manifest_hash'] == second['assumptions']['warmup_manifest_hash']
    frozen = provider._load_warmup_snapshot(evidence)
    assert frozen == provider._kline_rows_to_canonical(full.iloc[:200], START-200*STEP, START-1)
    assert evidence['checksum'] == canonical_json_sha256(frozen)
    full.loc[pd.to_datetime(START-STEP,unit='s'), 'Close'] += 1
    changed = request(client)
    assert changed['data_manifest'] == first['data_manifest']
    assert changed['assumptions']['warmup_manifest_hash'] != first['assumptions']['warmup_manifest_hash']
    path = provider.WARMUP_SNAPSHOT_DIR/(evidence['snapshot_ref']+'.json')
    payload = json.loads(path.read_text())
    payload['rows'][-1]['close'] = '999'
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='checksum'):
        provider._load_warmup_snapshot(evidence)


def test_unaligned_start_has_exact_adjacent_seed(client, monkeypatch):
    full = frame([100.]*202, [100.]*500)
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetcher(full))
    body = request(client, start=START+1)
    assert body['result_status'] == 'success'
    manifest = body['assumptions']['warmup_manifest']
    assert manifest['end_at'] == START+STEP
    assert manifest['start_at'] == START-199*STEP


def test_budget_separates_minimum_and_recursion():
    from warmup_rules import wilder_bars
    for name,params,required in [
        ('ema_cross', {'ema_fast':5,'ema_slow':20}, 200),
        ('keltner_breakout', {'ema_period':20,'atr_period':14}, max(200,wilder_bars(14))),
        ('volume_breakout', {'exit_ema':30}, 300),
        ('macd', {'fast':12,'slow':26,'signal':9}, 350),
        ('macd_above_zero', {}, 350),
        ('supertrend', {'atr_period':10}, wilder_bars(10)),
        ('adx_di_cross', {'adx_period':14}, wilder_bars(14, stages=2)),
        ('ema_trend_rsi', {'ema_fast':2,'ema_slow':3,'rsi_period':100}, wilder_bars(100)+1),
    ]:
        built = provider.TOOL_SPECS['local.backtesting_py.'+name]['build'](params)
        assert built['required_warmup'] == required, name
        assert built['min_bars'] < required
        assert built['warmup_rule_version'] == 'template-warmup.v1'
    assert (1-1/14)**(wilder_bars(14)-1) <= 1e-9
    assert (1-1/14)**(wilder_bars(14)-2) > 1e-9


def test_filter_budget_uses_same_recursive_rules():
    from strategy_entry_filters import FilterConfig
    from warmup_rules import wilder_bars
    assert FilterConfig(ema_enabled=True,ema_period=20).required_warmup == 200
    assert FilterConfig(macd_enabled=True,macd_slow=26).required_warmup == 260
    assert FilterConfig(supertrend_enabled=True,supertrend_atr_period=10).required_warmup == wilder_bars(10)


def test_over_limit_is_rejected_before_fetch(client, monkeypatch):
    monkeypatch.setattr(provider, 'EMA_WARMUP_MULTIPLIER', 2000)
    calls = []
    monkeypatch.setattr(provider, '_fetch_ohlcv', lambda *args: calls.append(args))
    failed = request(client)
    assert failed['error_type'] == 'INVALID_PARAMS'
    assert not calls


def test_mismatched_warmup_source_is_fetch_failure(client, monkeypatch):
    good = fetcher(frame([100.]*200,[100.]*500))
    def fetch(*args):
        data = good(*args)
        data.attrs['cutie_data_source'] = 'other' if args[-2]<START else 'central_market_data'
        return data
    monkeypatch.setattr(provider, '_fetch_ohlcv', fetch)
    assert request(client)['error_type'] == 'WARMUP_FETCH_FAILED'


def test_higher_timeframe_recursive_prefix_is_strict_and_bound(client, monkeypatch):
    full = frame([100.]*250, [100.]*500)
    coarse = full.resample('2h').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'})
    def fetch(exchange,market,symbol,timeframe,start,end):
        return fetcher(coarse if timeframe=='2h' else full)(exchange,market,symbol,timeframe,start,end)
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetch)
    params=dict(ema_fast=5,ema_slow=20,filter_layer_enabled=True,filter_ema_enabled=True,
                filter_ema_period=2,filter_timeframe='2h')
    body=request(client,params=params)
    assert body['result_status']=='success',body
    manifest=body['assumptions']['entry_filter_warmup_manifest']
    assert manifest['required_bars']==manifest['actual_bars']==20
    assert manifest['timeframe']=='2h'
    assert canonical_json_sha256(manifest)==body['assumptions']['entry_filter_warmup_manifest_hash']
    # The current primary candle cannot provide any of the closed coarse seed.
    coarse=coarse.drop(pd.to_datetime(START-10*STEP,unit='s'))
    failed=request(client,params=params)
    assert failed['error_type']=='WARMUP_INSUFFICIENT'
    assert failed['limitations']['suggested_start_at'] is None
    def broken(*args):
        if args[3]=='2h': raise RuntimeError('private')
        return fetch(*args)
    monkeypatch.setattr(provider,'_fetch_ohlcv',broken)
    failed=request(client,params=params)
    assert failed['error_type']=='WARMUP_FETCH_FAILED'
    assert 'missing_bars' not in failed['limitations']


def test_demo_snapshot_matches_complete_seed_golden(client, monkeypatch):
    from pathlib import Path
    full=frame([100+.01*i+12*math.sin(i/17) for i in range(-6000,0)],
               [100+.01*i+12*math.sin(i/17) for i in range(500)])
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full))
    monkeypatch.setattr(provider,'PROVIDER_REVISION','fixture-warmup-b')
    golden=json.loads((Path(__file__).parent/'fixtures/warmup_b_demo_v1.json').read_text())
    for name,expected in golden['cases'].items():
        body=request(client,name=name,params=expected['params'])
        assert body['result_status']=='success',body
        assert body['trades']==expected['trades']
        assert canonical_json_sha256({k:body[k] for k in V2_KEYS})==expected['canonical_result_hash']
        assert body['assumptions']['warmup_manifest']==expected['warmup_manifest']
        assert body['assumptions']['warmup_manifest_hash']==expected['warmup_manifest_hash']


def test_weekly_seed_uses_monday_grid(monkeypatch, tmp_path):
    main=frame([], [100.]*25)
    main.index=pd.date_range('2026-01-05',periods=25,freq='W-MON')
    warm=frame([], [100.]*200)
    warm.index=pd.date_range(end=main.index[0]-pd.Timedelta(weeks=1),periods=200,freq='W-MON')
    full=pd.concat([warm,main])
    start=int(main.index[0].timestamp());end=int((main.index[-1]+pd.Timedelta(weeks=1)).timestamp())
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full))
    monkeypatch.setattr(provider,'WARMUP_SNAPSHOT_DIR',tmp_path/'weekly')
    seed=provider._fetch_template_warmup('binance','spot','BTCUSDT','1w',start,200,main,end_sec=end,minimum=21)
    evidence=provider._warmup_evidence(seed,main,200,'BTCUSDT','spot','1w',start,'binance')
    manifest=evidence['warmup_manifest']
    assert manifest['end_at']==start
    assert manifest['start_at']==int(warm.index[0].timestamp())
    assert len(provider._load_warmup_snapshot(manifest))==200
    assert len(manifest)==14


def test_frozen_seed_replays_after_source_revision(client, monkeypatch, tmp_path):
    full=frame([180.]*180+[110.]*20,[100.+20*math.sin(i/12) for i in range(500)])
    monkeypatch.setattr(provider,'WARMUP_SNAPSHOT_DIR',tmp_path/'frozen')
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full))
    original=request(client)
    manifest=original['assumptions']['warmup_manifest']
    manifest_hash=original['assumptions']['warmup_manifest_hash']
    full.loc[full.index< pd.to_datetime(START,unit='s'),'Close']=1e6
    calls=[]
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full,calls))
    replay=request(client,warmup_manifest=manifest,warmup_manifest_hash=manifest_hash)
    assert replay['result_status']=='success',replay
    assert calls==[(START,START+500*STEP)]
    assert replay['trades']==original['trades']
    assert replay['assumptions']['warmup_manifest_hash']==manifest_hash
    assert canonical_json_sha256({k:replay[k] for k in V2_KEYS})==canonical_json_sha256({k:original[k] for k in V2_KEYS})
    for field,new in [('required_bars',199),('seed_rule_version','bad-version'),('checksum','0'*64)]:
        altered={**manifest,field:new}
        failed=request(client,warmup_manifest=altered,warmup_manifest_hash=manifest_hash)
        assert failed['error_type']=='INVALID_PARAMS'


def test_persistence_failure_is_not_history_fetch_failure(client, monkeypatch):
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(frame([100.]*200,[100.]*500)))
    def broken(*args): raise OSError('private filesystem error')
    monkeypatch.setattr(provider,'_warmup_evidence',broken)
    failed=request(client)
    assert failed['error_type']=='ENGINE_ERROR'
    assert failed['limitations']['reason']=='warmup_evidence_failed'
    assert 'private' not in failed['error_message']


def test_leading_main_gap_is_not_warmup_shortage(client, monkeypatch):
    full=frame([100.]*200,[100.]*500).drop(pd.to_datetime(START,unit='s'))
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full))
    failed=request(client)
    assert failed['error_type']=='TIME_DATA_GAP'
    assert not {'missing_bars','suggested_start_at'} & failed['limitations'].keys()


def test_dynamic_vwap_shortage_never_emits_unproved_hint(client, monkeypatch):
    full=frame([],[100.]*500).drop(pd.to_datetime(START,unit='s'))
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(full))
    failed=request(client,start=START+20*STEP,name='vwap_reversion',params={})
    assert failed['error_type']=='WARMUP_INSUFFICIENT',failed
    assert failed['limitations']['suggested_start_at'] is None


def test_risk_atr_uses_wilder_budget(client, monkeypatch):
    from warmup_rules import wilder_bars
    required=wilder_bars(14)
    monkeypatch.setattr(provider,'_fetch_ohlcv',fetcher(frame([100.]*(required-1),[100.]*500)))
    failed=request(client,params=dict(lookback=10,volume_avg_period=5,exit_ema=5,
        risk_layer_enabled=True,atr_stop_multiplier=2,risk_atr_period=14),name='volume_breakout')
    assert failed['error_type']=='WARMUP_INSUFFICIENT',failed
    assert failed['limitations']['required_bars']==required
    assert failed['limitations']['missing_bars']==1
