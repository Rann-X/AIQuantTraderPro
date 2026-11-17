from decimal import Decimal as D

import pytest

from quant.paper import PaperAccount
from quant.risk import (
    Instrument,
    Order,
    Policy,
    Tier,
    assess,
    depth_fill,
    liquidation,
    maintenance,
    pnl,
    select_tier,
)

SYMBOL = "SOL-USDT-SWAP"
NOW = 1_800_000_000_000
INST = Instrument(
    symbol=SYMBOL, base_per_contract="1", lot="0.01", minimum="0.01", tick="0.01", max_market="10000"
)
TIERS = [
    Tier(tier=1, minimum=0, maximum=1000, mmr="0.005", imr="0.01", max_leverage=100),
    Tier(tier=2, minimum=1000, maximum=2000, mmr="0.01", imr="0.02", max_leverage=50),
]


def market(now=NOW, last="100", mark="100", bid="99.99", ask="100.01"):
    return {
        "instrument": INST.model_dump(mode="json"),
        "instrument_ms": now,
        "tiers": [t.model_dump(mode="json") for t in TIERS],
        "tiers_ms": now,
        "ticker": {"last": last, "ts": str(now)},
        "mark": {"markPx": mark, "ts": str(now)},
        "book": {"ts": str(now), "bids": [[bid, "1000"]], "asks": [[ask, "1000"]]},
        "funding": {"ts": str(now), "fundingRate": "0.0001", "fundingTime": str(now + 3_600_000)},
    }


def order(side="long", qty="0.1"):
    return Order(
        symbol=SYMBOL,
        side=side,
        quantity=qty,
        stop="99" if side == "long" else "101",
        target="104" if side == "long" else "96",
    )


def account(tmp_path, capital="100"):
    a = PaperAccount(tmp_path / "paper.db")
    a.initialize(
        cny=capital,
        cny_per_usdt=7,
        rate_source="synthetic unit test conversion",
        rate_time_ms=NOW - 1000,
        now=NOW,
    )
    return a


def test_depth_execution_and_quantity():
    book = market()["book"]
    book["asks"] = [["100.01", "0.05"], ["100.02", "1"]]
    assert depth_fill(INST, D("0.1"), book, buy=True, slippage=0) == D("100.02")
    with pytest.raises(ValueError, match="liquidity"):
        depth_fill(INST, D(2), book, buy=True)
    for qty in ("0.001", "0.015", "10000.01"):
        with pytest.raises(ValueError):
            INST.quantity(qty)


@pytest.mark.parametrize("side", ["long", "short"])
def test_liquidation_is_root_of_official_equity_condition(side):
    qty, entry, margin, fee = D(10), D(100), D(50), D("0.0005")
    liq = liquidation(INST, qty, entry, margin, side, TIERS, fee)
    equity = margin + pnl(INST, qty, entry, liq, side)
    assert abs(equity - maintenance(INST, qty, liq, TIERS, fee)) < D("1e-22")
    assert liq != (D(95) if side == "long" else D(105))
    assert select_tier(TIERS, 1000).tier == 1
    assert select_tier(TIERS, "1000.01").tier == 2


def test_risk_minimum_and_cost_budgets():
    quote = assess(
        order(),
        INST,
        TIERS,
        market()["book"],
        Policy(),
        equity=D(100) / 7,
        free_cash=D(100) / 7,
        daily_budget=D(5) / 7,
    )
    assert quote["planned_loss"] < D(2) / 7
    assert quote["margin"] > 0
    expensive_minimum = INST.model_copy(update={"minimum": D(1), "lot": D(1)})
    with pytest.raises(ValueError, match="放弃交易"):
        assess(
            order(qty="1"),
            expensive_minimum,
            TIERS,
            market()["book"],
            Policy(),
            equity=D(100) / 7,
            free_cash=D(100) / 7,
            daily_budget=D(5) / 7,
        )
    with pytest.raises(ValueError):
        Policy(per_trade="0.03")


@pytest.mark.parametrize("side", ["long", "short"])
def test_roundtrip_persistence_and_idempotency(tmp_path, side):
    a = account(tmp_path)
    initial = D(a.read()["initial"])
    a.open(order(side), {SYMBOL: market()}, now=NOW, request_id="open1")
    with pytest.raises(ValueError, match="duplicate"):
        a.open(order(side), {SYMBOL: market()}, now=NOW, request_id="open1")
    a.close()
    a = PaperAccount(tmp_path / "paper.db")
    trade = a.close_position(SYMBOL, {SYMBOL: market(NOW + 1000)}, now=NOW + 1000)
    assert D(a.read()["cash"]) == initial + D(trade["account_net_pnl"])
    assert D(trade["net_pnl"]) < 0
    assert not a.read()["positions"]
    assert [x["kind"] for x in a.history()] == ["close", "open", "initialize"]
    a.close()


def test_stale_and_daily_budget_block_are_atomic(tmp_path):
    a = account(tmp_path)
    before = a.read()
    with pytest.raises(ValueError, match="stale"):
        a.open(order(), {SYMBOL: market(NOW - 11_000)}, now=NOW, request_id="stale")
    assert a.read() == before
    with a.transaction() as state:
        state["daily_loss"] = "1"
    with pytest.raises(ValueError, match="放弃交易"):
        a.open(order(), {SYMBOL: market()}, now=NOW, request_id="daily")
    assert not a.read()["positions"]
    a.close()


def test_funding_once_and_before_close(tmp_path):
    a = account(tmp_path)
    m = market()
    m["funding"]["fundingTime"] = str(NOW + 1000)
    a.open(order(), {SYMBOL: m}, now=NOW, request_id="fund")
    with pytest.raises(ValueError, match="funding"):
        a.close_position(SYMBOL, {SYMBOL: market(NOW + 1001)}, now=NOW + 1001)
    kwargs = dict(
        funding_ms=NOW + 1000,
        realized_rate="0.0001",
        mark_price="100",
        price_source="synthetic test",
        next_funding_ms=NOW + 3_600_000,
        now=NOW + 1001,
    )
    assert a.settle_funding(SYMBOL, **kwargs)
    assert not a.settle_funding(SYMBOL, **kwargs)
    pos = a.read()["positions"][SYMBOL]
    assert D(pos["funding_paid"]) == D("0.001")
    assert pos["next_funding_ms"] == NOW + 3_600_000
    a.close_position(SYMBOL, {SYMBOL: market(NOW + 1001)}, now=NOW + 1001)
    a.close()


@pytest.mark.parametrize(
    "last,mark,reason",
    [("98", "98", "stop_loss"), ("105", "105", "take_profit"), ("98", "90", "liquidation")],
)
def test_automatic_exit_and_liquidation_priority(tmp_path, last, mark, reason):
    a = account(tmp_path)
    a.open(order(), {SYMBOL: market()}, now=NOW, request_id="open")
    actions = a.step(
        {
            SYMBOL: market(
                NOW + 1000, last=last, mark=mark, bid=str(D(mark) - D("0.01")), ask=str(D(mark) + D("0.01"))
            )
        },
        now=NOW + 1000,
    )
    assert actions[0]["reason"] == reason
    if reason == "liquidation":
        assert D(actions[0]["clearance_fee"]) > 0
    assert not a.read()["positions"]
    a.close()


def test_trigger_persists_when_book_insufficient(tmp_path):
    a = account(tmp_path)
    a.open(order(), {SYMBOL: market()}, now=NOW, request_id="open")
    m = market(NOW + 1000, last="98", mark="98")
    m["book"]["bids"][0][1] = "0.01"
    assert a.step({SYMBOL: m}, now=NOW + 1000) == []
    assert a.read()["positions"][SYMBOL]["pending_exit"] == "stop_loss"
    assert a.step({SYMBOL: market(NOW + 2000)}, now=NOW + 2000)[0]["reason"] == "stop_loss"
    a.close()


def test_no_overwrite_existing_account(tmp_path):
    a = account(tmp_path)
    with pytest.raises(ValueError, match="already"):
        a.initialize(cny_per_usdt=7, rate_source="test", rate_time_ms=NOW, now=NOW)
    a.close()


async def test_actual_funding_service_uses_realized_rate_and_historical_mark(tmp_path):
    from unittest.mock import AsyncMock

    from quant.paper_service import reconcile_funding

    a = account(tmp_path)
    due = NOW + 60_000
    m = market()
    m["funding"]["fundingTime"] = str(due)
    a.open(order(), {SYMBOL: m}, now=NOW, request_id="fund-open")
    later = market(due + 1000)
    rest = AsyncMock()
    rest.get.side_effect = [
        [{"instId": SYMBOL, "fundingTime": str(due), "fundingRate": "0.1", "realizedRate": "0.0002"}],
        [[str(due), "101", "102", "100", "101", "1"]],
    ]
    await reconcile_funding(a, {SYMBOL: later}, rest, due + 1000)
    assert D(a.read()["positions"][SYMBOL]["funding_paid"]) == D("0.00202")
    assert rest.get.call_args_list[1].args[0] == "/api/v5/market/history-mark-price-candles"
    a.close()


async def test_missing_actual_funding_blocks_reconciliation(tmp_path):
    from unittest.mock import AsyncMock

    from quant.paper_service import reconcile_funding

    a = account(tmp_path)
    m = market()
    m["funding"]["fundingTime"] = str(NOW + 1000)
    a.open(order(), {SYMBOL: m}, now=NOW, request_id="fund-open")
    before = a.read()
    rest = AsyncMock()
    rest.get.return_value = []
    with pytest.raises(ValueError, match="not published"):
        await reconcile_funding(a, {SYMBOL: market(NOW + 2000)}, rest, NOW + 2000)
    assert a.read() == before
    a.close()


def test_paper_ledger_cannot_use_market_database(tmp_path):
    from quant.store import Store

    path = tmp_path / "market.db"
    store = Store(path)
    store.close()
    with pytest.raises(ValueError, match="separate"):
        PaperAccount(path)
