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



def extend_seed(seed, timeframe, bars, main):
    """Retain an audited prefix, extending only its older edge with flat rows.

    This is declared synthetic prehistory, not a replacement for the real reader.
    Callers must audit the resulting trade/equity/metric fields before use.
    """
    import cutie_backtesting_provider as provider
    if bars <= len(seed):
        return seed.tail(bars).copy() if bars else main.iloc[:0].copy()
    source = seed if len(seed) else main
    step = pd.Timedelta(milliseconds=provider._timeframe_milliseconds(timeframe))
    older = pd.DataFrame([source.iloc[0].to_dict()] * (bars - len(seed)),
        index=pd.date_range(end=source.index[0] - step, periods=bars - len(seed), freq=step))
    result = pd.concat([older, seed])
    result.attrs = main.attrs.copy()
    return result
