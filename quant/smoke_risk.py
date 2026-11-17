"""Real OKX data / virtual ledger integration smoke; FX is an explicitly labelled test assumption."""

import argparse
import asyncio
import json
import time
from decimal import ROUND_FLOOR
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

from quant.config import load_settings
from quant.models import validate_snapshot
from quant.okx import PublicREST
from quant.paper import PaperAccount, encode
from quant.risk import Instrument, Order, Tier


async def snapshot(rest, symbol):
    row = (await rest.get("/api/v5/public/instruments", instType="SWAP", instId=symbol))[0]
    instrument = Instrument.from_okx(row)
    tiers = await rest.get(
        "/api/v5/public/position-tiers", instType="SWAP", tdMode="isolated", instFamily=row["instFamily"]
    )
    now = int(time.time() * 1000)
    market = {
        "instrument": instrument.model_dump(mode="json"),
        "instrument_ms": now,
        "tiers_ms": now,
        "tiers": [Tier.from_okx(t).model_dump(mode="json") for t in tiers],
    }
    for key, channel, path, params in (
        ("book", "books5", "/api/v5/market/books", {"sz": "5"}),
        ("mark", "mark-price", "/api/v5/public/mark-price", {"instType": "SWAP"}),
        ("ticker", "tickers", "/api/v5/market/ticker", {}),
        ("funding", "funding-rate", "/api/v5/public/funding-rate", {}),
    ):
        item = (await rest.get(path, instId=symbol, **params))[0]
        if channel == "funding-rate" and "ts" not in item:
            item["ts"] = str(int(time.time() * 1000))
        market[key] = validate_snapshot(channel, item)
    return market


async def run(args):
    settings = load_settings()
    rest = PublicREST(settings)
    path = Path(args.ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    account = PaperAccount(path)
    now = int(time.time() * 1000)
    report = {
        "type": "real_OKX_snapshot_virtual_execution_test",
        "fx_scenario": {
            "cny_per_usdt": args.scenario_cny_per_usdt,
            "source": "explicit hypothetical test scenario; NOT live FX",
        },
        "real_orders": False,
        "results": {},
    }
    try:
        account.initialize(
            cny_per_usdt=args.scenario_cny_per_usdt,
            rate_source=report["fx_scenario"]["source"],
            rate_time_ms=now,
            now=now,
        )
        for symbol in settings.symbols:
            market = await snapshot(rest, symbol)
            instrument = Instrument(**market["instrument"])
            price, tick = D(market["ticker"]["last"]), instrument.tick
            stop = ((price * D("0.995")) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            target = ((price * D("1.02")) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            order = Order(symbol=symbol, side="long", quantity=instrument.minimum, stop=stop, target=target)
            now = int(time.time() * 1000)
            item = {"market": market, "hypothetical_order": order.model_dump(mode="json")}
            try:
                opened = account.open(order, {symbol: market}, now=now, request_id=str(uuid4()))
                # Separate REST snapshot for exit: never assume same/latest price instant fill.
                exit_market = await snapshot(rest, symbol)
                closed = account.close_position(symbol, {symbol: exit_market}, now=int(time.time() * 1000))
                item.update(open=opened, close=closed)
            except ValueError as exc:
                if account.read()["positions"]:
                    raise
                item["rejected"] = str(exc)
            report["results"][symbol] = item
        report["final_account"] = account.read()
        report["history"] = account.history()
        Path(args.report).write_text(encode(report), encoding="utf-8")
        print(
            json.dumps(
                {
                    s: "filled virtual roundtrip" if "close" in r else r["rejected"]
                    for s, r in report["results"].items()
                },
                ensure_ascii=False,
            )
        )
    finally:
        account.close()
        await rest.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario-cny-per-usdt", required=True)
    parser.add_argument("--ledger", required=True, help="must be a new, separate test ledger")
    parser.add_argument("--report", default="reports/risk-real-data-smoke.json")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
