"""
Momentum Strategy
=================
EMA crossover momentum with order book confirmation.

Signal logic:
  - Fast EMA crosses above Slow EMA + book imbalance > threshold → BUY
  - Fast EMA crosses below Slow EMA + book imbalance < -threshold → SELL
  - Exit when EMA crosses back or stop loss/take profit hit
"""

from __future__ import annotations
import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional, Deque

from config import MOMENTUM, TRADING, MomentumConfig
from core.models import Order, OrderBook, Side, OrderType, TimeInForce
from core.order_manager import OrderManager
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("MomentumStrategy")


class EMA:
    def __init__(self, period: int):
        self.period = period
        self.alpha  = 2.0 / (period + 1)
        self._value: Optional[float] = None
        self._count = 0

    def update(self, price: float) -> float:
        if self._value is None:
            self._value = price
        else:
            self._value = self.alpha * price + (1 - self.alpha) * self._value
        self._count += 1
        return self._value

    @property
    def value(self) -> Optional[float]:
        return self._value if self._count >= self.period else None


class MomentumStrategy:
    def __init__(self, bus: EventBus, order_manager: OrderManager,
                 config: MomentumConfig = MOMENTUM):
        self.bus = bus
        self.om  = order_manager
        self.cfg = config

        self._fast_ema: Dict[str, EMA] = {}
        self._slow_ema: Dict[str, EMA] = {}
        self._orderbooks: Dict[str, OrderBook] = {}
        self._position_entered: Dict[str, float] = {}  # symbol → entry time
        self._active = True

        for sym in TRADING.symbols:
            self._fast_ema[sym] = EMA(config.fast_ema_periods)
            self._slow_ema[sym] = EMA(config.slow_ema_periods)

        bus.subscribe(EventType.TRADE_TICK,       self._on_tick)
        bus.subscribe(EventType.ORDERBOOK_UPDATE, self._on_orderbook)
        bus.subscribe(EventType.SYSTEM_HALT,      self._on_halt)

        log.info(f"MomentumStrategy started | fast={config.fast_ema_periods} "
                 f"slow={config.slow_ema_periods} threshold={config.signal_threshold}")

    async def _on_orderbook(self, event: Event) -> None:
        ob: OrderBook = event.data
        self._orderbooks[ob.symbol] = ob

    async def _on_tick(self, event: Event) -> None:
        from core.models import Tick
        tick: Tick = event.data
        if not self._active:
            return

        sym   = tick.symbol
        price = tick.price

        fast = self._fast_ema[sym].update(price)
        slow = self._slow_ema[sym].update(price)

        if fast is None or slow is None:
            return

        diff      = (fast - slow) / slow
        ob        = self._orderbooks.get(sym)
        imbalance = ob.imbalance(5) if ob else 0.0
        pos       = self.om.get_position(sym)
        now       = time.time()

        # Check if we should exit existing position
        entered = self._position_entered.get(sym, 0)
        if entered and (now - entered) * 1000 >= self.cfg.position_hold_ms:
            if pos.qty != 0:
                await self._exit_position(sym, pos, price)
                return

        # Entry signals
        if pos.qty == 0:
            if (diff > self.cfg.signal_threshold and imbalance > 0.2):
                # Bullish signal
                await self._enter(sym, Side.BUY, price, ob)
                self._position_entered[sym] = now

            elif (diff < -self.cfg.signal_threshold and imbalance < -0.2):
                # Bearish signal
                await self._enter(sym, Side.SELL, price, ob)
                self._position_entered[sym] = now

    async def _enter(self, symbol: str, side: Side, price: float,
                     ob: Optional[OrderBook]) -> None:
        qty = TRADING.order_size_usd / price

        # Use aggressive limit (just inside spread) for fast fills
        if ob:
            if side == Side.BUY:
                entry_price = ob.best_ask.price if ob.best_ask else price
            else:
                entry_price = ob.best_bid.price if ob.best_bid else price
        else:
            entry_price = price

        order = Order(
            symbol        = symbol,
            side          = side,
            qty           = round(qty, 3),
            price         = entry_price,
            order_type    = OrderType.LIMIT,
            time_in_force = TimeInForce.IOC,   # IOC: fill immediately or cancel
            strategy_tag  = "momentum_entry",
        )
        self.om.register_order(order)
        await self.bus.publish(Event(EventType.ORDER_REQUEST, order,
                                      source="MomentumStrategy"))
        log.info(f"Momentum ENTRY: {side.value} {symbol} @ {entry_price:.4f} "
                 f"qty={qty:.4f}")

    async def _exit_position(self, symbol: str, pos, mark_price: float) -> None:
        exit_side = Side.SELL if pos.qty > 0 else Side.BUY
        qty       = abs(pos.qty)

        order = Order(
            symbol        = symbol,
            side          = exit_side,
            qty           = qty,
            price         = None,
            order_type    = OrderType.MARKET,
            time_in_force = TimeInForce.GTC,
            strategy_tag  = "momentum_exit",
        )
        self.om.register_order(order)
        await self.bus.publish(Event(EventType.ORDER_REQUEST, order,
                                      source="MomentumStrategy"))
        self._position_entered.pop(symbol, None)
        log.info(f"Momentum EXIT: {exit_side.value} {symbol} qty={qty:.4f}")

    async def _on_halt(self, event: Event) -> None:
        self._active = False
