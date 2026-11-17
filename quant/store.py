import json
import sqlite3
import time
from pathlib import Path

from quant.config import BAR_MS
from quant.models import Candle


class Store:
    """One collector writer; short SQLite WAL reads also support a separate UI process."""

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT, bar TEXT, ts INTEGER, payload TEXT NOT NULL, confirmed INTEGER NOT NULL,
                PRIMARY KEY(symbol,bar,ts));
            CREATE TABLE IF NOT EXISTS snapshots (
                symbol TEXT, channel TEXT, ts INTEGER NOT NULL, received INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(symbol,channel));
            CREATE TABLE IF NOT EXISTS status (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    def put_candles(self, candles):
        with self.db:
            self.db.executemany(
                """
                INSERT INTO candles VALUES (?,?,?,?,?) ON CONFLICT(symbol,bar,ts) DO UPDATE SET
                payload=excluded.payload, confirmed=excluded.confirmed
                WHERE candles.confirmed=0 OR excluded.confirmed=1
            """,
                [(c.symbol, c.bar, c.ts, c.model_dump_json(), int(c.confirmed)) for c in candles],
            )

    def candles(self, symbol, bar, limit=300, confirmed_only=False):
        rows = self.db.execute(
            """
            SELECT payload FROM candles WHERE symbol=? AND bar=? AND (?=0 OR confirmed=1)
            ORDER BY ts DESC LIMIT ?
        """,
            (symbol, bar, int(confirmed_only), limit),
        ).fetchall()
        return [Candle.model_validate_json(row[0]) for row in reversed(rows)]

    def gaps(self, symbol, bar, limit=300):
        candles = self.candles(symbol, bar, limit)
        step = BAR_MS[bar]
        return [(a.ts + step, b.ts - step) for a, b in zip(candles, candles[1:]) if b.ts - a.ts > step]

    def put_snapshot(self, symbol, channel, row):
        with self.db:
            self.db.execute(
                """
                INSERT INTO snapshots VALUES (?,?,?,?,?) ON CONFLICT(symbol,channel) DO UPDATE SET
                ts=excluded.ts, received=excluded.received, payload=excluded.payload
                WHERE excluded.ts>=snapshots.ts
            """,
                (symbol, channel, int(row["ts"]), int(time.time() * 1000), json.dumps(row)),
            )

    def snapshots(self, symbol):
        return {
            channel: {"exchange_ts": ts, "received_ts": received, "data": json.loads(payload)}
            for channel, ts, received, payload in self.db.execute(
                "SELECT channel,ts,received,payload FROM snapshots WHERE symbol=?", (symbol,)
            )
        }

    def status(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO status VALUES (?,?)", (key, json.dumps(value)))

    def statuses(self):
        return {
            key: json.loads(payload) for key, payload in self.db.execute("SELECT key,payload FROM status")
        }
