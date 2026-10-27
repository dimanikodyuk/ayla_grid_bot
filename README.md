# Ayla Grid Bot v0.4 — PAPER ONLY

Start on Windows: ` .\.venv\Scripts\python.exe -m pip install -r requirements.txt ` then ` .\.venv\Scripts\python.exe main.py `; open http://127.0.0.1:8090 . Server binds to 0.0.0.0:8090; use only on a trusted LAN, never forward the port to the internet.

## Telegram
Copy `.env.example` to `.env` and enter `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (BotFather and your chat id). Restart app, use Telegram test button, then enable notifications in web UI. Daily report defaults to 21:00 Europe/Kyiv. If bot is offline at that time, it sends once after restart later the same day; old days are not backfilled. Token is never returned by the API. Do not commit `.env`.

## Network
Mainnet and Testnet are **market-data-only**. No private Bybit API keys, no real orders. Network changes require pause and zero executed trades; Reset Paper first if needed. Testnet prices/liquidity can differ greatly from mainnet. Paper wallet starts at 100 USDT. v0.4 uses new `ayla_v04.db`; previous databases remain untouched.

## Limitations
Polling every 5 seconds; paper limit fills are optimistic; historical OHLC assumes at most one fill per bar, with no spread/slippage/partial fills. Backtest history is capped at 1000 candles and shorter than requested when timeframe requires more. Adaptive gap is experimental. Dashboard is unauthenticated: LAN only. Daily report includes cumulative PnL, not daily PnL; daily trades and fees are filtered to Kyiv date. Notification delivery is retried; a crash after sending but before recording can duplicate a message. No automatic live trading. No strategy optimization / multi-run walk-forward yet.
