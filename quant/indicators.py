import numpy as np
import pandas as pd

from quant.config import BAR_MS


def wilder(series, period=14):
    """SMA seed followed by Wilder recursive smoothing; no backfill."""
    seed = series.rolling(period).mean().first_valid_index()
    out = pd.Series(np.nan, index=series.index, dtype=float)
    if seed is None:
        return out
    position = series.index.get_loc(seed)
    values = series.to_numpy()
    output = out.to_numpy(copy=True)
    output[position] = series.iloc[position - period + 1 : position + 1].mean()
    for i in range(position + 1, len(series)):
        output[i] = (output[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(output, index=series.index)


def indicators(candles, *, as_of_ms):
    """Confirmed candles available by as_of only. All rolling calculations are trailing."""
    rows = [c for c in candles if c.confirmed and c.ts + BAR_MS[c.bar] <= as_of_ms]
    if not rows:
        return pd.DataFrame()
    if len({(c.symbol, c.bar) for c in rows}) != 1:
        raise ValueError("mixed instrument/timeframe")
    rows.sort(key=lambda c: c.ts)
    if len({c.ts for c in rows}) != len(rows):
        raise ValueError("duplicate candles")
    if any(b.ts - a.ts != BAR_MS[a.bar] for a, b in zip(rows, rows[1:])):
        raise ValueError("non-contiguous candles")
    df = pd.DataFrame(
        [
            {
                "ts": c.ts,
                **{
                    k: float(getattr(c, k))
                    for k in ("open", "high", "low", "close", "volume", "base_volume", "quote_volume")
                },
            }
            for c in rows
        ]
    )
    close = df.close
    for period in (5, 10, 20, 60):
        df[f"ma{period}"] = close.rolling(period).mean()
    for period in (9, 21):
        df[f"ema{period}"] = close.ewm(span=period, adjust=False, min_periods=period).mean()
    delta = close.diff()
    gain, loss = wilder(delta.clip(lower=0)), wilder(-delta.clip(upper=0))
    df["rsi14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    df.loc[(loss == 0) & (gain > 0), "rsi14"] = 100
    df.loc[(gain == 0) & (loss == 0), "rsi14"] = 50
    df["macd"] = (
        close.ewm(span=12, adjust=False, min_periods=12).mean()
        - close.ewm(span=26, adjust=False, min_periods=26).mean()
    )
    df["macd_signal"] = df.macd.ewm(span=9, adjust=False, min_periods=9).mean()
    df["macd_hist"] = df.macd - df.macd_signal
    std = close.rolling(20).std(ddof=0)
    df["bb_mid"], df["bb_upper"], df["bb_lower"] = df.ma20, df.ma20 + 2 * std, df.ma20 - 2 * std
    tr = pd.concat(
        [df.high - df.low, (df.high - close.shift()).abs(), (df.low - close.shift()).abs()], axis=1
    ).max(axis=1)
    df["atr14"] = wilder(tr)
    day = pd.to_datetime(df.ts, unit="ms", utc=True).dt.floor("D")
    # Actual traded quote/base sums, rather than typical-price approximation.
    df["vwap"] = df.quote_volume.groupby(day).cumsum() / df.base_volume.groupby(day).cumsum().replace(
        0, np.nan
    )
    df["volume_ratio"] = df.volume / df.volume.shift().rolling(20).mean().replace(0, np.nan)
    df["support"] = df.low.shift().rolling(20).min()
    df["resistance"] = df.high.shift().rolling(20).max()
    df["breakout_up"], df["breakout_down"] = close > df.resistance, close < df.support
    return df
