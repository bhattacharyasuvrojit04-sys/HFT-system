"""
Market Making Strategy
======================
Quotes two-sided limit orders around the mid price.
Uses inventory skew to manage position risk:
  - Long inventory  → tighten ask, widen bid (want to sell)
  - Short inventory → tighten bid, widen ask (want to buy)

Also implements:
  - Quote refresh on timer or significant market move
  - Order level management (multiple levels per side)
  - Spread widening when volatility increases
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Dict, List, Optional

from config import MKT_MAKER, TRADING, MarketMakerConfig
from core.models import Order, OrderBook, Side, OrderType, TimeInForce
from core.order_manager import OrderManager
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("MarketMaker")


class MarketMakerStrategy:
    """
    Symmetric market maker with inventory skew.
    
    Quote logic:
      bid_price = mid * (1 - spread/2 - skew * inventory_ratio)
      ask_price = mid * (1 + spread/2 - skew * inventory_ratio)
    
    Where inventory_ratio ∈ [-1, 1]:
      +1 = fully long  → skew asks lower (more aggressive)
      -1 = fully short → skew bids higher (more aggressive)
    """

    def __init__(self, bus: EventBus, order_manager: OrderManager,
                 config: MarketMakerConfig = MKT_MAKER):
        self.bus = bus
        self.om  = order_manager
        self.cfg = config

        self._orderbooks: Dict[str, OrderBook] = {}
        self._last_refresh: Dict[str, float]   = {}
        self._last_mid: Dict[str, float]       = {}
        self._active = True
        self._quote_order_ids: Dict[str, List[str]] = {}  # symbol → [cid, ...]

        bus.subscribe(EventType.ORDERBOOK_UPDATE, self._on_orderbook)
        bus.subscribe(EventType.ORDER_FILLED,     self._on_fill)
        bus.subscribe(EventType.SYSTEM_HALT,      self._on_halt)

        log.info(f"MarketMaker started | spread={config.spread_bps}bps "
                 f"levels={config.order_levels} refresh={config.order_refresh_ms}ms")

    # ── Event Handlers ────────────────────────────────────────────────────────

    async def _on_orderbook(self, event: Event) -> None:
        ob: OrderBook = event.data
        if not self._active:
            return

        self._orderbooks[ob.symbol] = ob

        # Check if refresh needed
        now         = time.time()
        last        = self._last_refresh.get(ob.symbol, 0)
        refresh_due = (now - last) * 1000 >= self.cfg.order_refresh_ms

        # Also refresh if mid price moved significantly (5bps)
        last_mid = self._last_mid.get(ob.symbol, 0)
        mid      = ob.mid_price or 0
        mid_moved = (abs(mid - last_mid) / last_mid > 0.0005) if last_mid else True

        if refresh_due or mid_moved:
            await self._refresh_quotes(ob)
            self._last_refresh[ob.symbol] = now
            self._last_mid[ob.symbol]     = mid

    async def _on_fill(self, event: Event) -> None:
        """On fill, immediately refresh quotes with updated inventory."""
        from core.models import Fill
        fill: Fill = event.data
        ob = self._orderbooks.get(fill.symbol)
        if ob:
            await self._refresh_quotes(ob)

    async def _on_halt(self, event: Event) -> None:
        self._active = False
        log.warning("MarketMaker halted")

    # ── Quote Management ──────────────────────────────────────────────────────

    async def _refresh_quotes(self, ob: OrderBook) -> None:
        if not ob.mid_price:
            return

        symbol = ob.symbol
        mid    = ob.mid_price
        pos    = self.om.get_position(symbol)

        # Inventory ratio: how "full" our position is
        inv_ratio = 0.0
        if self.cfg.max_inventory_usd > 0 and mid > 0:
            inv_usd   = pos.qty * mid  # + = long, - = short
            inv_ratio = inv_usd / self.cfg.max_inventory_usd
            inv_ratio = max(-1.0, min(1.0, inv_ratio))

        # Cancel existing quotes
        await self._cancel_existing_quotes(symbol)

        # Generate new quotes
        orders = self._generate_quotes(symbol, mid, inv_ratio, ob)

        # Submit all
        for order in orders:
            self.om.register_order(order)
            if symbol not in self._quote_order_ids:
                self._quote_order_ids[symbol] = []
            self._quote_order_ids[symbol].append(order.client_order_id)
            await self.bus.publish(Event(EventType.ORDER_REQUEST, order,
                                          source="MarketMaker"))

    def _generate_quotes(self, symbol: str, mid: float, inv_ratio: float,
                         ob: OrderBook) -> List[Order]:
        """Compute bid/ask levels with inventory skew."""
        orders = []
        spread_half = (self.cfg.spread_bps / 2.0) / 10_000
        skew        = self.cfg.inventory_skew_factor * inv_ratio / 10_000

        # Widen spread if order book imbalance is extreme
        imb = ob.imbalance(5)
        vol_adj = 1.0 + abs(imb) * 0.5  # up to 50% wider on extreme imbalance

        # Calculate order quantity
        qty_usd = TRADING.order_size_usd
        qty     = qty_usd / mid if mid > 0 else 0.0

        for level in range(self.cfg.order_levels):
            level_adj = level * (self.cfg.level_spacing_bps / 10_000)

            # BID
            bid_price = mid * (1 - spread_half * vol_adj - skew - level_adj)
            bid_price = self._round_price(bid_price, symbol)
            if bid_price > 0 and (ob.best_bid is None or bid_price < ob.best_bid.price):
                orders.append(Order(
                    symbol         = symbol,
                    side           = Side.BUY,
                    qty            = self._round_qty(qty * (1 + level * 0.2), symbol),
                    price          = bid_price,
                    order_type     = OrderType.LIMIT,
                    time_in_force  = TimeInForce.GTX,
                    strategy_tag   = f"mm_bid_L{level}",
                ))

            # ASK
            ask_price = mid * (1 + spread_half * vol_adj - skew + level_adj)
            ask_price = self._round_price(ask_price, symbol)
            if ask_price > 0 and (ob.best_ask is None or ask_price > ob.best_ask.price):
                orders.append(Order(
                    symbol         = symbol,
                    side           = Side.SELL,
                    qty            = self._round_qty(qty * (1 + level * 0.2), symbol),
                    price          = ask_price,
                    order_type     = OrderType.LIMIT,
                    time_in_force  = TimeInForce.GTX,
                    strategy_tag   = f"mm_ask_L{level}",
                ))

        return orders

    async def _cancel_existing_quotes(self, symbol: str) -> None:
        existing = self._quote_order_ids.get(symbol, [])
        if not existing:
            return

        active_orders = {o.client_order_id: o for o in self.om.active_orders(symbol)}

        for cid in existing:
            order = active_orders.get(cid)
            if order and order.is_active:
                await self.bus.publish(Event(
                    EventType.ORDER_CANCEL_REQ,
                    {
                        "symbol": symbol,
                        "client_order_id": cid,
                        "exchange_order_id": order.exchange_order_id,
                    },
                    source="MarketMaker"
                ))

        self._quote_order_ids[symbol] = []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _round_price(self, price: float, symbol: str) -> float:
        """Round to appropriate tick size. Override per symbol as needed."""
        tick = 0.1 if "BTC" in symbol else 0.01
        return round(price / tick) * tick

    def _round_qty(self, qty: float, symbol: str) -> float:
        """Round to appropriate lot size."""
        step = 0.001 if "BTC" in symbol else 0.01
        return round(qty / step) * step
