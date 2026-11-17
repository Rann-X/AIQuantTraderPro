import base64
import subprocess
from unittest.mock import Mock

import pytest

from quant.explain import explain_signal, review_trades
from quant.notifications import NotificationSettings, Notifier, native_toast
from quant.strategy import analyze


def signal(now=1_800_000_000_000):
    # Synthetic alert fixture only, not a generated market opportunity.
    return {
        "symbol": "SOL-USDT-SWAP",
        "status": "候选信号",
        "direction": "做多",
        "setup": "趋势突破",
        "price_close_ms": now,
        "expires_ms": now + 60_000,
        "current_price": 100,
        "stop": 99,
        "take_profit": 104,
        "conditions": ["synthetic unit test condition"],
    }


def test_no_data_explanation_does_not_invent_price_or_trades():
    result = explain_signal(analyze("BTC-USDT-SWAP", {}, as_of_ms=1_800_000_000_000))
    assert "暂不交易" in result["text"]
    assert "缺少可用" in result["text"]
    assert result["engine"] == "grounded_rules"
    assert review_trades([])["win_rate"] is None


def test_alert_dedup_cooldown_persistence_and_expiry(tmp_path):
    now = 1_800_000_000_000
    sender = Mock()
    path = tmp_path / "notify.db"
    notifier = Notifier(path, sender=sender)
    assert notifier.notify(signal(now), now=now)
    assert not notifier.notify(signal(now), now=now + 1000)
    assert not notifier.notify(signal(now + 60_000), now=now + 60_000)
    notifier.close()
    notifier = Notifier(path, sender=sender)
    assert not notifier.notify(signal(now), now=now + 500_000)
    assert notifier.notify(signal(now + 300_000), now=now + 300_000)
    assert sender.call_count == 2
    notifier.close()


def test_alert_failure_not_marked_sent_and_disabled(tmp_path):
    now = 1_800_000_000_000
    sender = Mock(side_effect=OSError("test"))
    notifier = Notifier(tmp_path / "notify.db", sender=sender)
    with pytest.raises(OSError):
        notifier.notify(signal(now), now=now)
    sender.side_effect = None
    assert notifier.notify(signal(now), now=now)
    notifier.close()
    notifier = Notifier(tmp_path / "disabled.db", NotificationSettings(enabled=False), sender=sender)
    assert not notifier.notify(signal(now), now=now)
    notifier.close()


def test_windows_toast_xml_is_data_not_executable(monkeypatch):
    import quant.notifications as module

    runner = Mock()
    monkeypatch.setattr(module.subprocess, "run", runner)
    # Target Windows build: verify XML data is double-encoded and passed through EncodedCommand.
    native_toast("test'; $(Write-Output unsafe)", "<data>&body")
    args = runner.call_args.args[0]
    assert "-WindowStyle" in args and "Hidden" in args
    script = base64.b64decode(args[-1]).decode("utf-16-le")
    assert "Write-Output unsafe" not in script
    assert "FromBase64String" in script
    assert runner.call_args.kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
