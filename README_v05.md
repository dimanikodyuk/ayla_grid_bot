# Ayla Grid Bot v0.5 — Candles & Bybit key verification

**PAPER ONLY.** Private Bybit endpoint used: `GET /v5/user/query-api` for credential verification. No real orders, no balance withdrawals.

Upgrade on Raspberry Pi:
```bash
cd ~/ayla_grid_bot
sudo systemctl stop ayla-grid
# Copy updated main.py, index.html, .gitignore, .env.example here.
nano .env
# Add AYLA_ADMIN_TOKEN=<long random string>
sudo systemctl start ayla-grid
```

Existing `ayla_v04.db` is preserved. Web chart refreshes every 30 seconds. Separate mainnet/testnet credentials are saved to `.bybit_credentials.json` with owner-only permissions (chmod 600), never displayed back. **This is local plaintext storage, not encryption.** Do not port-forward the dashboard. Use SSH tunnel or VPN for remote access. Prefer read-only keys and IP allowlisting.

Only key management requires admin token; other dashboard actions remain unauthenticated and must stay on a trusted LAN. The web browser sends credentials to the Pi over HTTP on LAN; for stronger security use an SSH tunnel (`ssh -L 8090:127.0.0.1:8090 pi@raspberry4`) and access localhost:8090.

Back up `ayla_v04.db` before deployment.
