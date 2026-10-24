
import logging
import os
import sqlite3
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "alerts.db"

load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
OWNER_RAW = os.getenv("TELEGRAM_USER_ID", "").strip()
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

COINS = {"ETH": "ethereum", "SOL": "solana"}
API_URL = "https://api.coingecko.com/api/v3/simple/price"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ---------- Database ----------

def connect_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                coin TEXT NOT NULL,
                target REAL NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(alerts)")
        }
        if "direction" not in columns:
            db.execute("""
                ALTER TABLE alerts ADD COLUMN
                direction TEXT NOT NULL DEFAULT 'below'
            """)


def add_alert(chat_id, coin, target, direction):
    with connect_db() as db:
        cur = db.execute(
            "INSERT INTO alerts (chat_id, coin, target, direction) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, coin, target, direction),
        )
        return cur.lastrowid


def list_alerts(chat_id):
    with connect_db() as db:
        return db.execute(
            "SELECT * FROM alerts WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()


def all_alerts():
    with connect_db() as db:
        return db.execute("SELECT * FROM alerts ORDER BY id").fetchall()


def delete_alert(alert_id, chat_id):
    with connect_db() as db:
        cur = db.execute(
            "DELETE FROM alerts WHERE id=? AND chat_id=?",
            (alert_id, chat_id),
        )
        return cur.rowcount > 0


# ---------- Security ----------

def owner_id():
    if not OWNER_RAW:
        return None
    try:
        return int(OWNER_RAW)
    except ValueError as exc:
        raise RuntimeError(
            "TELEGRAM_USER_ID must be a numeric Telegram ID."
        ) from exc


async def authorized(update: Update):
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat or chat.type != "private":
        return False

    allowed = owner_id()

    # Setup mode: configure your ID using /myid, then restart.
    if allowed is None:
        return True

    if user.id != allowed:
        if update.effective_message:
            await update.effective_message.reply_text(
                "This is a private bot."
            )
        return False

    return True


# ---------- Prices ----------

async def fetch_prices():
    params = {
        "ids": "ethereum,solana",
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(API_URL, params=params)
        response.raise_for_status()
        data = response.json()

    result = {}
    for symbol, coin_id in COINS.items():
        item = data[coin_id]
        result[symbol] = {
            "price": float(item["usd"]),
            "change": item.get("usd_24h_change"),
        }
    return result


def money(value):
    return f"${value:,.2f}" if value >= 100 else f"${value:,.4f}"


def menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Prices", callback_data="prices"),
            InlineKeyboardButton("🔔 Set alert", callback_data="new"),
        ],
        [
            InlineKeyboardButton("📋 My alerts", callback_data="alerts"),
            InlineKeyboardButton("ℹ️ Status", callback_data="status"),
        ],
    ])


def coin_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Ethereum (ETH)", callback_data="coin:ETH"),
            InlineKeyboardButton("Solana (SOL)", callback_data="coin:SOL"),
        ],
        [InlineKeyboardButton("⬅️ Menu", callback_data="home")],
    ])


def direction_menu(coin):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📉 Alert when price falls",
            callback_data=f"dir:{coin}:below",
        )],
        [InlineKeyboardButton(
            "📈 Alert when price rises",
            callback_data=f"dir:{coin}:above",
        )],
        [InlineKeyboardButton("⬅️ Back", callback_data="new")],
    ])


# ---------- Commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    context.user_data.pop("pending", None)
    await update.effective_message.reply_text(
        "📊 Welcome to SolWatcher!\n\n"
        "Track ETH/SOL and create price alerts using buttons.\n"
        "This bot never buys or sells crypto.",
        reply_markup=menu(),
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        await update.effective_message.reply_text(
            f"Your Telegram user ID: {update.effective_user.id}\n"
            "Put it in .env as TELEGRAM_USER_ID=YOUR_ID, "
            "then restart the bot."
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    await update.effective_message.reply_text(
        "/price — current prices\n"
        "/set ETH 2200 — alert at/below target\n"
        "/setabove ETH 2600 — alert at/above target\n"
        "/alerts — list alerts\n"
        "/remove 1 — delete alert #1\n"
        "/menu — open buttons\n"
        "/cancel — cancel alert setup",
        reply_markup=menu(),
    )


async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await authorized(update):
        return
    try:
        prices = await fetch_prices()
        lines = ["💰 Current prices (USD):"]
        for coin, info in prices.items():
            change = info["change"]
            change_text = (
                f"{change:+.2f}% in 24h"
                if isinstance(change, (int, float))
                else "24h change unavailable"
            )
            lines.append(
                f"{coin}: {money(info['price'])} ({change_text})"
            )
        await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=menu()
        )
    except Exception:
        log.exception("Price request failed")
        await update.effective_message.reply_text(
            "Couldn't fetch prices right now. Try again later."
        )


async def create_from_command(update, context, direction):
    if not await authorized(update):
        return
    if len(context.args) != 2:
        example = (
            "/set ETH 2200" if direction == "below"
            else "/setabove ETH 2600"
        )
        await update.effective_message.reply_text(f"Usage: {example}")
        return

    coin = context.args[0].upper()
    if coin not in COINS:
        await update.effective_message.reply_text("Use ETH or SOL.")
        return

    try:
        target = float(context.args[1])
        if not 0 < target < 10_000_000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid positive price."
        )
        return

    alert_id = add_alert(
        update.effective_chat.id, coin, target, direction
    )
    condition = "≤" if direction == "below" else "≥"
    await update.effective_message.reply_text(
        f"Alert #{alert_id} created: {coin} {condition} {money(target)}",
        reply_markup=menu(),
    )


async def set_below(update, context):
    await create_from_command(update, context, "below")


async def set_above(update, context):
    await create_from_command(update, context, "above")


async def alerts_command(update, context):
    if not await authorized(update):
        return
    rows = list_alerts(update.effective_chat.id)
    if not rows:
        await update.effective_message.reply_text(
            "No active alerts.", reply_markup=menu()
        )
        return

    lines = ["📋 Your alerts:"]
    for row in rows:
        op = "≤" if row["direction"] == "below" else "≥"
        lines.append(
            f"#{row['id']} — {row['coin']} {op} {money(row['target'])}"
        )
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=menu()
    )


async def remove_command(update, context):
    if not await authorized(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /remove 1")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid alert ID.")
        return

    deleted = delete_alert(alert_id, update.effective_chat.id)
    await update.effective_message.reply_text(
        "Alert removed." if deleted else "Alert not found.",
        reply_markup=menu(),
    )


async def cancel(update, context):
    if not await authorized(update):
        return
    context.user_data.pop("pending", None)
    await update.effective_message.reply_text(
        "Alert setup cancelled.", reply_markup=menu()
    )


# ---------- Button menu ----------

async def button_handler(update, context):
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    chat = update.effective_chat
    allowed = owner_id()

    if (
        not user or not chat or chat.type != "private"
        or (allowed is not None and user.id != allowed)
    ):
        await query.edit_message_text("This is a private bot.")
        return

    data = query.data or ""

    if data in ("home", "menu"):
        context.user_data.pop("pending", None)
        await query.edit_message_text(
            "📊 SolWatcher — choose an option:", reply_markup=menu()
        )

    elif data == "status":
        await query.edit_message_text(
            f"🟢 Running while this program is open.\n"
            f"Check interval: {INTERVAL} seconds\n"
            "Trading: disabled.",
            reply_markup=menu(),
        )

    elif data == "prices":
        try:
            prices = await fetch_prices()
            lines = ["💰 Current prices (USD):"]
            for coin, info in prices.items():
                change = info["change"]
                change_text = (
                    f"{change:+.2f}% in 24h"
                    if isinstance(change, (int, float))
                    else "change unavailable"
                )
                lines.append(
                    f"{coin}: {money(info['price'])} ({change_text})"
                )
            await query.edit_message_text(
                "\n".join(lines), reply_markup=menu()
            )
        except Exception:
            log.exception("Price request failed")
            await query.edit_message_text(
                "Couldn't fetch prices right now.", reply_markup=menu()
            )

    elif data == "new":
        context.user_data.pop("pending", None)
        await query.edit_message_text(
            "Choose a coin:", reply_markup=coin_menu()
        )

    elif data.startswith("coin:"):
        coin = data.split(":", 1)[1]
        if coin not in COINS:
            return
        context.user_data["pending"] = {"coin": coin}
        await query.edit_message_text(
            f"Choose an alert direction for {coin}:",
            reply_markup=direction_menu(coin),
        )

    elif data.startswith("dir:"):
        parts = data.split(":")
        if len(parts) != 3:
            return
        _, coin, direction = parts
        if coin not in COINS or direction not in ("below", "above"):
            return
        context.user_data["pending"] = {
            "coin": coin, "direction": direction
        }
        condition = (
            "falls to or below" if direction == "below"
            else "rises to or above"
        )
        await query.edit_message_text(
            f"Enter the USD target for {coin}. I'll alert when it "
            f"{condition} your target.\n\n"
            "Send only the number, e.g. 2200. Use /cancel to stop."
        )

    elif data == "alerts":
        rows = list_alerts(chat.id)
        if not rows:
            await query.edit_message_text(
                "No active alerts.", reply_markup=menu()
            )
            return

        lines = ["📋 Your active alerts:"]
        buttons = []
        for row in rows:
            op = "≤" if row["direction"] == "below" else "≥"
            lines.append(
                f"#{row['id']} — {row['coin']} {op} {money(row['target'])}"
            )
            buttons.append([InlineKeyboardButton(
                f"🗑 Remove #{row['id']}",
                callback_data=f"remove:{row['id']}",
            )])
        buttons.append([InlineKeyboardButton(
            "⬅️ Menu", callback_data="home"
        )])
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("remove:"):
        try:
            alert_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        deleted = delete_alert(alert_id, chat.id)
        await query.edit_message_text(
            f"Alert #{alert_id} removed." if deleted else "Alert not found.",
            reply_markup=menu(),
        )


async def target_input(update, context):
    if not await authorized(update):
        return
    pending = context.user_data.get("pending")
    if not pending:
        return

    try:
        target = float((update.effective_message.text or "").strip())
        if not 0 < target < 10_000_000:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Enter a valid positive price, e.g. 2200."
        )
        return

    coin = pending["coin"]
    direction = pending["direction"]
    alert_id = add_alert(
        update.effective_chat.id, coin, target, direction
    )
    context.user_data.pop("pending", None)
    condition = "≤" if direction == "below" else "≥"
    await update.effective_message.reply_text(
        f"✅ Alert #{alert_id} created: {coin} {condition} {money(target)}",
        reply_markup=menu(),
    )


# ---------- Scheduled alerts ----------

async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    try:
        prices = await fetch_prices()
    except Exception:
        log.warning("Market data unavailable; will retry.")
        return

    for alert in all_alerts():
        current = prices[alert["coin"]]["price"]
        target = alert["target"]
        direction = alert["direction"]

        triggered = (
            current <= target if direction == "below"
            else current >= target
        )
        if not triggered:
            continue

        condition = "at or below" if direction == "below" else "at or above"
        message = (
            f"🔔 Price alert #{alert['id']}!\n"
            f"{alert['coin']} is now {money(current)}.\n"
            f"Your target was {condition} {money(target)}.\n"
            "Notification only — not a buy/sell recommendation."
        )
        try:
            await context.bot.send_message(
                chat_id=alert["chat_id"], text=message
            )
            delete_alert(alert["id"], alert["chat_id"])
        except Exception:
            log.exception("Could not deliver alert #%s", alert["id"])


async def post_init(application: Application):
    application.job_queue.run_repeating(
        check_alerts,
        interval=INTERVAL,
        first=5,
        name="price-alert-checker",
    )


# ---------- Run bot ----------

def main():
    if not TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN. Add it to your .env file."
        )
    if INTERVAL < 60:
        raise RuntimeError("CHECK_INTERVAL_SECONDS must be at least 60.")
    owner_id()  # Validate owner ID if configured.
    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("price", prices_command))
    app.add_handler(CommandHandler("set", set_below))
    app.add_handler(CommandHandler("setabove", set_above))
    app.add_handler(CommandHandler("alerts", alerts_command))
    app.add_handler(CommandHandler("remove", remove_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, target_input)
    )

    log.info("Starting SolWatcher.")
    app.run_polling()


if __name__ == "__main__":
    main()
