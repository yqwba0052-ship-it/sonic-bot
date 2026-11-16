"""Freqtrade strategy used ONLY to run FreqAI's walk-forward training and dump its predictions.

It never trades (no entry or exit signals): Sonic's verified bridge does the trading later.
Features use the past only; the target looks 24 candles ahead and FreqAI computes it per window
on data cut at the training window's end (so the last 24 training rows have no target).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame

LABEL_CANDLES = 24
ROUND_TRIP_COST = 0.0014  # 2 x (0.05% fee + 0.02% slippage), as in Sonic's paper exchange


def sonic_features(df: DataFrame) -> DataFrame:
    """Past-only features of one candle series; names start with '%' as FreqAI requires."""
    close = df["close"]
    out = {}
    for n in (1, 6, 24):
        out[f"%-ret{n}"] = np.log(close / close.shift(n))
    for n in (20, 100, 200):
        out[f"%-ema{n}_gap"] = close / ta.EMA(df, timeperiod=n) - 1
    ema20 = ta.EMA(df, timeperiod=20)
    out["%-ema20_slope"] = ema20 / ema20.shift(6) - 1
    out["%-rsi14"] = ta.RSI(df, timeperiod=14)
    out["%-atr14_pct"] = ta.ATR(df, timeperiod=14) / close
    out["%-adx14"] = ta.ADX(df, timeperiod=14)
    out["%-vol24"] = np.log(close / close.shift(1)).rolling(24).std()
    out["%-volume_ratio30"] = df["volume"] / df["volume"].rolling(30).mean()
    roc6 = close / close.shift(6) - 1
    out["%-mom_change"] = roc6 - roc6.shift(6)
    return df.assign(**out)


def sonic_target(df: DataFrame) -> DataFrame:
    df["&-fwd_ret"] = np.log(df["close"].shift(-LABEL_CANDLES) / df["close"]) - ROUND_TRIP_COST
    return df


class SonicFreqAI(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "4h"
    can_short = False
    minimal_roi = {"0": 100.0}
    stoploss = -0.99
    startup_candle_count = 240
    process_only_new_candles = True

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, metadata: dict,
                                       **kwargs) -> DataFrame:
        return dataframe

    def feature_engineering_expand_basic(self, dataframe: DataFrame, metadata: dict,
                                         **kwargs) -> DataFrame:
        return sonic_features(dataframe)

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict,
                                     **kwargs) -> DataFrame:
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict, **kwargs) -> DataFrame:
        return sonic_target(dataframe)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        out_dir = self.config.get("sonic_predictions_dir")
        if out_dir:
            keep = dataframe[["date", "close", "&-fwd_ret", "do_predict"]].copy()
            path = Path(out_dir) / (metadata["pair"].replace("/", "_") + ".csv")
            path.parent.mkdir(parents=True, exist_ok=True)
            keep.to_csv(path, index=False)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 0
        return dataframe
