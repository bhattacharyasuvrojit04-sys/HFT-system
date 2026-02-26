"""
Performance Monitor
===================
Logs system metrics periodically:
  - P&L (realized, fees, net)
  - Positions per symbol
  - Order statistics
  - Event bus throughput
  - Latency estimates
"""

from __future__ import annotations
import asyncio
import logging
import time
from typing import Optional

from core.event_bus import EventBus, Event, EventType
from core.order_manager import OrderManager

log = logging.getLogger("Monitor")


class PerformanceMonitor:
    def __init__(self, bus: EventBus, order_manager: OrderManager,
                 interval_s: float = 10.0):
        self.bus   = bus
        self.om    = order_manager
        self.interval = interval_s
        self._start_time = time.time()
        self._fill_count = 0
        self._order_count = 0

        bus.subscribe(EventType.ORDER_FILLED,  self._on_fill)
        bus.subscribe(EventType.ORDER_APPROVED, self._on_order)

    async def _on_fill(self, event: Event) -> None:
        self._fill_count += 1

    async def _on_order(self, event: Event) -> None:
        self._order_count += 1

    async def run(self) -> None:
        log.info("PerformanceMonitor started")
        while True:
            await asyncio.sleep(self.interval)
            self._print_stats()

    def _print_stats(self) -> None:
        elapsed = time.time() - self._start_time
        summary = self.om.summary()

        log.info("=" * 60)
        log.info(f"  SYSTEM STATS  (uptime: {elapsed:.0f}s)")
        log.info("=" * 60)
        log.info(f"  Orders submitted : {self._order_count}")
        log.info(f"  Fills            : {self._fill_count}")
        log.info(f"  Active orders    : {summary['active_orders']}")
        log.info(f"  Realized P&L     : ${summary['realized_pnl']:+.4f}")
        log.info(f"  Total fees       : ${summary['fees']:.4f}")
        log.info(f"  Net P&L          : ${summary['net_pnl']:+.4f}")
        log.info("-" * 60)
        for sym, pos_repr in summary["positions"].items():
            log.info(f"  {pos_repr}")
        log.info("=" * 60)
