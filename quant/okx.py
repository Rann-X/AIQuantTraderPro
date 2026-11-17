import asyncio
import logging
import random
import time

import httpx

from quant.models import Candle

log = logging.getLogger(__name__)
PUBLIC_PATHS = {
    "/api/v5/public/time",
    "/api/v5/public/instruments",
    "/api/v5/market/ticker",
    "/api/v5/market/books",
    "/api/v5/market/candles",
    "/api/v5/market/history-candles",
    "/api/v5/public/mark-price",
    "/api/v5/public/funding-rate",
    "/api/v5/public/position-tiers",
    "/api/v5/public/funding-rate-history",
    "/api/v5/market/history-mark-price-candles",
}


class OKXError(RuntimeError):
    pass


class PublicREST:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(base_url=settings.rest_url, timeout=15)
        self.lock = asyncio.Lock()
        self.next_request = 0.0

    async def close(self):
        await self.client.aclose()

    async def get(self, path, **params):
        if path not in PUBLIC_PATHS:
            raise ValueError("private/trading endpoint prohibited")
        for attempt in range(5):
            try:
                async with self.lock:
                    await asyncio.sleep(max(0, self.next_request - time.monotonic()))
                    self.next_request = time.monotonic() + self.settings.rest_interval
                    response = await self.client.get(
                        path, params=params, headers={"Cache-Control": "no-cache"}
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise OKXError(f"temporary HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") in ("50011", "50040"):
                    raise OKXError(f"rate limit {payload['code']}")
                if payload.get("code") != "0" or not isinstance(payload.get("data"), list):
                    raise ValueError(f"invalid OKX response: {payload.get('code')} {payload.get('msg')}")
                return payload["data"]
            except (httpx.TransportError, OKXError) as exc:
                if attempt == 4:
                    raise
                delay = min(16, 2**attempt) + random.uniform(0, 0.5)
                log.warning("REST retry path=%s attempt=%d reason=%s", path, attempt + 1, exc)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def candles(self, symbol, bar, limit=300, *, history=False, after=None):
        params = {"instId": symbol, "bar": bar, "limit": str(limit)}
        if after is not None:
            params["after"] = str(after)
        path = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        return sorted(
            [Candle.from_okx(symbol, bar, row) for row in await self.get(path, **params)], key=lambda c: c.ts
        )

    async def history(self, symbol, bar, start_ms, end_ms):
        """Inclusive start, exclusive end; OKX 'after' pages towards older timestamps."""
        cursor, found = end_ms, {}
        while cursor > start_ms:
            batch = await self.candles(symbol, bar, history=True, after=cursor)
            if not batch:
                break
            for candle in batch:
                if start_ms <= candle.ts < end_ms and candle.confirmed:
                    found[candle.ts] = candle
            oldest = min(c.ts for c in batch)
            if oldest >= cursor:
                raise ValueError("OKX historical pagination made no progress")
            cursor = oldest
        return sorted(found.values(), key=lambda c: c.ts)
