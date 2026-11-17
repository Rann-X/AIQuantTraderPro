import argparse
import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from quant.collector import Collector
from quant.config import load_settings


def setup_logging():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    Path("logs").mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        f"logs/quant-{os.getpid()}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler, logging.StreamHandler()],
    )


def main():
    parser = argparse.ArgumentParser(description="OKX read-only market collector")
    parser.add_argument("command", choices=["collect", "probe", "api"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--seconds", type=int, default=75)
    parser.add_argument("--report", default="reports/live-probe.json")
    args = parser.parse_args()
    setup_logging()
    settings = load_settings(args.config)
    if args.command == "api":
        import uvicorn

        from quant.api import create_app

        uvicorn.run(create_app(settings), host="127.0.0.1", port=8765)
    elif args.command == "collect":
        asyncio.run(Collector(settings).run())
    else:
        try:
            report = asyncio.run(Collector(settings).run(args.seconds))
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps(report, indent=2, ensure_ascii=False))
            raise SystemExit(
                0
                if report["ready"]
                and len(report["ws_updates"]) == len(settings.symbols) * (len(settings.bars) + 4)
                else 2
            )
        except Exception:
            logging.exception("Live probe failed; no synthetic data substituted")
            raise


if __name__ == "__main__":
    main()
