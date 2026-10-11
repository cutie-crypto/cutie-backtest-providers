"""PIN7-G1 grid rule v2: sell one line above the buy line, one lot per line, top line never buys.

Lines for BASE are 100/110/120/130/140/150 (n=5). A close below line k with the reference on k
signals a buy "on line k"; v2 sells that lot only once a close reaches line k+1. Opens equal the
previous close so next-open fills sit next to the trigger closes.
"""
from __future__ import annotations

import sys
from decimal import Context, Decimal, localcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutie_backtesting_provider as provider  # noqa: E402
from scale_in_out_ledger import round_cash  # noqa: E402
from test_grid_template import BASE, CASES, _fetch, _post, _run, client  # noqa: E402,F401

D = Decimal


def _case(closes, *, capital='1000', first_open=None):
    opens = [closes[0] if first_open is None else first_open] + list(closes[:-1])
    return {**CASES[0], 'params': dict(BASE), 'initial_capital': capital, 'opens': opens, 'closes': list(closes)}


def _acts(actions):
    return [(a['bar'], a['action']) for a in actions]


def test_grid_sell_one_line_above_buy_line():
    # bar1 129 < line 3 -> buy on line 3; bars 2-3 close back on line 3 (133/135): v1 sold here
    # for ~zero cell profit, v2 holds; bar4 reaches line 4 (140) -> sell.
    result, a, actions, _ = _run(_case([135, 129, 133, 135, 140, 140]))
    assert _acts(actions) == [(1, 'buy'), (4, 'sell_lot')]
    assert [(t['entry_price'], t['exit_price']) for t in result.trades] == [(D(129), D(140))]
    assert a['grid_fills'] == 2


def test_grid_line_occupied_no_rebuy_until_sold():
    # Line 3 bought on bar1; bar3 dips below line 3 again (128) while that lot is held -> no
    # second lot on line 3. After the bar4 sell (140) the line is free and bar5 rebuys it.
    result, a, actions, _ = _run(_case([135, 129, 133, 128, 140, 129, 129]))
    assert _acts(actions) == [(1, 'buy'), (4, 'sell_lot'), (5, 'buy')]
    assert [t['entry_price'] for t in result.trades] == [D(129), D(129)]
    assert result.buy_fills == 2


def test_grid_cell_profit_avg_close_to_one_step_minus_fees():
    # Three cells on line 3: buy fills at 129, each closes back on line 3 (133) before line 4.
    # Sells fill at 140/141/142, so every cell earns about one 10-wide step (11..13 from 129).
    closes = [135, 129, 133, 140, 129, 133, 141, 129, 133, 142, 142]
    result, a, actions, _ = _run(_case(closes))
    assert [x for x in _acts(actions) if x[1] == 'sell_lot'] == [(3, 'sell_lot'), (6, 'sell_lot'), (9, 'sell_lot')]
    pnls = [t['pnl'] for t in result.trades]
    assert len(pnls) == 3
    fee = D('0.0015')  # 10 bps fee + 5 bps slippage per side
    for pnl, sell in zip(pnls, (140, 141, 142)):
        qty = D(100) / D(129)
        expected = qty * (D(sell) - D(129)) - fee * D(100) - fee * qty * D(sell)
        assert abs(pnl - expected) < D('0.001'), (pnl, expected)
    with localcontext(Context(prec=80)):
        mean = round_cash(sum(pnls, D(0)) / D(3))
    assert D(a['grid_cell_profit_avg']) == mean
    step_minus_fees = D(100) / D(129) * D(10) - fee * D(200)
    assert D(a['grid_cell_profit_avg']) > step_minus_fees > 0


def test_grid_top_line_never_buys():
    # Start above the upper line, then fall through every line: lines 4..1 buy, line 5 (top) never.
    closes = [160, 149, 139, 129, 119, 109, 101, 101]
    result, a, actions, _ = _run(_case(closes, capital='100000'))
    n = BASE['grid_count']
    assert _acts(actions) == [(2, 'buy'), (3, 'buy'), (4, 'buy'), (5, 'buy')]
    assert result.buy_fills == len(result.trades) == n - 1


def test_grid_rule_v2_marker_present(client, monkeypatch):
    _, a, _, _ = _run(CASES[0])
    assert a['grid_rule'] == 'v2'
    monkeypatch.setattr(provider, '_fetch_ohlcv', _fetch(CASES[0]))
    body = _post(client, CASES[0])
    assert body['assumptions']['grid_rule'] == 'v2'
    assert 'grid_rule' not in body['raw_report']


def test_grid_cash_short_does_not_advance_ref():
    # Capital 150 holds one lot. bar1 buys line 3; bar2 (119 < line 2) cannot be paid for, so the
    # reference stays on line 2: after bouncing back onto line 2 (121) the bar4 dip below it
    # signals line 2 again instead of skipping it (v1 / signal-time advance moved ref to 1).
    result, a, actions, fills = _run(_case([135, 129, 119, 121, 119, 119], capital='150'))
    assert _acts(actions) == [(1, 'buy'), (2, 'buy'), (4, 'buy')]
    assert [(i, f) for i, act, f, _ in fills] == [(2, True), (3, False), (5, False)]
    assert result.skipped_buys_insufficient_cash == 2
    assert a['grid_fills'] == 1
