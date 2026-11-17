from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from quant.config import Settings
from quant.preferences import save_settings

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_settings(Settings())
    return AppTest.from_file(str(APP), default_timeout=20)


@pytest.mark.parametrize(
    "page", ["市场总览", "行情与指标", "AI分析", "交易模拟", "策略回测", "风险管理", "系统设置"]
)
def test_all_pages_handle_empty_real_data_without_placeholders(ui, page):
    ui.run()
    ui.sidebar.radio(key="page").set_value(page).run()
    assert not ui.exception
    assert not ui.error
    if page == "AI分析":
        assert any("暂不交易" in x.value for x in ui.markdown)


def test_invalid_fx_is_rejected_in_form(ui):
    ui.run()
    ui.sidebar.radio(key="page").set_value("交易模拟").run()
    ui.text_input[0].set_value("NaN")
    ui.text_input[1].set_value("synthetic unit test")
    next(b for b in ui.button if b.label == "初始化虚拟账户").click().run()
    assert not ui.exception
    assert ui.error


def test_settings_roundtrip_and_model_fallback(ui):
    ui.run()
    ui.sidebar.radio(key="page").set_value("系统设置").run()
    save = next(b for b in ui.button if b.label == "校验并保存配置")
    save.click().run()
    assert not ui.exception
    assert ui.success
    ui.sidebar.radio(key="page").set_value("AI分析").run()
    next(b for b in ui.button if b.label == "调用兼容模型整理解释").click().run()
    assert any("not_configured" in x.value for x in ui.info)


def test_simulation_form_initialization_and_order_roundtrip(ui):
    import time

    from quant.paper import PaperAccount
    from quant.store import Store

    symbol = "SOL-USDT-SWAP"
    save_settings(Settings(symbols=[symbol]))
    store = Store("data/market.sqlite3")
    now = int(time.time() * 1000)
    # Explicit synthetic UI fixture, isolated under pytest's temporary directory.
    instrument = {
        "instId": symbol,
        "ctType": "linear",
        "settleCcy": "USDT",
        "state": "live",
        "ctValCcy": "SOL",
        "ctVal": "1",
        "ctMult": "1",
        "lotSz": "0.01",
        "minSz": "0.01",
        "tickSz": "0.01",
        "maxMktSz": "10000",
    }
    tiers = [{"tier": "1", "minSz": "0", "maxSz": "1000", "mmr": "0.005", "imr": "0.01", "maxLever": "100"}]
    store.status(f"instrument:{symbol}", {"received_ms": now, "data": instrument})
    store.status(f"tiers:{symbol}", {"received_ms": now, "data": tiers})
    rows = {
        "tickers": {
            "instId": symbol,
            "ts": str(now),
            "last": "100",
            "open24h": "100",
            "high24h": "101",
            "low24h": "99",
            "vol24h": "1000",
        },
        "mark-price": {"instId": symbol, "ts": str(now), "markPx": "100"},
        "books5": {"ts": str(now), "bids": [["99.99", "1000"]], "asks": [["100.01", "1000"]]},
        "funding-rate": {
            "instId": symbol,
            "ts": str(now),
            "fundingRate": "0.0001",
            "fundingTime": str(now + 3_600_000),
        },
    }
    for channel, row in rows.items():
        store.put_snapshot(symbol, channel, row)
    store.close()
    ui.run()
    ui.sidebar.radio(key="page").set_value("交易模拟").run()
    ui.text_input[0].set_value("7")
    ui.text_input[1].set_value("synthetic UI conversion test only")
    next(b for b in ui.button if b.label == "初始化虚拟账户").click().run()
    assert not ui.error and not ui.exception
    ui.text_input[0].set_value("0.1")
    ui.text_input[1].set_value("99")
    ui.text_input[2].set_value("104")
    next(b for b in ui.button if b.label == "提交模拟开仓").click().run()
    assert not ui.error and not ui.exception
    ui.run()
    next(b for b in ui.button if b.label == "模拟平仓").click().run()
    assert not ui.error and not ui.exception
    account = PaperAccount("data/paper.sqlite3")
    assert not account.read()["positions"]
    assert [r["kind"] for r in account.history()] == ["close", "open", "initialize"]
    account.close()
