"""
Core Data Models
================
Immutable-friendly dataclasses for all trading primitives.
Using __slots__ for memory efficiency in high-throughput paths.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, List, Dict
import time
import uuid


# ─── ENUMS ────────────────────────────────────────────────────────────────────

class Side(Enum):
    BUY  = "BUY"
    SELL = "SELL"

    def opposite(self) -> "Side":
        return Side.SELL if self == Side.BUY else Side.BUY


class OrderStatus(Enum):
    PENDING    = auto()   # Created locally, not yet sent
    SENT       = auto()   # Submitted to exchange
    NEW        = auto()   # Acknowledged by exchange
    PARTIAL    = auto()   # Partially filled
    FILLED     = auto()   # Fully filled
    CANCELLED  = auto()   # Cancelled
    REJECTED   = auto()   # Rejected by exchange
    EXPIRED    = auto()   # GTX/GTT expired


class OrderType(Enum):
    LIMIT  = "LIMIT"
    MARKET = "MARKET"
    STOP   = "STOP_MARKET"


class TimeInForce(Enum):
    GTC = "GTC"   # Good Till Cancel
    IOC = "IOC"   # Immediate or Cancel
    FOK = "FOK"   # Fill or Kill
    GTX = "GTX"   # Post-Only (Good Till Crossing) — maker only


# ─── ORDERBOOK ────────────────────────────────────────────────────────────────

@dataclass
class PriceLevel:
    price: float
    qty:   float

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass
class OrderBook:
    symbol:    str
    bids:      List[PriceLevel] = field(default_factory=list)  # sorted desc
    asks:      List[PriceLevel] = field(default_factory=list)  # sorted asc
    timestamp: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[PriceLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[PriceLevel]:
        return self.asks[0] if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None

    @property
    def spread_bps(self) -> Optional[float]:
        if self.mid_price and self.spread:
            return (self.spread / self.mid_price) * 10_000
        return None

    def imbalance(self, depth: int = 5) -> float:
        """Order book imbalance: >0 = buy pressure, <0 = sell pressure."""
        bid_vol = sum(lvl.qty for lvl in self.bids[:depth])
        ask_vol = sum(lvl.qty for lvl in self.asks[:depth])
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total


# ─── ORDER ────────────────────────────────────────────────────────────────────

@dataclass
class Order:
    symbol:       str
    side:         Side
    qty:          float
    price:        Optional[float]       = None
    order_type:   OrderType             = OrderType.LIMIT
    time_in_force: TimeInForce          = TimeInForce.GTX

    # Filled in by execution layer
    client_order_id: str               = field(default_factory=lambda: f"HFT-{uuid.uuid4().hex[:12].upper()}")
    exchange_order_id: Optional[str]   = None
    status:       OrderStatus          = OrderStatus.PENDING
    filled_qty:   float                = 0.0
    avg_fill_price: Optional[float]    = None
    created_at:   float                = field(default_factory=time.time)
    updated_at:   float                = field(default_factory=time.time)

    # Strategy tag for P&L attribution
    strategy_tag: str = "unknown"

    @property
    def remaining_qty(self) -> float:
        return self.qty - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.SENT,
                               OrderStatus.NEW, OrderStatus.PARTIAL)

    @property
    def notional(self) -> float:
        p = self.price or self.avg_fill_price or 0.0
        return p * self.qty

    def __repr__(self) -> str:
        return (f"Order({self.client_order_id} {self.side.value} "
                f"{self.qty:.6f} {self.symbol} @ {self.price} "
                f"[{self.status.name}])")


# ─── TRADE / FILL ─────────────────────────────────────────────────────────────

@dataclass
class Fill:
    order_id:    str
    symbol:      str
    side:        Side
    qty:         float
    price:       float
    fee:         float = 0.0
    fee_asset:   str   = "USDT"
    timestamp:   float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.price * self.qty


# ─── POSITION ─────────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:     str
    qty:        float = 0.0            # + = long, - = short
    avg_price:  float = 0.0
    realized_pnl: float = 0.0
    total_fees:   float = 0.0

    def update_from_fill(self, fill: Fill) -> None:
        """Update position and compute realized P&L on close/reduce."""
        fill_qty = fill.qty if fill.side == Side.BUY else -fill.qty

        if self.qty == 0:
            # Opening position
            self.qty = fill_qty
            self.avg_price = fill.price
        elif (self.qty > 0 and fill_qty > 0) or (self.qty < 0 and fill_qty < 0):
            # Adding to position
            total_cost = self.avg_price * abs(self.qty) + fill.price * abs(fill_qty)
            self.qty += fill_qty
            self.avg_price = total_cost / abs(self.qty) if self.qty != 0 else 0.0
        else:
            # Reducing / closing position
            closed = min(abs(self.qty), abs(fill_qty))
            if self.qty > 0:
                pnl = (fill.price - self.avg_price) * closed
            else:
                pnl = (self.avg_price - fill.price) * closed
            self.realized_pnl += pnl
            self.qty += fill_qty
            if abs(self.qty) < 1e-9:
                self.qty = 0.0
                self.avg_price = 0.0

        self.total_fees += fill.fee

    @property
    def unrealized_pnl(self, mark_price: float = 0.0) -> float:
        if self.qty == 0 or mark_price == 0:
            return 0.0
        if self.qty > 0:
            return (mark_price - self.avg_price) * self.qty
        return (self.avg_price - mark_price) * abs(self.qty)

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.avg_price

    def __repr__(self) -> str:
        side = "LONG" if self.qty > 0 else "SHORT" if self.qty < 0 else "FLAT"
        return (f"Position({self.symbol} {side} {abs(self.qty):.6f} "
                f"@ {self.avg_price:.4f} | rPnL={self.realized_pnl:.4f})")


# ─── MARKET TICK ──────────────────────────────────────────────────────────────

@dataclass
class Tick:
    symbol:    str
    price:     float
    qty:       float
    side:      Side         # aggressor side
    timestamp: float = field(default_factory=time.time)

    @property
    def notional(self) -> float:
        return self.price * self.qty
