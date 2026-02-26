"""
Binance Execution Engine
========================
Handles all communication with Binance USDT-margined Futures API.

Responsibilities:
  - Submit / cancel orders via REST
  - Receive user data stream (fills, order updates) via WebSocket
  - Translate exchange messages → internal events
  - Manage listen key keepalive

REST endpoint: POST /fapi/v1/order
WS user stream: wss://fstream.binance.com/ws/<listenKey>
"""

from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional
from urllib.parse import urlencode

import aiohttp
import websockets

from config import BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_REST_URL, BINANCE_WS_URL
from core.models import Order, OrderStatus, Fill, Side
from core.event_bus import EventBus, Event, EventType

log = logging.getLogger("ExecutionEngine")


def _sign(params: Dict, secret: str) -> str:
    query = urlencode(params)
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceExecutionEngine:
    """
    Connects to Binance Futures REST + WS.
    Translates ORDER_APPROVED events into real exchange orders.
    """

    def __init__(self, bus: EventBus):
        self.bus    = bus
        self._session: Optional[aiohttp.ClientSession] = None
        self._listen_key: Optional[str] = None
        self._running = False

        bus.subscribe(EventType.ORDER_APPROVED,   self._on_order_approved)
        bus.subscribe(EventType.ORDER_CANCEL_REQ, self._on_cancel_request)
        bus.subscribe(EventType.SYSTEM_HALT,      self._on_system_halt)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={
                "X-MBX-APIKEY": BINANCE_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )
        self._running = True
        log.info(f"ExecutionEngine started → {BINANCE_REST_URL}")

        # Get listen key and start user data stream
        await self._refresh_listen_key()
        asyncio.create_task(self._user_data_stream())
        asyncio.create_task(self._keepalive_listen_key())

    async def stop(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()
        log.info("ExecutionEngine stopped")

    # ── Order Submission ──────────────────────────────────────────────────────

    async def _on_order_approved(self, event: Event) -> None:
        order: Order = event.data
        await self.bus.publish(Event(EventType.ORDER_SENT, order, source="ExecutionEngine"))
        asyncio.create_task(self._submit_order(order))

    async def _submit_order(self, order: Order) -> None:
        params = {
            "symbol":           order.symbol,
            "side":             order.side.value,
            "type":             order.order_type.value,
            "quantity":         f"{order.qty:.6f}",
            "newClientOrderId": order.client_order_id,
            "timestamp":        int(time.time() * 1000),
        }
        if order.price is not None:
            params["price"]       = f"{order.price:.8f}"
            params["timeInForce"] = order.time_in_force.value

        params["signature"] = _sign(params, BINANCE_API_SECRET)

        try:
            async with self._session.post(
                f"{BINANCE_REST_URL}/fapi/v1/order",
                data=params
            ) as resp:
                body = await resp.json()
                if resp.status == 200:
                    eid = str(body.get("orderId", ""))
                    log.info(f"Order submitted: clientId={order.client_order_id} exchId={eid}")
                    await self.bus.publish(Event(
                        EventType.ORDER_ACK,
                        {"client_order_id": order.client_order_id,
                         "exchange_order_id": eid},
                        source="ExecutionEngine"
                    ))
                else:
                    log.error(f"Order submit failed [{resp.status}]: {body}")
                    await self.bus.publish(Event(
                        EventType.ORDER_REJECTED,
                        {"order": order, "reason": str(body)},
                        source="ExecutionEngine"
                    ))
        except Exception as e:
            log.error(f"Order submit exception: {e}", exc_info=True)

    # ── Order Cancellation ────────────────────────────────────────────────────

    async def _on_cancel_request(self, event: Event) -> None:
        payload = event.data  # {symbol, exchange_order_id or client_order_id}
        asyncio.create_task(self._cancel_order(payload))

    async def _cancel_order(self, payload: Dict) -> None:
        params = {
            "symbol":    payload["symbol"],
            "timestamp": int(time.time() * 1000),
        }
        if "exchange_order_id" in payload:
            params["orderId"] = payload["exchange_order_id"]
        else:
            params["origClientOrderId"] = payload["client_order_id"]

        params["signature"] = _sign(params, BINANCE_API_SECRET)

        try:
            async with self._session.delete(
                f"{BINANCE_REST_URL}/fapi/v1/order",
                params=params
            ) as resp:
                body = await resp.json()
                if resp.status == 200:
                    log.info(f"Order cancelled: {payload}")
                    await self.bus.publish(Event(
                        EventType.ORDER_CANCELLED, payload,
                        source="ExecutionEngine"
                    ))
                else:
                    log.error(f"Cancel failed [{resp.status}]: {body}")
        except Exception as e:
            log.error(f"Cancel exception: {e}", exc_info=True)

    async def cancel_all(self, symbol: str) -> None:
        """Cancel all open orders for a symbol (e.g. on halt)."""
        params = {
            "symbol":    symbol,
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, BINANCE_API_SECRET)
        try:
            async with self._session.delete(
                f"{BINANCE_REST_URL}/fapi/v1/allOpenOrders",
                params=params
            ) as resp:
                if resp.status == 200:
                    log.info(f"All orders cancelled for {symbol}")
        except Exception as e:
            log.error(f"Cancel all exception: {e}")

    # ── User Data Stream (WebSocket) ──────────────────────────────────────────

    async def _refresh_listen_key(self) -> None:
        params = {"timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(params, BINANCE_API_SECRET)
        async with self._session.post(
            f"{BINANCE_REST_URL}/fapi/v1/listenKey",
            data=params
        ) as resp:
            body = await resp.json()
            self._listen_key = body.get("listenKey")
            log.info(f"Listen key obtained: {self._listen_key[:8]}...")

    async def _keepalive_listen_key(self) -> None:
        """Ping listen key every 25 minutes (expires after 30)."""
        while self._running:
            await asyncio.sleep(25 * 60)
            if self._listen_key:
                try:
                    params = {"listenKey": self._listen_key,
                              "timestamp": int(time.time() * 1000)}
                    params["signature"] = _sign(params, BINANCE_API_SECRET)
                    async with self._session.put(
                        f"{BINANCE_REST_URL}/fapi/v1/listenKey",
                        data=params
                    ) as resp:
                        if resp.status == 200:
                            log.debug("Listen key refreshed")
                except Exception as e:
                    log.error(f"Listen key keepalive error: {e}")

    async def _user_data_stream(self) -> None:
        """Subscribe to user data WebSocket and translate events."""
        if not self._listen_key:
            log.error("No listen key — user data stream unavailable")
            return

        url = f"{BINANCE_WS_URL}/{self._listen_key}"
        reconnect_delay = 1

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    log.info(f"User data stream connected")
                    reconnect_delay = 1
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_user_event(msg)
                        except Exception as e:
                            log.error(f"User stream message error: {e}")
            except Exception as e:
                log.error(f"User data stream error: {e} — reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)

    async def _handle_user_event(self, msg: Dict) -> None:
        event_type = msg.get("e")

        if event_type == "ORDER_TRADE_UPDATE":
            order_data = msg.get("o", {})
            status     = order_data.get("X")          # order status
            ex_oid     = str(order_data.get("i", "")) # exchange order id
            cl_oid     = order_data.get("c", "")      # client order id
            symbol     = order_data.get("s", "")
            side_str   = order_data.get("S", "BUY")
            fill_qty   = float(order_data.get("l", 0))  # last filled qty
            fill_px    = float(order_data.get("L", 0))  # last fill price
            fee        = float(order_data.get("n", 0))
            fee_asset  = order_data.get("N", "USDT")

            if status in ("FILLED", "PARTIALLY_FILLED") and fill_qty > 0:
                fill = Fill(
                    order_id  = cl_oid or ex_oid,
                    symbol    = symbol,
                    side      = Side(side_str),
                    qty       = fill_qty,
                    price     = fill_px,
                    fee       = fee,
                    fee_asset = fee_asset,
                )
                evt_type = (EventType.ORDER_FILLED if status == "FILLED"
                            else EventType.ORDER_PARTIAL)
                await self.bus.publish(Event(evt_type, fill, source="ExecutionEngine"))

            elif status == "CANCELED":
                await self.bus.publish(Event(
                    EventType.ORDER_CANCELLED,
                    {"exchange_order_id": ex_oid, "client_order_id": cl_oid},
                    source="ExecutionEngine"
                ))
            elif status == "REJECTED":
                log.warning(f"Order REJECTED by exchange: {order_data}")

        elif event_type == "ACCOUNT_UPDATE":
            log.debug(f"Account update: {msg.get('a', {})}")

    # ── System Halt ───────────────────────────────────────────────────────────

    async def _on_system_halt(self, event: Event) -> None:
        from config import TRADING
        log.critical("HALT received — cancelling all open orders")
        for symbol in TRADING.symbols:
            await self.cancel_all(symbol)
