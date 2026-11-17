import argparse
import asyncio
import json
import time
from uuid import uuid4

from quant.cli import setup_logging
from quant.config import load_settings
from quant.okx import PublicREST
from quant.paper import PaperAccount, encode
from quant.paper_service import market_inputs, reconcile_funding, watch
from quant.risk import Order, Policy, assess
from quant.store import Store


def main():
    parser = argparse.ArgumentParser(description="Isolated virtual-money account only")
    parser.add_argument("command", choices=["init", "status", "history", "quote", "open", "close", "watch"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--database", default="data/paper.sqlite3")
    parser.add_argument("--cny", default="100")
    parser.add_argument("--cny-per-usdt")
    parser.add_argument("--rate-source")
    parser.add_argument("--rate-time-ms", type=int)
    parser.add_argument("--symbol", default="BTC-USDT-SWAP")
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--quantity")
    parser.add_argument("--stop")
    parser.add_argument("--target")
    parser.add_argument("--request-id")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings(args.config)
    account, store = PaperAccount(args.database), Store(settings.database)
    try:
        now = int(time.time() * 1000)
        if args.command == "init":
            if not all((args.cny_per_usdt, args.rate_source, args.rate_time_ms)):
                parser.error("init requires explicit --cny-per-usdt --rate-source --rate-time-ms")
            result = account.initialize(
                cny=args.cny,
                cny_per_usdt=args.cny_per_usdt,
                rate_source=args.rate_source,
                rate_time_ms=args.rate_time_ms,
                now=now,
                policy=Policy(**settings.risk),
            )
        elif args.command == "status":
            result = account.read()
            try:
                result["equity"] = account.valuation(
                    result, market_inputs(store, list(result["positions"])), now
                )
            except (ValueError, KeyError) as exc:
                result["equity"] = None
                result["valuation_error"] = str(exc)
        elif args.command == "history":
            result = account.history()
        elif args.command == "watch":
            asyncio.run(watch(account, store, settings))
            return
        else:
            state = account.read()
            markets = market_inputs(store, sorted(set(state["positions"]) | {args.symbol}))
            if args.command == "close":

                async def reconcile():
                    rest = PublicREST(settings)
                    try:
                        await reconcile_funding(account, markets, rest, int(time.time() * 1000))
                    finally:
                        await rest.close()

                asyncio.run(reconcile())
                result = account.close_position(
                    args.symbol, market_inputs(store, list(state["positions"])), now=int(time.time() * 1000)
                )
            else:
                if not all((args.quantity, args.stop, args.target)):
                    parser.error("quote/open require --quantity --stop --target")
                order = Order(
                    symbol=args.symbol,
                    side=args.side,
                    quantity=args.quantity,
                    stop=args.stop,
                    target=args.target,
                )
                if args.command == "open":
                    result = account.open(order, markets, now=now, request_id=args.request_id or str(uuid4()))
                else:
                    from decimal import Decimal as D

                    from quant.paper import fresh_market

                    equity = account.valuation(state, markets, now)
                    account.roll_day(state, equity, now)
                    policy = Policy(**state["policy"])
                    instrument, tiers = fresh_market(markets[args.symbol], now)
                    result = assess(
                        order,
                        instrument,
                        tiers,
                        markets[args.symbol]["book"],
                        policy,
                        equity=max(D(0), equity),
                        free_cash=state["cash"],
                        daily_budget=max(
                            D(0), D(state["day_start"]) * policy.daily_limit - D(state["daily_loss"])
                        ),
                        reserved=sum(
                            (
                                max(D(0), D(p["planned_loss"]) - D(p["entry_fee"]))
                                for p in state["positions"].values()
                            ),
                            D(0),
                        ),
                    )
        print(json.dumps(json.loads(encode(result)), ensure_ascii=False, indent=2))
    finally:
        account.close()
        store.close()


if __name__ == "__main__":
    main()
