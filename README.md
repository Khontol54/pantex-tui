# Pantex TUI Trading Bot

Automated trading bot for Pantex DEX on Canton Network mainnet. Runs a one-direction-per-day swap strategy between Amulet (CC) and USDCx with a full terminal UI.

---

## Requirements

- Python 3.10 or higher
- A registered Pantex account on mainnet (https://pantex.io)
- A VPS or always-on machine (Ubuntu 20.04+ recommended)
- Your Pantex operator key and trading key

---

## Getting Your Keys

1. Log in to https://pantex.io
2. Go to Admin > Keys
3. Copy your **Operator Key** and **Trading Key**

Keep them private and never commit them to version control.

---

## Installation

```bash
git clone https://github.com/Khontol54/pantex-tui.git
cd pantex-tui
python3 -m venv venv
source venv/bin/activate
pip install -e .
pip install rich python-dotenv
```

---

## Configuration

Copy the example env file:

```bash
cp .env.example .env
nano .env
```

Fill in your keys and settings:

```env
# Required — your Pantex account keys
PANTEX_OPERATOR_KEY=your_operator_key_here
PANTEX_TRADING_KEY=your_trading_key_here
PANTEX_BASE_URL=https://api.pantex.io

# Required — set this to a path your user can write to
STATE_FILE=/home/youruser/pantex-tui/state.json

# Optional — swap frequency (random target rolled each day)
SWAPS_PER_DAY_MIN=15
SWAPS_PER_DAY_MAX=22

# Optional — CC day: random amount per swap (CC → USDCx)
CC_SELL_MIN=4
CC_SELL_MAX=15

# Optional — USDCx day: random % of balance per swap (USDCx → CC)
USDC_PCT_MIN=0.03
USDC_PCT_MAX=0.07

# Optional — interval between swaps (in minutes)
SLEEP_MIN_MINUTES=5
SLEEP_MAX_MINUTES=10

# Optional — network fee guard: skip swap if fee exceeds this (in CC)
MAX_NETWORK_FEE_CC=2.0

# Optional — how long to wait before re-quoting when fee is too high (in seconds)
FEE_RETRY_SECONDS=120

# Optional — seconds to wait after swap before reading new balance
BALANCE_SETTLE_SEC=4

# Optional — max slippage per tier
MAX_SLIPPAGE_FREE=0.010
MAX_SLIPPAGE_PAID=0.003
```

> **Note**: All `Optional` settings already have defaults and do not need to be set unless you want to customize them.

---

## Running the Bot

```bash
source venv/bin/activate
python3 bot.py
```

To keep the bot running after you close your terminal, use `screen`:

```bash
screen -S pantex-bot
source venv/bin/activate
python3 bot.py
# Detach: Ctrl+A then D
# Reattach: screen -r pantex-bot
```

---

## Strategy

One direction per day, alternating automatically:

- **CC day**: sells random 4–15 CC per swap (CC → USDCx)
- **USDCx day**: sells random 3–7% of current USDCx balance per swap (USDCx → CC)

Each day the bot rolls a random target of **15–22 swaps**. After completing the daily target, the bot sleeps and resumes the next day at a random time between **06:00–12:00 UTC**.

The direction alternates automatically based on the date — no manual intervention needed.

---

## Guards

Before each swap the bot fetches a quote and checks two guards:

1. **Network fee guard** — if the Canton network fee exceeds `MAX_NETWORK_FEE_CC` (default 2.0 CC), the bot waits `FEE_RETRY_SECONDS` and re-quotes. It keeps waiting until the fee drops below the limit.
2. **Slippage guard** — if slippage exceeds the tier limit the swap is skipped entirely.

| Tier | Swaps | Default max slippage |
|------|-------|----------------------|
| Free | 1–3   | 1.0%                 |
| Paid | 4+    | 0.3%                 |

---

## Settings Reference

| Setting | Default | Description |
|---------|---------|-------------|
| SWAPS_PER_DAY_MIN | 15 | Minimum swaps per day |
| SWAPS_PER_DAY_MAX | 22 | Maximum swaps per day |
| CC_SELL_MIN | 4 | Minimum CC per swap on CC day |
| CC_SELL_MAX | 15 | Maximum CC per swap on CC day |
| USDC_PCT_MIN | 0.03 | Minimum % of USDCx balance per swap on USDCx day |
| USDC_PCT_MAX | 0.07 | Maximum % of USDCx balance per swap on USDCx day |
| SLEEP_MIN_MINUTES | 5 | Minimum wait between swaps |
| SLEEP_MAX_MINUTES | 10 | Maximum wait between swaps |
| MAX_NETWORK_FEE_CC | 2.0 | Skip swap if Canton network fee exceeds this (CC) |
| FEE_RETRY_SECONDS | 120 | Wait time before re-quoting when fee is too high |
| BALANCE_SETTLE_SEC | 4 | Seconds to wait after swap before reading balance |
| MAX_SLIPPAGE_FREE | 0.010 | Max slippage for free tier swaps (first 3/day) |
| MAX_SLIPPAGE_PAID | 0.003 | Max slippage for paid tier swaps |
| STATE_FILE | /root/pantex-bot/state.json | Path to state file — change this to a writable path |

---

## State Persistence

The bot saves progress to `state.json` after every successful swap. If restarted mid-day it resumes from the correct swap count and all-time total. The daily counter resets automatically on the next day.

---

## Multi-Wallet Mode

To run multiple wallets simultaneously, use `manager.py` which starts all wallets in `wallets/` and exposes a web UI at port 8080:

```bash
python3 manager.py
```

Access the web dashboard at `http://YOUR_VPS_IP:8080` (default password: `pantex123`).

---

## Files

| File | Description |
|------|-------------|
| bot.py | Single-wallet bot with Rich terminal UI |
| bot_core.py | Reusable core trading logic (used by manager) |
| manager.py | Multi-wallet manager with web UI |
| web.py | FastAPI web server for multi-wallet dashboard |
| pyproject.toml | Package definition, includes pantex_sdk |
| state.json | Auto-generated, tracks daily and all-time swap counts |
| .env | Your private keys and settings — never commit this file |
| .env.example | Template for .env |

---

## Notes

- The bot is designed for mainnet only. Keys registered on testnet will not work.
- Network fees on Canton are dynamic and can vary significantly. The fee guard prevents swapping during high-fee periods.
- Settings are configured via `.env` only — there is no in-app settings panel.
- This bot does not guarantee profit. It is a volume trading tool intended for DEX reward programs.
