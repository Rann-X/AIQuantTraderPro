import asyncio
import logging
import time
from decimal import Decimal

import httpx

from quant.models import validate_snapshot
from quant.okx import PublicREST
from quant.paper import PaperAccount
from quant.risk import Instrument, Tier

log = logging.getLogger(__name__)


def market_inputs(store, symbols):
    statuses = store.statuses()
    result = {}
    for symbol in symbols:
        instrument = statuses.get(f"instrument:{symbol}")
        tiers = statuses.get(f"tiers:{symbol}")
        if not instrument or not tiers:
            raise ValueError(f"{symbol}: missing OKX instrument/tier snapshot; run collector")
        parsed = Instrument.from_okx(instrument["data"])
        snapshots = store.snapshots(symbol)
        market = {
            "instrument": parsed.model_dump(mode="json"),
            "tiers": [Tier.from_okx(t).model_dump(mode="json") for t in tiers["data"]],
            "instrument_ms": instrument["received_ms"],
            "tiers_ms": tiers["received_ms"],
        }
        for key, channel in (
            ("book", "books5"),
            ("mark", "mark-price"),
            ("ticker", "tickers"),
            ("funding", "funding-rate"),
        ):
            if channel not in snapshots:
                raise ValueError(f"{symbol}: missing {channel}")
            row = snapshots[channel]["data"]
            if row.get("instId", symbol) != symbol:
                raise ValueError("snapshot instrument mismatch")
            market[key] = validate_snapshot(channel, row)
        market["tiers"].sort(key=lambda t: t["tier"])
        result[symbol] = market
    return result


async def reconcile_funding(account, markets, rest, now):
    for symbol, pos in account.read()["positions"].items():
        due = pos["next_funding_ms"]
        if due > now:
            continue
        found, cursor = {}, now + 1
        while True:
            rows = await rest.get(
                "/api/v5/public/funding-rate-history", instId=symbol, after=str(cursor), limit="100"
            )
            if not rows:
                break
            oldest = min(int(r["fundingTime"]) for r in rows)
            for row in rows:
                ts = int(row["fundingTime"])
                if due <= ts <= now:
                    found[ts] = row
            if oldest <= due:
                break
            if oldest >= cursor:
                raise ValueError("funding pagination not progressing")
            cursor = oldest
        if due not in found:
            raise ValueError("actual funding settlement not published/retained; execution waits")
        timestamps = sorted(found)
        next_due = int(markets[symbol]["funding"]["fundingTime"])
        if next_due <= now:
            raise ValueError("next funding timestamp unavailable")
        for i, ts in enumerate(timestamps):
            row = found[ts]
            rate = Decimal(row["realizedRate"])
            if row["instId"] != symbol or not rate.is_finite():
                raise ValueError("invalid actual funding event")
            marks = await rest.get(
                "/api/v5/market/history-mark-price-candles",
                instId=symbol,
                bar="1m",
                after=str(ts + 1),
                limit="1",
            )
            if not marks or int(marks[0][0]) != ts:
                raise ValueError("settlement minute historical mark unavailable")
            account.settle_funding(
                symbol,
                funding_ms=ts,
                realized_rate=rate,
                mark_price=marks[0][1],
                price_source="OKX settlement-minute mark candle open (sampling approximation)",
                next_funding_ms=timestamps[i + 1] if i + 1 < len(timestamps) else next_due,
                now=now,
            )


async def watch(account: PaperAccount, store, settings, *, seconds=None):
    rest = PublicREST(settings)
    end = time.monotonic() + seconds if seconds is not None else float("inf")
    try:
        while time.monotonic() < end:
            try:
                try:
                    account.read()
                except ValueError:
                    store.status(
                        "paper_worker",
                        {"ok": True, "waiting_initialization": True, "time_ms": int(time.time() * 1000)},
                    )
                    await asyncio.sleep(3)
                    continue
                symbols = list(account.read()["positions"])
                if symbols:
                    markets = market_inputs(store, symbols)
                    now = int(time.time() * 1000)
                    await reconcile_funding(account, markets, rest, now)
                    # Funding REST can take seconds: reload prices before executing.
                    actions = account.step(market_inputs(store, symbols), now=int(time.time() * 1000))
                    for action in actions:
                        log.info("Paper close %s reason=%s", action["symbol"], action["reason"])
                store.status("paper_worker", {"ok": True, "time_ms": int(time.time() * 1000)})
            except (ValueError, KeyError, ArithmeticError, RuntimeError, httpx.HTTPError, OSError) as exc:
                log.warning("Paper worker waiting for valid data: %s", exc)
                store.status(
                    "paper_worker", {"ok": False, "time_ms": int(time.time() * 1000), "error": str(exc)}
                )
            await asyncio.sleep(1)
    finally:
        await rest.close()
