"""
Event Bus
=========
Lightweight pub/sub message bus. All components communicate through events,
keeping the architecture decoupled and testable.

Events flow:
  DataFeed → bus → Strategy → bus → RiskManager → bus → ExecutionEngine → exchange
                                                       ↓
                                               OrderManager (state)
"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Coroutine, Dict, List
import time

log = logging.getLogger("EventBus")


class EventType(Enum):
    # Market data
    ORDERBOOK_UPDATE  = auto()
    TRADE_TICK        = auto()
    KLINE_UPDATE      = auto()

    # Order lifecycle
    ORDER_REQUEST     = auto()   # Strategy → Risk
    ORDER_APPROVED    = auto()   # Risk → Execution
    ORDER_REJECTED    = auto()   # Risk → Strategy
    ORDER_SENT        = auto()   # Execution → OrderManager
    ORDER_ACK         = auto()   # Exchange → OrderManager
    ORDER_FILLED      = auto()   # Exchange → PositionManager
    ORDER_PARTIAL     = auto()   # Exchange → PositionManager
    ORDER_CANCELLED   = auto()   # Exchange/System → OrderManager
    ORDER_CANCEL_REQ  = auto()   # Strategy → Execution (cancel request)

    # System
    POSITION_UPDATE   = auto()
    RISK_BREACH       = auto()   # Risk limit hit → halt/reduce
    SYSTEM_HALT       = auto()   # Kill switch
    HEARTBEAT         = auto()


@dataclass
class Event:
    type:      EventType
    data:      Any
    timestamp: float = field(default_factory=time.time)
    source:    str   = "unknown"

    def __repr__(self) -> str:
        return f"Event({self.type.name} from={self.source} t={self.timestamp:.3f})"


# Handler type: async callable receiving an Event
Handler = Callable[[Event], Coroutine]


class EventBus:
    """
    Async pub/sub event bus.
    Handlers are coroutines registered per EventType.
    Publishing dispatches to all handlers concurrently.
    """

    def __init__(self):
        self._handlers: Dict[EventType, List[Handler]] = defaultdict(list)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        self._running = False
        self._stats: Dict[EventType, int] = defaultdict(int)

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)
        log.debug(f"Subscribed {handler.__qualname__} → {event_type.name}")

    def subscribe_many(self, mappings: Dict[EventType, Handler]) -> None:
        for evt_type, handler in mappings.items():
            self.subscribe(evt_type, handler)

    async def publish(self, event: Event) -> None:
        """Non-blocking publish — puts event on queue."""
        try:
            self._queue.put_nowait(event)
            self._stats[event.type] += 1
        except asyncio.QueueFull:
            log.error(f"Event queue full! Dropping {event}")

    async def publish_sync(self, event: Event) -> None:
        """Immediately dispatch to all handlers (bypasses queue)."""
        handlers = self._handlers.get(event.type, [])
        if handlers:
            await asyncio.gather(*[h(event) for h in handlers],
                                 return_exceptions=True)

    async def run(self) -> None:
        """Main dispatch loop — runs until stopped."""
        self._running = True
        log.info("EventBus started")
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                handlers = self._handlers.get(event.type, [])
                if handlers:
                    results = await asyncio.gather(
                        *[h(event) for h in handlers],
                        return_exceptions=True
                    )
                    for r in results:
                        if isinstance(r, Exception):
                            log.error(f"Handler error for {event}: {r}", exc_info=r)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error(f"EventBus dispatch error: {e}", exc_info=True)

    def stop(self) -> None:
        self._running = False
        log.info("EventBus stopping")

    def stats(self) -> Dict[str, int]:
        return {k.name: v for k, v in self._stats.items()}
