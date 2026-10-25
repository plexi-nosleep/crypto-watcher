import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# -------------------- Configuration --------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN").strip()
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/alerts.db")
CHECK_INTERVAL_SECONDS = max(60, int(os.getenv("CHECK_INTERVAL_SECONDS", "60")))

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
BINANCE_KLINES_URL = "https://data-api.binance.vision/api/v3/klines"

# Symbol -> CoinGecko ID and Binance spot symbol.
COINS: dict[str, dict[str, str]] = {
    "BTC": {"coingecko": "bitcoin", "binance": "BTCUSDT"},
    "ETH": {"coingecko": "ethereum", "binance": "ETHUSDT"},
    "SOL": {"coingecko": "solana", "binance": "SOLUSDT"},
    "BNB": {"coingecko": "binancecoin", "binance": "BNBUSDT"},
    "XRP": {"coingecko": "ripple", "binance": "XRPUSDT"},
    "ADA": {"coingecko": "cardano", "binance": "ADAUSDT"},
    "DOGE": {"coingecko": "dogecoin", "binance": "DOGEUSDT"},
    "AVAX": {"coingecko": "avalanche-2", "binance": "AVAXUSDT"},
    "LINK": {"coingecko": "chainlink", "binance": "LINKUSDT"},
    "DOT": {"coingecko": "polkadot", "binance": "DOTUSDT"},
}

PRICE_CACHE_SECONDS = 30
VOLUME_LOOKBACK_CANDLES = 20
MAX_CHANGE_MINUTES = 900
HTTP_TIMEOUT_SECONDS = 15

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("solwatcher")

_price_cache: dict[str, Any] = {"timestamp": 0.0, "data": None}
_http_client: httpx.AsyncClient | None = None


# -------------------- Database --------------------

def db_connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create new tables and safely add columns to an existing alerts table."""
    with db_connect() as conn:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alerts'"
        ).fetchone()

        if not existing:
            conn.execute(
                """
                CREATE TABLE alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    coin TEXT NOT NULL,
                    target REAL,
                    created_at TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'above',
                    alert_type TEXT NOT NULL DEFAULT 'price',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    threshold REAL,
                    window_minutes INTEGER,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 900,
                    last_triggered_at REAL
                )
                """
            )
        else:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(alerts)").fetchall()
            }
            additions = {
                "direction": "TEXT NOT NULL DEFAULT 'above'",
                "alert_type": "TEXT NOT NULL DEFAULT 'price'",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "threshold": "REAL",
                "window_minutes": "INTEGER",
                "cooldown_seconds": "INTEGER NOT NULL DEFAULT 900",
                "last_triggered_at": "REAL",
            }
            for column, definition in additions.items():
                if column not in columns:
                    conn.execute(
                        f"ALTER TABLE alerts ADD COLUMN {column} {definition}"
                    )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER,
                chat_id INTEGER NOT NULL,
                coin TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                message TEXT NOT NULL,
                triggered_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alerts_chat ON alerts(chat_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_chat ON alert_history(chat_id, id DESC)"
        )


def add_alert(
    chat_id: int,
    coin: str,
    alert_type: str,
    target: float | None = None,
    direction: str = "above",
    threshold: float | None = None,
    window_minutes: int | None = None,
    cooldown_seconds: int = 900,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO alerts
                (chat_id, coin, target, created_at, direction, alert_type,
                 enabled, threshold, window_minutes, cooldown_seconds)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                chat_id,
                coin,
                target,
                now,
                direction,
                alert_type,
                threshold,
                window_minutes,
                cooldown_seconds,
            ),
        )
        return int(cur.lastrowid)


def get_alert(chat_id: int, alert_id: int) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM alerts WHERE id=? AND chat_id=?",
            (alert_id, chat_id),
        ).fetchone()


def get_alerts(chat_id: int, enabled_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM alerts WHERE chat_id=?"
    args: list[Any] = [chat_id]
    if enabled_only:
        sql += " AND enabled=1"
    sql += " ORDER BY id"
    with db_connect() as conn:
        return conn.execute(sql, args).fetchall()


def delete_alert(chat_id: int, alert_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE id=? AND chat_id=?",
            (alert_id, chat_id),
        )
        return cur.rowcount > 0


def set_alert_enabled(chat_id: int, alert_id: int, enabled: bool) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "UPDATE alerts SET enabled=? WHERE id=? AND chat_id=?",
            (1 if enabled else 0, alert_id, chat_id),
        )
        return cur.rowcount > 0


def record_history(
    alert_id: int,
    chat_id: int,
    coin: str,
    alert_type: str,
    message: str,
) -> None:
    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO alert_history
                (alert_id, chat_id, coin, alert_type, message, triggered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                alert_id,
                chat_id,
                coin,
                alert_type,
                message,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_history(chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            """
            SELECT * FROM alert_history
            WHERE chat_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()


# -------------------- Authentication / helpers --------------------

def user_allowed(update: Update) -> bool:
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    if TELEGRAM_USER_ID:
        return bool(
            update.effective_user
            and str(update.effective_user.id) == TELEGRAM_USER_ID
        )
    return True


async def require_allowed(update: Update) -> bool:
    if user_allowed(update):
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            "This bot is available only in an authorized private chat."
        )
    return False


def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:,.8f}".rstrip("0").rstrip(".")


def parse_coin(value: str) -> str | None:
    coin = value.upper().strip()
    return coin if coin in COINS else None


def alert_description(alert: sqlite3.Row) -> str:
    coin = alert["coin"]
    enabled = "🟢" if alert["enabled"] else "⏸"
    typ = alert["alert_type"] or "price"
    if typ == "price":
        direction = "above" if alert["direction"] == "above" else "below"
        desc = f"{coin} {direction} {fmt_price(float(alert['target']))}"
    elif typ == "change":
        sign = "+" if alert["direction"] == "up" else "-"
        desc = (
            f"{coin} moves {sign}{float(alert['threshold']):g}% "
            f"within {int(alert['window_minutes'])} min"
        )
    elif typ == "volume":
        desc = f"{coin} 1m volume spike ≥ {float(alert['threshold']):g}× average"
    else:
        desc = f"{coin} {typ} alert"
    return f"{enabled} #{alert['id']} — {desc}"


# -------------------- Market data with shared cache --------------------

async def fetch_prices(force: bool = False) -> dict[str, dict[str, float]]:
    now = time.monotonic()
    cached = _price_cache["data"]
    if (
        not force
        and cached is not None
        and now - float(_price_cache["timestamp"]) < PRICE_CACHE_SECONDS
    ):
        return cached

    if _http_client is None:
        raise RuntimeError("HTTP client is not initialized")

    ids = ",".join(item["coingecko"] for item in COINS.values())
    response = await _http_client.get(
        COINGECKO_URL,
        params={"ids": ids, "vs_currencies": "usd", "include_24hr_change": "true"},
    )
    response.raise_for_status()
    raw = response.json()
    prices: dict[str, dict[str, float]] = {}
    for symbol, item in COINS.items():
        entry = raw.get(item["coingecko"])
        if not entry or "usd" not in entry:
            continue
        prices[symbol] = {
            "usd": float(entry["usd"]),
            "change24h": float(entry.get("usd_24h_change", 0.0) or 0.0),
        }

    if not prices:
        raise RuntimeError("Market data provider returned no usable prices")

    _price_cache["timestamp"] = now
    _price_cache["data"] = prices
    return prices


async def fetch_klines(coin: str, interval: str, limit: int) -> list[list[Any]]:
    if _http_client is None:
        raise RuntimeError("HTTP client is not initialized")
    response = await _http_client.get(
        BINANCE_KLINES_URL,
        params={
            "symbol": COINS[coin]["binance"],
            "interval": interval,
            "limit": limit,
        },
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError("Unexpected candle data response")
    return data


async def completed_klines(coin: str, interval: str, limit: int) -> list[list[Any]]:
    candles = await fetch_klines(coin, interval, limit)
    now_ms = int(time.time() * 1000)
    # Binance candle indexes: 0=open time, 4=close, 5=base volume,
    # 7=quote volume, 6=close time.
    return [c for c in candles if int(c[6]) < now_ms]


async def movement_percent(coin: str, window_minutes: int) -> float | None:
    # A 1-minute series gives a simple, predictable rolling window.
    candles = await completed_klines(coin, "1m", window_minutes + 1)
    if len(candles) < window_minutes + 1:
        return None
    start = float(candles[-(window_minutes + 1)][4])
    end = float(candles[-1][4])
    if start <= 0:
        return None
    return (end - start) / start * 100.0


async def volume_multiple(coin: str) -> tuple[float, float] | None:
    # Compare the latest completed 1m candle with the prior 20 completed candles.
    candles = await completed_klines(coin, "1m", VOLUME_LOOKBACK_CANDLES + 1)
    if len(candles) < VOLUME_LOOKBACK_CANDLES + 1:
        return None
    latest_quote_volume = float(candles[-1][7])
    prior_volumes = [float(c[7]) for c in candles[-(VOLUME_LOOKBACK_CANDLES + 1):-1]]
    avg = sum(prior_volumes) / len(prior_volumes)
    if avg <= 0:
        return None
    return latest_quote_volume / avg, latest_quote_volume


# -------------------- Commands --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    text = (
        "👋 *SolWatcher*\n\n"
        "Watch crypto prices and create alerts.\n\n"
        "Commands:\n"
        "/price — current prices\n"
        "/set BTC 70000 — alert when price reaches or exceeds target\n"
        "/setabove BTC 70000 — price above target\n"
        "/setbelow BTC 60000 — price below target\n"
        "/setchange BTC 1.5 15 — alert on ±1.5% move in 15 minutes\n"
        "/setvolume BTC 2 — alert when 1m quote volume is 2× its previous-20-candle average\n"
        "/alerts — list alerts\n"
        "/pause 1 and /resume 1 — pause/resume an alert\n"
        "/remove 1 — delete alert\n"
        "/history — recent alert history\n"
        "/menu — inline menu\n"
        "/help — show help\n\n"
        "Market prices: CoinGecko. Movement and volume alerts: Binance spot BTC/USDT-style candles."
    )
    await update.effective_message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    keyboard = [
        [InlineKeyboardButton("💰 Prices", callback_data="prices"),
         InlineKeyboardButton("📋 My alerts", callback_data="alerts")],
        [InlineKeyboardButton("📜 History", callback_data="history"),
         InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "SolWatcher menu:", reply_markup=markup
        )
    else:
        await update.effective_message.reply_text(
            "SolWatcher menu:", reply_markup=markup
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_allowed(update):
        return
    if query.data == "prices":
        try:
            prices = await fetch_prices()
            lines = [
                f"*{coin}*: {fmt_price(data['usd'])} ({data['change24h']:+.2f}% 24h)"
                for coin, data in prices.items()
            ]
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
        except Exception as exc:
            logger.warning("Price fetch failed: %s", exc)
            await query.edit_message_text("Couldn't fetch prices right now. Try again shortly.")
    elif query.data == "alerts":
        rows = get_alerts(update.effective_chat.id)
        text = "\n".join(alert_description(row) for row in rows) or "No alerts yet."
        await query.edit_message_text(text)
    elif query.data == "history":
        rows = get_history(update.effective_chat.id)
        text = "\n".join(
            f"#{row['alert_id']} {row['coin']} — {row['message']} ({row['triggered_at'][:19].replace('T', ' ')} UTC)"
            for row in rows
        ) or "No alert history yet."
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(
            "Use /help to see commands. Use /menu to return to the menu."
        )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    try:
        prices = await fetch_prices()
        lines = [
            f"{coin}: {fmt_price(data['usd'])} ({data['change24h']:+.2f}% 24h)"
            for coin, data in prices.items()
        ]
        await update.effective_message.reply_text("\n".join(lines))
    except Exception as exc:
        logger.warning("Price fetch failed: %s", exc)
        await update.effective_message.reply_text("Couldn't fetch prices right now. Please try again shortly.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user:
        await update.effective_message.reply_text(
            f"Your Telegram user ID: {update.effective_user.id}"
        )


async def create_price_alert(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    direction: str,
) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text(
            f"Usage: /{'setabove' if direction == 'above' else 'setbelow'} BTC 70000"
        )
        return
    coin = parse_coin(context.args[0])
    try:
        target = float(context.args[1])
        if target <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Target must be a positive number.")
        return
    if not coin:
        await update.effective_message.reply_text(f"Unsupported coin. Available: {', '.join(COINS)}")
        return
    alert_id = add_alert(update.effective_chat.id, coin, "price", target=target, direction=direction)
    await update.effective_message.reply_text(
        f"✅ Created price alert #{alert_id}: {coin} {direction} {fmt_price(target)}."
    )


async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_price_alert(update, context, "above")


async def setabove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_price_alert(update, context, "above")


async def setbelow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await create_price_alert(update, context, "below")


async def setchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 3:
        await update.effective_message.reply_text("Usage: /setchange BTC 1.5 15")
        return
    coin = parse_coin(context.args[0])
    try:
        threshold = abs(float(context.args[1]))
        window = int(context.args[2])
        if threshold <= 0 or window < 1 or window > MAX_CHANGE_MINUTES:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            f"Threshold must be > 0 and window must be 1–{MAX_CHANGE_MINUTES} minutes."
        )
        return
    if not coin:
        await update.effective_message.reply_text(f"Unsupported coin. Available: {', '.join(COINS)}")
        return
    alert_id = add_alert(
        update.effective_chat.id,
        coin,
        "change",
        direction="either",
        threshold=threshold,
        window_minutes=window,
    )
    await update.effective_message.reply_text(
        f"✅ Created movement alert #{alert_id}: {coin} moves at least {threshold:g}% in either direction within {window} minutes."
    )


async def setvolume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 2:
        await update.effective_message.reply_text("Usage: /setvolume BTC 2")
        return
    coin = parse_coin(context.args[0])
    try:
        threshold = float(context.args[1])
        if threshold <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Volume multiplier must be greater than 0.")
        return
    if not coin:
        await update.effective_message.reply_text(f"Unsupported coin. Available: {', '.join(COINS)}")
        return
    alert_id = add_alert(
        update.effective_chat.id,
        coin,
        "volume",
        direction="above",
        threshold=threshold,
    )
    await update.effective_message.reply_text(
        f"✅ Created volume alert #{alert_id}: {coin} 1m quote volume ≥ {threshold:g}× its previous 20 completed candles' average."
    )


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    rows = get_alerts(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text("You don't have any alerts.")
        return
    await update.effective_message.reply_text("\n".join(alert_description(row) for row in rows))


async def remove_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /remove 1")
        return
    alert_id = int(context.args[0])
    if delete_alert(update.effective_chat.id, alert_id):
        await update.effective_message.reply_text(f"🗑 Removed alert #{alert_id}.")
    else:
        await update.effective_message.reply_text("Alert not found.")


async def pause_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /pause 1")
        return
    alert_id = int(context.args[0])
    if set_alert_enabled(update.effective_chat.id, alert_id, False):
        await update.effective_message.reply_text(f"⏸ Paused alert #{alert_id}.")
    else:
        await update.effective_message.reply_text("Alert not found.")


async def resume_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /resume 1")
        return
    alert_id = int(context.args[0])
    if set_alert_enabled(update.effective_chat.id, alert_id, True):
        await update.effective_message.reply_text(f"▶️ Resumed alert #{alert_id}.")
    else:
        await update.effective_message.reply_text("Alert not found.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    rows = get_history(update.effective_chat.id, 10)
    if not rows:
        await update.effective_message.reply_text("No alert history yet.")
        return
    lines = [
        f"#{row['alert_id']} {row['coin']} [{row['alert_type']}] — {row['message']} ({row['triggered_at'][:19].replace('T', ' ')} UTC)"
        for row in rows
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await require_allowed(update):
        await update.effective_message.reply_text("Nothing pending to cancel.")


# -------------------- Alert engine --------------------

async def notify_trigger(
    app: Application,
    alert: sqlite3.Row,
    message: str,
    one_shot: bool,
) -> None:
    chat_id = int(alert["chat_id"])
    alert_id = int(alert["id"])
    record_history(
        alert_id,
        chat_id,
        str(alert["coin"]),
        str(alert["alert_type"] or "price"),
        message,
    )
    try:
        await app.bot.send_message(chat_id=chat_id, text=message)
    except Exception:
        logger.exception("Could not send alert notification for alert #%s", alert_id)
    now = time.time()
    with db_connect() as conn:
        if one_shot:
            conn.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
        else:
            conn.execute(
                "UPDATE alerts SET last_triggered_at=? WHERE id=?",
                (now, alert_id),
            )


async def check_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    with db_connect() as conn:
        alerts = conn.execute(
            "SELECT * FROM alerts WHERE enabled=1 ORDER BY id"
        ).fetchall()
    if not alerts:
        return

    try:
        prices = await fetch_prices()
    except Exception as exc:
        logger.warning("Could not fetch CoinGecko prices: %s", exc)
        prices = {}

    # Avoid fetching the same candle set repeatedly for multiple alerts on one coin.
    change_cache: dict[tuple[str, int], float | None] = {}
    volume_cache: dict[str, tuple[float, float] | None] = {}
    for alert in alerts:
        alert_id = int(alert["id"])
        coin = str(alert["coin"])
        alert_type = str(alert["alert_type"] or "price")
        try:
            if alert_type == "price":
                price_data = prices.get(coin)
                if not price_data:
                    continue
                price = price_data["usd"]
                target = float(alert["target"])
                direction = str(alert["direction"] or "above")
                crossed = price >= target if direction == "above" else price <= target
                if crossed:
                    await notify_trigger(
                        app,
                        alert,
                        f"🔔 Price alert: {coin} is {fmt_price(price)} "
                        f"({direction} {fmt_price(target)}).",
                        one_shot=True,
                    )

            elif alert_type == "change":
                window = int(alert["window_minutes"])
                key = (coin, window)
                if key not in change_cache:
                    change_cache[key] = await movement_percent(coin, window)
                movement = change_cache[key]
                if movement is None:
                    continue
                threshold = float(alert["threshold"])
                last_triggered = alert["last_triggered_at"]
                cooldown = int(alert["cooldown_seconds"] or 900)
                cooled = last_triggered is None or (
                    time.time() - float(last_triggered) >= cooldown
                )
                if abs(movement) >= threshold and cooled:
                    await notify_trigger(
                        app,
                        alert,
                        f"📈 Movement alert: {coin} moved {movement:+.2f}% "
                        f"over the last {window} completed minutes "
                        f"(threshold {threshold:g}%).",
                        one_shot=False,
                    )

            elif alert_type == "volume":
                if coin not in volume_cache:
                    volume_cache[coin] = await volume_multiple(coin)
                result = volume_cache[coin]
                if result is None:
                    continue
                multiple, latest_quote_volume = result
                threshold = float(alert["threshold"])
                last_triggered = alert["last_triggered_at"]
                cooldown = int(alert["cooldown_seconds"] or 900)
                cooled = last_triggered is None or (
                    time.time() - float(last_triggered) >= cooldown
                )
                if multiple >= threshold and cooled:
                    await notify_trigger(
                        app,
                        alert,
                        f"📊 Volume spike: {coin} completed 1m candle quote volume "
                        f"was {multiple:.2f}× the previous {VOLUME_LOOKBACK_CANDLES}-candle average "
                        f"(threshold {threshold:g}×; quote volume ≈ ${latest_quote_volume:,.0f}).",
                        one_shot=False,
                    )
        except Exception:
            logger.exception("Failed to evaluate alert #%s", alert_id)


# -------------------- Application lifecycle --------------------

async def post_init(application: Application) -> None:
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "SolWatcher/1.0"},
    )
    init_db()
    logger.info("Database initialized at %s", DB_PATH)


async def post_shutdown(application: Application) -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is missing. Add it to your .env file before starting the bot."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("set", set_alert))
    application.add_handler(CommandHandler("setabove", setabove))
    application.add_handler(CommandHandler("setbelow", setbelow))
    application.add_handler(CommandHandler("setchange", setchange))
    application.add_handler(CommandHandler("setvolume", setvolume))
    application.add_handler(CommandHandler("alerts", list_alerts))
    application.add_handler(CommandHandler("remove", remove_alert))
    application.add_handler(CommandHandler("pause", pause_alert))
    application.add_handler(CommandHandler("resume", resume_alert))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.job_queue.run_repeating(
        check_alerts,
        interval=CHECK_INTERVAL_SECONDS,
        first=10,
        name="check_alerts",
    )

    logger.info("Starting SolWatcher; interval=%ss", CHECK_INTERVAL_SECONDS)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
