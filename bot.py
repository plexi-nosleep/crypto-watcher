import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
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


# -------------------- Button-first mobile interface --------------------

PAGE_SIZE = 5
HISTORY_PAGE_SIZE = 5


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Market prices", callback_data="home:prices"),
         InlineKeyboardButton("🔔 Create alert", callback_data="home:new")],
        [InlineKeyboardButton("📋 My alerts", callback_data="home:alerts"),
         InlineKeyboardButton("🕘 History", callback_data="home:history")],
        [InlineKeyboardButton("ℹ️ How it works", callback_data="home:help")],
    ])


async def edit_screen(query, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=markup)
    except Exception as exc:
        if "Message is not modified" not in str(exc):
            logger.warning("Could not update bot menu: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    context.user_data.pop("alert_flow", None)
    await update.effective_message.reply_text(
        "SOLWATCHER\nYour crypto alerts, all in one place.\n\nChoose an option below.",
        reply_markup=home_keyboard(),
    )


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Price target", callback_data="new:price")],
        [InlineKeyboardButton("📈 Price movement", callback_data="new:change")],
        [InlineKeyboardButton("📊 Volume spike", callback_data="new:volume")],
        [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")],
    ])


def coin_keyboard() -> InlineKeyboardMarkup:
    coins = list(COINS)
    rows = [[InlineKeyboardButton(c, callback_data=f"coin:{c}") for c in coins[i:i + 2]]
            for i in range(0, len(coins), 2)]
    rows += [[InlineKeyboardButton("⬅️ Alert types", callback_data="home:new")],
             [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")]]
    return InlineKeyboardMarkup(rows)


def preset_keyboard(kind: str) -> InlineKeyboardMarkup:
    values = ["0.5", "1", "2", "5", "10"] if kind == "change" else ["1.5", "2", "3", "5"]
    unit = "%" if kind == "change" else "×"
    rows, row = [], []
    for value in values:
        row.append(InlineKeyboardButton(f"{value}{unit}", callback_data=f"threshold:{kind}:{value}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows += [[InlineKeyboardButton("⌨️ Custom value", callback_data=f"custom:{kind}")],
             [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")]]
    return InlineKeyboardMarkup(rows)


def window_keyboard() -> InlineKeyboardMarkup:
    values = [("1 min", 1), ("5 min", 5), ("15 min", 15), ("30 min", 30),
              ("1 hour", 60), ("4 hours", 240), ("15 hours", 900)]
    rows, row = [], []
    for label, minutes in values:
        row.append(InlineKeyboardButton(label, callback_data=f"window:{minutes}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows += [[InlineKeyboardButton("⌨️ Custom minutes", callback_data="custom:window")],
             [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")]]
    return InlineKeyboardMarkup(rows)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")]])


async def prompt_number(query, context: ContextTypes.DEFAULT_TYPE, step: str, text: str) -> None:
    flow = context.user_data["alert_flow"]
    flow.update(step=step, chat_id=query.message.chat_id, message_id=query.message.message_id)
    await edit_screen(query, text, cancel_keyboard())


async def finish_alert(context: ContextTypes.DEFAULT_TYPE, flow: dict) -> None:
    kind, coin = flow["kind"], flow["coin"]
    chat_id = int(flow["chat_id"])
    if kind == "price":
        target, direction = float(flow["target"]), flow["direction"]
        alert_id = add_alert(chat_id, coin, "price", target=target, direction=direction)
        details = f"{coin} {direction} {fmt_price(target)}"
    elif kind == "change":
        threshold, window = float(flow["threshold"]), int(flow["window_minutes"])
        alert_id = add_alert(chat_id, coin, "change", direction="either",
                             threshold=threshold, window_minutes=window)
        details = f"{coin} moves ±{threshold:g}% within {window} minutes"
    else:
        threshold = float(flow["threshold"])
        alert_id = add_alert(chat_id, coin, "volume", direction="above", threshold=threshold)
        details = f"{coin} 1-minute volume ≥ {threshold:g}× recent average"

    context.user_data.pop("alert_flow", None)
    text = f"✅ ALERT CREATED\n\n#{alert_id} · {details}\n\nWe’ll message you when it triggers."
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Create another", callback_data="home:new")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ])
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=int(flow["message_id"]),
            text=text, reply_markup=markup,
        )
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=home_keyboard())


async def show_prices(query, force: bool = False) -> None:
    try:
        prices = await fetch_prices(force=force)
        lines = ["MARKET PRICES · USD", ""]
        for coin in COINS:
            data = prices.get(coin)
            if data:
                change = float(data.get("change24h", 0))
                lines.append(f"{coin:<5} {fmt_price(float(data['usd']))}   {'▲' if change >= 0 else '▼'} {change:+.2f}%")
        text = "\n".join(lines)
    except Exception:
        logger.exception("Price screen failed")
        text = "Prices are temporarily unavailable. Please try again shortly."
    await edit_screen(query, text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="home:refresh")],
        [InlineKeyboardButton("🏠 Home", callback_data="nav:home")],
    ]))


def alerts_keyboard(rows: list[sqlite3.Row], page: int, total: int) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        aid = int(row["id"])
        buttons.append([
            InlineKeyboardButton("⏸ Pause" if row["enabled"] else "▶️ Resume",
                                 callback_data=f"toggle:{aid}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"delete:{aid}"),
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Newer", callback_data=f"alerts:{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Older →", callback_data=f"alerts:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons += [[InlineKeyboardButton("🔔 Create alert", callback_data="home:new")],
                [InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]
    return InlineKeyboardMarkup(buttons)


async def show_alerts(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, page: int = 0) -> None:
    all_rows = get_alerts(chat_id)
    total = len(all_rows)
    page = min(max(page, 0), max(0, (total - 1) // PAGE_SIZE))
    rows = all_rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if rows:
        lines = [f"MY ALERTS · {total} total", ""]
        for row in rows:
            lines.extend([alert_description(row), ""])
        text = "\n".join(lines).rstrip()
    else:
        text = "MY ALERTS\n\nNo alerts yet. Create one to start watching the market."
    context.user_data["alerts_page"] = page
    await edit_screen(query, text, alerts_keyboard(rows, page, total))


def history_page(chat_id: int, page: int) -> tuple[list[sqlite3.Row], int]:
    with db_connect() as conn:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM alert_history WHERE chat_id=?", (chat_id,)
        ).fetchone()[0])
        rows = conn.execute(
            "SELECT * FROM alert_history WHERE chat_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (chat_id, HISTORY_PAGE_SIZE, page * HISTORY_PAGE_SIZE),
        ).fetchall()
    return rows, total


async def show_history(query, chat_id: int, page: int = 0) -> None:
    rows, total = history_page(chat_id, page)
    if rows:
        lines = [f"ALERT HISTORY · {total} total", ""]
        for row in rows:
            stamp = str(row["triggered_at"])[:16].replace("T", " ")
            lines.extend([
                f"#{row['alert_id']} · {row['coin']} · {row['alert_type']}",
                str(row["message"]),
                f"{stamp} UTC",
                "",
            ])
        text = "\n".join(lines).rstrip()
    else:
        text = "ALERT HISTORY\n\nNo triggered alerts yet."
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Newer", callback_data=f"history:{page - 1}"))
    if (page + 1) * HISTORY_PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("Older →", callback_data=f"history:{page + 1}"))
    rows_markup = [nav] if nav else []
    rows_markup.append([InlineKeyboardButton("🏠 Home", callback_data="nav:home")])
    await edit_screen(query, text, InlineKeyboardMarkup(rows_markup))


async def text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update):
        return
    flow = context.user_data.get("alert_flow")
    if not flow:
        await update.effective_message.reply_text(
            "Tap a menu button to continue.", reply_markup=home_keyboard()
        )
        return
    try:
        raw = (update.effective_message.text or "").replace(",", "").strip()
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Please send a number greater than zero, or tap Cancel."
        )
        return

    step = flow.get("step")
    if step == "price_target":
        flow["target"] = value
        await finish_alert(context, flow)
    elif step == "change_threshold":
        flow["threshold"] = value
        flow["step"] = "change_window"
        await context.bot.edit_message_text(
            chat_id=int(flow["chat_id"]), message_id=int(flow["message_id"]),
            text="Choose the movement time window.", reply_markup=window_keyboard(),
        )
    elif step == "volume_threshold":
        flow["threshold"] = value
        await finish_alert(context, flow)
    elif step == "change_window":
        if not value.is_integer() or not 1 <= int(value) <= MAX_CHANGE_MINUTES:
            await update.effective_message.reply_text(
                f"Enter a whole number between 1 and {MAX_CHANGE_MINUTES}."
            )
            return
        flow["window_minutes"] = int(value)
        await finish_alert(context, flow)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await require_allowed(update):
        return
    data = query.data or ""
    chat_id = update.effective_chat.id

    if data == "nav:home":
        context.user_data.pop("alert_flow", None)
        await edit_screen(query, "SOLWATCHER\n\nChoose an option below.", home_keyboard())
    elif data == "home:help":
        await edit_screen(query,
            "HOW IT WORKS\n\nPrice target: one notification when a coin goes above or below your target.\n\n"
            "Price movement: repeating alert when the coin moves by your chosen percentage in a time window.\n\n"
            "Volume spike: repeating alert when 1-minute volume is above its recent average.\n\n"
            "Pause, resume, or remove alerts from My alerts. Repeat alerts have a 15-minute cooldown.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Home", callback_data="nav:home")]]))
    elif data == "home:new":
        context.user_data.pop("alert_flow", None)
        await edit_screen(query, "CREATE AN ALERT\n\nChoose an alert type.", type_keyboard())
    elif data in ("home:prices", "home:refresh"):
        await show_prices(query, force=data == "home:refresh")
    elif data == "home:alerts":
        await show_alerts(query, context, chat_id)
    elif data.startswith("alerts:"):
        await show_alerts(query, context, chat_id, int(data.split(":")[1]))
    elif data == "home:history":
        await show_history(query, chat_id)
    elif data.startswith("history:"):
        await show_history(query, chat_id, int(data.split(":")[1]))
    elif data.startswith("new:"):
        kind = data.split(":")[1]
        context.user_data["alert_flow"] = {"kind": kind}
        await edit_screen(query, "Choose a coin.", coin_keyboard())
    elif data.startswith("coin:"):
        flow = context.user_data.get("alert_flow")
        if not flow:
            await edit_screen(query, "Start by choosing an alert type.", type_keyboard())
            return
        coin = data.split(":")[1]
        flow["coin"] = coin
        context.user_data["alert_flow"] = flow
        if flow["kind"] == "price":
            await edit_screen(query, f"{coin} price alert\n\nTrigger when price goes:", InlineKeyboardMarkup([
                [InlineKeyboardButton("▲ Above", callback_data="direction:above"),
                 InlineKeyboardButton("▼ Below", callback_data="direction:below")],
                [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")],
            ]))
        else:
            await edit_screen(query,
                "Choose a movement threshold." if flow["kind"] == "change" else "Choose a volume multiple.",
                preset_keyboard(flow["kind"]))
    elif data.startswith("direction:"):
        flow = context.user_data.get("alert_flow")
        if not flow:
            await edit_screen(query, "Setup expired. Start again.", type_keyboard())
            return
        flow["direction"] = data.split(":")[1]
        context.user_data["alert_flow"] = flow
        try:
            price = float((await fetch_prices())[flow["coin"]]["usd"])
            sign = 1 if flow["direction"] == "above" else -1
            rows = []
            for pct in (1, 2, 5, 10):
                target = price * (1 + sign * pct / 100)
                label = "+" if sign > 0 else "−"
                rows.append(InlineKeyboardButton(
                    f"{label}{pct}% · {fmt_price(target)}", callback_data=f"pricepct:{pct}"
                ))
            await edit_screen(query, f"{flow['coin']} is {fmt_price(price)} now.\n\nTap a quick target or enter an exact price.", InlineKeyboardMarkup([
                rows[:2], rows[2:],
                [InlineKeyboardButton("⌨️ Exact price", callback_data="custom:price")],
                [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")],
            ]))
        except Exception:
            logger.exception("Could not load current price")
            await edit_screen(query, "Current price unavailable. Enter an exact target instead.", InlineKeyboardMarkup([
                [InlineKeyboardButton("⌨️ Exact price", callback_data="custom:price")],
                [InlineKeyboardButton("🏠 Cancel", callback_data="nav:home")],
            ]))
    elif data.startswith("pricepct:"):
        flow = context.user_data.get("alert_flow")
        if not flow:
            await edit_screen(query, "Setup expired. Start again.", type_keyboard())
            return
        price = float((await fetch_prices())[flow["coin"]]["usd"])
        pct = float(data.split(":")[1])
        sign = 1 if flow["direction"] == "above" else -1
        flow.update(target=price * (1 + sign * pct / 100),
                    chat_id=chat_id, message_id=query.message.message_id)
        await finish_alert(context, flow)
    elif data.startswith("threshold:"):
        _, kind, raw = data.split(":")
        flow = context.user_data.get("alert_flow")
        if not flow or flow.get("kind") != kind:
            await edit_screen(query, "Setup expired. Start again.", type_keyboard())
            return
        flow["threshold"] = float(raw)
        if kind == "change":
            context.user_data["alert_flow"] = flow
            await edit_screen(query, "Choose the movement time window.", window_keyboard())
        else:
            flow.update(chat_id=chat_id, message_id=query.message.message_id)
            await finish_alert(context, flow)
    elif data.startswith("window:"):
        flow = context.user_data.get("alert_flow")
        if not flow:
            await edit_screen(query, "Setup expired. Start again.", type_keyboard())
            return
        flow.update(window_minutes=int(data.split(":")[1]),
                    chat_id=chat_id, message_id=query.message.message_id)
        await finish_alert(context, flow)
    elif data.startswith("custom:"):
        kind = data.split(":")[1]
        flow = context.user_data.get("alert_flow")
        if not flow:
            await edit_screen(query, "Setup expired. Start again.", type_keyboard())
            return
        prompts = {
            "price": ("price_target", f"Send the exact {flow['coin']} price in USD. Example: 150"),
            "change": ("change_threshold", "Send the movement threshold in percent. Example: 2.5"),
            "volume": ("volume_threshold", "Send the volume multiple. Example: 2"),
            "window": ("change_window", f"Send a whole number of minutes from 1 to {MAX_CHANGE_MINUTES}."),
        }
        step, prompt = prompts[kind]
        await prompt_number(query, context, step, prompt)
    elif data.startswith("toggle:"):
        aid = int(data.split(":")[1])
        row = get_alert(chat_id, aid)
        if row:
            set_alert_enabled(chat_id, aid, not bool(row["enabled"]))
            await show_alerts(query, context, chat_id, int(context.user_data.get("alerts_page", 0)))
    elif data.startswith("delete:"):
        aid = int(data.split(":")[1])
        row = get_alert(chat_id, aid)
        if row:
            await edit_screen(query, f"Remove alert #{aid}?\n\n{alert_description(row)}", InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, remove", callback_data=f"deleteyes:{aid}")],
                [InlineKeyboardButton("Keep alert", callback_data=f"alerts:{context.user_data.get('alerts_page', 0)}")],
            ]))
    elif data.startswith("deleteyes:"):
        delete_alert(chat_id, int(data.split(":")[1]))
        await show_alerts(query, context, chat_id, int(context.user_data.get("alerts_page", 0)))
    else:
        await edit_screen(query, "Choose an option below.", home_keyboard())


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
    await application.bot.set_my_commands([BotCommand("start", "Open SolWatcher menu")])
    try:
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logger.warning("Could not set Telegram menu button", exc_info=True)
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
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
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

