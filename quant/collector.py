import asyncio
import json
import logging
import random
import time
from decimal import Decimal

from websockets.asyncio.client import connect

from quant.config import BAR_MS
from quant.models import Candle, number, validate_snapshot
from quant.okx import OKXError, PublicREST
from quant.store import Store

log = logging.getLogger(__name__)
CHANNELS = ("tickers", "books5", "mark-price", "funding-rate")


class Collector:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.database)
        self.rest = PublicREST(settings)
        self.connections = {}
        self.repair = asyncio.Event()
        self.invalid_messages = 0
        self.ws_updates = {}
        self.active_sockets = {}

    def subscriptions(self, business):
        channels = [f"candle{b}" for b in self.settings.bars] if business else CHANNELS
        return [{"channel": c, "instId": s} for s in self.settings.symbols for c in channels]

    async def initialize(self):
        started = time.time() * 1000
        clock = await self.rest.get("/api/v5/public/time")
        self.store.status(
            "clock",
            {
                "server_ms": int(clock[0]["ts"]),
                "offset_ms": int(clock[0]["ts"]) - (started + time.time() * 1000) / 2,
            },
        )
        for symbol in self.settings.symbols:
            rows = await self.rest.get("/api/v5/public/instruments", instType="SWAP", instId=symbol)
            if len(rows) != 1:
                raise ValueError(f"missing instrument {symbol}")
            instrument = rows[0]
            if (
                instrument["instId"] != symbol
                or instrument["state"] != "live"
                or instrument["ctType"] != "linear"
                or instrument["settleCcy"] != "USDT"
            ):
                raise ValueError(f"unsupported instrument {symbol}")
            for key in ("ctVal", "lotSz", "minSz", "tickSz"):
                number(instrument[key], positive=True)
            self.store.status(
                f"instrument:{symbol}", {"received_ms": int(time.time() * 1000), "data": instrument}
            )
            tiers = await self.rest.get(
                "/api/v5/public/position-tiers",
                instType="SWAP",
                tdMode="isolated",
                instFamily=instrument["instFamily"],
            )
            from quant.risk import Tier

            if not tiers:
                raise ValueError(f"missing position tiers {symbol}")
            for row in tiers:
                Tier.from_okx(row)
            self.store.status(f"tiers:{symbol}", {"received_ms": int(time.time() * 1000), "data": tiers})
            for channel, path, params in (
                ("tickers", "/api/v5/market/ticker", {}),
                ("books5", "/api/v5/market/books", {"sz": "5"}),
                ("mark-price", "/api/v5/public/mark-price", {"instType": "SWAP"}),
                ("funding-rate", "/api/v5/public/funding-rate", {}),
            ):
                data = await self.rest.get(path, instId=symbol, **params)
                for row in data:
                    # REST funding has no ts; fundingTime is the next settlement, NOT observation time.
                    if channel == "funding-rate" and "ts" not in row:
                        row["ts"] = str(int(time.time() * 1000))
                    self.store.put_snapshot(symbol, channel, validate_snapshot(channel, row))
            for bar in self.settings.bars:
                await self.refresh_candles(symbol, bar)
        self.store.status("initialized", {"time_ms": int(time.time() * 1000)})

    async def refresh_candles(self, symbol, bar):
        recent = await self.rest.candles(symbol, bar, self.settings.initial_candles)
        if not recent:
            raise ValueError(f"empty candles {symbol} {bar}")
        self.store.put_candles(recent)
        for start, end in self.store.gaps(symbol, bar, self.settings.initial_candles):
            self.store.put_candles(await self.rest.history(symbol, bar, start, end + BAR_MS[bar]))

    def ingest(self, raw):
        if raw == "pong":
            return
        message = json.loads(raw)
        if message.get("event") == "error":
            raise OKXError(f"WebSocket rejected subscription: {message.get('code')} {message.get('msg')}")
        if message.get("event") == "notice":
            raise OKXError(f"WebSocket notice: {message.get('code')}")
        if message.get("event"):
            return
        arg = message["arg"]
        symbol, channel = arg["instId"], arg["channel"]
        if symbol not in self.settings.symbols:
            raise ValueError("unexpected instrument")
        if channel.startswith("candle"):
            bar = channel.removeprefix("candle")
            if bar not in self.settings.bars:
                raise ValueError("unexpected timeframe")
            candles = [Candle.from_okx(symbol, bar, row) for row in message["data"]]
            if any(c.ts > time.time() * 1000 + 60_000 for c in candles):
                raise ValueError("future candle timestamp")
            self.store.put_candles(candles)
            if self.store.gaps(symbol, bar):
                self.repair.set()
        else:
            for row in message["data"]:
                self.store.put_snapshot(symbol, channel, validate_snapshot(channel, row))
        key = f"{symbol}:{channel}"
        self.ws_updates[key] = self.ws_updates.get(key, 0) + 1

    async def socket(self, business):
        name = "business" if business else "public"
        url = self.settings.business_ws if business else self.settings.public_ws
        failures = 0
        while True:
            state = {
                "connected": False,
                "connected_ms": None,
                "last_message_ms": None,
                "subscriptions": [],
                "reconnects": failures,
            }
            self.connections[name] = state
            started = time.monotonic()
            try:
                async with connect(
                    url, ping_interval=None, open_timeout=15, close_timeout=5, max_size=2**20
                ) as ws:
                    self.active_sockets[name] = ws
                    state.update(connected=True, connected_ms=int(time.time() * 1000))
                    await ws.send(json.dumps({"op": "subscribe", "args": self.subscriptions(business)}))
                    self.repair.set()
                    log.info("WebSocket connected %s", name)
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        except TimeoutError:
                            await ws.send("ping")
                            raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        state["last_message_ms"] = int(time.time() * 1000)
                        try:
                            self.ingest(raw)
                            if raw != "pong":
                                msg = json.loads(raw)
                                if msg.get("event") == "subscribe":
                                    state["subscriptions"].append(msg["arg"])
                        except (ValueError, KeyError, TypeError, ArithmeticError):
                            self.invalid_messages += 1
                            log.exception("Rejected malformed %s message", name)
                        if time.monotonic() > deadline and len(state["subscriptions"]) != len(
                            self.subscriptions(business)
                        ):
                            raise OKXError("subscription acknowledgements incomplete")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("WebSocket disconnected %s: %s", name, exc)
                state["error"] = str(exc)
            finally:
                self.active_sockets.pop(name, None)
                state["connected"] = False
            failures = 1 if time.monotonic() - started > 60 else failures + 1
            await asyncio.sleep(min(60, 2 ** min(failures, 6)) + random.random())

    async def reconcile(self):
        while True:
            try:
                await asyncio.wait_for(self.repair.wait(), timeout=60)
            except TimeoutError:
                pass
            self.repair.clear()
            try:
                for symbol in self.settings.symbols:
                    saved = self.store.statuses().get(f"instrument:{symbol}", {})
                    if time.time() * 1000 - saved.get("received_ms", 0) >= 3_600_000:
                        rows = await self.rest.get(
                            "/api/v5/public/instruments", instType="SWAP", instId=symbol
                        )
                        from quant.risk import Instrument, Tier

                        Instrument.from_okx(rows[0])
                        tiers = await self.rest.get(
                            "/api/v5/public/position-tiers",
                            instType="SWAP",
                            tdMode="isolated",
                            instFamily=rows[0]["instFamily"],
                        )
                        if not tiers:
                            raise ValueError("missing position tiers refresh")
                        for tier in tiers:
                            Tier.from_okx(tier)
                        stamp = int(time.time() * 1000)
                        self.store.status(f"instrument:{symbol}", {"received_ms": stamp, "data": rows[0]})
                        self.store.status(f"tiers:{symbol}", {"received_ms": stamp, "data": tiers})
                    for bar in self.settings.bars:
                        await self.refresh_candles(symbol, bar)
                self.store.status("repair", {"ok": True, "time_ms": int(time.time() * 1000)})
            except Exception as exc:
                log.exception("Candle reconciliation failed")
                self.store.status(
                    "repair", {"ok": False, "error": str(exc), "time_ms": int(time.time() * 1000)}
                )
                await asyncio.sleep(10)
                self.repair.set()

    def health(self):
        now = int(time.time() * 1000)
        markets = {}
        for symbol in self.settings.symbols:
            snapshots = self.store.snapshots(symbol)
            channels = {}
            for c in CHANNELS:
                item = snapshots.get(c)
                # Funding push frequency is much lower than price/book frequency.
                threshold = 180_000 if c == "funding-rate" else self.settings.stale_seconds * 1000
                channels[c] = {
                    "present": bool(item),
                    "stale": item is None
                    or (now - item["exchange_ts"] > threshold or item["exchange_ts"] > now + 60_000),
                }
            bars = {}
            for bar in self.settings.bars:
                candles = self.store.candles(symbol, bar, 300, confirmed_only=True)
                latest = candles[-1].ts if candles else None
                gaps = self.store.gaps(symbol, bar)
                bars[bar] = {
                    "confirmed_count": len(candles),
                    "latest_open_ms": latest,
                    "gaps": gaps,
                    "stale": latest is None or now - latest > BAR_MS[bar] * 2 + 30_000,
                }
            markets[symbol] = {"channels": channels, "bars": bars}
        ready = all(v.get("connected") for v in self.connections.values()) and len(self.connections) == 2
        for market in markets.values():
            ready = ready and all(not x["stale"] for x in market["channels"].values())
            ready = ready and all(
                not x["stale"] and not x["gaps"] and x["confirmed_count"] >= 60
                for x in market["bars"].values()
            )
        return {
            "time_ms": now,
            "ready": bool(ready),
            "mode": "PUBLIC_DATA_ONLY",
            "connections": json.loads(json.dumps(self.connections)),
            "invalid_messages": self.invalid_messages,
            "ws_updates": dict(self.ws_updates),
            "markets": markets,
        }

    async def heartbeat(self):
        while True:
            self.store.status("collector", self.health())
            await asyncio.sleep(2)

    async def run(self, seconds=None):
        tasks = []
        try:
            await self.initialize()
            tasks = [
                asyncio.create_task(self.socket(False)),
                asyncio.create_task(self.socket(True)),
                asyncio.create_task(self.reconcile()),
                asyncio.create_task(self.heartbeat()),
            ]
            if seconds is None:
                await asyncio.gather(*tasks)
            else:
                await asyncio.sleep(seconds)
                return self.health()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.store.status("collector", {"ready": False, "stopped_ms": int(time.time() * 1000)})
            await self.rest.close()
            self.store.close()


def market_overview(store, settings):
    result = []
    for symbol in settings.symbols:
        snapshots = store.snapshots(symbol)
        ticker = snapshots.get("tickers", {}).get("data", {})
        last, opening = ticker.get("last"), ticker.get("open24h")
        change = (Decimal(last) / Decimal(opening) - 1) * 100 if last and opening else None
        result.append(
            {
                "symbol": symbol,
                "last": last,
                "change_24h_pct": str(change) if change is not None else None,
                "snapshots": snapshots,
            }
        )
    return result
