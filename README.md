# HFT Algorithmic Trading System — Binance Futures

A production-quality algorithmic trading system for Binance USDT-Margined Perpetual Futures.

## Architecture

```
main.py
├── EventBus          — Async pub/sub backbone (all components talk through this)
├── OrderManager      — Single source of truth for orders & positions
├── MarketDataFeed    — WebSocket orderbook + trade stream (Binance combined stream)
├── RiskManager       — Pre-trade risk checks, rate limiting, kill switch
├── ExecutionEngine   — REST order submission + WS user data stream
├── Strategy          — MarketMaker or Momentum
└── PerformanceMonitor — P&L and stats logging
```

## Event Flow

```
DataFeed ──(ORDERBOOK_UPDATE/TRADE_TICK)──► Strategy
Strategy ──(ORDER_REQUEST)──────────────► RiskManager
RiskManager ──(ORDER_APPROVED)──────────► ExecutionEngine
ExecutionEngine ──(REST POST)───────────► Binance Exchange
Binance ──(WS fill)─────────────────────► ExecutionEngine
ExecutionEngine ──(ORDER_FILLED)────────► OrderManager
OrderManager ──(POSITION_UPDATE)────────► RiskManager / Monitor
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get Binance Testnet API Keys
- Go to https://testnet.binancefuture.com
- Generate API key + secret (free, safe to test with)

### 3. Set environment variables
```bash
export BINANCE_API_KEY="your_key_here"
export BINANCE_API_SECRET="your_secret_here"
```

### 4. Configure the system
Edit `config.py`:
- `TRADING.symbols` — which perp futures to trade (e.g. `["BTCUSDT", "ETHUSDT"]`)
- `TRADING.active_strategy` — `"market_maker"` or `"momentum"`
- `TRADING.order_size_usd` — $ size per order
- `RISK.max_position_usd` — max position per symbol
- `RISK.max_daily_loss_usd` — daily loss kill switch
- `USE_TESTNET = True` — keep this True until you're confident

### 5. Run
```bash
python main.py
```

## Strategies

### Market Maker (`market_maker`)
Quotes two-sided limit orders using Post-Only (GTX) time-in-force to earn maker rebates.
- Adjusts bid/ask based on current inventory (inventory skew)
- Quotes multiple levels (configurable)
- Refreshes quotes on timer or significant mid-price movement
- Best for: liquid markets with stable spreads

### Momentum (`momentum`)
EMA crossover with order book imbalance confirmation.
- Fast/slow EMA crossover signal
- Confirmed by order book imbalance (buy or sell pressure)
- IOC entry orders for fast fills
- Market exit after configurable hold time
- Best for: trending markets

## Risk Controls

| Control | Default | Description |
|---|---|---|
| Max position per symbol | $500 | Won't add to position beyond this |
| Max open orders | 10 | Per symbol |
| Max orders/second | 5 | Rate limiting |
| Daily loss limit | $200 | Halts system when breached |
| Price sanity check | 200bps | Rejects orders wildly far from mid |
| Min spread | 2bps | Won't quote if spread is too tight |

## ⚠️ Important Warnings

1. **Test on testnet first** — keep `USE_TESTNET = True` until fully validated
2. **Start with tiny sizes** — `order_size_usd = 10`, `max_position_usd = 50`
3. **This is NOT financial advice** — algorithmic trading carries significant risk of loss
4. **Binance API limits** — Futures: 300 orders per 10 seconds. The risk manager enforces this.
5. **Monitor continuously** — don't leave unattended, especially in early testing

## File Structure

```
hft_system/
├── main.py                    # Entry point
├── config.py                  # All configuration
├── requirements.txt
├── core/
│   ├── models.py              # Order, Fill, Position, OrderBook, Tick
│   ├── event_bus.py           # Async pub/sub event bus
│   └── order_manager.py       # Order & position state
├── data/
│   └── market_feed.py         # Binance WebSocket market data
├── execution/
│   └── binance_engine.py      # Order submission + user data stream
├── risk/
│   └── risk_manager.py        # Pre-trade risk checks
├── strategies/
│   ├── market_maker.py        # Market making with inventory skew
│   └── momentum.py            # EMA crossover momentum
└── utils/
    └── monitor.py             # P&L monitoring & logging
```
