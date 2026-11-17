"""Auditable public OKX historical dataset download. No synthetic replacement data."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from quant.cli import setup_logging
from quant.config import BAR_MS, load_settings
from quant.models import number
from quant.okx import PublicREST


async def download(settings, *, start, end, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rest = PublicREST(settings)
    manifest = {
        "source": "OKX official public API",
        "start_ms": start,
        "end_ms": end,
        "downloaded_ms": int(time.time() * 1000),
        "files": {},
        "errors": [],
    }
    try:
        for symbol in settings.symbols:
            path = directory / f"{symbol}.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("start_ms") == start and cached.get("end_ms") == end:
                    manifest["files"][symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
                    continue
            print(f"Downloading {symbol}", flush=True)
            instrument = (await rest.get("/api/v5/public/instruments", instType="SWAP", instId=symbol))[0]
            tiers = await rest.get(
                "/api/v5/public/position-tiers",
                instType="SWAP",
                tdMode="isolated",
                instFamily=instrument["instFamily"],
            )
            data = {
                "symbol": symbol,
                "start_ms": start,
                "end_ms": end,
                "instrument": instrument,
                "tiers": tiers,
                "rules_observed_ms": int(time.time() * 1000),
                "candles": {},
            }
            for bar in BAR_MS:
                rows = await rest.history(symbol, bar, start, end)
                expected = (end - start) // BAR_MS[bar]
                if len(rows) != expected or any(b.ts - a.ts != BAR_MS[bar] for a, b in zip(rows, rows[1:])):
                    raise ValueError(f"missing price candles {symbol}/{bar}: {len(rows)} != {expected}")
                data["candles"][bar] = [c.model_dump(mode="json") for c in rows]
                print(f"  {bar}: {len(rows)} actual candles", flush=True)
            cursor, marks = end, {}
            while cursor > start:
                rows = await rest.get(
                    "/api/v5/market/history-mark-price-candles",
                    instId=symbol,
                    bar="1m",
                    after=str(cursor),
                    limit="100",
                )
                if not rows:
                    break
                oldest = min(int(r[0]) for r in rows)
                for row in rows:
                    ts = int(row[0])
                    if start <= ts < end and row[5] == "1":
                        values = [number(v, positive=True) for v in row[1:5]]
                        if (
                            ts % 60_000
                            or not values[2]
                            <= min(values[0], values[3])
                            <= max(values[0], values[3])
                            <= values[1]
                        ):
                            raise ValueError("invalid historical mark OHLC")
                        marks[ts] = {"ts": ts, "open": row[1], "high": row[2], "low": row[3], "close": row[4]}
                if oldest >= cursor:
                    raise ValueError("mark pagination did not advance")
                cursor = oldest
            if len(marks) != (end - start) // 60_000:
                raise ValueError(f"missing historical mark candles: {symbol}")
            data["marks"] = [marks[t] for t in sorted(marks)]
            cursor, funding = end, {}
            while cursor > start:
                rows = await rest.get(
                    "/api/v5/public/funding-rate-history", instId=symbol, after=str(cursor), limit="100"
                )
                if not rows:
                    break
                oldest = min(int(r["fundingTime"]) for r in rows)
                for row in rows:
                    ts = int(row["fundingTime"])
                    if start <= ts < end:
                        if row["instId"] != symbol or not row.get("realizedRate"):
                            raise ValueError("actual historical funding missing")
                        from decimal import Decimal

                        rate = Decimal(row["realizedRate"])
                        if not rate.is_finite() or abs(rate) > 1:
                            raise ValueError("invalid historical funding")
                        funding[ts] = row
                if oldest >= cursor:
                    raise ValueError("funding pagination did not advance")
                cursor = oldest
            if not funding:
                raise ValueError("funding dataset empty")
            data["funding"] = [funding[t] for t in sorted(funding)]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            manifest["files"][symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
            (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return manifest
    finally:
        await rest.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--warmup-days", type=int, default=2)
    parser.add_argument("--end", help="exclusive UTC date YYYY-MM-DD")
    parser.add_argument("--directory", default="data/history")
    args = parser.parse_args()
    if args.days < 1 or args.warmup_days < 2:
        parser.error("at least one test day and two warmup days required")
    end = (
        int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
        if args.end
        else (int(time.time() * 1000) // 86_400_000 * 86_400_000)
    )
    setup_logging()
    asyncio.run(
        download(
            load_settings(),
            start=end - (args.days + args.warmup_days) * 86_400_000,
            end=end,
            directory=args.directory,
        )
    )


if __name__ == "__main__":
    main()
