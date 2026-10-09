"""Independent DST anchors and actual registered next-open execution."""
from pathlib import Path
import sys
import pandas as pd
import pytest
from backtesting import Backtest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
from test_f3_us_open import response as calendar_response
NAME = 'cme_weekend_gap'


def frame(friday='2026-03-06', sunday='2026-03-08', friday_clock='22:00', sunday_clock='22:00', price=99):
    data = pd.DataFrame(dict(Open=float(price), High=price+.1, Low=price-.1, Close=float(price), Volume=100.),
        index=pd.date_range(friday, periods=7*24, freq='h'))
    data.loc[pd.Timestamp(friday+' '+friday_clock)-pd.Timedelta(hours=1), ['Open','High','Low','Close']] = [100,100.5,99.5,100]
    return data


def response(data, params=None):
    return calendar_response(data, params, NAME, '1h')


@pytest.mark.parametrize('friday,sunday,friday_clock,sunday_clock', [
    ('2026-03-06','2026-03-08','22:00','22:00'),
    ('2026-10-30','2026-11-01','21:00','23:00'),
])
@pytest.mark.parametrize('price,side', [(99,'long'),(101,'short')])
def test_dst_equal_threshold_no_sunday_lookahead_expiry(friday,sunday,friday_clock,sunday_clock,price,side):
    data = frame(friday,sunday,friday_clock,sunday_clock,price)
    # The 17:00-opening bar has a deliberately different Close. It cannot be the entry signal.
    data.loc[sunday+' '+sunday_clock, 'Close'] = 99.5 if side == 'long' else 100.5
    data.loc[sunday+' '+sunday_clock, ['High','Low']] = [max(price,99.5 if side=='long' else 100.5)+.1,min(price,99.5 if side=='long' else 100.5)-.1]
    result = response(data)
    assert result['result_status']=='success',result
    assert len(result['trades']) == 1
    trade = result['trades'][0]
    assert trade['opened_at'] == int(pd.Timestamp(sunday+' '+sunday_clock).timestamp())
    assert trade['side'] == side
    wednesday = '2026-03-11 00:00' if sunday=='2026-03-08' else '2026-11-04 00:00'
    assert trade['closed_at'] == int(pd.Timestamp(wednesday).timestamp())
    item=result['raw_report'][NAME]['entries'][0]
    assert item['friday_close_at'] == int(pd.Timestamp(friday+' '+friday_clock).timestamp())
    assert item['target'] == '100.0'
    assert result['raw_report'][NAME]['exits'][0]['reason']=='time_expiry'


@pytest.mark.parametrize('clock,reason', [('2026-03-06 21:00','friday_bar_missing'),('2026-03-08 21:00','sunday_bar_missing')])
def test_missing_exact_anchor_skips_without_neighbor(clock,reason):
    result=response(frame().drop(pd.Timestamp(clock)))
    assert result['result_status']=='success',result
    assert not result['trades']
    assert any(item['reason']==reason and item['week']=='2026-03-08' for item in result['raw_report'][NAME]['skipped'])


@pytest.mark.parametrize('price,side,touch', [(99,'long',100),(101,'short',100)])
def test_target_touch_high_low_next_open(price,side,touch):
    data=frame(price=price)
    data.loc['2026-03-09 03:00','High' if side=='long' else 'Low']=touch
    result=response(data)
    assert len(result['trades'])==1
    assert result['trades'][0]['closed_at']==int(pd.Timestamp('2026-03-09 04:00').timestamp())
    assert result['raw_report'][NAME]['exits'][0]['reason']=='gap_filled'


def test_stop_then_expiry_then_profit_and_user_stop_override():
    data=frame()
    data.loc['2026-03-10 23:00',['High','Low']]=[100,96]
    result=response(data)
    assert result['raw_report'][NAME]['exits'][0]['reason']=='stop_loss'
    data.loc['2026-03-10 23:00','Low']=98.8
    assert response(data)['raw_report'][NAME]['exits'][0]['reason']=='time_expiry'
    data=frame()
    data.loc['2026-03-09 03:00','Low']=97.5
    assert response(data)['trades'][0]['closed_at']==int(pd.Timestamp('2026-03-11').timestamp())
    result=response(data,{'stop_loss_pct':1})
    assert result['trades'][0]['closed_at']==int(pd.Timestamp('2026-03-09 04:00').timestamp())
    assert result['raw_report'][NAME]['exits'][0]['reason']=='stop_loss'


@pytest.mark.parametrize('tf,market,params',[('4h','futures',{}),('2h','futures',{}),('1h','spot',{}),
    ('1h','spot',{'direction':'short'}),('1h','spot',{'direction':'both'}),
    ('1h','futures',{'gap_pct':.1}),('1h','futures',{'filter_layer_enabled':True,'filter_ema_enabled':True})])
def test_invalid_before_fetch(monkeypatch,tf,market,params):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a: pytest.fail('invalid request fetched'))
    body={'backtest':dict(run_id='cme-invalid',provider_tool_id='local.backtesting_py.'+NAME,
        provider_params=params,symbol='BTCUSDT',timeframe=tf,market=market,start_at=1772755200,end_at=1773360000,initial_capital='10000')}
    assert TestClient(p.app).post('/cutie/backtest',json=body).json()['error_type']=='INVALID_PARAMS'


def test_direction_and_optional_entry_gates():
    assert not response(frame(price=101),{'direction':'long'})['trades']
    assert calendar_response(frame(),{'direction':'long'},NAME,'1h','spot')['trades']
    assert not response(frame(),dict(direction='long',time_layer_enabled=True,
        time_session_start='01:00',time_session_end='02:00'))['trades']
    assert not response(frame(),dict(direction='long',filter_layer_enabled=True,
        filter_ema_enabled=True,filter_ema_period=2))['trades']


def test_leverage_one_and_off_layers_bytes():
    import json
    from strategy_time_layer import _TIME_PARAM_SCHEMA_PROPERTIES
    from strategy_entry_filters import FILTER_PARAM_SCHEMA_PROPERTIES
    before=response(frame(), {'direction':'long'})
    params=dict(direction='long',leverage=1)
    params.update({k:v['default'] for k,v in _TIME_PARAM_SCHEMA_PROPERTIES.items()})
    params.update({k:v['default'] for k,v in FILTER_PARAM_SCHEMA_PROPERTIES.items()})
    after=response(frame(),params)
    assert before['trades']
    for k in ('schema_version','trades','metrics','data_manifest','equity_curve','assumptions','raw_report'):
        assert json.dumps(before[k],sort_keys=True)==json.dumps(after[k],sort_keys=True)


def test_missing_fill_open_is_not_delayed_entry():
    result = response(frame().drop(pd.Timestamp('2026-03-08 22:00')))
    assert result['result_status'] == 'success', result
    assert not result['trades']
    assert any(x['reason'] == 'entry_fill_bar_missing' for x in result['raw_report'][NAME]['skipped'])
