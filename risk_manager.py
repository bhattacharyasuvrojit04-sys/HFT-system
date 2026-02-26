"""
Risk Manager
============
All orders pass through here before reaching execution.
Implements pre-trade risk checks, position limits, drawdown controls,
rate limiting, and the system kill switch.

Risk hierarchy:
  1. Kill switch (system halt) — checked first, overrides everything
  2. Rate limiting — max orders per second
  3. Position limits — max position per symbol
  4. Open order limits
  5. Daily loss limit
  6. Spread / price sanity checks
"""

from __future__ import annotations
import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional, Tuple

from config import RISK, RiskConfig
from core.models import Order, OrderBook, Side
from core.order_manager import OrderManager
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("RiskManager")


class RateLimiter:
    """Token bucket rate limiter for order submission."""

    def __init__(self, max_per_second: int):
        self.max_per_second = max_per_second
        self._timestamps: deque = deque()

    def allow(self) -> bool:
        now = time.monotonic()
        # Remove timestamps older than 1 second
        while self._timestamps and now - self._timestamps[0] > 1.0:
            self._timestamps.popleft()
        if len(self._timestamps) < self.max_per_second:
            self._timestamps.append(now)
            return True
        return False


class RiskManager:
    """
    Pre-trade risk gateway.
    Subscribes to ORDER_REQUEST, evaluates risk, then publishes
    ORDER_APPROVED or ORDER_REJECTED.
    """

    def __init__(self, bus: EventBus, order_manager: OrderManager,
                 config: RiskConfig = RISK):
        self.bus = bus
        self.om  = order_manager
        self.cfg = config

        self._halted = False
        self._daily_loss_start = time.time()
        self._daily_loss_usd   = 0.0
        self._consecutive_losses = 0
        self._rate_limiter = RateLimiter(config.max_orders_per_second)

        # Latest orderbooks for spread checking
        self._orderbooks: Dict[str, OrderBook] = {}

        bus.subscribe(EventType.ORDER_REQUEST,  self._on_order_request)
        bus.subscribe(EventType.ORDERBOOK_UPDATE, self._on_orderbook_update)
        bus.subscribe(EventType.ORDER_FILLED,   self._on_fill)
        bus.subscribe(EventType.SYSTEM_HALT,    self._on_system_halt)

        log.info(f"RiskManager started | max_pos=${config.max_position_usd} "
                 f"max_daily_loss=${config.max_daily_loss_usd}")

    # ── Public ────────────────────────────────────────────────────────────────

    def halt(self, reason: str = "Manual halt") -> None:
        if not self._halted:
            self._halted = True
            log.critical(f"🛑 SYSTEM HALT: {reason}")
            asyncio.create_task(
                self.bus.publish(Event(EventType.SYSTEM_HALT,
                                       {"reason": reason}, source="RiskManager"))
            )

    def is_halted(self) -> bool:
        return self._halted

    def resume(self) -> None:
        """Resume after manual halt — use carefully."""
        self._halted = False
        log.warning("RiskManager: system RESUMED")

    # ── Event Handlers ────────────────────────────────────────────────────────

    async def _on_order_request(self, event: Event) -> None:
        order: Order = event.data
        approved, reason = self._evaluate(order)

        if approved:
            await self.bus.publish(Event(EventType.ORDER_APPROVED, order,
                                          source="RiskManager"))
            log.debug(f"✅ Order approved: {order.client_order_id}")
        else:
            order.status_reason = reason
            await self.bus.publish(Event(EventType.ORDER_REJECTED,
                                          {"order": order, "reason": reason},
                                          source="RiskManager"))
            log.warning(f"❌ Order REJECTED [{reason}]: {order}")

    async def _on_orderbook_update(self, event: Event) -> None:
        ob: OrderBook = event.data
        self._orderbooks[ob.symbol] = ob

    async def _on_fill(self, event: Event) -> None:
        # Track realized losses for daily loss limit
        from core.models import Fill
        fill: Fill = event.data
        pos = self.om.get_position(fill.symbol)
        # Check drawdown after every fill
        net_pnl = self.om.net_pnl()
        if net_pnl < -self.cfg.max_daily_loss_usd:
            self.halt(f"Daily loss limit breached: ${net_pnl:.2f}")

    async def _on_system_halt(self, event: Event) -> None:
        self._halted = True

    # ── Core Risk Evaluation ──────────────────────────────────────────────────

    def _evaluate(self, order: Order) -> Tuple[bool, str]:
        """Returns (approved: bool, reason: str)."""

        # 1. Kill switch
        if self._halted:
            return False, "System halted"

        # 2. Rate limit
        if not self._rate_limiter.allow():
            return False, f"Rate limit: max {self.cfg.max_orders_per_second} orders/s"

        # 3. Open order count
        open_count = self.om.open_order_count(order.symbol)
        if open_count >= self.cfg.max_open_orders:
            return False, f"Max open orders reached: {open_count}/{self.cfg.max_open_orders}"

        # 4. Position limit
        pos = self.om.get_position(order.symbol)
        ob  = self._orderbooks.get(order.symbol)
        mark = (ob.mid_price if ob else None) or (order.price or 0.0)

        if mark > 0:
            order_notional = order.qty * mark
            current_notional = abs(pos.qty) * mark

            if order.side == Side.BUY and pos.qty >= 0:
                # Adding to long
                if current_notional + order_notional > self.cfg.max_position_usd:
                    return False, (f"Position limit: long would be "
                                   f"${current_notional + order_notional:.0f} "
                                   f"> max ${self.cfg.max_position_usd}")
            elif order.side == Side.SELL and pos.qty <= 0:
                # Adding to short
                if current_notional + order_notional > self.cfg.max_position_usd:
                    return False, (f"Position limit: short would be "
                                   f"${current_notional + order_notional:.0f} "
                                   f"> max ${self.cfg.max_position_usd}")

        # 5. Price sanity — don't submit orders wildly far from market
        if order.price and ob and ob.mid_price:
            deviation_bps = abs(order.price - ob.mid_price) / ob.mid_price * 10_000
            if deviation_bps > 200:  # 2% from mid — likely stale data
                return False, f"Price sanity: order {deviation_bps:.0f}bps from mid"

        # 6. Spread check for limit orders
        if ob and ob.spread_bps is not None:
            if ob.spread_bps < self.cfg.min_spread_bps:
                return False, f"Spread too tight: {ob.spread_bps:.2f}bps < min {self.cfg.min_spread_bps}bps"

        # 7. Daily loss reset (midnight UTC)
        now = time.time()
        if now - self._daily_loss_start > 86400:
            self._daily_loss_start = now
            self._daily_loss_usd   = 0.0
            log.info("Daily P&L counter reset")

        return True, "OK"
