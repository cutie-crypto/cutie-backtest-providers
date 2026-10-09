"""Causal, long-entry-only filters. Disabled configuration has no runtime work."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Mapping

import numpy as np
import pandas as pd

from strategy_time_layer import fixed_timeframe_milliseconds

FILTER_PARAM_SCHEMA_PROPERTIES = {
    'filter_timeframe': {'type': 'string', 'default': '',
                         'enum': ['', '1m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w']},
    'filter_layer_enabled': {'type': 'boolean', 'default': False},
    'filter_ema_enabled': {'type': 'boolean', 'default': False},
    'filter_ema_period': {'type': 'integer', 'default': 200, 'minimum': 2, 'maximum': 500},
    'filter_macd_enabled': {'type': 'boolean', 'default': False},
    'filter_macd_fast': {'type': 'integer', 'default': 12, 'minimum': 2, 'maximum': 100},
    'filter_macd_slow': {'type': 'integer', 'default': 26, 'minimum': 3, 'maximum': 300},
    'filter_supertrend_enabled': {'type': 'boolean', 'default': False},
    'filter_supertrend_atr_period': {'type': 'integer', 'default': 10, 'minimum': 5, 'maximum': 30},
    'filter_supertrend_multiplier': {'type': 'number', 'default': 3, 'minimum': 1, 'maximum': 6},
}


@dataclass(frozen=True)
class FilterConfig:
    timeframe: str = ""
    enabled: bool = False
    ema_enabled: bool = False
    ema_period: int = 200
    macd_enabled: bool = False
    macd_fast: int = 12
    macd_slow: int = 26
    supertrend_enabled: bool = False
    supertrend_atr_period: int = 10
    supertrend_multiplier: float = 3

    @classmethod
    def parse(cls, params: Mapping[str, Any]) -> FilterConfig:
        for key in params:
            if key.startswith('filter_') and key not in FILTER_PARAM_SCHEMA_PROPERTIES:
                raise ValueError(f'INVALID_PARAMS:unknown filter parameter {key}')
        values = {key: params.get(key, spec['default']) for key, spec in FILTER_PARAM_SCHEMA_PROPERTIES.items()}
        for key, spec in FILTER_PARAM_SCHEMA_PROPERTIES.items():
            value = values[key]
            if spec['type'] == 'number':
                try:
                    valid = type(value) in (int, float) and math.isfinite(value)
                except OverflowError:
                    valid = False
            else:
                valid = type(value) is {'boolean': bool, 'integer': int, 'string': str}[spec['type']]
            if not valid:
                raise ValueError(f'INVALID_PARAMS:{key} must be a finite {spec["type"]}')
            if 'enum' in spec and value not in spec['enum']:
                raise ValueError(f'INVALID_PARAMS:{key} must be one of {spec["enum"]}')
            if 'minimum' in spec and not spec['minimum'] <= value <= spec['maximum']:
                raise ValueError(f'INVALID_PARAMS:{key} must be within {spec["minimum"]}-{spec["maximum"]}')
        if not values['filter_layer_enabled'] and any(values[k] != s['default'] for k, s in FILTER_PARAM_SCHEMA_PROPERTIES.items()):
            raise ValueError('INVALID_PARAMS:non-default filter parameters require filter_layer_enabled=true')
        groups = {'ema': ('period',), 'macd': ('fast', 'slow'), 'supertrend': ('atr_period', 'multiplier')}
        for name, options in groups.items():
            if not values[f'filter_{name}_enabled'] and any(
                values[f'filter_{name}_{option}'] != FILTER_PARAM_SCHEMA_PROPERTIES[f'filter_{name}_{option}']['default']
                for option in options
            ):
                raise ValueError(f'INVALID_PARAMS:non-default {name} parameters require filter_{name}_enabled=true')
        if values['filter_layer_enabled'] and not any(values[f'filter_{name}_enabled'] for name in groups):
            raise ValueError('INVALID_PARAMS:enabled filter layer requires at least one filter')
        if values['filter_macd_fast'] >= values['filter_macd_slow']:
            raise ValueError('INVALID_PARAMS:filter_macd_fast must be less than filter_macd_slow')
        return cls(**{key.removeprefix('filter_').replace('layer_enabled', 'enabled'): value for key, value in values.items()})

    def validate_timeframe(self, primary: str) -> int:
        primary_step = fixed_timeframe_milliseconds(primary)
        if self.timeframe and fixed_timeframe_milliseconds(self.timeframe) <= primary_step:
            raise ValueError('INVALID_PARAMS:filter_timeframe must be larger than the primary timeframe')
        return primary_step

    @property
    def required_bars(self) -> int:
        return max(self.ema_period if self.ema_enabled else 0,
                   3 * self.macd_slow if self.macd_enabled else 0,
                   self.supertrend_atr_period + 1 if self.supertrend_enabled else 0)

    def report(self) -> dict[str, Any]:
        predicates = {}
        if self.ema_enabled:
            predicates['ema'] = dict(rule='close > ema', period=self.ema_period, smoothing='ewm_adjust_false')
        if self.macd_enabled:
            predicates['macd'] = dict(rule='dif > 0', fast=self.macd_fast, slow=self.macd_slow)
        if self.supertrend_enabled:
            predicates['supertrend'] = dict(rule='trend == +1', atr_period=self.supertrend_atr_period,
                                           multiplier=self.supertrend_multiplier)
        return dict(combine='AND', direction='long', clock='closed_signal_bar',
                    scope='entry_only', fill='next_bar_open', required_bars=self.required_bars,
                    blocked_signal='discard_without_replay', predicates=predicates)


def entry_mask(config: FilterConfig, columns: Mapping[str, Any], supertrend: Callable) -> np.ndarray:
    """Compute causal prefixes once; indexing belongs to the signal-close adapter."""
    close = np.asarray(columns['Close'], dtype='float64')
    allowed = np.ones(len(close), dtype=bool)
    series = pd.Series(close)
    if config.ema_enabled:
        ema = series.ewm(span=config.ema_period, adjust=False).mean().to_numpy()
        allowed &= np.isfinite(close) & np.isfinite(ema) & (close > ema)
    if config.macd_enabled:
        dif = (series.ewm(span=config.macd_fast, adjust=False).mean()
               - series.ewm(span=config.macd_slow, adjust=False).mean()).to_numpy()
        allowed &= np.isfinite(dif) & (dif > 0)
    if config.supertrend_enabled:
        trend = supertrend(columns['High'], columns['Low'], close,
                           config.supertrend_atr_period, config.supertrend_multiplier)['trend']
        allowed &= np.isfinite(trend) & (trend == 1)
    allowed[:max(0, config.required_bars - 1)] = False
    return allowed


class FilterHistoryError(ValueError):
    """The requested closed higher-timeframe history cannot be proven complete."""


@dataclass(frozen=True)
class HigherTimeframeContext:
    mask: np.ndarray
    report: dict[str, Any]

    @classmethod
    def build(cls, config: FilterConfig, primary: str, opens: pd.DatetimeIndex,
              fetch: Callable[[int, int], pd.DataFrame], supertrend: Callable,
              grid_offset_ms: int = 0) -> HigherTimeframeContext:
        """One strict fetch; align indicator values by close <= primary decision.

        History includes required_bars already closed at the first decision.
        Weekly grids use the provider's Monday UTC anchor. Never fill gaps or
        substitute primary candles. Source tails outside the span are ignored.
        """
        primary_step = config.validate_timeframe(primary)
        step = fixed_timeframe_milliseconds(config.timeframe)
        decisions = opens.as_unit('ns').asi8 // 1000000 + primary_step
        latest_opens = ((decisions - grid_offset_ms) // step) * step + grid_offset_ms - step
        first = int(latest_opens[0] - (config.required_bars - 1) * step)
        last = int(latest_opens[-1])
        if first < 0:
            raise FilterHistoryError('Higher-timeframe history precedes available timestamps')
        try:
            data = fetch(first // 1000, (last + step) // 1000)
        except Exception as exc:
            raise FilterHistoryError('Higher-timeframe history fetch unavailable') from exc
        expected = pd.to_datetime(np.arange(first, last + step, step, dtype=np.int64), unit='ms', utc=True)
        index = pd.to_datetime(data.index, utc=True)
        data = data.loc[(index >= expected[0]) & (index <= expected[-1])].copy()
        actual = pd.to_datetime(data.index, utc=True)
        if not actual.equals(expected):
            raise FilterHistoryError('Higher-timeframe history has missing or misaligned candles')
        if not np.isfinite(data[['Open', 'High', 'Low', 'Close', 'Volume']].to_numpy(dtype='float64')).all():
            raise FilterHistoryError('Higher-timeframe history has non-finite candles')
        coarse = entry_mask(config, data, supertrend)
        closes = expected.as_unit('ns').asi8 // 1000000 + step
        # Exact close boundary is available; the current unfinished candle is not.
        positions = np.searchsorted(closes, decisions, side='right') - 1
        if np.any(positions < config.required_bars - 1):
            raise FilterHistoryError('Higher-timeframe indicator warmup is insufficient')
        return cls(coarse[positions], dict(
            timeframe=config.timeframe, primary_timeframe=primary,
            clock='higher_bar_close_lte_primary_bar_close', boundary='inclusive',
            fetched_bars=len(data), history_start_at=first // 1000,
            history_end_at=(last + step) // 1000,
            data_source=data.attrs.get('cutie_data_source'),
            central_market_data_used=data.attrs.get('cutie_central_market_data_used'),
            market_data_cache_hit=data.attrs.get('cutie_market_data_cache_hit', False)))
