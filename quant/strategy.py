import math

from pydantic import BaseModel, ConfigDict, Field

from quant.config import BAR_MS
from quant.indicators import indicators


class StrategyParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    volume_ratio: float = Field(1.2, ge=1, le=10)
    high_atr_ratio: float = Field(0.02, gt=0, le=0.2)
    trend_separation: float = Field(0.0005, gt=0, le=0.1)
    stop_atr: float = Field(1.5, ge=1, le=5)
    reward_multiple: float = Field(2.5, ge=1, le=10)
    taker_fee: float = Field(0.0005, ge=0, le=0.01)
    slippage: float = Field(0.0003, ge=0, le=0.01)
    min_net_rr: float = Field(1.5, gt=0, le=10)


def analyze(symbol, candles_by_bar, *, as_of_ms, parameters=None, _prepared=None):
    p = parameters or StrategyParameters()
    result = {
        "symbol": symbol,
        "as_of_ms": as_of_ms,
        "status": "暂不交易",
        "direction": None,
        "current_price": None,
        "price_source": "最近已完成1分钟K线收盘价",
        "trend": "无法判断",
        "setup": None,
        "conditions": [],
        "invalidation": [],
        "entry": None,
        "stop": None,
        "take_profit": None,
        "gross_rr": None,
        "net_rr": None,
        "estimated_roundtrip_cost_rate": 2 * (p.taker_fee + p.slippage),
        "warnings": ["规则候选不是盈利预测；需另行通过仓位风险预算审核。"],
        "risk_approved": False,
        "observations": {},
        "indicators": {},
    }
    frames = {}
    try:
        for bar in BAR_MS:
            candles = candles_by_bar.get(bar, [])
            if any(c.symbol != symbol or c.bar != bar for c in candles):
                raise ValueError("币种/周期不一致")
            frame = indicators(candles, as_of_ms=as_of_ms) if _prepared is None else _prepared[bar]
            if len(frame) and int(frame.iloc[-1].ts) + BAR_MS[bar] > as_of_ms:
                raise ValueError("预计算指标包含未来K线")
            if len(frame) < 100:
                raise ValueError(f"{bar}已完成K线不足100根")
            if as_of_ms - (int(frame.iloc[-1].ts) + BAR_MS[bar]) > BAR_MS[bar] + 30_000:
                raise ValueError(f"{bar}数据已过期")
            frames[bar] = frame
    except ValueError as exc:
        result["warnings"].append(str(exc))
        return result
    one, five, trend = (frames[b].iloc[-1] for b in ("1m", "5m", "15m"))
    previous_trend = frames["15m"].iloc[-2]
    result["current_price"] = float(one.close)
    result["price_close_ms"] = int(one.ts) + 60_000
    for bar, frame in frames.items():
        result["indicators"][bar] = {
            k: (float(v) if math.isfinite(float(v)) else None) for k, v in frame.iloc[-1].items()
        }
    separation = abs(trend.ema21 - trend.ma60) / trend.close
    direction = (
        1
        if trend.close > trend.ema21 > trend.ma60 and trend.ema21 > previous_trend.ema21
        else -1
        if trend.close < trend.ema21 < trend.ma60 and trend.ema21 < previous_trend.ema21
        else 0
    )
    high_vol = any(float(x.atr14 / x.close) > p.high_atr_ratio for x in (one, five, trend))
    result["trend"] = "上涨" if direction == 1 else "下跌" if direction == -1 else "横盘震荡"
    result["observations"] = {
        "abnormal_volume": bool(one.volume_ratio >= 2),
        "high_volatility": high_vol,
        "ranging": bool(direction == 0 or separation < p.trend_separation),
        "indicator_conflict": bool(direction and direction * (one.ema9 - one.ema21) <= 0),
    }
    if high_vol:
        result["warnings"].append("ATR波动超过阈值，禁止生成交易候选。")
        return result
    if not direction or separation < p.trend_separation:
        result["warnings"].append("15分钟趋势不明确或均线间距不足。")
        return result
    breakout = bool(five.breakout_up if direction == 1 else five.breakout_down)
    pullback = (
        five.low <= five.ema21 < five.close and five.close > five.open
        if direction == 1
        else five.high >= five.ema21 > five.close and five.close < five.open
    )
    momentum = (
        one.ema9 > one.ema21 and one.macd_hist > 0 and 50 < one.rsi14 < 75
        if direction == 1
        else one.ema9 < one.ema21 and one.macd_hist < 0 and 25 < one.rsi14 < 50
    )
    volume = bool(one.volume_ratio >= p.volume_ratio)
    result["conditions"] = [
        f"15分钟趋势：{result['trend']}",
        f"5分钟突破：{breakout}；回调确认：{bool(pullback)}",
        f"1分钟EMA/MACD/RSI动能一致：{bool(momentum)}",
        f"1分钟量比≥{p.volume_ratio}：{volume}",
    ]
    if not ((breakout or pullback) and momentum and volume):
        return result
    entry = float(one.close)
    distance = float(five.atr14) * p.stop_atr
    cost = entry * result["estimated_roundtrip_cost_rate"]
    net_rr = (distance * p.reward_multiple - cost) / (distance + cost)
    if distance <= 0 or net_rr < p.min_net_rr or entry - direction * distance <= 0:
        result["warnings"].append("扣除双边手续费和滑点后的收益风险比不足或止损无效。")
        return result
    result.update(
        status="候选信号",
        direction="做多" if direction == 1 else "做空",
        setup="趋势突破" if breakout else "趋势回调",
        entry=entry,
        stop=entry - direction * distance,
        take_profit=entry + direction * distance * p.reward_multiple,
        gross_rr=p.reward_multiple,
        net_rr=net_rr,
        estimated_cost_per_base_unit=cost,
        invalidation=["15分钟趋势反转或失去均线排列", "参考止损被触及", "下一根1分钟K线完成后需重新验证条件"],
        expires_ms=int(one.ts) + 120_000,
    )
    result["warnings"].append("费用为可配置假设，未含持仓资金费；参考价格需结合实时盘口和合约最小张数审核。")
    return result
