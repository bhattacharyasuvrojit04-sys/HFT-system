"""
Order Manager
=============
Single source of truth for all order state and positions.
All order mutations go through here — no component touches order state directly.
"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional

from core.models import Order, OrderStatus, Fill, Position, Side
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("OrderManager")


class OrderManager:
    """
    Maintains:
      - All orders (active and historical)
      - Positions per symbol
      - P&L tracking
    """

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._orders:    Dict[str, Order]    = {}   # client_order_id → Order
        self._ex_map:    Dict[str, str]      = {}   # exchange_order_id → client_order_id
        self._positions: Dict[str, Position] = {}   # symbol → Position
        self._fills:     List[Fill]          = []
        self._lock = asyncio.Lock()

        # Subscribe to lifecycle events
        bus.subscribe(EventType.ORDER_SENT,      self._on_order_sent)
        bus.subscribe(EventType.ORDER_ACK,       self._on_order_ack)
        bus.subscribe(EventType.ORDER_FILLED,    self._on_order_filled)
        bus.subscribe(EventType.ORDER_PARTIAL,   self._on_order_partial)
        bus.subscribe(EventType.ORDER_CANCELLED, self._on_order_cancelled)

    # ── Public API ────────────────────────────────────────────────────────────

    def register_order(self, order: Order) -> None:
        self._orders[order.client_order_id] = order

    def get_order(self, client_id: str) -> Optional[Order]:
        return self._orders.get(client_id)

    def get_order_by_exchange_id(self, ex_id: str) -> Optional[Order]:
        cid = self._ex_map.get(ex_id)
        return self._orders.get(cid) if cid else None

    def get_position(self, symbol: str) -> Position:
        if symbol not in self._positions:
            self._positions[symbol] = Position(symbol=symbol)
        return self._positions[symbol]

    def active_orders(self, symbol: Optional[str] = None) -> List[Order]:
        orders = [o for o in self._orders.values() if o.is_active]
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    def open_order_count(self, symbol: Optional[str] = None) -> int:
        return len(self.active_orders(symbol))

    def total_realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self._positions.values())

    def total_fees(self) -> float:
        return sum(p.total_fees for p in self._positions.values())

    def net_pnl(self) -> float:
        return self.total_realized_pnl() - self.total_fees()

    def summary(self) -> Dict:
        return {
            "positions": {s: repr(p) for s, p in self._positions.items()},
            "active_orders": len(self.active_orders()),
            "total_orders": len(self._orders),
            "realized_pnl": self.total_realized_pnl(),
            "fees": self.total_fees(),
            "net_pnl": self.net_pnl(),
        }

    # ── Event Handlers ────────────────────────────────────────────────────────

    async def _on_order_sent(self, event: Event) -> None:
        order: Order = event.data
        async with self._lock:
            order.status = OrderStatus.SENT
            self._orders[order.client_order_id] = order
        log.debug(f"Order sent: {order}")

    async def _on_order_ack(self, event: Event) -> None:
        payload = event.data   # dict with client_order_id, exchange_order_id
        cid = payload.get("client_order_id")
        eid = payload.get("exchange_order_id")
        async with self._lock:
            order = self._orders.get(cid)
            if order:
                order.exchange_order_id = eid
                order.status = OrderStatus.NEW
                if eid:
                    self._ex_map[eid] = cid
                log.info(f"Order ACK: {order.client_order_id} → exchId={eid}")

    async def _on_order_filled(self, event: Event) -> None:
        fill: Fill = event.data
        async with self._lock:
            order = self._get_order_for_fill(fill)
            if order:
                order.filled_qty = order.qty
                order.avg_fill_price = fill.price
                order.status = OrderStatus.FILLED
            self._apply_fill(fill)
        log.info(f"Order FILLED: {fill.order_id} qty={fill.qty} @ {fill.price}")

        # Notify position update
        pos = self.get_position(fill.symbol)
        await self.bus.publish(Event(EventType.POSITION_UPDATE, pos, source="OrderManager"))

    async def _on_order_partial(self, event: Event) -> None:
        fill: Fill = event.data
        async with self._lock:
            order = self._get_order_for_fill(fill)
            if order:
                order.filled_qty += fill.qty
                order.avg_fill_price = fill.price
                order.status = OrderStatus.PARTIAL
            self._apply_fill(fill)
        log.debug(f"Order PARTIAL: {fill.order_id} qty={fill.qty} @ {fill.price}")

    async def _on_order_cancelled(self, event: Event) -> None:
        payload = event.data
        cid = payload.get("client_order_id") or self._ex_map.get(payload.get("exchange_order_id", ""))
        async with self._lock:
            order = self._orders.get(cid)
            if order:
                order.status = OrderStatus.CANCELLED
                log.info(f"Order CANCELLED: {cid}")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_order_for_fill(self, fill: Fill) -> Optional[Order]:
        # Try by exchange_order_id first, then client_order_id
        cid = self._ex_map.get(fill.order_id) or fill.order_id
        return self._orders.get(cid)

    def _apply_fill(self, fill: Fill) -> None:
        self._fills.append(fill)
        pos = self.get_position(fill.symbol)
        pos.update_from_fill(fill)
