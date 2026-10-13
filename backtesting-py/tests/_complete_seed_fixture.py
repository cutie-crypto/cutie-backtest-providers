"""Declared synthetic prehistory for legacy HTTP success fixtures.

The hand-authored main interval stays untouched. This adapter supplies older
constant observations for hand-authored success fixtures. Continuity/source/gap
validation remains covered through the real reader in strict warmup tests.
"""
import pandas as pd


def complete_seed(exchange, market, symbol, timeframe, start, bars, main, **kwargs):
    import cutie_backtesting_provider as provider
    step = pd.Timedelta(milliseconds=provider._timeframe_milliseconds(timeframe))
    seed = pd.DataFrame([main.iloc[0].to_dict()] * bars,
        index=pd.date_range(end=main.index[0] - step, periods=bars, freq=step))
    seed.attrs = main.attrs.copy()
    return seed

