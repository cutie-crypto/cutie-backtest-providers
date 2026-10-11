"""Spot long-only price breakout confirmed by closed Binance USD open interest.

Metric timestamps are interval opens; available_at = ts + interval. Both OI
observations are selected at their respective bar closes, never at future closes.
Orders and low-touch stops fill at the next open through the shared risk mixin.
"""
from copy import deepcopy
from decimal import Decimal

TOOL_NAME = 'oi_breakout'
GAP_REASON = 'open_interest_data_gap'
HISTORY_REASON = 'open_interest_history_unavailable'
HOURLY_EARLIEST_TS = 1767830400  # 2026-01-08 00:00 UTC
SCHEMA = {
    'breakout_lookback_bars': dict(type='integer', default=20, minimum=5, maximum=200),
    'oi_lookback_bars': dict(type='integer', default=20, minimum=2, maximum=200),
    'oi_increase_pct': dict(type='number', default=5, minimum=0.1, maximum=100),
    'exit_ema_period': dict(type='integer', default=20, minimum=2, maximum=200),
    'stop_loss_pct': dict(type='number', default=2, minimum=0.1, maximum=50),
}


class OIBreakoutConfig:
    def __init__(self, params):
        self.values = {k: params.get(k, v['default']) for k, v in SCHEMA.items()}
        for key, spec in SCHEMA.items():
            if spec['type'] == 'integer':
                value = self.values[key]
                number = Decimal(str(value)) if type(value) in (int, float) else None
                if number is None or not number.is_finite() or number != number.to_integral_value():
                    raise ValueError(f'INVALID_PARAMS:{key} must be an integer')
                self.values[key] = int(value)
        self.rows = ()
        self.bar_seconds = None
        self.series = {}
        self.assumptions = {TOOL_NAME: {
            **self.values, 'direction': '做空镜像未开放',
            'entry_rule': 'close_t > max(high[t-breakout_lookback_bars .. t-1]) and '
                          'OI_t / OI_{t-oi_lookback_bars} - 1 >= oi_increase_pct/100; next open buy',
            'exit_rule': 'close < EMA(close, exit_ema_period); next open sell (close_below_ema)',
            'stop_rule': 'stop_loss_pct frozen at actual fill; low touch sells at next open; stop first',
            'open_interest_series': self.series,
        }}

    def need_range(self, opens, step):
        # Include the first decision's baseline, but not the final bar: it has no next open.
        return opens[0] - self.values['oi_lookback_bars'] * step, opens[-2]

    def bind(self, rows, *, symbol, bar_seconds, interval):
        self.rows = tuple(rows)
        self.bar_seconds = bar_seconds
        self.series.update(symbol=symbol, exchange='Binance', metric='open_interest', unit='usd',
                           interval=interval, first_ts=rows[0]['ts'], last_ts=rows[-1]['ts'],
                           count=len(rows), revision=rows[0]['revision'], gap_policy='fail_closed',
                           available_at_rule='available_at = ts + interval; available_at <= bar close')


def make_strategy(mixin, config, risk, initial_capital):
    from backtesting import Strategy
    import pandas as pd

    class OIBreakoutStrategy(mixin, Strategy):
        _risk = risk
        _initial_capital = initial_capital

        def init(self):
            self._risk_init()
            values = config.values
            self._oi_decisions = []
            self._oi_readable = []
            self._oi_older = []
            # Separate causal cursors: a later observation never changes the old baseline.
            current = older = -1
            self._oi_opens = [int(pd.Timestamp(stamp).value // 10**9) for stamp in self.data.index]
            for bar, stamp in enumerate(self._oi_opens):
                close = stamp + (config.bar_seconds or 0)
                old_bar = bar - values['oi_lookback_bars']
                old_close = (self._oi_opens[old_bar] + (config.bar_seconds or 0) if old_bar >= 0
                             else close - values['oi_lookback_bars'] * (config.bar_seconds or 0))
                while current + 1 < len(config.rows) and config.rows[current + 1]['available_at'] <= close:
                    current += 1
                while older + 1 < len(config.rows) and config.rows[older + 1]['available_at'] <= old_close:
                    older += 1
                self._oi_readable.append(current)
                self._oi_older.append(older)
            def prior_high(high):
                return pd.Series(high).shift(1).rolling(values['breakout_lookback_bars']).max().to_numpy()
            def ema(close):
                return pd.Series(close).ewm(span=values['exit_ema_period'], adjust=False,
                                            min_periods=values['exit_ema_period']).mean().to_numpy()
            self._oi_high = self._warm(prior_high, 'High')(self.data.High)
            self._oi_ema = self._warm(ema, 'Close')(self.data.Close)

        def _sizing_template_stop(self, order):
            fill = Decimal(str(self._broker._adjusted_price(order.size, self.data.Open[-1])))
            return fill * (1 - Decimal(str(config.values['stop_loss_pct'])) / 100)

        def _log(self, action, reason, **detail):
            self._oi_decisions.append(dict(
                decision_bar_open=int(pd.Timestamp(self.data.index[-1]).value // 10**9),
                action=action, reason=reason, **detail))

        def next(self):
            bar = len(self.data) - 1
            if self.position:
                self._risk_exit_reason = None
                if self._risk_check_exit():
                    self._log('exit', self._risk_exit_reason or 'risk_exit')
                    return
                if self.data.Close[-1] < self._oi_ema[bar]:
                    self._log('exit', 'close_below_ema')
                    self.position.close()
                return
            if self.orders:
                return
            current, older = self._oi_readable[bar], self._oi_older[bar]
            if current < 0 or older < 0:
                return
            now, old = config.rows[current], config.rows[older]
            base = Decimal(old['value'])
            if base <= 0:
                return  # No defined relative increase against a zero observation.
            increase = Decimal(now['value']) / base - 1
            if (self.data.Close[-1] > self._oi_high[bar]
                    and increase >= Decimal(str(config.values['oi_increase_pct'])) / 100):
                count = len(self.orders)
                self._risk_buy()
                self._log('entry' if len(self.orders) > count else 'entry_blocked',
                          'price_breakout_with_oi_increase', value_ts=now['ts'],
                          baseline_ts=old['ts'], value=now['value'], baseline=old['value'],
                          oi_increase=str(increase))

    return OIBreakoutStrategy


def report(strategy, trades):
    """Derive execution evidence only after result.v2 has settled the trades."""
    decisions = list(strategy._oi_decisions)
    next_opens = dict(zip(strategy._oi_opens, strategy._oi_opens[1:]))
    exits = {next_opens[d['decision_bar_open']]: d['reason'] for d in decisions
             if d['action'] == 'exit' and d['decision_bar_open'] in next_opens}
    return dict(decisions=decisions, executions=[dict(
        seq=t['seq'], opened_at=t['opened_at'], closed_at=t['closed_at'],
        entry_price=t['entry_price'], exit_price=t['exit_price'],
        reason=exits.get(t['closed_at'], 'end_of_data')) for t in trades])


def schema():
    return deepcopy(SCHEMA)
