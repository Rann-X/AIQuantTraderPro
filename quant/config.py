import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(
        default_factory=lambda: [f"{s}-USDT-SWAP" for s in ("BTC", "ETH", "SOL", "UNI")]
    )
    bars: list[str] = Field(default_factory=lambda: list(BAR_MS))
    rest_url: str = "https://openapi.okx.com"
    public_ws: str = "wss://ws.okx.com/ws/v5/public"
    business_ws: str = "wss://ws.okx.com/ws/v5/business"
    database: Path = Path("data/market.sqlite3")
    initial_candles: int = Field(300, ge=60, le=300)
    rest_interval: float = Field(0.55, ge=0.5)
    stale_seconds: int = Field(30, ge=10)
    strategy: dict[str, float] = Field(default_factory=dict)
    risk: dict[str, str] = Field(default_factory=dict)
    notifications: dict = Field(default_factory=dict)

    @field_validator("notifications")
    @classmethod
    def notifications_valid(cls, value):
        from quant.notifications import NotificationSettings

        return NotificationSettings(**value).model_dump()

    @field_validator("risk")
    @classmethod
    def risk_valid(cls, value):
        from quant.risk import Policy

        return Policy(**value).model_dump(mode="json")

    @field_validator("strategy")
    @classmethod
    def strategy_valid(cls, value):
        from quant.strategy import StrategyParameters

        return StrategyParameters(**value).model_dump()

    @field_validator("symbols")
    @classmethod
    def symbols_valid(cls, value):
        allowed = {f"{s}-USDT-SWAP" for s in ("BTC", "ETH", "SOL", "UNI")}
        if not value or len(set(value)) != len(value) or not set(value) <= allowed:
            raise ValueError("symbols must be unique supported USDT perpetuals")
        return value

    @field_validator("bars")
    @classmethod
    def bars_valid(cls, value):
        if not value or len(set(value)) != len(value) or not set(value) <= BAR_MS.keys():
            raise ValueError("bars must be unique 1m/5m/15m")
        return value

    @field_validator("rest_url", "public_ws", "business_ws")
    @classmethod
    def public_only(cls, value, info):
        allowed = {
            "rest_url": {"https://openapi.okx.com", "https://www.okx.com"},
            "public_ws": {"wss://ws.okx.com/ws/v5/public", "wss://ws.okx.com:8443/ws/v5/public"},
            "business_ws": {"wss://ws.okx.com/ws/v5/business", "wss://ws.okx.com:8443/ws/v5/business"},
        }
        if value not in allowed[info.field_name]:
            raise ValueError("only documented OKX public endpoints are allowed")
        return value


def load_settings(path="config.toml") -> Settings:
    with open(path, "rb") as file:
        values = tomllib.load(file)
    for key in ("rest_url", "public_ws", "business_ws"):
        if override := os.environ.get(f"QUANT_{key.upper()}"):
            values[key] = override
    return Settings(**values)
