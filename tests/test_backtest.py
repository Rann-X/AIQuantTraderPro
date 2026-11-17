from copy import deepcopy
from decimal import Decimal as D

import pytest

from quant.backtest import Backtester, metrics
from quant.config import BAR_MS
from quant.models import Candle
from quant.strategy import analyze

START = 1_800_000_000_000 // 900_000 * 900_000


def dataset():
    end = START + 3 * 86_400_000
    candles = {}
    for bar, step in BAR_MS.items():
        candles[bar] = [
            Candle(
                symbol="SOL-USDT-SWAP",
                bar=bar,
                ts=t,
                open=100,
                high="100.2",
                low="99.8",
                close=100,
                volume=100,
                base_volume=100,
                quote_volume=10000,
                confirmed=True,
            ).model_dump(mode="json")
            for t in range(START, end, step)
        ]
    return {
        "start_ms": START,
        "end_ms": end,
        "symbol": "SOL-USDT-SWAP",
        "candles": candles,
        "instrument": {
            "instId": "SOL-USDT-SWAP",
            "ctType": "linear",
            "settleCcy": "USDT",
            "state": "live",
            "ctValCcy": "SOL",
            "ctVal": "1",
            "ctMult": "1",
            "lotSz": "0.01",
            "minSz": "0.01",
            "tickSz": "0.01",
            "maxMktSz": "10000",
        },
        "tiers": [
            {"tier": "1", "minSz": "0", "maxSz": "1000", "mmr": "0.005", "imr": "0.01", "maxLever": "100"}
        ],
        "marks": [
            {"ts": t, "open": "100", "high": "100.2", "low": "99.8", "close": "100"}
            for t in range(START, end, 60_000)
        ],
        "funding": [{"fundingTime": str(START + 2 * 86_400_000 + 60_000), "realizedRate": "0.0002"}],
    }


def candidate(now):
    return {"status": "候选信号", "direction": "做多", "stop": 99, "take_profit": 104, "price_close_ms": now}


def test_precomputed_signal_equals_prefix_and_rejects_future():
    data = dataset()
    bt = Backtester(data)
    now = START + 2 * 86_400_000
    prefix = {b: [c for c in rows if c.ts + BAR_MS[b] <= now] for b, rows in bt.candles.items()}
    assert bt.signal_at(now) == analyze(bt.instrument.symbol, prefix, as_of_ms=now)
    bad = deepcopy(data)
    for bar in BAR_MS:
        for row in bad["candles"][bar]:
            if row["ts"] >= now:
                row.update(open="999", high="1000", low="998", close="999")
    assert Backtester(bad).signal_at(now) == bt.signal_at(now)
    future = analyze(bt.instrument.symbol, {}, as_of_ms=now, _prepared=bt.frames)
    assert future["status"] == "暂不交易"
    assert "未来" in future["warnings"][-1]


@pytest.mark.parametrize(
    "mode,reason", [("both", "stop_loss"), ("mark", "liquidation"), ("hold", "time_exit")]
)
def test_causal_fills_costs_and_adverse_intrabar_order(mode, reason):
    data = dataset()
    now = START + 2 * 86_400_000
    index = (now - START) // 60_000
    if mode == "both":
        data["candles"]["1m"][index].update(high="105", low="98")
    elif mode == "mark":
        data["marks"][index]["low"] = "90"
        data["candles"]["1m"][index]["low"] = "90"
    bt = Backtester(data)
    bt.signal_at = lambda t: candidate(t) if t == now else {"status": "暂不交易"}
    result = bt.run(now, now + 60 * 60_000)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["opened_ms"] >= trade["signal_close_ms"]
    assert trade["reason"] == reason
    assert trade["entry"] > 100
    assert trade["entry_fee"] > 0 and trade["exit_fee"] > 0
    assert abs(
        D(result["metrics"]["final_equity"])
        - D(result["metrics"]["initial_equity"])
        - trade["account_net_pnl"]
    ) < D("1e-24")
    if mode == "hold":
        assert trade["funding_paid"] > 0


def test_minimum_liquidity_blocks_order_and_no_fake_trades():
    data = dataset()
    now = START + 2 * 86_400_000
    data["candles"]["1m"][(now - START) // 60_000 - 1]["volume"] = "0"
    bt = Backtester(data)
    bt.signal_at = lambda t: candidate(t) if t == now else {"status": "暂不交易"}
    result = bt.run(now, now + 120_000)
    assert not result["trades"]
    assert result["metrics"]["win_rate"] is None
    assert result["rejections"]


def test_statistics_and_drawdown():
    trades = [
        {
            "account_net_pnl": str(x),
            "entry_fee": "0.1",
            "exit_fee": "0.1",
            "clearance_fee": "0",
            "funding_paid": "0",
            "execution_cost": "0.01",
        }
        for x in (2, -1, -2, 1)
    ]
    result = metrics(trades, [{"equity": str(x)} for x in (10, 12, 11, 9, 10)], D(10))
    assert result["win_rate"] == 0.5
    assert result["max_consecutive_losses"] == 2
    assert D(result["max_drawdown_pct"]) == 25
    assert D(result["average_loss"]) == D("-1.5")
