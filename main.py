"""
HFT Trading System — Main Entry Point
======================================
Wires all components together and runs the async event loop.

Architecture:
                    ┌─────────────────────────────────────────┐
                    │              EVENT BUS                   │
                    └──┬──────┬──────┬──────┬──────┬──────────┘
                       │      │      │      │      │
               ┌───────┘  ┌───┘  ┌───┘  ┌───┘  ┌───┘
               ▼          ▼      ▼      ▼      ▼
          DataFeed   Strategy  Risk   Execution  Monitor
               │          │      │      │
               │    ORDER_REQUEST│      │
               │          └──────►      │
               │                 │ORDER_APPROVED
               │                 └──────►
               │                        │ REST /order
               │                        ├──────────── Binance Exchange
               │                        │ WS fills
               ◄────────────────────────┘

Usage:
  # Paper trading (testnet)
  python main.py

  # Live (set USE_TESTNET=False in config.py and set real API keys)
  BINANCE_API_KEY=xxx BINANCE_API_SECRET=yyy python main.py

  # Change strategy
  Edit config.py → TRADING.active_strategy = "momentum"
"""

import asyncio
import logging
import signal
import sys
from typing import Optional

# ─── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)-20s %(message)s",
    datefmt  = "%H:%M:%S",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("trading.log"),
    ]
)
log = logging.getLogger("Main")

# ─── Internal Imports ─────────────────────────────────────────────────────────
from config import TRADING, USE_TESTNET
from core.event_bus import EventBus, Event, EventType
from core.order_manager import OrderManager
from data.market_feed import MarketDataFeed
from execution.binance_engine import BinanceExecutionEngine
from risk.risk_manager import RiskManager
from utils.monitor import PerformanceMonitor


class TradingSystem:
    """
    Top-level orchestrator.
    Instantiates all components, wires them via the event bus,
    and manages startup/shutdown lifecycle.
    """

    def __init__(self):
        log.info("=" * 60)
        log.info("  INITIALIZING HFT TRADING SYSTEM")
        log.info(f"  Mode     : {'TESTNET' if USE_TESTNET else '⚠️  LIVE'}")
        log.info(f"  Strategy : {TRADING.active_strategy}")
        log.info(f"  Symbols  : {TRADING.symbols}")
        log.info("=" * 60)

        # Core infrastructure
        self.bus           = EventBus()
        self.order_manager = OrderManager(self.bus)

        # Market data
        self.data_feed = MarketDataFeed(self.bus, TRADING.symbols)

        # Risk
        self.risk_manager = RiskManager(self.bus, self.order_manager)

        # Execution
        self.execution = BinanceExecutionEngine(self.bus)

        # Strategy
        self.strategy = self._load_strategy()

        # Monitoring
        self.monitor = PerformanceMonitor(self.bus, self.order_manager,
                                          interval_s=15.0)

        # System halt listener
        self.bus.subscribe(EventType.SYSTEM_HALT, self._on_halt)
        self._running = True

    def _load_strategy(self):
        name = TRADING.active_strategy.lower()
        if name == "market_maker":
            from strategies.market_maker import MarketMakerStrategy
            return MarketMakerStrategy(self.bus, self.order_manager)
        elif name == "momentum":
            from strategies.momentum import MomentumStrategy
            return MomentumStrategy(self.bus, self.order_manager)
        else:
            raise ValueError(f"Unknown strategy: {name}. "
                             f"Available: market_maker, momentum")

    async def start(self) -> None:
        log.info("Starting all components...")

        # Start execution first (need session for listen key)
        await self.execution.start()

        # Start market data feed
        await self.data_feed.start()

        log.info("✅ All components started. Running...")

        # Run event bus + monitor concurrently
        await asyncio.gather(
            self.bus.run(),
            self.monitor.run(),
        )

    async def shutdown(self, reason: str = "User shutdown") -> None:
        log.info(f"Shutdown initiated: {reason}")
        self._running = False

        # Emergency cancel all orders
        log.info("Cancelling all open orders...")
        for sym in TRADING.symbols:
            await self.execution.cancel_all(sym)

        # Print final stats
        summary = self.order_manager.summary()
        log.info("=" * 60)
        log.info("  FINAL SUMMARY")
        log.info("=" * 60)
        log.info(f"  Net P&L  : ${summary['net_pnl']:+.4f}")
        log.info(f"  Fills    : {summary['total_orders']} orders")
        log.info("=" * 60)

        self.bus.stop()
        await self.data_feed.stop()
        await self.execution.stop()

    async def _on_halt(self, event: Event) -> None:
        reason = event.data.get("reason", "Unknown") if isinstance(event.data, dict) else str(event.data)
        log.critical(f"🛑 SYSTEM HALT EVENT: {reason}")
        # Cancel all open orders via execution engine (already subscribed)


# ─── Entry Point ──────────────────────────────────────────────────────────────

async def main():
    system = TradingSystem()

    loop = asyncio.get_event_loop()

    # Graceful shutdown on SIGINT / SIGTERM
    def _signal_handler():
        log.info("Signal received — shutting down gracefully...")
        asyncio.create_task(system.shutdown("Signal interrupt"))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        await system.start()
    except KeyboardInterrupt:
        pass
    finally:
        await system.shutdown()


if __name__ == "__main__":
    # Verify config sanity before starting
    from config import BINANCE_API_KEY, BINANCE_API_SECRET
    if BINANCE_API_KEY == "YOUR_API_KEY":
        log.warning("⚠️  API keys not set. Set BINANCE_API_KEY and BINANCE_API_SECRET environment variables.")
        log.warning("⚠️  Running on testnet with placeholder keys — will fail at order submission.")

    asyncio.run(main())
