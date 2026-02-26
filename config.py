"""
HFT System Configuration
========================
Central configuration for all system components.
"""

import os
from dataclasses import dataclass, field
from typing import List, Dict

# ─── API CREDENTIALS ──────────────────────────────────────────────────────────
# Set these via environment variables — NEVER hardcode keys in source.
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "YOUR_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_API_SECRET")

# Toggle between real Binance and Testnet
USE_TESTNET = True

BINANCE_REST_URL = (
    "https://testnet.binancefuture.com" if USE_TESTNET
    else "https://fapi.binance.com"
)
BINANCE_WS_URL = (
    "wss://stream.binancefuture.com/ws" if USE_TESTNET
    else "wss://fstream.binance.com/ws"
)


@dataclass
class TradingConfig:
    # Symbols to trade (Binance USDT-margined perp futures)
    symbols: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])

    # Order sizing
    order_size_usd: float = 100.0       # $ per order
    max_position_usd: float = 500.0     # Max total position per symbol
    leverage: int = 1                   # 1 = no leverage (safest)

    # Execution
    default_order_type: str = "LIMIT"   # LIMIT or MARKET
    time_in_force: str = "GTX"          # GTX = Post-Only (maker only, avoids taker fees)
    tick_interval_ms: int = 100         # Main loop tick rate (ms)

    # Strategy selection
    active_strategy: str = "market_maker"  # market_maker | momentum | stat_arb


@dataclass
class RiskConfig:
    # Per-symbol position limits
    max_position_usd: float = 500.0
    max_open_orders: int = 10

    # Drawdown controls
    max_daily_loss_usd: float = 200.0
    max_drawdown_pct: float = 0.05      # 5% drawdown kills system

    # Order rate limiting
    max_orders_per_second: int = 5      # Binance allows 300/10s for futures

    # Spread / slippage
    min_spread_bps: float = 2.0         # Minimum spread before quoting (bps)
    max_slippage_bps: float = 10.0      # Max acceptable slippage

    # Kill switch
    halt_on_consecutive_losses: int = 5


@dataclass
class MarketMakerConfig:
    """Parameters for the market making strategy."""
    spread_bps: float = 5.0            # Quote spread in basis points
    order_levels: int = 3              # Number of levels per side
    level_spacing_bps: float = 3.0    # Spacing between levels
    order_refresh_ms: int = 500        # How often to refresh quotes
    inventory_skew_factor: float = 0.3  # Skew quotes based on inventory
    max_inventory_usd: float = 300.0


@dataclass
class MomentumConfig:
    """Parameters for the momentum strategy."""
    fast_ema_periods: int = 5
    slow_ema_periods: int = 20
    signal_threshold: float = 0.001    # Min EMA diff to trade
    position_hold_ms: int = 5000       # Hold time before reassessing


TRADING  = TradingConfig()
RISK     = RiskConfig()
MKT_MAKER = MarketMakerConfig()
MOMENTUM  = MomentumConfig()
