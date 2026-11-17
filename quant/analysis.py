import time
from decimal import Decimal

from quant.config import BAR_MS
from quant.strategy import StrategyParameters, analyze


def analyze_market(store, settings, symbol, *, as_of_ms=None):
    now = int(time.time() * 1000) if as_of_ms is None else as_of_ms
    signal = analyze(
        symbol,
        {b: store.candles(symbol, b, 300, True) for b in BAR_MS},
        as_of_ms=now,
        parameters=StrategyParameters(**settings.strategy),
    )
    signal["reference_close"] = signal["current_price"]
    signal["current_price"] = None
    signal["price_source"] = "OKX tickers 实时成交价；规则只使用reference_close及确认K线"
    ticker = store.snapshots(symbol).get("tickers")
    if ticker and 0 <= now - ticker["exchange_ts"] <= 10_000:
        signal["current_price"] = ticker["data"]["last"]
        signal["current_price_ms"] = ticker["exchange_ts"]
        if signal["status"] == "候选信号":
            direction = 1 if signal["direction"] == "做多" else -1
            price = Decimal(signal["current_price"])
            if (
                direction * (price - Decimal(str(signal["stop"]))) <= 0
                or direction * (price - Decimal(str(signal["take_profit"]))) >= 0
            ):
                signal["warnings"].append("实时价已触及参考止损或目标，原候选失效，等待下一根确认K线。")
                signal.update(status="暂不交易", direction=None, entry=None, stop=None, take_profit=None)
    else:
        signal["warnings"].append("实时成交价缺失或超过10秒，暂不发布交易候选。")
        signal.update(status="暂不交易", direction=None, entry=None, stop=None, take_profit=None)
    return signal
