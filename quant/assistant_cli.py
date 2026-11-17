import argparse
import json
import logging
import subprocess
import time

from quant.analysis import analyze_market
from quant.cli import setup_logging
from quant.config import load_settings
from quant.explain import explain_signal, review_trades
from quant.notifications import NotificationSettings, Notifier, native_toast
from quant.paper import PaperAccount
from quant.store import Store

log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Grounded market explanations and Windows notifications")
    parser.add_argument("command", choices=["explain", "watch", "review", "test-toast"])
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--paper-database", default="data/paper.sqlite3")
    parser.add_argument(
        "--llm", action="store_true", help="explicitly call configured compatible model for explanations"
    )
    args = parser.parse_args()
    setup_logging()
    if args.command == "test-toast":
        native_toast("AI Quant 系统测试", "这是系统通知测试，不代表交易机会。")
        print("Windows toast API accepted the system test notification")
        return
    if args.command == "review":
        account = PaperAccount(args.paper_database)
        try:
            print(json.dumps(review_trades(account.history(100000)), ensure_ascii=False, indent=2))
        finally:
            account.close()
        return
    settings = load_settings(args.config)
    notification_settings = NotificationSettings(**settings.notifications)
    store, notifier = Store(settings.database), Notifier("data/notifications.sqlite3", notification_settings)
    try:
        while True:
            now = int(time.time() * 1000)
            for symbol in settings.symbols:
                signal = analyze_market(store, settings, symbol, as_of_ms=now)
                explanation = explain_signal(signal)
                if args.llm:
                    from quant.llm import explain_with_model

                    explanation = explain_with_model(signal)
                if args.command == "explain":
                    print(json.dumps(explanation, ensure_ascii=False, indent=2))
                else:
                    store.status(f"analysis:{symbol}", {"signal": signal, "explanation": explanation})
                    try:
                        notifier.notify(signal, now=now)
                    except (OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
                        log.warning("Desktop notification failed: %s", exc)
            if args.command != "watch":
                break
            time.sleep(notification_settings.analysis_interval_seconds)
    finally:
        notifier.close()
        store.close()


if __name__ == "__main__":
    main()
