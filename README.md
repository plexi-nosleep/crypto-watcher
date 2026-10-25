# SolWatcher Mini App

A Telegram Mini App UI and FastAPI backend for the existing SolWatcher alert engine. The original `bot.py` remains available as a fallback while you migrate.

## Features carried over
- USD market prices and 24-hour change for BTC, ETH, SOL, BNB, XRP, ADA, DOGE, AVAX, LINK, and DOT.
- One-shot price alerts above or below a target.
- Repeating ± movement alerts within 1–900 minutes, with the existing 15-minute cooldown.
- Repeating 1-minute quote-volume alerts against the previous 20 completed candles, with the existing cooldown.
- Pause, resume, delete, and recent trigger history.
- Telegram chat notifications when an alert triggers.

## Run locally
1. Use Python 3.10+ and install dependencies: `python -m pip install -r requirements.txt`.
2. Keep your existing `.env` and set `TELEGRAM_BOT_TOKEN`. Set `DB_PATH=data/alerts.db` (the default) to reuse the existing database.
3. From the repository root run: `uvicorn backend.main:app --host 0.0.0.0 --port 8000`.
4. Open `http://localhost:8000` to check that the interface is served. Authenticated API calls require a valid Telegram Mini App session.
5. Deploy behind HTTPS, then configure the bot's Menu Button / Main App URL in BotFather to point to that public URL.

Telegram verifies the signed `initData` on every API request. Do not put the bot token in the frontend. In production run a single backend worker so alert polling is not duplicated.

## Structure
- `backend/main.py` — FastAPI routes, Telegram Web App authentication, SQLite access, market data, background alert engine.
- `frontend/` — responsive dark Telegram Mini App.
- `bot.py` — original bot retained for rollback.
- `data/alerts.db` — SQLite alerts and history (kept out of version control).
