from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant.config import BAR_MS


def number(value, *, positive=False) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("invalid nonnegative/positive decimal")
    return result


class Candle(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    symbol: str
    bar: Literal["1m", "5m", "15m"]
    ts: int = Field(gt=0)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    base_volume: Decimal = Field(ge=0)
    quote_volume: Decimal = Field(ge=0)
    confirmed: bool

    @model_validator(mode="after")
    def valid_ohlc(self):
        if self.ts % BAR_MS[self.bar]:
            raise ValueError("unaligned candle timestamp")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC")
        return self

    @classmethod
    def from_okx(cls, symbol, bar, row):
        if len(row) != 9 or row[8] not in ("0", "1"):
            raise ValueError("expected 9-field OKX candle with confirmation flag")
        return cls(
            symbol=symbol,
            bar=bar,
            ts=int(row[0]),
            open=row[1],
            high=row[2],
            low=row[3],
            close=row[4],
            volume=row[5],
            base_volume=row[6],
            quote_volume=row[7],
            confirmed=row[8] == "1",
        )


def validate_snapshot(channel, row):
    """Reject broken external rows before storing. Keep native OKX string precision."""
    if int(row["ts"]) <= 0:
        raise ValueError("invalid snapshot timestamp")
    if channel == "tickers":
        for key in ("last", "open24h", "high24h", "low24h"):
            number(row[key], positive=True)
        number(row["vol24h"])
        if number(row["low24h"]) > number(row["high24h"]):
            raise ValueError("invalid 24h range")
    elif channel == "mark-price":
        number(row["markPx"], positive=True)
    elif channel == "funding-rate":
        rate = Decimal(row["fundingRate"])
        if not rate.is_finite() or abs(rate) > 1 or int(row["fundingTime"]) <= 0:
            raise ValueError("invalid funding rate/time")
    elif channel == "books5":
        for side, reverse in (("bids", True), ("asks", False)):
            levels = row[side]
            if not levels or len(levels) > 5:
                raise ValueError("invalid five-level book")
            prices = [number(x[0], positive=True) for x in levels]
            for level in levels:
                number(level[1], positive=True)
            if prices != sorted(set(prices), reverse=reverse):
                raise ValueError("unordered book")
        if Decimal(row["bids"][0][0]) >= Decimal(row["asks"][0][0]):
            raise ValueError("crossed order book")
    else:
        raise ValueError("unsupported public channel")
    return row
