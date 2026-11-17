"""Independent scalar reference calculations (stdlib only) for indicator regression."""

import json
import math
import statistics
from pathlib import Path


def ema(values, period):
    result, previous, count = [], None, 0
    for value in values:
        if value is None:
            result.append(None)
            continue
        previous = value if previous is None else previous + 2 / (period + 1) * (value - previous)
        count += 1
        result.append(previous if count >= period else None)
    return result


def smooth(values, period=14):
    result, buffer, previous = [], [], None
    for value in values:
        if value is None:
            result.append(None)
            continue
        buffer.append(value)
        if previous is None and len(buffer) == period:
            previous = statistics.fmean(buffer)
        elif previous is not None:
            previous += (value - previous) / period
        result.append(previous)
    return result


def reference(rows):
    closes = [float(c.close) for c in rows]
    lows, highs = [float(c.low) for c in rows], [float(c.high) for c in rows]
    gains = [None] + [max(0, b - a) for a, b in zip(closes, closes[1:])]
    losses = [None] + [max(0, a - b) for a, b in zip(closes, closes[1:])]
    up, down = smooth(gains), smooth(losses)
    tr = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(rows))
    ]
    e12, e26 = ema(closes, 12), ema(closes, 26)
    macd = [a - b if a is not None and b is not None else None for a, b in zip(e12, e26)]
    rsi = [
        None if a is None else 50 if a == b == 0 else 100 if b == 0 else 100 - 100 / (1 + a / b)
        for a, b in zip(up, down)
    ]
    last = rows[-1]
    same_day = [c for c in rows if c.ts // 86_400_000 == last.ts // 86_400_000]
    base = sum(float(c.base_volume) for c in same_day)
    avgvol = statistics.fmean(float(c.volume) for c in rows[-21:-1])
    mid, std = statistics.fmean(closes[-20:]), statistics.pstdev(closes[-20:])
    signal = ema(macd, 9)[-1]
    result = {f"ma{n}": statistics.fmean(closes[-n:]) for n in (5, 10, 20, 60)}
    result.update(
        ema9=ema(closes, 9)[-1],
        ema21=ema(closes, 21)[-1],
        rsi14=rsi[-1],
        macd=macd[-1],
        macd_signal=signal,
        macd_hist=macd[-1] - signal,
        atr14=smooth(tr)[-1],
        bb_mid=mid,
        bb_upper=mid + 2 * std,
        bb_lower=mid - 2 * std,
        vwap=sum(float(c.quote_volume) for c in same_day) / base if base else None,
        volume_ratio=float(last.volume) / avgvol if avgvol else None,
        support=min(lows[-21:-1]),
        resistance=max(highs[-21:-1]),
    )
    result.update(breakout_up=closes[-1] > result["resistance"], breakout_down=closes[-1] < result["support"])
    return result


def validate_store(store, symbols, *, as_of_ms, report_path):
    from quant.config import BAR_MS
    from quant.indicators import indicators

    checks, errors, max_relative_error = 0, [], 0.0
    windows = {}
    for symbol in symbols:
        for bar, step in BAR_MS.items():
            rows = [c for c in store.candles(symbol, bar, 300, True) if c.ts + step <= as_of_ms]
            if len(rows) < 100:
                raise ValueError(f"not enough actual data: {symbol}/{bar}")
            actual = indicators(rows, as_of_ms=as_of_ms).iloc[-1]
            expected = reference(rows)
            windows[f"{symbol}:{bar}"] = {"count": len(rows), "last_ts": rows[-1].ts}
            for key, value in expected.items():
                checks += 1
                observed = actual[key]
                if value is None:
                    matches = math.isnan(float(observed))
                else:
                    relative = abs(float(observed) - float(value)) / max(1, abs(float(value)))
                    max_relative_error = max(max_relative_error, relative)
                    matches = relative <= 1e-9
                if not matches:
                    errors.append(
                        {
                            "symbol": symbol,
                            "bar": bar,
                            "indicator": key,
                            "actual": str(observed),
                            "expected": str(value),
                        }
                    )
    result = {
        "as_of_ms": as_of_ms,
        "source": "actual stored OKX confirmed candles",
        "reference": "independent Python scalar recurrences and statistics; same documented conventions",
        "checks": checks,
        "max_relative_error": max_relative_error,
        "errors": errors,
        "windows": windows,
    }
    Path(report_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if errors:
        raise AssertionError(f"{len(errors)} indicator reference mismatches")
    return result
