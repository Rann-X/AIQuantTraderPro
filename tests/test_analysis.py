import pytest

from quant.analysis import analyze_market
from quant.config import Settings
from quant.store import Store


def test_measured_live_price_separate_from_signal_close_and_stale_guard(tmp_path):
    store = Store(tmp_path / "market.db")
    now = 1_800_000_000_000
    store.put_snapshot("BTC-USDT-SWAP", "tickers", {"ts": str(now), "last": "65000.1"})
    result = analyze_market(store, Settings(), "BTC-USDT-SWAP", as_of_ms=now)
    assert result["current_price"] == "65000.1"
    assert result["reference_close"] is None
    stale = analyze_market(store, Settings(), "BTC-USDT-SWAP", as_of_ms=now + 11_000)
    assert stale["current_price"] is None
    assert stale["status"] == "暂不交易"
    store.close()


@pytest.mark.parametrize(
    "side,price,stop,target",
    [("做多", "98", 99, 104), ("做多", "105", 99, 104), ("做空", "102", 101, 96), ("做空", "95", 101, 96)],
)
def test_current_price_can_invalidate_but_never_create_candidate(
    tmp_path, monkeypatch, side, price, stop, target
):
    store = Store(tmp_path / "market.db")
    now = 1_800_000_000_000
    store.put_snapshot("BTC-USDT-SWAP", "tickers", {"ts": str(now), "last": price})
    fixture = {
        "status": "候选信号",
        "current_price": 100,
        "direction": side,
        "stop": stop,
        "take_profit": target,
        "entry": 100,
        "warnings": [],
    }
    monkeypatch.setattr("quant.analysis.analyze", lambda *a, **kw: dict(fixture, warnings=[]))
    result = analyze_market(store, Settings(), "BTC-USDT-SWAP", as_of_ms=now)
    assert result["status"] == "暂不交易"
    assert result["entry"] is None
    assert result["current_price"] == price
    store.close()
