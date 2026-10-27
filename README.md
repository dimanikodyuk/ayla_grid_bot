# Ayla Grid Bot v0.8 — PAPER ONLY

## What's new
- Live Bybit public Spot WebSocket in browser for the selected **chart** market, with reconnect and REST fallback. WebSocket only updates the chart; backend paper fills still use REST ticker every ~5 seconds.
- Mainnet/Testnet chart selector is **view-only**; paper execution always uses `BYBIT_ENV` from `.env`. Overlay of paper orders/trades only on the trading market.
- Live candle updates, volume histogram, OHLC on hover, Kyiv timestamps, zoom and pan. 500 historical candles.
- Market comparison card (public tickers) with 24h change.
- Nearest BUY distance, 20-candle high-low range, last-candle price change.
- Transient REST outages no longer permanently pause the bot; it skips fills until valid fresh quotes return. **8% drawdown stop remains permanent until manually restarted**.
- Compatible with existing `ayla_v04.db`; no schema changes.

## Installation on Raspberry Pi
1. Stop: `sudo systemctl stop ayla-grid`.
2. Backup: `cp main.py main_before_v08.py; cp index.html index_before_v08.html; cp ayla_v04.db ayla_v04_before_v08.db`.
3. Copy `main.py`, `index.html` from ZIP to `~/ayla_grid_bot/`. **Do not overwrite `.env` or `ayla_v04.db`.**
4. `cd ~/ayla_grid_bot && .venv/bin/python -m py_compile main.py`
5. `sudo systemctl start ayla-grid && sudo systemctl status ayla-grid --no-pager`
6. `curl -fsS http://127.0.0.1:8090/api/health` and `curl -fsS http://127.0.0.1:8090/api/markets`
7. Browser Ctrl+F5. WebSocket requires the viewing browser to reach Bybit public WS. If unavailable, REST updates every 30 seconds.

## Week-long run
- For a meaningful paper test, choose `BYBIT_ENV=mainnet` and leave it unchanged for the entire period. Changing environment mid-run mixes histories.
- The UI **does not need to remain open** for paper fills; `systemd` keeps the backend alive. Live WS is only for browser chart rendering.
- Enable Telegram daily/trade notifications if desired; verify with Test button.
- Verify `systemctl is-enabled ayla-grid` returns `enabled`; check `journalctl -u ayla-grid -n 30 --no-pager` if issues.
- Preserve the database; do not press Reset during the test.

## Security
- No real trading; no private Bybit API calls. API keys not needed.
- Web UI is unauthenticated, including Start/Pause/Reset. Never expose port 8090 publicly. Use `HOST=127.0.0.1` and SSH forwarding, or a trusted LAN only.
- WebSocket chart data is public. TradingView Lightweight Charts loaded via CDN; fallback chart remains available.
- Paper fills are optimistic, spread included but slippage/latency not modeled. Backtest OHLC is approximate.
