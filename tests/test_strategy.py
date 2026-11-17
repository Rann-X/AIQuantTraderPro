import json
from decimal import Decimal

import numpy as np
import pytest

from quant.config import BAR_MS
from quant.indicators import indicators
from quant.models import Candle
from quant.strategy import StrategyParameters, analyze
from quant.validation import reference


def series(bar="1m", n=130):
    start = 1_700_000_100_000 // BAR_MS[bar] * BAR_MS[bar]
    result = []
    for i in range(n):
        price = Decimal(str(100 + i * 0.03 + np.sin(i) * 0.1))
        result.append(
            Candle(
                symbol="BTC-USDT-SWAP",
                bar=bar,
                ts=start + i * BAR_MS[bar],
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=10,
                base_volume=1,
                quote_volume=price,
                confirmed=True,
            )
        )
    return result


def test_indicators_causal_and_reference():
    rows = series()
    now = rows[99].ts + 60_000
    a = indicators(rows[:100], as_of_ms=now)
    b = indicators(rows, as_of_ms=now)
    assert a.equals(b)
    assert a.iloc[-1].ma5 == pytest.approx(float(sum(c.close for c in rows[95:100]) / 5))
    assert a.iloc[-1].vwap == pytest.approx(sum(float(c.quote_volume) for c in rows[:100]) / 100)
    assert a.iloc[-1].support == min(float(c.low) for c in rows[79:99])
    assert 0 <= a.iloc[-1].rsi14 <= 100
    assert all(k in a for k in ("ema9", "ema21", "atr14", "macd", "bb_upper", "volume_ratio"))
    for key, expected in reference(rows[:100]).items():
        assert float(a.iloc[-1][key]) == pytest.approx(float(expected), rel=1e-9, abs=1e-9), key


def test_unconfirmed_excluded_and_gaps_rejected():
    rows = series()
    now = rows[-1].ts + 60_000
    rows[-1] = rows[-1].model_copy(update={"confirmed": False})
    assert len(indicators(rows, as_of_ms=now)) == len(rows) - 1
    with pytest.raises(ValueError, match="contiguous"):
        indicators(rows[:80] + rows[81:], as_of_ms=now)


def test_strategy_no_fabrication_or_stale_signal():
    result = analyze("BTC-USDT-SWAP", {}, as_of_ms=1_800_000_000_000)
    assert result["status"] == "暂不交易"
    assert result["entry"] is None
    data = {bar: series(bar) for bar in BAR_MS}
    result = analyze("BTC-USDT-SWAP", data, as_of_ms=1_800_000_000_000)
    assert result["status"] == "暂不交易"
    assert "过期" in result["warnings"][-1]


def test_strategy_future_candles_do_not_change_decision():
    data = {bar: series(bar, 2200 if bar == "1m" else 450 if bar == "5m" else 150) for bar in BAR_MS}
    asof = data["15m"][120].ts + BAR_MS["15m"]
    prefix = {bar: [c for c in rows if c.ts + BAR_MS[bar] <= asof] for bar, rows in data.items()}
    assert analyze("BTC-USDT-SWAP", data, as_of_ms=asof) == analyze("BTC-USDT-SWAP", prefix, as_of_ms=asof)


@pytest.mark.parametrize("direction", [1, -1])
def test_candidate_requires_confluence_and_costs(direction, monkeypatch):
    import quant.strategy as engine

    data = {bar: series(bar, 2200 if bar == "1m" else 450 if bar == "5m" else 150) for bar in BAR_MS}
    asof = data["15m"][120].ts + BAR_MS["15m"]
    frames = {bar: indicators(rows, as_of_ms=asof) for bar, rows in data.items()}
    # Synthetic decision fixture tests both candidate branches; never product data.
    for frame in frames.values():
        idx = frame.index[-1]
        for key, value in {
            "close": 100,
            "ema9": 100 + direction,
            "ema21": 100,
            "ma60": 100 - direction,
            "atr14": 0.5,
            "rsi14": 60 if direction == 1 else 40,
            "macd_hist": direction,
            "volume_ratio": 2,
        }.items():
            frame.loc[idx, key] = value
    trend = frames["15m"]
    trend.loc[trend.index[-1], "close"] = 100 + 2 * direction
    trend.loc[trend.index[-2], "ema21"] = 100 - direction
    frames["5m"].loc[frames["5m"].index[-1], "breakout_up" if direction == 1 else "breakout_down"] = True
    monkeypatch.setattr(engine, "indicators", lambda rows, **kw: frames[rows[0].bar])
    result = analyze("BTC-USDT-SWAP", data, as_of_ms=asof)
    assert result["status"] == "候选信号"
    assert result["direction"] == ("做多" if direction == 1 else "做空")
    assert direction * (result["entry"] - result["stop"]) > 0
    assert direction * (result["take_profit"] - result["entry"]) > 0
    assert result["net_rr"] < result["gross_rr"]
    assert not result["risk_approved"]
    json.dumps(result, allow_nan=False)
    rejected = analyze(
        "BTC-USDT-SWAP", data, as_of_ms=asof, parameters=StrategyParameters(taker_fee=0.01, slippage=0.01)
    )
    assert rejected["status"] == "暂不交易"
