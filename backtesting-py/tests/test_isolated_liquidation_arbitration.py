"""SHORT-T2-2b: real broker arbitration, independent scalar answers, HTTP wiring."""
from __future__ import annotations
import sys
import json
import subprocess
import types
from decimal import Decimal as D
from pathlib import Path
import pandas as pd
import pytest
from backtesting import Backtest, Strategy
from fastapi.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cutie_backtesting_provider as p
import test_risk_overlay_compatibility as compat
from canonical_json import canonical_json
from strategy_time_layer import TimeConfig, TimeContext
from test_isolated_liquidation_settlement import server_recompute
from test_leverage_params import request

START, STEP = 1800000000, 3600
WAVEB_GOLDEN = json.loads((Path(__file__).parent/'fixtures/isolated_off_waveb_e698d26_6b85dbf_9f8a6b0.json').read_text())
# 9-T4 / 9-T6 OFF-state golden, captured once at each source head (fd52acb, ab56369) with capture_isolated_off_waveb.py.
WAVEC_GOLDEN = json.loads((Path(__file__).parent/'fixtures/isolated_off_wavec_fd52acb_ab56369.json').read_text())


SHORT_PAT1_GOLDEN = json.loads((Path(__file__).parent/'fixtures/short_pat1_off.json').read_text())




# Wave-B 时未接过滤层的 12 个工具（原 test_entry_filters.FROZEN_UNWIRED_WAVE_B；7-P4 名单清空时冻结集合到期删除，此处内联）。
WAVEB_TOOLS = frozenset((
    'opening_range_breakout asia_range_breakout calendar_schedule red_streak_rsi bullish_engulfing '
    'hammer_pin_bar morning_star three_white_soldiers bullish_doji_reversal inside_bar_breakout '
    'double_bottom inverse_head_shoulders').split())


def test_waveb_golden_covers_exactly_the_filter_unwired_tools():
    assert set(WAVEB_GOLDEN['cases'])==WAVEB_TOOLS
    # Keep Wave-B bytes intact; the two futures-only SHORT-PAT-1 tools have their own head golden.
    # 7-P4 起过滤层名单为空：Wave-B 与做空一的工具都已接过滤层，其关态金样不变。
    assert not p.FILTER_LAYER_UNWIRED_TOOLS
    wired = set(WAVEB_GOLDEN['cases']) | set(SHORT_PAT1_GOLDEN['single'])
    assert all(getattr(p.TOOL_SPECS['local.backtesting_py.'+name]['build'], '_supports_entry_filters', False) for name in wired)
    assert all(set(v)=={'futures','spot'} for v in WAVEB_GOLDEN['cases'].values())


def test_wavec_golden_comes_from_source_heads():
    assert WAVEC_GOLDEN['source_shas'][0].startswith('fd52acb') and WAVEC_GOLDEN['source_shas'][1].startswith('ab56369')
    assert set(WAVEC_GOLDEN['cases'])=={'macd_bullish_divergence','rsi_bullish_divergence','chan_3buy'}
    assert set(WAVEC_GOLDEN['cases']).isdisjoint(WAVEB_GOLDEN['cases'])
    assert all(set(v)=={'futures','spot'} for v in WAVEC_GOLDEN['cases'].values())
FLAT = [100, 101, 99, 100]
SIDES = ['long', 'short']


def frame(rows):
    df = pd.DataFrame(rows, columns=['Open','High','Low','Close'], dtype=float,
        index=pd.to_datetime([START+i*STEP for i in range(len(rows))], unit='s'))
    df['Volume'] = 1.
    return df


def run(extra=None, *, side='long', rows=None, signal=False, reentry=False, fee=0, slip=0, cash=1000):
    data = frame(rows or [FLAT]*3+[[100,131,69,100]]+[FLAT]*4)
    params = {'leverage':5, 'position_size_pct':20, **(extra or {})}
    config = TimeConfig.parse(params)
    class Manual(p._FixedRiskMixin, Strategy):
        _risk = p._parse_fixed_risk_params(params)
        _time_config = config if config.enabled else None
        _time_context = TimeContext.build(config,'1h',data.index) if config.enabled else None
        _isolated_fee_bps, _isolated_slippage_bps = D(fee), D(slip)
        def init(self):
            self._risk_init()
            self.decisions, self.signal_orders, self.blocked_orders = {}, 0, []
        def next(self):
            i = len(self.data)-1
            if self.position:
                done = self._risk_check_exit()
                self.decisions[i] = (done,getattr(self,'_risk_exit_reason',None))
                if done:
                    before = len(self.orders)
                    assert self._risk_check_exit() and len(self.orders)==before
                    if reentry and self._risk_exit_reason=='liquidation':
                        self._risk_buy()
                        self._risk_sell()
                        self.blocked_orders.append(len(self.orders))
                    return
                if signal and i>=3:
                    self.signal_orders += 1
                    self.position.close()
                    return
            if i==1 or (reentry and i==4 and not self.position):
                (self._risk_buy if side=='long' else self._risk_sell)()
    stats = Backtest(data,Manual,cash=cash,commission=fee/10000,margin=.2,
        exclusive_orders=True,finalize_trades=True).run()
    obj = stats['_strategy']
    result = p._build_result_v2(stats_trades=stats['_trades'],equity_scale_dec=D(1),fee_bps=D(fee),
        slippage_bps=D(slip),initial_capital=D(cash),start_at=START,end_at=START+len(data)*STEP,
        symbol='BTCUSDT',market='futures',timeframe='1h',exchange_id='binance',df=data,
        leverage=5,liquidations=obj._isolated_liquidations,
        liquidation_units=obj._isolated_liquidation_units)
    report = p._build_isolated_risk_report(result['trades'],leverage=5,market='futures',
        df=data,step=STEP,liquidations=obj._isolated_liquidations,
        liquidation_units=obj._isolated_liquidation_units)['isolated_risk']
    return stats,result,report


def expect_liquidation(extra=None, *, side='long', rows=None, signal=False, gap=False, price=None):
    stats,result,report = run(extra,side=side,rows=rows,signal=signal)
    obj = stats['_strategy']
    assert obj.decisions[3]==(True,'liquidation')
    assert obj.signal_orders==0
    assert obj._isolated_liquidations==[{'opened_at':START+2*STEP,'liquidation_bar_open_time':START+3*STEP}]
    assert result['trades'][0]['exit_price']==(price or ('80' if side=='long' else '120'))
    assert result['trades'][0]['closed_at']==START+3*STEP
    assert report['liquidation_count']==1
    assert report['liquidations'][0]['liquidation_gap'] is gap
    return stats,result,report


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_gap_open_beyond_liquidation(side):
    bar=[70,101,69,100] if side=='long' else [130,131,99,100]
    expect_liquidation(dict(risk_layer_enabled=True,stop_loss_pct=10),side=side,
        rows=[FLAT]*3+[bar]+[FLAT]*4,gap=True,price='70' if side=='long' else '130')


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_stop_nearer_than_liquidation(side):
    stats,result,report=run(dict(risk_layer_enabled=True,stop_loss_pct=10),side=side)
    assert stats['_strategy'].decisions[3]==(True,'stop_loss')
    assert report['liquidation_count']==0
    assert result['trades'][0]['closed_at']==START+4*STEP


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_stop_beyond_liquidation(side):
    expect_liquidation(dict(risk_layer_enabled=True,stop_loss_pct=25),side=side)


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_stop_equal_liquidation(side):
    expect_liquidation(dict(risk_layer_enabled=True,stop_loss_pct=20),side=side)


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_take_profit_and_liquidation(side):
    expect_liquidation(dict(risk_layer_enabled=True,take_profit_pct=10),side=side)


@pytest.mark.parametrize('risk_layer',[True,False],ids=['risk','legacy'])
def test_same_bar_time_expiry_and_liquidation(risk_layer):
    expect_liquidation(dict(risk_layer_enabled=risk_layer,time_layer_enabled=True,max_holding_bars=2))


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_signal_exit_and_liquidation(side):
    expect_liquidation(dict(risk_layer_enabled=True),side=side,signal=True)


@pytest.mark.parametrize('side',SIDES)
def test_same_bar_legacy_close_stop(side):
    bar=[100,131,69,85] if side=='long' else [100,131,69,115]
    expect_liquidation(dict(stop_loss_pct=10),side=side,rows=[FLAT]*3+[bar]+[FLAT]*4)


def test_dynamic_atr_stop_beyond_liquidation():
    stats,_,_=expect_liquidation(dict(risk_layer_enabled=True,atr_stop_multiplier=12,risk_atr_period=2))
    assert stats['_strategy']._risk_state.initial_stop==D(76)
    assert stats['_strategy']._isolated_stop_beyond_trades==1


@pytest.mark.parametrize('kind',['trailing','breakeven'])
def test_dynamic_effective_stop_nearer_than_liquidation(kind):
    params=dict(risk_layer_enabled=True,stop_loss_pct=25)
    params.update(trailing_stop_pct=25) if kind=='trailing' else params.update(breakeven_stop=True)
    advance=[100,110,99,110] if kind=='trailing' else [100,126,99,125]
    stats,_,report=run(params,rows=[FLAT]*3+[advance,[110,131,69,100]]+[FLAT]*3)
    obj=stats['_strategy']
    assert obj.decisions[4]==(True,'stop_loss')
    assert obj._risk_state.stop_state.effective_stop==(D('82.5') if kind=='trailing' else D(100))
    assert obj._isolated_stop_beyond_trades==1  # frozen 75, not later effective stop
    assert report['liquidation_count']==0


@pytest.mark.parametrize('gap,quantity,pnl',[(False,8,'-200'),(True,7,'-300')],ids=['non_gap','gap'])
def test_liquidation_reentry_uses_rewritten_equity(gap,quantity,pnl):
    bar=[70,101,69,100] if gap else [100,101,69,100]
    stats,result,report=run(rows=[FLAT]*3+[bar,[110,111,99,100]]+[FLAT]*3,reentry=True)
    obj=stats['_strategy']
    assert obj.blocked_orders==[1]
    assert list(stats['_trades'].EntryBar)==[2,5]
    assert list(abs(stats['_trades'].Size))==[10,quantity]
    assert result['trades'][0]['pnl']==pnl
    assert result['trades'][1]['qty']==str(quantity)
    assert obj._broker._cash==(700 if gap else 800)
    assert report['liquidation_count']==1


@pytest.mark.parametrize('time_layer',[False,True])
def test_legacy_no_stops_still_liquidates(time_layer):
    expect_liquidation(dict(time_layer_enabled=time_layer))


def http(monkeypatch,tmp_path,data,params):
    monkeypatch.setattr(p,'AUTH_TOKEN','')
    monkeypatch.setattr(p,'REPORTS_DIR',tmp_path)
    monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**k:data.copy())
    monkeypatch.setattr(p,'_fetch_template_warmup',lambda *a,**k:data.iloc[:0].copy())
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k:None)
    body=request(params,market='futures')
    body['backtest'].update(start_at=START,end_at=START+len(data)*STEP,initial_capital='1000',fee_bps='10',slippage_bps='5')
    response=TestClient(p.app).post('/cutie/backtest',json=body)
    assert response.status_code==200
    result=response.json()
    assert result['result_status']=='success',result
    return result


def margin_assumptions(stop=False,count=0):
    return {'leverage':5,'mmr':'0','funding_rate_included':False,'fees_in_liquidation_price':False,
        'liquidation_price_formula':'long: E*(1-1/L); short: E*(1+1/L)',
        'gap_fill':'跳空按开盘价成交、result.v2 按冻结公式可超保证金、逐仓封顶见 raw_report',
        'stop_beyond_liquidation':stop,'stop_beyond_liquidation_trades':count,
        'stop_beyond_liquidation_trades_definition':
        '启用 ATR/移动/保本的开仓笔数：初始止损价在爆仓价之外（含相等）；不计后续移动止损'}


def test_http_registered_template_liquidations_and_server_recompute(monkeypatch,tmp_path):
    data=frame([FLAT,FLAT,[100,101,98,99],[100,103,99,102],[100,103,70,100],FLAT,FLAT,
                [100,103,99,102],[100,104,99,103],[70,101,69,100],FLAT,FLAT])
    body=http(monkeypatch,tmp_path,data,dict(leverage=5,ema_fast=2,ema_slow=3,position_size_pct=20))
    server_recompute(body,data)
    assert body['assumptions']['isolated_margin']==margin_assumptions()
    # Cash scale=1/10300. q1=102897/10300=9.99. Equity after -202.4973 = 797.5027.
    # Next units=floor(797.5027*10300/100.1)=82060; q2=82060/10300.
    assert body['raw_report']['isolated_risk']=={
        'leverage':5,'liquidation_count':2,'liquidated_margin_total':'359.1398058252427184466019417',
        'liquidation_gap_count':1,'loss_beyond_margin_total':'79.66990291262135922330097087',
        'liquidations':[
            {'seq':1,'liquidation_price':'80','fill_price':'80','liquidation_gap':False,
             'margin_lost':'199.8','loss_beyond_margin':'0'},
            {'seq':2,'liquidation_price':'80','fill_price':'70','liquidation_gap':True,
             'margin_lost':'159.3398058252427184466019417','loss_beyond_margin':'79.66990291262135922330097087'}]}


def test_http_raw_stop_percentage_and_dynamic_count(monkeypatch,tmp_path):
    data=frame([FLAT,FLAT,[100,101,98,99],[100,103,99,102],FLAT,FLAT,FLAT])
    body=http(monkeypatch,tmp_path,data,dict(leverage=5,ema_fast=2,ema_slow=3,
        position_size_pct=20,risk_layer_enabled=True,stop_loss_pct=20,breakeven_stop=True))
    assert body['assumptions']['isolated_margin']==margin_assumptions(True,1)
    body=http(monkeypatch,tmp_path,data,dict(leverage=5,ema_fast=2,ema_slow=3,position_size_pct=20,stop_loss_pct=2))
    assert body['assumptions']['isolated_margin']==margin_assumptions(False,0)
    body=http(monkeypatch,tmp_path,data,dict(leverage=5,ema_fast=2,ema_slow=3,position_size_pct=20,stop_loss_pct=20))
    assert body['assumptions']['isolated_margin']==margin_assumptions(True,0)


@pytest.fixture(scope='module')
def baseline_provider():
    # Read immutable source from the assigned starting SHA, never regenerate a fixture.
    source=subprocess.check_output(['git','show','e25886e:backtesting-py/cutie_backtesting_provider.py'],text=True)
    module=types.ModuleType('t22b_baseline_provider')
    module.__file__=p.__file__
    sys.modules[module.__name__]=module
    exec(compile(source,module.__file__,'exec'),module.__dict__)
    return module


# New calendar / short-pattern tools did not exist at frozen e25886e; their L=1 proof is in their route suite.
@pytest.mark.parametrize('name',[name for name in compat.enumerate_mixin_cases() if name not in ('us_open_momentum', 'cme_weekend_gap', 'macd_bearish_divergence', 'rsi_bearish_divergence', 'double_top', 'head_shoulders', 'chan_3sell',
                                  'bearish_engulfing', 'shooting_star', 'evening_star',
                                  'three_black_crows', 'bearish_doji_reversal',
                                  # P-EVENT0: event_window postdates e25886e (no baseline output to compare)
                                  # and needs inline events; its L=1 off state is pinned byte-for-byte by
                                  # tests/fixtures/event0_window_golden.json (test_event0_event_window.py).
                                  'event_window')])  # SHORT-PAT-4: futures-only, see test_short_pat4_candles
@pytest.mark.parametrize('market,extra',[('futures',{}),('futures',{'leverage':1}),('spot',{})],
                         ids=['default','leverage_one','spot'])
def test_runtime_mixin_off_state_bytes(monkeypatch,tmp_path,baseline_provider,name,market,extra):
    golden=WAVEB_GOLDEN['cases'].get(name) or WAVEC_GOLDEN['cases'].get(name)
    data=compat.frame().iloc[WAVEB_GOLDEN['data_offset'].get(name,60):].copy()
    params={'direction':'short'} if name.endswith('_short') else {}
    params.update(WAVEB_GOLDEN['tool_params'].get(name,{}))
    if market == 'spot' and name == 'ema_pullback_short':
        # P-GATE1: spot EMA pullback short is rejected before fetch, so there is no off-state run to compare.
        monkeypatch.setattr(p,'_fetch_ohlcv',lambda *a,**k:pytest.fail('spot ema_pullback short must not fetch'))
        req=request(params,market=market,name=compat.tool_name(name))
        out=TestClient(p.app).post('/cutie/backtest',json=req).json()
        assert (out['error_type'],out['error_message'])==('INVALID_PARAMS','short/both direction requires futures market')
        return
    def forbidden(*a,**k):
        pytest.fail('L=1 called liquidation arbitration or settlement installation')
    monkeypatch.setattr(p,'_isolated_liquidation_candidate',forbidden)
    monkeypatch.setattr(p._FixedRiskMixin,'_isolated_install_settlement',forbidden)
    monkeypatch.setattr(Backtest,'plot',lambda *a,**k:None)
    def invoke(module):
        monkeypatch.setattr(module,'_fetch_ohlcv',lambda *a,**k:data.copy())
        monkeypatch.setattr(module,'_fetch_template_warmup',lambda *a,**k:pd.DataFrame())
        monkeypatch.setattr(module,'REPORTS_DIR',tmp_path)
        req=request({**params,**extra},market=market,name=compat.tool_name(name))
        req['backtest'].update(start_at=int(data.index[0].timestamp()),end_at=int(data.index[-1].timestamp())+STEP)
        out=TestClient(module.app).post('/cutie/backtest',json=req).json()
        assert out['result_status']=='success',out
        return out
    result=invoke(p)
    if name == 'ema_cross':
        # Validate the added S4b disclosure separately, then keep the exact
        # historical comparator for every pre-existing field and result.v2.
        assert result['assumptions'].pop('ema_warmup') == 'EMA 预热取 10×最长周期（目标 600 根，实得 0 根）'
        assert result['raw_report'].pop('ema_warmup') == dict(requested_bars=600,
            target_bars=600, actual_bars=0, truncated=False, tenfold_reached=False)
    v2=('schema_version','trades','equity_curve','metrics','data_manifest')
    if name in ('vwap_reversion', 'fibonacci_retracement') or golden:
        # F5, 9T5 and the Wave-B tools did not exist at e25886e: compare their independent immutable goldens
        # (each captured at its own feature head), keeping the historical-source comparator for every other tool.
        import hashlib
        from pathlib import Path
        if golden:
            expected=golden[market]
        elif name == 'fibonacci_retracement':
            expected=json.loads((Path(__file__).parent/'fixtures/isolated_off_9t5_b42210b.json').read_text())['cases'][market]
        else:
            expected=json.loads((Path(__file__).parent/'fixtures/isolated_off_f5_b42210b.json').read_text())['cases'][market]
        digest=lambda value:hashlib.sha256(value.encode()).hexdigest()
        assert digest(canonical_json({k:result[k] for k in v2}))==expected['v2']
        for key in ('assumptions','raw_report'):
            assert digest(json.dumps(result[key],sort_keys=True,separators=(',',':')))==expected[key]
    else:
        baseline=invoke(baseline_provider)
        assert canonical_json({k:result[k] for k in v2})==canonical_json({k:baseline[k] for k in v2})
        for key in ('assumptions','raw_report'):
            assert json.dumps(result[key],sort_keys=True,separators=(',',':'))==json.dumps(baseline[key],sort_keys=True,separators=(',',':'))
    assert 'isolated_margin' not in result['assumptions'] and 'isolated_risk' not in result['raw_report']


def test_liquidation_preserves_unallocated_equity_before_broker_insolvency():
    stats,result,report=run(dict(position_size_pct=50),
        rows=[FLAT]*3+[[100,101,49,50],[110,111,99,100]]+[FLAT]*3,reentry=True)
    assert result['trades'][0]['pnl']=='-500'
    assert list(abs(stats['_trades'].Size))==[25,12]
    assert stats['_strategy']._broker._cash==500
    assert report['liquidation_count']==1


def test_partial_take_profit_then_liquidation_only_settles_remaining_units():
    stats,result,report=run(dict(risk_layer_enabled=True,stop_loss_pct=20,
        tp1_r=1,tp1_close_pct=50,tp2_r=2,tp2_close_pct=50),
        rows=[FLAT]*3+[[100,121,99,120],[100,101,69,100]]+[FLAT]*3)
    assert list(abs(stats['_trades'].Size))==[5,5]
    assert [t['pnl'] for t in result['trades']]==['0','-100']
    assert [t['exit_price'] for t in result['trades']]==['100','80']
    assert stats['_strategy']._broker._cash==900
    assert report=={'leverage':5,'liquidation_count':1,'liquidated_margin_total':'100',
        'liquidation_gap_count':0,'loss_beyond_margin_total':'0','liquidations':[
        {'seq':2,'liquidation_price':'80','fill_price':'80','liquidation_gap':False,
         'margin_lost':'100','loss_beyond_margin':'0'}]}
