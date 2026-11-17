import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from quant.api import create_app
from quant.collector import Collector
from quant.config import Settings
from quant.models import Candle, validate_snapshot
from quant.okx import PublicREST
from quant.store import Store

SYMBOL = "BTC-USDT-SWAP"


def candle(ts=1_700_000_040_000, confirmed="1", bar="1m"):
    # Synthetic unit-test fixture only; never used by production or live reports.
    return Candle.from_okx(SYMBOL, bar, [str(ts), "100", "102", "99", "101", "10", "0.1", "10.1", confirmed])


def test_decimal_confirmation_and_ohlc():
    assert candle().close == Decimal("101")
    assert not candle(confirmed="0").confirmed
    for bad in (
        ["1"] * 8,
        ["1"] * 8 + ["2"],
        ["1700000040000", "100", "99", "90", "101", "1", "1", "1", "1"],
    ):
        with pytest.raises(ValueError):
            Candle.from_okx(SYMBOL, "1m", bad)


def test_invalid_alignment_and_nonfinite():
    with pytest.raises(ValueError):
        candle(ts=1)
    with pytest.raises(ValueError):
        candle().model_copy(update={}).__class__(**{**candle().model_dump(), "close": "NaN"})


def test_store_confirmed_not_downgraded_and_gaps(tmp_path):
    store = Store(tmp_path / "test.db")
    c = candle()
    store.put_candles([c, candle(c.ts + 120_000, "0")])
    store.put_candles([candle(confirmed="0")])
    assert store.candles(SYMBOL, "1m", confirmed_only=True) == [c]
    assert store.gaps(SYMBOL, "1m") == [(c.ts + 60_000, c.ts + 60_000)]
    store.put_candles([candle(c.ts + 60_000)])
    assert not store.gaps(SYMBOL, "1m")
    store.close()


def test_snapshot_out_of_order(tmp_path):
    store = Store(tmp_path / "test.db")
    store.put_snapshot(SYMBOL, "mark-price", {"ts": "100", "markPx": "100"})
    store.put_snapshot(SYMBOL, "mark-price", {"ts": "99", "markPx": "1"})
    assert store.snapshots(SYMBOL)["mark-price"]["data"]["markPx"] == "100"
    store.close()


@pytest.mark.parametrize(
    "row",
    [
        {"ts": "1", "bids": [["101", "2"]], "asks": [["100", "1"]]},
        {"ts": "1", "bids": [["99", "0"]], "asks": [["100", "1"]]},
        {"ts": "1", "bids": [["99", "1"], ["100", "1"]], "asks": [["101", "1"]]},
    ],
)
def test_invalid_books(row):
    with pytest.raises(ValueError):
        validate_snapshot("books5", row)


@pytest.mark.parametrize(
    "values", [{"symbols": ["BTC-USDT"]}, {"bars": ["1h"]}, {"public_ws": "wss://ws.okx.com/ws/v5/private"}]
)
def test_config_boundaries(values):
    with pytest.raises(ValueError):
        Settings(**values)


async def test_rest_public_only():
    rest = PublicREST(Settings())
    with pytest.raises(ValueError):
        await rest.get("/api/v5/trade/order")
    await rest.close()


async def test_rest_rate_limit_retry(monkeypatch):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"code": "50011" if count == 1 else "0", "data": []})

    sleep = AsyncMock()
    monkeypatch.setattr("quant.okx.asyncio.sleep", sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://www.okx.com")
    rest = PublicREST(Settings(), client)
    assert await rest.get("/api/v5/public/time") == []
    assert count == 2
    assert sleep.await_count >= 3
    await rest.close()


async def test_history_pagination_and_confirmed_filter():
    rest = PublicREST(Settings())
    c = candle()
    rest.candles = AsyncMock(side_effect=[[candle(c.ts + 60_000, "0"), c], [candle(c.ts - 60_000)], []])
    data = await rest.history(SYMBOL, "1m", c.ts - 60_000, c.ts + 120_000)
    assert [x.ts for x in data] == [c.ts - 60_000, c.ts]
    assert rest.candles.call_args_list[1].kwargs["after"] == c.ts
    await rest.close()


async def test_ingestion_and_rejection(tmp_path):
    collector = Collector(Settings(database=tmp_path / "test.db"))
    c = candle()
    data = {
        "arg": {"instId": SYMBOL, "channel": "candle1m"},
        "data": [[str(c.ts), "100", "102", "99", "101", "10", "0.1", "10.1", "1"]],
    }
    collector.ingest(json.dumps(data))
    assert collector.store.candles(SYMBOL, "1m") == [c]
    collector.ingest("pong")
    with pytest.raises(RuntimeError):
        collector.ingest('{"event":"error","code":"60012"}')
    data["arg"]["instId"] = "INVALID"
    with pytest.raises(ValueError):
        collector.ingest(json.dumps(data))
    assert not collector.health()["ready"]
    await collector.rest.close()
    collector.store.close()


def test_api_empty_data_is_unhealthy(tmp_path):
    app = create_app(Settings(database=tmp_path / "test.db"))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503
        assert client.get(f"/candles/{SYMBOL}/1m").json() == []
        assert client.get(f"/candles/{SYMBOL}/1h").status_code == 404
        assert client.get(f"/candles/{SYMBOL}/1m?limit=999999").status_code == 422
        assert all(x["last"] is None for x in client.get("/markets").json())


async def test_ws_reconnect_resubscribes(tmp_path, monkeypatch):
    from quant import collector as module

    collector = Collector(Settings(database=tmp_path / "test.db"))
    sockets = []

    class FakeSocket:
        async def __aenter__(self):
            sockets.append(self)
            if len(sockets) == 2:
                raise asyncio.CancelledError()
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, value):
            self.sent = json.loads(value)

        async def recv(self):
            raise OSError("test disconnect")

    monkeypatch.setattr(module, "connect", lambda *a, **kw: FakeSocket())
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await collector.socket(False)
    assert sockets[0].sent["args"] == collector.subscriptions(False)
    assert len(sockets) == 2
    assert not collector.connections["public"]["connected"]
    await collector.rest.close()
    collector.store.close()
