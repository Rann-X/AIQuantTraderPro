"""Decimal risk model for isolated linear USDT perpetuals, with official tier inputs."""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quant.models import number, validate_snapshot

D = Decimal
ZERO = D(0)


class FinancialModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Instrument(FinancialModel):
    symbol: str
    base_per_contract: Decimal = Field(gt=0)
    lot: Decimal = Field(gt=0)
    minimum: Decimal = Field(gt=0)
    tick: Decimal = Field(gt=0)
    max_market: Decimal = Field(gt=0)

    @classmethod
    def from_okx(cls, row):
        if (
            row["ctType"] != "linear"
            or row["settleCcy"] != "USDT"
            or row["state"] != "live"
            or row["ctValCcy"] != row["instId"].split("-")[0]
        ):
            raise ValueError("only live base-valued linear USDT contracts supported")
        return cls(
            symbol=row["instId"],
            base_per_contract=number(row["ctVal"], positive=True) * number(row["ctMult"], positive=True),
            lot=row["lotSz"],
            minimum=row["minSz"],
            tick=row["tickSz"],
            max_market=row["maxMktSz"],
        )

    def quantity(self, value):
        value = number(value, positive=True)
        if value < self.minimum or value % self.lot or value > self.max_market:
            raise ValueError("quantity violates minimum/lot/market maximum")
        return value


class Tier(FinancialModel):
    tier: int = Field(ge=1)
    minimum: Decimal = Field(ge=0)
    maximum: Decimal = Field(gt=0)
    mmr: Decimal = Field(gt=0, lt=1)
    imr: Decimal = Field(gt=0, le=1)
    max_leverage: Decimal = Field(ge=1)

    @model_validator(mode="after")
    def valid_range(self):
        if self.minimum >= self.maximum or self.imr < self.mmr:
            raise ValueError("invalid tier")
        return self

    @classmethod
    def from_okx(cls, row):
        return cls(
            tier=int(row["tier"]),
            minimum=row["minSz"],
            maximum=row["maxSz"],
            mmr=row["mmr"],
            imr=row["imr"],
            max_leverage=row["maxLever"],
        )


def select_tier(tiers, quantity):
    quantity = number(quantity, positive=True)
    # At a shared boundary choose the lower tier, whose maximum includes this size.
    for tier in sorted(tiers, key=lambda t: t.tier):
        if tier.minimum <= quantity <= tier.maximum:
            return tier
    raise ValueError("no matching OKX position tier")


class Policy(FinancialModel):
    leverage: Decimal = Field(D(20), ge=1, le=20)
    per_trade: Decimal = Field(D("0.02"), gt=0, le=D("0.02"))
    daily_limit: Decimal = Field(D("0.05"), gt=0, le=D("0.05"))
    fee: Decimal = Field(D("0.0005"), ge=0, le=D("0.01"))
    slippage: Decimal = Field(D("0.0003"), ge=0, le=D("0.01"))
    funding_reserve_rate: Decimal = Field(D("0.001"), ge=0, le=D("0.1"))
    minimum_rr: Decimal = Field(D("1.5"), gt=0)


class Order(FinancialModel):
    symbol: str
    side: Literal["long", "short"]
    quantity: Decimal = Field(gt=0)
    stop: Decimal = Field(gt=0)
    target: Decimal = Field(gt=0)


def sign(side):
    if side not in ("long", "short"):
        raise ValueError("invalid direction")
    return D(1) if side == "long" else D(-1)


def pnl(instrument, quantity, entry, exit_price, side):
    return (
        instrument.base_per_contract
        * number(quantity, positive=True)
        * sign(side)
        * (number(exit_price, positive=True) - number(entry, positive=True))
    )


def depth_fill(instrument, quantity, book, *, buy, slippage=D("0.0003")):
    """Full-or-reject fill against five visible levels; sizes are contracts, never coins."""
    quantity = instrument.quantity(quantity)
    slippage = number(slippage)
    if slippage >= 1:
        raise ValueError("invalid slippage")
    validate_snapshot("books5", book)
    remaining, total = quantity, ZERO
    for level in book["asks" if buy else "bids"]:
        take = min(remaining, D(level[1]))
        total += take * D(level[0])
        remaining -= take
        if remaining == 0:
            break
    if remaining:
        raise ValueError("visible book liquidity insufficient; order not filled")
    price = total / quantity * (1 + slippage if buy else 1 - slippage)
    return (price / instrument.tick).to_integral_value(
        rounding=ROUND_CEILING if buy else ROUND_FLOOR
    ) * instrument.tick


def liquidation(instrument, quantity, entry, margin, side, tiers, fee):
    """Solve equity = mark notional * (tier MMR + fee); estimate, not exchange guarantee."""
    base = instrument.base_per_contract * number(quantity, positive=True)
    tier = select_tier(tiers, quantity)
    margin, entry, fee = number(margin), number(entry, positive=True), number(fee)
    if tier.mmr + fee >= 1:
        raise ValueError("invalid maintenance requirement")
    if side == "long":
        price = (base * entry - margin) / (base * (1 - tier.mmr - fee))
    elif side == "short":
        price = (base * entry + margin) / (base * (1 + tier.mmr + fee))
    else:
        raise ValueError("invalid direction")
    return max(ZERO, price)


def maintenance(instrument, quantity, mark, tiers, fee):
    tier = select_tier(tiers, quantity)
    return (
        instrument.base_per_contract
        * number(quantity, positive=True)
        * number(mark, positive=True)
        * (tier.mmr + fee)
    )


def assess(order, instrument, tiers, book, policy, *, equity, free_cash, daily_budget, reserved=ZERO):
    if order.symbol != instrument.symbol:
        raise ValueError("instrument mismatch")
    qty = instrument.quantity(order.quantity)
    tier = select_tier(tiers, qty)
    # V1 small account stays in tier 1: no inaccurate imitation of partial-liquidation ladders.
    if tier.tier != 1:
        raise ValueError("paper V1 position cap is OKX first tier")
    if policy.leverage > tier.max_leverage or 1 / policy.leverage < tier.imr:
        raise ValueError("leverage exceeds tier rule")
    entry = depth_fill(instrument, qty, book, buy=order.side == "long", slippage=policy.slippage)
    s = sign(order.side)
    if order.stop % instrument.tick or order.target % instrument.tick:
        raise ValueError("stop/target must align to contract tick")
    if not s * (entry - order.stop) > 0 or not s * (order.target - entry) > 0:
        raise ValueError("stop and target must bracket actual depth fill")
    base = qty * instrument.base_per_contract
    spread = D(book["asks"][0][0]) - D(book["bids"][0][0])
    # Budget a full current spread at exit in addition to adverse slippage; gaps can still exceed this.
    stop_exit = order.stop * (1 - s * policy.slippage) - s * spread
    target_exit = order.target * (1 - s * policy.slippage) - s * spread
    rounding = ROUND_FLOOR if order.side == "long" else ROUND_CEILING
    stop_exit = (stop_exit / instrument.tick).to_integral_value(rounding=rounding) * instrument.tick
    target_exit = (target_exit / instrument.tick).to_integral_value(rounding=rounding) * instrument.tick
    if min(stop_exit, target_exit) <= 0:
        raise ValueError("invalid planned execution price")
    entry_fee = base * entry * policy.fee
    exit_fee = base * stop_exit * policy.fee
    funding = base * entry * policy.funding_reserve_rate
    loss = -pnl(instrument, qty, entry, stop_exit, order.side) + entry_fee + exit_fee + funding
    reward = (
        pnl(instrument, qty, entry, target_exit, order.side)
        - entry_fee
        - base * target_exit * policy.fee
        - funding
    )
    margin = base * entry / policy.leverage
    liq = liquidation(instrument, qty, entry, margin, order.side, tiers, policy.fee)
    if s * (order.stop - liq) <= 0:
        raise ValueError("stop beyond estimated liquidation threshold")
    budget = min(number(equity) * policy.per_trade, max(ZERO, number(daily_budget) - number(reserved)))
    if loss > budget:
        raise ValueError("放弃交易：含成本的计划亏损超过单次或每日剩余预算")
    if margin + entry_fee > number(free_cash):
        raise ValueError("insufficient free paper cash for margin and entry fee")
    if reward / loss < policy.minimum_rr:
        raise ValueError("net reward/risk below threshold")
    return {
        "entry": entry,
        "quantity": qty,
        "base_quantity": base,
        "notional": base * entry,
        "margin": margin,
        "entry_fee": entry_fee,
        "exit_fee_estimate": exit_fee,
        "funding_reserve": funding,
        "slippage_rate": policy.slippage,
        "spread": spread,
        "planned_loss": loss,
        "planned_reward": reward,
        "net_rr": reward / loss,
        "liquidation_estimate": liq,
        "budget": budget,
        "tier": tier.tier,
        "warnings": [
            "计划亏损不是最大实际亏损保证；跳空、资金费变化和深度不足可能突破预算。",
            "强平价仅限当前第一阶梯逐仓模型；非OKX真实账户强平保证。",
        ],
    }
