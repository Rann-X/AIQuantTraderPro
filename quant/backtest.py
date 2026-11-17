"""Causal candle execution research; explicit historical liquidity assumptions, never live fills."""

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path

from pydantic import Field

from quant.config import BAR_MS
from quant.indicators import indicators
from quant.models import Candle
from quant.paper import encode, trading_day
from quant.risk import (
    FinancialModel,
    Instrument,
    Order,
    Policy,
    Tier,
    assess,
    liquidation,
    pnl,
    select_tier,
    sign,
)
from quant.strategy import StrategyParameters, analyze


class Assumptions(FinancialModel):
    spread_rate: D = Field(D("0.0002"), ge=0, le=D("0.01"))
    volume_participation: D = Field(D("0.01"), gt=0, le=D("0.05"))
    max_hold_minutes: int = Field(30, ge=1, le=1440)


def round_tick(price, tick, *, buy):
    return (price / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick


def candle_book(price, previous_volume, instrument, assumptions, ts):
    capacity = (previous_volume * assumptions.volume_participation / instrument.lot).to_integral_value(
        rounding=ROUND_FLOOR
    ) * instrument.lot
    if capacity < instrument.minimum:
        raise ValueError("previous candle volume proxy below minimum liquidity")
    half = max(instrument.tick, price * assumptions.spread_rate / 2)
    return {
        "ts": str(ts),
        "bids": [[str(round_tick(price - half, instrument.tick, buy=False)), str(capacity)]],
        "asks": [[str(round_tick(price + half, instrument.tick, buy=True)), str(capacity)]],
    }


def size_order(signal, instrument, tiers, book, policy, equity, cash, daily):
    side = "long" if signal["direction"] == "做多" else "short"
    stop = round_tick(D(str(signal["stop"])), instrument.tick, buy=side == "short")
    target = round_tick(D(str(signal["take_profit"])), instrument.tick, buy=side == "long")
    cap = min(
        instrument.max_market,
        min(t.maximum for t in tiers if t.tier == 1),
        D(book["asks" if side == "long" else "bids"][0][1]),
    )
    low = int((instrument.minimum / instrument.lot).to_integral_value(rounding=ROUND_CEILING))
    high = int((cap / instrument.lot).to_integral_value(rounding=ROUND_FLOOR))
    best, error = None, "minimum order exceeds budget or execution constraints"
    while low <= high:
        mid = (low + high) // 2
        order = Order(
            symbol=instrument.symbol, side=side, quantity=mid * instrument.lot, stop=stop, target=target
        )
        try:
            quote = assess(
                order,
                instrument,
                tiers,
                book,
                policy,
                equity=max(D(0), equity),
                free_cash=max(D(0), cash),
                daily_budget=max(D(0), daily),
            )
            best = (order, quote)
            low = mid + 1
        except ValueError as exc:
            error = str(exc)
            high = mid - 1
    if best is None:
        raise ValueError(error)
    return best


def metrics(trades, curve, initial):
    values = [D(t["account_net_pnl"]) for t in trades]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    peak, max_dd, streak, max_streak = D(initial), D(0), 0, 0
    for point in curve:
        equity = D(point["equity"])
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    for v in values:
        streak = streak + 1 if v < 0 else 0
        max_streak = max(max_streak, streak)
    average_win = sum(wins, D(0)) / len(wins) if wins else None
    average_loss = sum(losses, D(0)) / len(losses) if losses else None
    final = D(curve[-1]["equity"]) if curve else D(initial)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "initial_equity": str(initial),
        "final_equity": str(final),
        "net_pnl": str(final - D(initial)),
        "return_pct": str((final / D(initial) - 1) * 100),
        "max_drawdown_pct": str(max_dd * 100),
        "average_win": str(average_win) if average_win is not None else None,
        "average_loss": str(average_loss) if average_loss is not None else None,
        "payoff_ratio": str(average_win / -average_loss) if average_win and average_loss else None,
        "profit_factor": str(sum(wins, D(0)) / -sum(losses, D(0))) if losses else None,
        "max_consecutive_losses": max_streak,
        "fees": str(
            sum((D(t["entry_fee"]) + D(t["exit_fee"]) + D(t["clearance_fee"]) for t in trades), D(0))
        ),
        "funding_paid": str(sum((D(t["funding_paid"]) for t in trades), D(0))),
        "spread_slippage_cost": str(sum((D(t["execution_cost"]) for t in trades), D(0))),
    }


class Backtester:
    def __init__(self, dataset, parameters=None, policy=None, assumptions=None):
        self.dataset = dataset
        self.parameters, self.policy = parameters or StrategyParameters(), policy or Policy()
        self.assumptions = assumptions or Assumptions()
        self.instrument = Instrument.from_okx(dataset["instrument"])
        self.tiers = [Tier.from_okx(t) for t in dataset["tiers"]]
        self.candles = {b: [Candle(**c) for c in dataset["candles"][b]] for b in BAR_MS}
        for bar, rows in self.candles.items():
            if any(c.symbol != self.instrument.symbol or c.bar != bar or not c.confirmed for c in rows):
                raise ValueError("mixed/unconfirmed historical dataset")
        self.frames = {b: indicators(rows, as_of_ms=dataset["end_ms"]) for b, rows in self.candles.items()}
        self.marks = {int(r["ts"]): r for r in dataset["marks"]}
        self.funding = {int(r["fundingTime"]): D(r["realizedRate"]) for r in dataset["funding"]}
        if any(c.ts not in self.marks for c in self.candles["1m"]):
            raise ValueError("missing historical mark candles")

    def signal_at(self, now):
        frames = {
            b: frame.iloc[: frame.ts.searchsorted(now - step, side="right")]
            for (b, step), frame in zip(BAR_MS.items(), self.frames.values())
        }
        return analyze(self.instrument.symbol, {}, as_of_ms=now, parameters=self.parameters, _prepared=frames)

    def run(self, start, end, *, initial=D(100) / D(7)):
        initial = D(initial)
        if (
            not initial.is_finite()
            or initial <= 0
            or not self.dataset["start_ms"] <= start < end <= self.dataset["end_ms"]
        ):
            raise ValueError("invalid evaluation interval/capital")
        inst, policy, a = self.instrument, self.policy, self.assumptions
        cash, position, day_start, daily_loss, day = initial, None, initial, D(0), None
        trades, curve, signals, rejected = [], [{"ts": start, "equity": str(initial)}], [], Counter()

        def close_position(price_ref, now, reason, capacity):
            nonlocal cash, position, daily_loss
            pos = position
            if pos["quantity"] > capacity:
                pos["pending_exit"] = reason
                rejected["exit volume proxy insufficient"] += 1
                return False
            s, base = sign(pos["side"]), pos["quantity"] * inst.base_per_contract
            execution = round_tick(
                price_ref * (1 - s * (policy.slippage + a.spread_rate / 2)),
                inst.tick,
                buy=pos["side"] == "short",
            )
            gross = pnl(inst, pos["quantity"], pos["entry"], execution, pos["side"])
            fee = base * execution * policy.fee
            clearance = (
                base * price_ref * select_tier(self.tiers, pos["quantity"]).mmr
                if reason == "liquidation"
                else D(0)
            )
            release = pos["margin"] + gross - fee - clearance
            deficit = max(D(0), -release)
            cash += max(D(0), release)
            daily_loss += max(D(0), -gross) + fee + clearance
            net = gross - pos["entry_fee"] - pos["funding_paid"] - fee - clearance
            trades.append(
                {
                    **pos,
                    "closed_ms": now,
                    "exit": execution,
                    "reason": reason,
                    "gross_pnl": gross,
                    "exit_fee": fee,
                    "clearance_fee": clearance,
                    "net_pnl": net,
                    "account_net_pnl": net + deficit,
                    "isolated_deficit": deficit,
                    "execution_cost": pos["execution_cost"] + base * abs(execution - price_ref),
                }
            )
            position = None
            return True

        rows = self.candles["1m"]
        for index, bar in enumerate(rows):
            now = bar.ts
            if now < start or now >= end:
                continue
            mark = self.marks[now]
            mark_open = D(mark["open"])
            equity = (
                cash
                if position is None
                else cash
                + position["margin"]
                + pnl(inst, position["quantity"], position["entry"], mark_open, position["side"])
            )
            if day != trading_day(now):
                day, day_start, daily_loss = trading_day(now), max(D(0), equity), D(0)
            capacity = (
                (rows[index - 1].volume * a.volume_participation / inst.lot).to_integral_value(
                    rounding=ROUND_FLOOR
                )
                * inst.lot
                if index
                else D(0)
            )
            if position is not None and position["opened_ms"] < now and now in self.funding:
                payment = (
                    position["quantity"]
                    * inst.base_per_contract
                    * mark_open
                    * self.funding[now]
                    * sign(position["side"])
                )
                position["margin"] -= payment
                position["funding_paid"] += payment
                daily_loss += max(D(0), payment)
            if position is None and cash > 0 and index:
                signal = self.signal_at(now)
                if signal["status"] == "候选信号":
                    signals.append(
                        {
                            "decision_ms": now,
                            "source_close_ms": signal["price_close_ms"],
                            "direction": signal["direction"],
                        }
                    )
                    try:
                        book = candle_book(bar.open, rows[index - 1].volume, inst, a, now)
                        order, quote = size_order(
                            signal,
                            inst,
                            self.tiers,
                            book,
                            policy,
                            cash,
                            cash,
                            day_start * policy.daily_limit - daily_loss,
                        )
                        cash -= quote["margin"] + quote["entry_fee"]
                        daily_loss += quote["entry_fee"]
                        position = {
                            **order.model_dump(),
                            **quote,
                            "opened_ms": now,
                            "signal_close_ms": signal["price_close_ms"],
                            "funding_paid": D(0),
                            "execution_cost": quote["base_quantity"] * abs(quote["entry"] - bar.open),
                            "pending_exit": None,
                        }
                    except ValueError as exc:
                        rejected[str(exc)] += 1
            if position is not None:
                pos, side = position, position["side"]
                liq = liquidation(
                    inst,
                    pos["quantity"],
                    pos["entry"],
                    max(D(0), pos["margin"]),
                    side,
                    self.tiers,
                    policy.fee,
                )
                liquidated = D(mark["low"]) <= liq if side == "long" else D(mark["high"]) >= liq
                stop_hit = bar.low <= pos["stop"] if side == "long" else bar.high >= pos["stop"]
                target_hit = bar.high >= pos["target"] if side == "long" else bar.low <= pos["target"]
                reason, price_ref = None, None
                if liquidated:
                    reason, price_ref = "liquidation", bar.low if side == "long" else bar.high
                elif pos.get("pending_exit"):
                    reason, price_ref = pos["pending_exit"], bar.open
                elif stop_hit:
                    reason, price_ref = (
                        "stop_loss",
                        min(bar.open, pos["stop"]) if side == "long" else max(bar.open, pos["stop"]),
                    )
                elif target_hit:
                    reason, price_ref = "take_profit", pos["target"]
                elif now - pos["opened_ms"] >= a.max_hold_minutes * 60_000:
                    reason, price_ref = "time_exit", bar.open
                elif now + 60_000 >= end:
                    reason, price_ref = "sample_end", bar.close
                if reason:
                    close_position(price_ref, now + 60_000, reason, capacity)
            equity_close = (
                cash
                if position is None
                else cash
                + position["margin"]
                + pnl(inst, position["quantity"], position["entry"], D(mark["close"]), position["side"])
            )
            curve.append({"ts": now + 60_000, "equity": str(max(D(0), equity_close))})
        return {
            "start_ms": start,
            "end_ms": end,
            "metrics": metrics(trades, curve, initial),
            "trades": trades,
            "equity_curve": curve,
            "candidate_signals": signals,
            "rejections": dict(rejected),
            "open_position_at_end": position,
            "valuation_note": "equity includes open position mark PnL; unfilled sample-end exits remain open",
        }


def run_report(directory, output, *, cny=100, cny_per_usdt, parameters=None, policy=None, assumptions=None):
    directory, output = Path(directory), Path(output)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    initial = D(str(cny)) / D(str(cny_per_usdt))
    start, end = manifest["start_ms"] + 2 * 86_400_000, manifest["end_ms"]
    split = start + ((end - start) * 7 // 10 // 60_000) * 60_000
    report = {
        "source": "actual downloaded OKX public data",
        "fx": {
            "cny": str(cny),
            "cny_per_usdt": str(cny_per_usdt),
            "type": "user-selected scenario, not historical FX observation",
        },
        "initial_per_symbol": initial,
        "parameters": (parameters or StrategyParameters()).model_dump(),
        "policy": (policy or Policy()).model_dump(),
        "execution_assumptions": (assumptions or Assumptions()).model_dump(),
        "results": {},
        "limitations": [
            "每币种独立账户，不是四币种共享本金的组合回测。",
            "历史期间假设当前合约参数及阶梯不变；未获得严格历史时点规则快照。",
            "无历史盘口；点差/滑点为假设，流动性使用前一分钟成交量参与限制。",
            "一分钟OHLC没有逐笔路径，采用强平优先、止损先于止盈，结果可能偏保守。",
            "资金费使用实际realizedRate与结算分钟历史标记开盘价（采样近似）。",
            "拆分前固定参数，不做优化；短样本不足以证明盈利能力。",
            "EMA从预热区间递推；实时模块从初始化300根K线开始递推。",
        ],
    }
    for symbol, digest in manifest["files"].items():
        path = directory / f"{symbol}.json"
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"dataset digest mismatch {symbol}")
        data = json.loads(path.read_text(encoding="utf-8"))
        bt = Backtester(data, parameters, policy, assumptions)
        report["results"][symbol] = {
            "dataset_sha256": digest,
            "in_sample": bt.run(start, split, initial=initial),
            "out_of_sample": bt.run(split, end, initial=initial),
        }
    output.mkdir(parents=True, exist_ok=True)
    (output / "backtest.json").write_text(encode(report), encoding="utf-8")
    lines = [
        "# 策略历史回测报告",
        "",
        "真实OKX数据；每币种独立虚拟账户；固定参数，不做优化。",
        "",
        f"换算情景：{cny}元人民币 / {cny_per_usdt}元每USDT（假设输入，非历史汇率）。",
        f"样本内UTC：{datetime.fromtimestamp(start / 1000, timezone.utc).isoformat()} 至 {datetime.fromtimestamp(split / 1000, timezone.utc).isoformat()}（不含结束）；样本外至 {datetime.fromtimestamp(end / 1000, timezone.utc).isoformat()}（不含结束）。前两天仅预热。",
        "",
        "| 币种 | 样本 | 交易数 | 净损益USDT | 胜率 | 最大回撤% |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for symbol, result in report["results"].items():
        for key, label in (("in_sample", "样本内"), ("out_of_sample", "样本外")):
            m = result[key]["metrics"]
            win = f"{m['win_rate']:.1%}" if m["win_rate"] is not None else "无成交"
            lines.append(
                f"| {symbol} | {label} | {m['trades']} | {D(m['net_pnl']):.6f} | {win} | {D(m['max_drawdown_pct']):.3f} |"
            )
    lines += ["", "## 测试范围及限制", ""] + [f"- {x}" for x in report["limitations"]]
    lines += ["", "完整参数、交易明细、收益曲线、成本与拒绝原因见backtest.json。没有成交不会补造交易。"]
    (output / "BACKTEST.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="data/history")
    parser.add_argument("--output", default="reports")
    parser.add_argument("--cny-per-usdt", required=True)
    args = parser.parse_args()
    from quant.config import load_settings

    settings = load_settings()
    report = run_report(
        args.directory,
        args.output,
        cny_per_usdt=args.cny_per_usdt,
        parameters=StrategyParameters(**settings.strategy),
        policy=Policy(**settings.risk),
    )
    print(
        {
            s: {k: r[k]["metrics"]["trades"] for k in ("in_sample", "out_of_sample")}
            for s, r in report["results"].items()
        }
    )


if __name__ == "__main__":
    main()
