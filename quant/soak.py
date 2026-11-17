import argparse
import asyncio
import json
import logging
import time
from pathlib import Path

from quant.cli import setup_logging
from quant.collector import Collector
from quant.config import load_settings


async def run(seconds, path):
    settings = load_settings().model_copy(update={"database": Path("../../work/soak-market.sqlite3")})
    collector = Collector(settings)
    process = asyncio.create_task(collector.run(seconds))
    history, disconnected = [], False
    initial_rules = None
    began = time.monotonic()
    try:
        while not process.done():
            await asyncio.sleep(5)
            if process.done():
                break
            if collector.store.db:
                snapshot = collector.health()
                history.append(snapshot)
            if not disconnected and time.monotonic() - began > 60 and len(collector.active_sockets) == 2:
                # Actually close both live server connections; production reconnect loop must recover.
                await asyncio.gather(
                    *(
                        s.close(code=1012, reason="controlled recovery verification")
                        for s in list(collector.active_sockets.values())
                    )
                )
                disconnected = True
            if initial_rules is None and snapshot["ready"]:
                initial_rules = (
                    collector.store.statuses().get("instrument:BTC-USDT-SWAP", {}).get("received_ms")
                )
                # Expire cached rules to exercise the real periodic refresh branch.
                for symbol in settings.symbols:
                    for key in (f"instrument:{symbol}", f"tiers:{symbol}"):
                        value = collector.store.statuses()[key]
                        value["received_ms"] = 0
                        collector.store.status(key, value)
                collector.repair.set()
            if process.done():
                break
        final = await process
        snapshots = history + [final]
        connections = {
            name: sorted(
                {
                    r["connections"].get(name, {}).get("connected_ms")
                    for r in snapshots
                    if r["connections"].get(name, {}).get("connected_ms")
                }
            )
            for name in ("public", "business")
        }
        bar_changes = {
            f"{s}:{b}": len(
                {
                    r["markets"][s]["bars"][b]["latest_open_ms"]
                    for r in snapshots
                    if r["markets"][s]["bars"][b]["latest_open_ms"] is not None
                }
            )
            for s in settings.symbols
            for b in settings.bars
        }
        report = {
            "seconds_requested": seconds,
            "elapsed_seconds": time.monotonic() - began,
            "controlled_disconnect": disconnected,
            "connections_seen": connections,
            "confirmed_bar_versions": bar_changes,
            "healthy_samples": sum(r["ready"] for r in history),
            "samples": len(history),
            "final": final,
            "history": history,
            "passed": bool(
                final["ready"]
                and disconnected
                and all(len(v) >= 2 for v in connections.values())
                and all(v >= 2 for v in bar_changes.values())
                and final["invalid_messages"] == 0
            ),
        }
        Path(path).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in ("history", "final")}, indent=2))
        return report["passed"]
    finally:
        if not process.done():
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1000)
    parser.add_argument("--report", default="reports/soak.json")
    args = parser.parse_args()
    setup_logging()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    raise SystemExit(0 if asyncio.run(run(args.seconds, args.report)) else 2)


if __name__ == "__main__":
    main()
