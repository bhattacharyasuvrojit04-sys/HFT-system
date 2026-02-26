"""
Market Data Feed
================
Connects to Binance public WebSocket streams:
  - Depth (order book) updates  → ORDERBOOK_UPDATE events
  - Aggregate trade stream      → TRADE_TICK events

Maintains a local order book using Binance's incremental depth update protocol.
Uses combined stream endpoint for efficiency (1 WS connection per N symbols).
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import aiohttp
import websockets

from config import BINANCE_REST_URL, BINANCE_WS_URL, TRADING
from core.models import OrderBook, PriceLevel, Tick, Side
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("DataFeed")

DEPTH_LEVELS = 20  # How many orderbook levels to maintain


class LocalOrderBook:
    """
    Maintains a local copy of the Binance order book using
    the incremental update protocol (diff depth stream).
    
    Protocol:
      1. Subscribe to <symbol>@depth stream
      2. Fetch snapshot via REST /fapi/v1/depth
      3. Discard buffered events with lastUpdateId <= snapshot.lastUpdateId
      4. Apply updates in order
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: Dict[float, float] = {}  # price → qty
        self.asks: Dict[float, float] = {}
        self.last_update_id: int = 0
        self._initialized = False
        self._buffer: List[Dict] = []

    async def initialize(self, session: aiohttp.ClientSession) -> None:
        """Fetch REST snapshot to bootstrap the book."""
        url = f"{BINANCE_REST_URL}/fapi/v1/depth"
        params = {"symbol": self.symbol, "limit": DEPTH_LEVELS}

        async with session.get(url, params=params) as resp:
            data = await resp.json()

        self.last_update_id = data["lastUpdateId"]
        self.bids = {float(p): float(q) for p, q in data["bids"]}
        self.asks = {float(p): float(q) for p, q in data["asks"]}
        self._initialized = True

        # Replay any buffered updates
        for event in self._buffer:
            self._apply(event)
        self._buffer.clear()
        log.info(f"{self.symbol} orderbook initialized (lastUpdateId={self.last_update_id})")

    def on_diff(self, event: Dict) -> Optional["LocalOrderBook"]:
        """Process a depth diff event. Returns self if valid, None to skip."""
        if not self._initialized:
            self._buffer.append(event)
            return None

        u = event["u"]   # final update ID in event
        pu = event["pu"]  # prev final update ID

        # Discard stale events
        if u <= self.last_update_id:
            return None

        self._apply(event)
        return self

    def _apply(self, event: Dict) -> None:
        self.last_update_id = event["u"]
        for price_str, qty_str in event.get("b", []):
            price = float(price_str)
            qty   = float(qty_str)
            if qty == 0:
                self.bids.pop(price, None)
            else:
                self.bids[price] = qty

        for price_str, qty_str in event.get("a", []):
            price = float(price_str)
            qty   = float(qty_str)
            if qty == 0:
                self.asks.pop(price, None)
            else:
                self.asks[price] = qty

    def to_model(self) -> OrderBook:
        sorted_bids = sorted(self.bids.items(), reverse=True)[:DEPTH_LEVELS]
        sorted_asks = sorted(self.asks.items())[:DEPTH_LEVELS]
        return OrderBook(
            symbol    = self.symbol,
            bids      = [PriceLevel(p, q) for p, q in sorted_bids],
            asks      = [PriceLevel(p, q) for p, q in sorted_asks],
            timestamp = time.time(),
        )


class MarketDataFeed:
    """
    Manages WebSocket connections to Binance market data streams.
    Emits ORDERBOOK_UPDATE and TRADE_TICK events to the bus.
    """

    def __init__(self, bus: EventBus, symbols: Optional[List[str]] = None):
        self.bus     = bus
        self.symbols = symbols or TRADING.symbols
        self._books: Dict[str, LocalOrderBook] = {
            s: LocalOrderBook(s) for s in self.symbols
        }
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._running = True

        # Initialize all books from REST snapshot
        await asyncio.gather(*[
            self._books[s].initialize(self._session)
            for s in self.symbols
        ])

        # Start combined stream
        asyncio.create_task(self._stream())
        log.info(f"MarketDataFeed started for {self.symbols}")

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def _stream(self) -> None:
        """Connect to Binance combined stream for depth + aggTrade."""
        streams = []
        for sym in self.symbols:
            s = sym.lower()
            streams.append(f"{s}@depth@100ms")   # depth diff, 100ms updates
            streams.append(f"{s}@aggTrade")       # aggregated trades

        url = f"{BINANCE_WS_URL}/stream?streams={'/' .join(streams)}"
        reconnect_delay = 1

        while self._running:
            try:
                async with websockets.connect(
                    url, ping_interval=20, max_size=10 * 1024 * 1024
                ) as ws:
                    log.info(f"Market data stream connected ({len(streams)} streams)")
                    reconnect_delay = 1
                    async for raw in ws:
                        try:
                            envelope = json.loads(raw)
                            stream   = envelope.get("stream", "")
                            data     = envelope.get("data", {})
                            await self._dispatch(stream, data)
                        except Exception as e:
                            log.error(f"Stream message error: {e}")
            except Exception as e:
                log.error(f"Stream error: {e} — reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)

    async def _dispatch(self, stream: str, data: Dict) -> None:
        if "@depth" in stream:
            await self._handle_depth(data)
        elif "@aggTrade" in stream:
            await self._handle_agg_trade(data)

    async def _handle_depth(self, data: Dict) -> None:
        symbol = data.get("s", "").upper()
        book   = self._books.get(symbol)
        if not book:
            return

        result = book.on_diff(data)
        if result:
            ob = result.to_model()
            await self.bus.publish(Event(
                EventType.ORDERBOOK_UPDATE, ob, source="DataFeed"
            ))

    async def _handle_agg_trade(self, data: Dict) -> None:
        symbol   = data.get("s", "").upper()
        price    = float(data.get("p", 0))
        qty      = float(data.get("q", 0))
        is_buyer = data.get("m", True)  # m=True means market sell (buyer is mm)
        side     = Side.SELL if is_buyer else Side.BUY  # aggressor side

        tick = Tick(
            symbol    = symbol,
            price     = price,
            qty       = qty,
            side      = side,
            timestamp = data.get("T", time.time() * 1000) / 1000,
        )
        await self.bus.publish(Event(EventType.TRADE_TICK, tick, source="DataFeed"))
