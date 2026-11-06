# Ayla Grid Bot v0.9 — Mainnet prices, PAPER only

## Changes
- `BYBIT_ENV=mainnet`: uses **public** Bybit mainnet ticker/kline/WebSocket. No private Bybit order API is used. `mode=PAPER` always.
- Database separation: testnet retains `ayla_v04.db`; mainnet creates `ayla_mainnet_paper.db` with fresh 100 virtual USDT. Never mix historical testnet SOL with mainnet prices.
- Safe automatic re-centering: enabled by default (`AYLA_AUTO_RECENTER=1`), only when SOL balance is zero, every open order is BUY, and price has remained outside all BUY levels for 6 hours. Open BUY orders are replaced; trade history and wallet preserved. **No forced sales** and no re-centering while holding SOL.
- `/api/strategy`: realized FIFO-per-level PnL, unrealized PnL, cost basis, portfolio exposure, range status and persistent 15-minute samples.
- `/api/strategy/compare`: Static vs conservative adaptive backtest on the same public OHLC candles (1/3/7/14/30 days, capped to 1000 candles), plus buy-and-hold. No spread/slippage; not a live-execution guarantee.
- Telegram alerts: once per condition per Kyiv day for out-of-range, all-SELL, and 24h inactivity, if Telegram is enabled. Alerts require working Telegram credentials.
- Updated `index.html` with analytics, range panel, strategy comparison.

## Install on Raspberry Pi

1. `cd ~/ayla_grid_bot && sudo systemctl stop ayla-grid`
2. Backup files AND consistent SQLite snapshot:
   ```bash
   cp main.py main_v082_backup.py
   cp index.html index_v081_backup.html
   .venv/bin/python - <<'PY'
   import sqlite3
   src=sqlite3.connect('ayla_v04.db')
   dst=sqlite3.connect('ayla_v04_before_v09.db')
   src.backup(dst)
   dst.close();src.close()
   PY
   ```
3. Copy new `main.py` and `index.html` into `/home/pi/ayla_grid_bot` (do not overwrite `.env` or databases).
4. Edit `.env`: **set** `BYBIT_ENV=mainnet` (replace existing line, do not append a duplicate). Optional `AYLA_AUTO_RECENTER=1` or `0`. If systemd provides `BYBIT_ENV` via `Environment=`, it overrides `.env`; inspect `systemctl cat ayla-grid`.
5. `.venv/bin/python -m py_compile main.py && sudo systemctl start ayla-grid`
6. Check `curl -s http://127.0.0.1:8090/api/status | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["network"],d["mode"],d["running"],d["usdt"],d["sol"],d["error"])'`
7. Expect `mainnet PAPER False 100.0 0.0 None` (the quote might take several seconds). Press Start Paper in dashboard only after verifying network and paper mode.

## Important
- This release was syntax-checked and the BUY→SELL cycle was tested locally on an isolated SQLite DB; it has **not** been tested against your live Raspberry Pi or real Bybit mainnet feed.
- `Reset Paper` deletes the current network's paper history. Avoid it.
- Switching back to testnet by changing `.env` and restarting reopens the original testnet DB and its old paper state. Pause it before switching if needed.
- The service binds 0.0.0.0 by default; do not expose port 8090 to the public internet without authentication.
- Comparison is intentionally conservative and adaptive may have no advantage or may not re-center at all while SOL is held.
