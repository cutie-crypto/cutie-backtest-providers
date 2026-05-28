"""
SampleStrategy - Simple EMA crossover strategy for Freqtrade.

This is a minimal example strategy provided with the Cutie Freqtrade provider.
It uses EMA 20/60 crossover to generate entry/exit signals on spot markets.

Copy this file to your Freqtrade user_data/strategies/ directory to use it.
"""
from freqtrade.strategy import IStrategy
import talib.abstract as ta


class SampleStrategy(IStrategy):
    """EMA crossover strategy: enter when fast EMA crosses above slow EMA."""

    INTERFACE_VERSION = 3

    timeframe = "1h"
    minimal_roi = {"0": 0.1}
    stoploss = -0.05

    # Disable trailing stop for simplicity
    trailing_stop = False

    # Run on all candles (not just new candles)
    process_only_new_candles = True

    # Number of candles the strategy needs before producing valid signals
    startup_candle_count: int = 60

    def populate_indicators(self, dataframe, metadata):
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=60)
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        dataframe.loc[
            (dataframe["ema_fast"] > dataframe["ema_slow"]),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        dataframe.loc[
            (dataframe["ema_fast"] < dataframe["ema_slow"]),
            "exit_long",
        ] = 1
        return dataframe
