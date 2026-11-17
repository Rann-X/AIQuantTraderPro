import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from quant.analysis import analyze_market
from quant.collector import market_overview
from quant.config import BAR_MS, load_settings
from quant.explain import explain_signal
from quant.store import Store


def create_app(settings=None):
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(settings.database)
        yield
        app.state.store.close()

    app = FastAPI(title="AI Quant Trader Pro — public data", lifespan=lifespan)

    @app.get("/health")
    def health():
        status = app.state.store.statuses().get("collector", {})
        current = time.time() * 1000 - status.get("time_ms", 0) < 10_000
        ready = bool(status.get("ready")) and current
        return JSONResponse(
            {**status, "ready": ready, "collector_alive": current}, status_code=200 if ready else 503
        )

    @app.get("/markets")
    def markets():
        return market_overview(app.state.store, settings)

    @app.get("/candles/{symbol}/{bar}")
    def candles(symbol: str, bar: str, limit: int = Query(300, ge=1, le=5000), confirmed_only: bool = True):
        if symbol not in settings.symbols or bar not in BAR_MS:
            raise HTTPException(404, "unsupported symbol/timeframe")
        return [
            c.model_dump(mode="json") for c in app.state.store.candles(symbol, bar, limit, confirmed_only)
        ]

    @app.get("/instruments")
    def instruments():
        return {k: v for k, v in app.state.store.statuses().items() if k.startswith("instrument:")}

    @app.get("/analysis/{symbol}")
    def analysis(symbol: str):
        if symbol not in settings.symbols:
            raise HTTPException(404, "unsupported symbol")
        return analyze_market(app.state.store, settings, symbol)

    @app.get("/explanation/{symbol}")
    def explanation(symbol: str):
        return explain_signal(analysis(symbol))

    return app
