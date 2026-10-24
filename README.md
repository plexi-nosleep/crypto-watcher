# crypto-watcher
A personal Telegram bot for ETH and SOL price alerts.
# 🚀 Crypto Watcher — Telegram Bot

A lightweight Python Telegram bot for monitoring **Ethereum (ETH)** and **Solana (SOL)** prices in real time using public market data.

Set custom price alerts and receive Telegram notifications when your target price is reached. No exchange account or trading API keys required.

## ✨ Features

- 📊 **Live prices** — Check ETH and SOL prices in USD.
- 🔔 **Custom price alerts** — Get notified when a cryptocurrency reaches your target.
- 📈 **Above and below alerts** — Choose whether to trigger above or below a specific price.
- 🤖 **Telegram interface** — Use commands or interactive buttons.
- 💾 **Persistent storage** — Save alerts in an SQLite database.
- 🔄 **Automatic monitoring** — Check prices periodically.
- 🔐 **User access control** — Restrict access to your Telegram user ID.
- 🆓 **Free market data** — Uses the public CoinGecko API.

## 🛠️ Built With

- Python
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- [CoinGecko API](https://www.coingecko.com/en/api)
- SQLite
- HTTPX
- python-dotenv

## 📋 Requirements

- Python 3.10 or newer
- A Telegram account
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An internet connection

## ⚙️ Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR-USERNAME/crypto-watcher.git
cd crypto-watcher
```

Replace `YOUR-USERNAME` with your GitHub username.

### 2. Install dependencies

```bash
python -m venv .venv
```

Activate the environment:

**Windows:**
```bash
.venv\Scripts\activate
```

**Linux / macOS:**
```bash
source .venv/bin/activate
```

Install the required packages:

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy `.env.example` to a new file named `.env` and fill in your own values:

```dotenv
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_USER_ID=your_telegram_user_id
CHECK_INTERVAL_SECONDS=60
```

**Keep your `.env` file private. Never commit your real bot token to GitHub.**

### 4. Start the bot

```bash
python bot.py
```

Open your bot in Telegram and send `/start`.

## 💬 Commands

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/menu` | Open the interactive menu |
| `/help` | Show available commands |
| `/myid` | Display your Telegram user ID |
| `/price` | Check current ETH and SOL prices |
| `/set ETH 2500` | Set an ETH price alert |
| `/set SOL 100` | Set a SOL price alert |
| `/setabove ETH 2500` | Alert when ETH reaches or exceeds $2,500 |
| `/alerts` | View your active alerts |
| `/remove 1` | Remove alert number 1 |
| `/cancel` | Cancel the current interaction |

## 🔔 How Alerts Work

1. Choose a cryptocurrency.
2. Set your target price.
3. Choose whether the alert should trigger above or below the target.
4. The bot checks prices periodically.
5. When the target is reached, the bot sends a Telegram notification.

Alerts are one-shot: after an alert triggers, it is removed.

## 📁 Project Structure

```text
crypto-watcher/
├── bot.py
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── data/
    └── alerts.db
```

The `data/` directory and database are generated locally and should not be committed to the public repository.

## ⚠️ Disclaimer

This project is for educational and informational purposes only. It provides price monitoring and notifications, not financial advice. Cryptocurrency prices can change rapidly.

This bot does **not** execute trades or require exchange trading API keys. Market-data availability depends on the external provider and its rate limits.

## 📜 License

This project is available under the MIT License. See the `LICENSE` file for details.

---

Made with ❤️ and Python.
