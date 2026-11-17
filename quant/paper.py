"""Local, isolated virtual-money ledger. This module has no network/order transport."""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from quant.models import number
from quant.risk import (
    Instrument,
    Policy,
    Tier,
    assess,
    depth_fill,
    liquidation,
    maintenance,
    pnl,
    sign,
)

D = Decimal


def encode(value):
    return json.dumps(
        value,
        default=lambda x: str(x) if isinstance(x, D) else x.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
    )


def trading_day(now):
    return datetime.fromtimestamp(now / 1000, timezone(timedelta(hours=8))).date().isoformat()


def fresh_market(market, now):
    """Validate independently of the UI; caller cannot bypass stale execution guards."""
    for channel in ("book", "mark", "ticker"):
        ts = int(market[channel]["ts"])
        if not 0 <= now - ts <= 10_000:
            raise ValueError(f"stale or future {channel}; paper execution refused")
    for key in ("instrument_ms", "tiers_ms"):
        if not 0 <= now - int(market[key]) <= 86_400_000:
            raise ValueError("missing/stale contract rules")
    return Instrument.model_validate(market["instrument"]), [Tier.model_validate(t) for t in market["tiers"]]


class PaperAccount:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('candles','snapshots')"
        ).fetchone():
            self.db.close()
            raise ValueError("paper ledger must be separate from market database")
        self.lock = threading.RLock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS journal (id TEXT PRIMARY KEY, ts INTEGER, kind TEXT, payload TEXT);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute("SELECT payload FROM account WHERE id=1").fetchone()
                state = json.loads(row[0]) if row else {}
                yield state
                self.db.execute("INSERT OR REPLACE INTO account VALUES (1,?)", (encode(state),))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def event(self, kind, now, data, event_id=None):
        self.db.execute(
            "INSERT INTO journal VALUES (?,?,?,?)", (event_id or str(uuid4()), now, kind, encode(data))
        )

    def initialize(self, *, cny=100, cny_per_usdt, rate_source, rate_time_ms, now, policy=None):
        cny, rate = number(cny, positive=True), number(cny_per_usdt, positive=True)
        if len(rate_source.strip()) < 3 or not 0 < rate_time_ms <= now:
            raise ValueError("explicit conversion source and observation time required")
        with self.transaction() as state:
            if state:
                raise ValueError("paper account already initialized; no implicit reset")
            state.update(
                mode="PAPER_ONLY",
                initial=cny / rate,
                cash=cny / rate,
                positions={},
                policy=(policy or Policy()).model_dump(mode="json"),
                day=trading_day(now),
                day_start=cny / rate,
                daily_loss=D(0),
                created_ms=now,
                conversion={
                    "cny": str(cny),
                    "cny_per_usdt": str(rate),
                    "source": rate_source,
                    "rate_time_ms": rate_time_ms,
                },
            )
            self.event("initialize", now, state)
        return self.read()

    def read(self):
        with self.lock:
            row = self.db.execute("SELECT payload FROM account WHERE id=1").fetchone()
            if row is None:
                raise ValueError("initialize virtual account with an explicit CNY/USDT conversion first")
            return json.loads(row[0])

    def history(self, limit=500):
        with self.lock:
            return [
                {"id": i, "ts": ts, "kind": k, "data": json.loads(p)}
                for i, ts, k, p in self.db.execute(
                    "SELECT id,ts,kind,payload FROM journal ORDER BY rowid DESC LIMIT ?", (limit,)
                )
            ]

    @staticmethod
    def valuation(state, markets, now):
        if not state:
            raise ValueError("paper account not initialized")
        equity = D(state["cash"])
        for symbol, pos in state["positions"].items():
            instrument, _ = fresh_market(markets[symbol], now)
            equity += D(pos["margin"]) + pnl(
                instrument, pos["quantity"], pos["entry"], markets[symbol]["mark"]["markPx"], pos["side"]
            )
        return equity

    @staticmethod
    def roll_day(state, equity, now):
        if state["day"] != trading_day(now):
            state.update(day=trading_day(now), day_start=str(max(D(0), equity)), daily_loss="0")

    def open(self, order, markets, *, now, request_id):
        if not request_id or len(request_id) > 100:
            raise ValueError("bounded unique request_id required")
        with self.transaction() as state:
            if self.db.execute("SELECT 1 FROM journal WHERE id=?", (request_id,)).fetchone():
                raise ValueError("duplicate request_id; no second fill")
            equity = self.valuation(state, markets, now)
            self.roll_day(state, equity, now)
            if order.symbol in state["positions"]:
                raise ValueError("one isolated paper position per instrument; close before reopening")
            instrument, tiers = fresh_market(markets[order.symbol], now)
            policy = Policy(**state["policy"])
            daily = max(D(0), D(state["day_start"]) * policy.daily_limit - D(state["daily_loss"]))
            reserved = sum(
                (max(D(0), D(p["planned_loss"]) - D(p["entry_fee"])) for p in state["positions"].values()),
                D(0),
            )
            quote = assess(
                order,
                instrument,
                tiers,
                markets[order.symbol]["book"],
                policy,
                equity=max(D(0), equity),
                free_cash=state["cash"],
                daily_budget=daily,
                reserved=reserved,
            )
            funding = markets[order.symbol]["funding"]
            if not 0 <= now - int(funding["ts"]) <= 180_000 or int(funding["fundingTime"]) <= now:
                raise ValueError("stale funding data; open refused")
            if abs(D(funding["fundingRate"])) > policy.funding_reserve_rate:
                raise ValueError("current funding exceeds planned reserve")
            state["cash"] = D(state["cash"]) - quote["margin"] - quote["entry_fee"]
            state["daily_loss"] = D(state["daily_loss"]) + quote["entry_fee"]
            pos = {
                **order.model_dump(mode="json"),
                **quote,
                "opened_ms": now,
                "funding_paid": D(0),
                "next_funding_ms": int(funding["fundingTime"]),
                "last_funding_ms": 0,
                "instrument": instrument.model_dump(mode="json"),
                "pending_exit": None,
            }
            state["positions"][order.symbol] = pos
            self.event("open", now, pos, request_id)
        return json.loads(encode(pos))

    def settle_funding(
        self, symbol, *, funding_ms, realized_rate, mark_price, price_source, next_funding_ms, now
    ):
        rate = D(str(realized_rate))
        if not rate.is_finite() or abs(rate) > 1 or not price_source or next_funding_ms <= funding_ms:
            raise ValueError("invalid actual funding rate/source")
        with self.transaction() as state:
            if not state:
                raise ValueError("paper account not initialized")
            pos = state["positions"].get(symbol)
            event_id = f"funding:{symbol}:{funding_ms}:{pos['opened_ms']}" if pos else ""
            if not pos or self.db.execute("SELECT 1 FROM journal WHERE id=?", (event_id,)).fetchone():
                return False
            if funding_ms != pos["next_funding_ms"]:
                raise ValueError("funding settlement must match next expected event")
            if not pos["opened_ms"] < funding_ms <= now or funding_ms <= pos["last_funding_ms"]:
                raise ValueError("invalid/unordered funding event")
            instrument = Instrument(**pos["instrument"])
            payment = (
                instrument.base_per_contract
                * D(pos["quantity"])
                * number(mark_price, positive=True)
                * rate
                * sign(pos["side"])
            )
            pos["margin"] = D(pos["margin"]) - payment
            pos["funding_paid"] = D(pos["funding_paid"]) + payment
            pos["last_funding_ms"] = funding_ms
            pos["next_funding_ms"] = next_funding_ms
            if state["day"] == trading_day(funding_ms):
                state["daily_loss"] = D(state["daily_loss"]) + max(D(0), payment)
            self.event(
                "funding",
                now,
                {
                    "symbol": symbol,
                    "settlement_ms": funding_ms,
                    "rate": rate,
                    "mark": mark_price,
                    "price_source": price_source,
                    "payment": payment,
                },
                event_id,
            )
        return True

    def _close(self, state, symbol, market, now, reason):
        pos = state["positions"][symbol]
        instrument, tiers = fresh_market(market, now)
        policy = Policy(**state["policy"])
        price = depth_fill(
            instrument, pos["quantity"], market["book"], buy=pos["side"] == "short", slippage=policy.slippage
        )
        gross = pnl(instrument, pos["quantity"], pos["entry"], price, pos["side"])
        base = D(pos["quantity"]) * instrument.base_per_contract
        fee = base * price * policy.fee
        clearance = base * D(market["mark"]["markPx"]) * tiers[0].mmr if reason == "liquidation" else D(0)
        release = D(pos["margin"]) + gross - fee - clearance
        deficit = max(D(0), -release)
        state["cash"] = D(state["cash"]) + max(D(0), release)
        state["daily_loss"] = D(state["daily_loss"]) + max(D(0), -gross) + fee + clearance
        trade = {
            **pos,
            "closed_ms": now,
            "exit": price,
            "reason": reason,
            "gross_pnl": gross,
            "exit_fee": fee,
            "clearance_fee": clearance,
            "isolated_deficit": deficit,
            "net_pnl": gross - D(pos["entry_fee"]) - D(pos["funding_paid"]) - fee - clearance,
            "account_net_pnl": gross
            - D(pos["entry_fee"])
            - D(pos["funding_paid"])
            - fee
            - clearance
            + deficit,
        }
        del state["positions"][symbol]
        self.event("close", now, trade)
        return trade

    def close_position(self, symbol, markets, *, now):
        with self.transaction() as state:
            if not state or symbol not in state["positions"]:
                raise ValueError("no paper position")
            equity = self.valuation(state, markets, now)
            self.roll_day(state, equity, now)
            pos = state["positions"][symbol]
            if pos["last_funding_ms"] < pos["next_funding_ms"] <= now:
                raise ValueError("settled funding must be reconciled before closing")
            instrument, tiers = fresh_market(markets[symbol], now)
            policy = Policy(**state["policy"])
            mark = markets[symbol]["mark"]["markPx"]
            pos_equity = D(pos["margin"]) + pnl(instrument, pos["quantity"], pos["entry"], mark, pos["side"])
            reason = (
                "liquidation"
                if pos_equity <= maintenance(instrument, pos["quantity"], mark, tiers, policy.fee)
                else "manual"
            )
            result = self._close(state, symbol, markets[symbol], now, reason)
        return json.loads(encode(result))

    def step(self, markets, *, now):
        """Observed tick execution only; never fabricate fills during app downtime."""
        actions = []
        with self.transaction() as state:
            equity = self.valuation(state, markets, now)
            self.roll_day(state, equity, now)
            for symbol, pos in list(state["positions"].items()):
                market = markets[symbol]
                instrument, tiers = fresh_market(market, now)
                policy = Policy(**state["policy"])
                mark, last = (
                    number(market["mark"]["markPx"], positive=True),
                    number(market["ticker"]["last"], positive=True),
                )
                equity_pos = D(pos["margin"]) + pnl(
                    instrument, pos["quantity"], pos["entry"], mark, pos["side"]
                )
                liq = equity_pos <= maintenance(instrument, pos["quantity"], mark, tiers, policy.fee)
                reason = "liquidation" if liq else pos.get("pending_exit")
                s = sign(pos["side"])
                if not reason and s * (last - D(pos["stop"])) <= 0:
                    reason = "stop_loss"
                elif not reason and s * (last - D(pos["target"])) >= 0:
                    reason = "take_profit"
                pos["liquidation_estimate"] = liquidation(
                    instrument,
                    pos["quantity"],
                    pos["entry"],
                    max(D(0), D(pos["margin"])),
                    pos["side"],
                    tiers,
                    policy.fee,
                )
                if reason:
                    pos["pending_exit"] = reason
                    try:
                        if pos["last_funding_ms"] < pos["next_funding_ms"] <= now:
                            raise ValueError("funding reconciliation required before exit")
                        actions.append(self._close(state, symbol, market, now, reason))
                    except ValueError as exc:
                        self.event(
                            "exit_pending", now, {"symbol": symbol, "reason": reason, "error": str(exc)}
                        )
        return json.loads(encode(actions))
