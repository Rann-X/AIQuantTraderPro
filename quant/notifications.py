import base64
import os
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from pydantic import BaseModel, ConfigDict, Field


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    cooldown_seconds: int = Field(300, ge=10, le=86400)
    minimum_interval_seconds: int = Field(10, ge=1, le=3600)
    analysis_interval_seconds: int = Field(10, ge=2, le=300)


def native_toast(title, body):
    if os.name != "nt":
        raise OSError("native toast requires Windows")
    toast = Element("toast")
    binding = SubElement(SubElement(toast, "visual"), "binding", template="ToastGeneric")
    SubElement(binding, "text").text = title
    SubElement(binding, "text").text = body
    xml = base64.b64encode(tostring(toast, encoding="utf-8")).decode("ascii")
    # Only base64-encoded XML enters this constant script, never interpolated executable text.
    script = """
$ErrorActionPreference='Stop'
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null
$doc=New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('PAYLOAD')))
$toast=[Windows.UI.Notifications.ToastNotification]::new($doc)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.WindowsPowerShell').Show($toast)
""".replace("PAYLOAD", xml)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ],
        check=True,
        timeout=15,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class Notifier:
    def __init__(self, path, settings=None, sender=None):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=20, isolation_level=None, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY,symbol TEXT,ts INTEGER)")
        self.settings = settings or NotificationSettings()
        self.sender = sender or native_toast
        self.lock = threading.Lock()

    def close(self):
        self.db.close()

    def notify(self, signal, *, now):
        if not self.settings.enabled or signal["status"] != "候选信号":
            return False
        if not signal.get("price_close_ms") or not signal["price_close_ms"] <= now < signal["expires_ms"]:
            return False
        key = f"{signal['symbol']}:{signal['direction']}:{signal['setup']}:{signal['price_close_ms']}"
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                prior = self.db.execute(
                    "SELECT MAX(ts) FROM sent WHERE symbol=?", (signal["symbol"],)
                ).fetchone()[0]
                any_prior = self.db.execute("SELECT MAX(ts) FROM sent").fetchone()[0]
                seen = self.db.execute("SELECT 1 FROM sent WHERE id=?", (key,)).fetchone()
                if (
                    seen
                    or (prior is not None and now - prior < self.settings.cooldown_seconds * 1000)
                    or (
                        any_prior is not None
                        and now - any_prior < self.settings.minimum_interval_seconds * 1000
                    )
                ):
                    self.db.execute("ROLLBACK")
                    return False
                stamp = datetime.fromtimestamp(
                    signal["price_close_ms"] / 1000, timezone(timedelta(hours=8))
                ).isoformat()
                body = (
                    f"{signal['direction']}；当前价{signal['current_price']}；信号收盘价{signal.get('reference_close', signal['current_price'])}\n"
                    f"触发：{'；'.join(signal['conditions'])}\n止损{signal['stop']}；止盈{signal['take_profit']}\n"
                    f"风险：高（20倍参考杠杆，尚需账户预算审核）\n信号时间{stamp}"
                )
                self.sender(f"AI Quant · {signal['symbol']} · 规则候选", body)
                self.db.execute("INSERT INTO sent VALUES (?,?,?)", (key, signal["symbol"], now))
                self.db.execute("COMMIT")
                return True
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise
