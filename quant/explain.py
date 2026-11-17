"""Grounded rule explanations. Values come exclusively from measured analysis/ledger fields."""

from decimal import Decimal


def explain_signal(signal):
    paragraphs = [f"{signal['symbol']}：{signal['status']}。"]
    reference = signal.get("reference_close", signal.get("current_price"))
    if reference is None:
        paragraphs.append("缺少可用的已完成K线，不能判断趋势或给出参考价格。")
    else:
        paragraphs.append(f"最近已完成1分钟K线收盘价为{reference}；15分钟规则趋势为{signal['trend']}。")
        observations = signal.get("observations", {})
        if observations.get("indicator_conflict"):
            paragraphs.append("1分钟均线动能与15分钟趋势冲突，短周期尚未确认趋势。")
        if observations.get("abnormal_volume"):
            paragraphs.append("1分钟成交量达到异常量阈值；放量本身不代表方向已经确认。")
        if observations.get("high_volatility"):
            paragraphs.append("ATR相对价格波动超过规则阈值，候选已被波动过滤器拦截。")
        if observations.get("ranging"):
            paragraphs.append("趋势排列或均线间距不足，当前归为震荡或趋势不明。")
    if signal.get("current_price_ms") and signal.get("current_price") is not None:
        paragraphs.append(
            f"OKX实时成交价为{signal['current_price']}，观察时间UTC毫秒{signal['current_price_ms']}；该价格不改变收盘规则。"
        )
    paragraphs.extend(signal.get("conditions", []))
    if signal["status"] == "候选信号":
        paragraphs.append(
            f"规则识别{signal['setup']}的{signal['direction']}候选；参考入场{signal['entry']}，"
            f"参考止损{signal['stop']}，参考止盈{signal['take_profit']}，"
            f"扣除假设费用后的收益风险比{signal['net_rr']:.3f}。"
        )
        paragraphs.append("失效条件：" + "；".join(signal["invalidation"]) + "。")
    else:
        paragraphs.append("完整共振或数据条件尚未满足，暂不交易。")
    paragraphs.extend(signal.get("warnings", []))
    return {
        "engine": "grounded_rules",
        "as_of_ms": signal["as_of_ms"],
        "symbol": signal["symbol"],
        "text": "\n\n".join(paragraphs),
        "source": "confirmed OKX candles and deterministic strategy output",
        "prediction": False,
        "risk_approved": False,
    }


def review_trades(journal):
    trades = [row["data"] for row in journal if row["kind"] == "close"]
    if not trades:
        return {
            "closed_trades": 0,
            "net_pnl": "0",
            "win_rate": None,
            "text": "尚无已平仓模拟交易，不能推断策略盈利表现。",
        }
    values = [Decimal(t["account_net_pnl"]) for t in trades]
    wins = sum(v > 0 for v in values)
    fees = sum(
        (Decimal(t["entry_fee"]) + Decimal(t["exit_fee"]) + Decimal(t["clearance_fee"]) for t in trades),
        Decimal(0),
    )
    funding = sum((Decimal(t["funding_paid"]) for t in trades), Decimal(0))
    return {
        "closed_trades": len(trades),
        "net_pnl": str(sum(values, Decimal(0))),
        "win_rate": wins / len(trades),
        "fees": str(fees),
        "funding_paid": str(funding),
        "text": f"基于{len(trades)}笔已平仓模拟交易，盈利笔数为{wins}。费用与资金费已纳入损益。"
        "手动交易不等于规则策略业绩；小样本不支持盈利预测。",
    }


def review_backtest(result):
    m = result["metrics"]
    if not m["trades"]:
        return "该区间没有通过策略和风险审核的成交，胜率与平均盈亏无法计算；不能由零交易推断策略安全或盈利。"
    outcome = "亏损" if Decimal(m["net_pnl"]) < 0 else "盈利" if Decimal(m["net_pnl"]) > 0 else "持平"
    return (
        f"该区间共{m['trades']}笔成交，净损益{Decimal(m['net_pnl']):.6f} USDT，整体{outcome}；"
        f"胜率{m['win_rate']:.1%}，最大回撤{Decimal(m['max_drawdown_pct']):.3f}%，最长连续亏损{m['max_consecutive_losses']}笔。"
        f"交易费用{Decimal(m['fees']):.6f} USDT，净支付资金费{Decimal(m['funding_paid']):.6f} USDT。"
        "结果依赖已披露的执行假设；样本外表现也不构成未来盈利预测。"
    )
