"""
Cantex Trading Bot  —  Rich Terminal UI
Strategy  : ONE direction per day, alternates daily
            CC->USDCx day : random 4-15 CC per swap
            USDCx->CC day : random 3-7% of current USDCx balance per swap
Frequency : 15-22 swaps per day (random, re-rolled each day)
Guards    : network fee guard (skip if > MAX_NETWORK_FEE_CC, retry every 2 min)
            slippage guard (FREE <= 1%, PAID <= 0.3%)
Persistence: state.json survives restarts
"""
from datetime import datetime

today = datetime.utcnow().date()

if today.toordinal() % 2 == 0:
    print("Today is CC → USDCx day")
else:
    print("Today is USDCx → CC day")
import asyncio
import json
import logging
import os
import random
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent / '.env', override=True)
except ImportError:
    pass
# DEBUG sementara
import os
from datetime import datetime

print("Operator key prefix:", os.getenv("CANTEX_OPERATOR_KEY", "")[:8])
print("Trading key prefix :", os.getenv("CANTEX_TRADING_KEY", "")[:8])

today = datetime.utcnow().date()
if today.toordinal() % 2 == 0:
    print("DEBUG: Today is CC → USDCx day")
else:
    print("DEBUG: Today is USDCx → CC day")

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box
from rich.align import Align

from cantex_sdk import (
    CantexSDK,
    OperatorKeySigner,
    IntentTradingKeySigner,
    CantexAPIError,
    CantexAuthError,
    CantexTimeoutError,
)
from cantex_sdk._sdk import InstrumentId

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SWAPS_PER_DAY_MIN   = int(os.environ.get("SWAPS_PER_DAY_MIN", "15"))
SWAPS_PER_DAY_MAX   = int(os.environ.get("SWAPS_PER_DAY_MAX", "22"))
CC_SELL_MIN         = Decimal(os.environ.get("CC_SELL_MIN", "4"))
CC_SELL_MAX         = Decimal(os.environ.get("CC_SELL_MAX", "15"))
USDC_PCT_MIN        = Decimal(os.environ.get("USDC_PCT_MIN", "0.03"))   # 3%
USDC_PCT_MAX        = Decimal(os.environ.get("USDC_PCT_MAX", "0.07"))   # 7%
MIN_SWAP_AMOUNT     = Decimal("0.01")
FREE_SWAPS_PER_DAY  = 3
SLEEP_MIN_MINUTES   = int(os.environ.get("SLEEP_MIN_MINUTES", "5"))
SLEEP_MAX_MINUTES   = int(os.environ.get("SLEEP_MAX_MINUTES", "10"))
BALANCE_SETTLE_SEC  = int(os.environ.get("BALANCE_SETTLE_SEC", "4"))
MAX_SLIPPAGE_FREE   = Decimal(os.environ.get("MAX_SLIPPAGE_FREE", "0.010"))
MAX_SLIPPAGE_PAID   = Decimal(os.environ.get("MAX_SLIPPAGE_PAID", "0.003"))
MAX_NETWORK_FEE_CC  = Decimal(os.environ.get("MAX_NETWORK_FEE_CC", "2.0"))
FEE_RETRY_SECONDS   = int(os.environ.get("FEE_RETRY_SECONDS", "120"))
STATE_FILE          = Path(os.environ.get("STATE_FILE", "/root/cantex-bot/state.json"))

CC_ID      = "Amulet"
CC_ADMIN   = "DSO::1220b1431ef217342db44d516bb9befde802be7d8899637d290895fa58880f19accc"
USDC_ID    = "USDCx"
USDC_ADMIN = "decentralized-usdc-interchain-rep::12208115f1e168dd7e792320be9c4ca720c751a02a3053c7606e1c1cd3dad9bf60ef"
BASE_URL   = os.environ.get("CANTEX_BASE_URL", "https://api.cantex.io")

CC_INSTRUMENT   = InstrumentId(admin=CC_ADMIN,   id=CC_ID)
USDC_INSTRUMENT = InstrumentId(admin=USDC_ADMIN, id=USDC_ID)

logging.getLogger("cantex_sdk").setLevel(logging.WARNING)
console = Console()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_bal(info, instrument_id: str) -> Decimal:
    return next(
        (t.unlocked_amount for t in info.tokens if t.instrument.id == instrument_id),
        Decimal(0)
    )

def random_cc_amount() -> Decimal:
    val = Decimal(str(random.uniform(float(CC_SELL_MIN), float(CC_SELL_MAX))))
    return val.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)

def random_usdc_amount(bal: Decimal) -> Decimal:
    pct = Decimal(str(random.uniform(float(USDC_PCT_MIN), float(USDC_PCT_MAX))))
    val = (bal * pct).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return val

def is_cc_day(date) -> bool:
    """Alternate direction per day. Mar 8 2026 = CC day (even offset from epoch)."""
    epoch = datetime(2026, 3, 8, tzinfo=timezone.utc).date()
    delta = (date - epoch).days
    return delta % 2 == 0

def roll_daily_swaps() -> int:
    return random.randint(SWAPS_PER_DAY_MIN, SWAPS_PER_DAY_MAX)

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if data.get("date") == today:
                return data
        except Exception:
            pass
    swaps_today = roll_daily_swaps()
    return {
        "date":             today,
        "daily_swap_count": 0,
        "daily_target":     swaps_today,
        "daily_skipped":    0,
        "daily_errors":     0,
        "daily_vol_cc":     "0",
        "daily_vol_usdc":   "0",
        "total_swap_count": 0,
        "fee_wait_count":   0,
    }

def save_state(state: "BotState"):
    data = {
        "date":             state.day_start.isoformat(),
        "daily_swap_count": state.daily_swap_count,
        "daily_target":     state.daily_target,
        "daily_skipped":    state.daily_skipped,
        "daily_errors":     state.daily_errors,
        "daily_vol_cc":     str(state.daily_vol_cc),
        "daily_vol_usdc":   str(state.daily_vol_usdc),
        "total_swap_count": state.total_swap_count,
        "fee_wait_count":   state.fee_wait_count,
    }
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class BotState:
    def __init__(self):
        data = load_state()
        self.daily_swap_count = data["daily_swap_count"]
        self.daily_target     = data.get("daily_target", roll_daily_swaps())
        self.daily_skipped    = data["daily_skipped"]
        self.daily_errors     = data["daily_errors"]
        self.daily_vol_cc     = Decimal(data["daily_vol_cc"])
        self.daily_vol_usdc   = Decimal(data["daily_vol_usdc"])
        self.total_swap_count = data["total_swap_count"]
        self.fee_wait_count   = data.get("fee_wait_count", 0)
        self.day_start        = datetime.now(timezone.utc).date()
        self.bal_cc           = Decimal(0)
        self.bal_usdc         = Decimal(0)
        self.last_price       = Decimal(0)
        self.last_slippage    = Decimal(0)
        self.last_fee_pct     = Decimal(0)
        self.last_network_fee = Decimal(0)
        self.next_swap_at     = None
        self.activity         = deque(maxlen=18)
        self.status           = "STARTING"
        self.started_at       = datetime.now(timezone.utc)

    @property
    def sell_cc(self) -> bool:
        return is_cc_day(self.day_start)

    def log(self, msg: str, style: str = "white"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity.appendleft((ts, msg, style))

    def reset_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day_start:
            self._print_daily_summary()
            new_target            = roll_daily_swaps()
            self.daily_swap_count = 0
            self.daily_target     = new_target
            self.daily_skipped    = 0
            self.daily_errors     = 0
            self.daily_vol_cc     = Decimal(0)
            self.daily_vol_usdc   = Decimal(0)
            self.fee_wait_count   = 0
            self.day_start        = today
            direction             = "CC → USDCx" if self.sell_cc else "USDCx → CC"
            self.log(f"New day {today} — {direction} day — target {new_target} swaps", "bold cyan")
            save_state(self)

    def _print_daily_summary(self):
        paid = max(0, self.daily_swap_count - FREE_SWAPS_PER_DAY)
        self.log("=" * 40, "dim white")
        self.log(f"DAILY SUMMARY [{self.day_start}]", "bold white")
        self.log(
            f"Completed: {self.daily_swap_count}/{self.daily_target}  "
            f"Free: {min(self.daily_swap_count, FREE_SWAPS_PER_DAY)}/3  Paid: {paid}",
            "white"
        )
        self.log(
            f"CC sold: {self.daily_vol_cc:.4f}  USDCx sold: {self.daily_vol_usdc:.4f}",
            "yellow"
        )
        self.log(
            f"Skipped: {self.daily_skipped}  Errors: {self.daily_errors}  "
            f"Fee waits: {self.fee_wait_count}",
            "dim white"
        )

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

def render_ui(state: BotState) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(name="left", ratio=2),
        Layout(name="right", ratio=3),
    )
    layout["left"].split_column(
        Layout(name="wallet"),
        Layout(name="stats"),
        Layout(name="round"),
    )

    uptime    = datetime.now(timezone.utc) - state.started_at
    h, rem    = divmod(int(uptime.total_seconds()), 3600)
    m, s      = divmod(rem, 60)
    header_text = Text(justify="center")
    header_text.append("⬡ CANTEX TRADING BOT ", style="bold bright_cyan")
    header_text.append(f"  uptime {h:02d}:{m:02d}:{s:02d}", style="dim cyan")
    header_text.append(f"  {BASE_URL}", style="dim white")
    layout["header"].update(Panel(Align.center(header_text), style="cyan", box=box.HEAVY))

    wallet_table = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    wallet_table.add_column(style="bright_yellow")
    wallet_table.add_column(style="bold white", justify="right")
    wallet_table.add_column(style="dim white", justify="right")
    wallet_table.add_row("Amulet (CC)", f"{state.bal_cc:.4f}", "CC")
    wallet_table.add_row("USDCx",       f"{state.bal_usdc:.4f}", "USDCx")
    layout["wallet"].update(Panel(wallet_table, title="[bold yellow]💰 Wallet", border_style="yellow"))

    free_used = min(state.daily_swap_count, FREE_SWAPS_PER_DAY)
    paid_used = max(0, state.daily_swap_count - FREE_SWAPS_PER_DAY)
    stats_table = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    stats_table.add_column(style="dim white")
    stats_table.add_column(style="bold bright_green", justify="right")
    stats_table.add_row("Today",        f"{state.daily_swap_count}/{state.daily_target}")
    stats_table.add_row("Free used",    f"{free_used}/{FREE_SWAPS_PER_DAY}")
    stats_table.add_row("Paid swaps",   f"{paid_used}")
    stats_table.add_row("Skipped",      f"{state.daily_skipped}")
    stats_table.add_row("Fee waits",    f"{state.fee_wait_count}")
    stats_table.add_row("All-time",     f"{state.total_swap_count}")
    layout["stats"].update(Panel(stats_table, title="[bold green]📊 Your Stats", border_style="green"))

    direction = "CC → USDCx" if state.sell_cc else "USDCx → CC"
    tier      = "FREE" if state.daily_swap_count < FREE_SWAPS_PER_DAY else "PAID"
    tier_col  = "bright_green" if tier == "FREE" else "bright_red"
    if state.sell_cc:
        amt_note = f"{CC_SELL_MIN}-{CC_SELL_MAX} CC (random)"
    else:
        pct_min  = int(USDC_PCT_MIN * 100)
        pct_max  = int(USDC_PCT_MAX * 100)
        amt_note = f"{pct_min}-{pct_max}% of USDCx balance"
    fee_col = "bright_red" if state.last_network_fee > MAX_NETWORK_FEE_CC else "bright_green"

    if state.next_swap_at:
        remaining = state.next_swap_at - datetime.now(timezone.utc)
        secs      = max(0, int(remaining.total_seconds()))
        nh, nr    = divmod(secs, 3600)
        nm, ns    = divmod(nr, 60)
        countdown = f"{nh:02d}:{nm:02d}:{ns:02d}"
    else:
        countdown = "--:--:--"

    round_table = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    round_table.add_column(style="dim white")
    round_table.add_column(style="bold white", justify="right")
    round_table.add_row("Direction",   f"[bold cyan]{direction}")
    round_table.add_row("Day type",    f"[bright_cyan]{'CC day' if state.sell_cc else 'USDCx day'}")
    round_table.add_row("Amount",      f"[bright_white]{amt_note}")
    round_table.add_row("Tier",        f"[{tier_col}]{tier}")
    round_table.add_row("Next swap",   f"[bright_white]{countdown}")
    round_table.add_row("Last price",  f"{state.last_price:.6f}")
    round_table.add_row("Slippage",    f"{state.last_slippage:.6f}")
    round_table.add_row("Fee %",       f"{state.last_fee_pct:.4f}%")
    round_table.add_row("Network fee", f"[{fee_col}]{state.last_network_fee:.4f} CC  (max {MAX_NETWORK_FEE_CC})")
    layout["round"].update(Panel(
        round_table,
        title=f"[bold cyan]🔄 Round {state.daily_swap_count + 1}/{state.daily_target}",
        border_style="cyan"
    ))

    log_table = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    log_table.add_column(style="dim white", width=9)
    log_table.add_column()
    for (ts, msg, style) in list(state.activity):
        log_table.add_row(ts, Text(msg, style=style))
    layout["right"].update(Panel(log_table, title="[bold white]📋 Activity Log", border_style="white"))

    status_colors = {
        "STARTING": "yellow",         "AUTHENTICATING": "yellow",
        "IDLE": "dim white",          "CHECKING": "cyan",
        "SETTLING": "dim yellow",     "QUOTING": "bright_cyan",
        "EXECUTING": "bright_yellow", "SLEEPING": "dim white",
        "SUCCESS": "bright_green",    "SKIPPED": "yellow",
        "FEE_WAIT": "bright_red",     "ERROR": "bright_red",
    }
    sc = status_colors.get(state.status, "white")
    footer_text = Text(justify="center")
    footer_text.append(f" STATUS: ",       style="dim white")
    footer_text.append(f"{state.status} ", style=f"bold {sc}")
    footer_text.append(f"  Vol CC: {state.daily_vol_cc:.4f}  ",    style="dim yellow")
    footer_text.append(f"Vol USDCx: {state.daily_vol_usdc:.4f}  ", style="dim cyan")
    footer_text.append(f"Max fee: {MAX_NETWORK_FEE_CC} CC  ",      style="dim white")
    footer_text.append(f"Day: {state.day_start}",                   style="dim white")
    layout["footer"].update(Panel(Align.center(footer_text), style="dim", box=box.SIMPLE))

    return layout

# ---------------------------------------------------------------------------
# Swap execution
# ---------------------------------------------------------------------------

async def execute_swap(sdk: CantexSDK, state: BotState) -> bool:
    is_free    = state.daily_swap_count < FREE_SWAPS_PER_DAY
    max_slip   = MAX_SLIPPAGE_FREE if is_free else MAX_SLIPPAGE_PAID
    tier_label = "FREE" if is_free else "PAID"

    try:
        state.status = "CHECKING"
        info           = await sdk.get_account_info()
        state.bal_cc   = get_bal(info, CC_ID)
        state.bal_usdc = get_bal(info, USDC_ID)
        state.log(f"Balance: CC={state.bal_cc:.4f}  USDCx={state.bal_usdc:.4f}", "white")

        if state.sell_cc:
            sell_instrument = CC_INSTRUMENT
            buy_instrument  = USDC_INSTRUMENT
            label           = "CC → USDCx"
            rand_amount     = random_cc_amount()
            sell_amount     = min(rand_amount, state.bal_cc).quantize(
                                  Decimal("0.00000001"), rounding=ROUND_DOWN)
            state.log(
                f"Random CC amount: {rand_amount} → using {sell_amount} "
                f"(balance: {state.bal_cc:.4f})",
                "dim white"
            )
        else:
            sell_instrument = USDC_INSTRUMENT
            buy_instrument  = CC_INSTRUMENT
            label           = "USDCx → CC"
            pct             = Decimal(str(random.uniform(
                                  float(USDC_PCT_MIN), float(USDC_PCT_MAX))))
            sell_amount     = (state.bal_usdc * pct).quantize(
                                  Decimal("0.00000001"), rounding=ROUND_DOWN)
            state.log(
                f"Random USDCx pct: {float(pct)*100:.2f}% of {state.bal_usdc:.4f} "
                f"= {sell_amount}",
                "dim white"
            )

        if sell_amount < MIN_SWAP_AMOUNT:
            state.log(
                f"[{label}] Amount too low ({sell_amount}) — skipping this round",
                "yellow"
            )
            state.status = "SKIPPED"
            state.daily_skipped += 1
            save_state(state)
            return False

        # --- Quote loop: retry every 2 min if network fee too high ---
        while True:
            state.status = "QUOTING"
            state.log(
                f"Quote: {sell_amount} {sell_instrument.id} → {buy_instrument.id} [{tier_label}]",
                "cyan"
            )

            quote = await sdk.get_swap_quote(
                sell_amount     = sell_amount,
                sell_instrument = sell_instrument,
                buy_instrument  = buy_instrument,
            )

            state.last_price       = quote.trade_price
            state.last_slippage    = quote.slippage
            state.last_fee_pct     = quote.fees.fee_percentage
            state.last_network_fee = quote.fees.network_fee.amount

            state.log(
                f"Quote: {sell_amount} → {quote.returned_amount:.4f} "
                f"(rate: {quote.trade_price:.6f})",
                "bright_cyan"
            )
            state.log(
                f"Slip: {quote.slippage:.6f} (limit {max_slip})  "
                f"Fee: {quote.fees.fee_percentage}%  "
                f"Net fee: {quote.fees.network_fee.amount} {quote.fees.network_fee.instrument.id}",
                "dim white"
            )

            # Network fee guard
            if quote.fees.network_fee.amount > MAX_NETWORK_FEE_CC:
                state.fee_wait_count += 1
                state.status = "FEE_WAIT"
                state.log(
                    f"⏳ Network fee {quote.fees.network_fee.amount} CC > "
                    f"{MAX_NETWORK_FEE_CC} CC limit. "
                    f"Waiting {FEE_RETRY_SECONDS // 60} min... (wait #{state.fee_wait_count})",
                    "bright_red"
                )
                save_state(state)
                await asyncio.sleep(FEE_RETRY_SECONDS)
                # re-check balance after wait
                info2          = await sdk.get_account_info()
                state.bal_cc   = get_bal(info2, CC_ID)
                state.bal_usdc = get_bal(info2, USDC_ID)
                continue

            # Slippage guard
            if quote.slippage > max_slip:
                state.log(
                    f"⚠ Slippage {quote.slippage} too high [{tier_label}] — skipping",
                    "bright_red"
                )
                state.status = "SKIPPED"
                state.daily_skipped += 1
                save_state(state)
                return False

            break  # fee and slippage OK

        state.status = "EXECUTING"
        state.log(f"Executing {label}...", "bright_yellow")

        await sdk.swap(
            sell_amount     = sell_amount,
            sell_instrument = sell_instrument,
            buy_instrument  = buy_instrument,
        )

        state.log("✅ Swap completed!", "bold bright_green")

        state.status = "SETTLING"
        state.log(f"Settling... waiting {BALANCE_SETTLE_SEC}s", "dim yellow")
        await asyncio.sleep(BALANCE_SETTLE_SEC)

        info2          = await sdk.get_account_info()
        state.bal_cc   = get_bal(info2, CC_ID)
        state.bal_usdc = get_bal(info2, USDC_ID)
        state.log(
            f"New balance: CC={state.bal_cc:.4f}  USDCx={state.bal_usdc:.4f}",
            "green"
        )

        if state.sell_cc:
            state.daily_vol_cc += sell_amount
        else:
            state.daily_vol_usdc += sell_amount

        state.daily_swap_count += 1
        state.total_swap_count += 1
        state.status            = "SUCCESS"
        save_state(state)
        return True

    except CantexAuthError as e:
        state.log(f"Auth error {e.status} — re-authenticating", "bright_red")
        state.status = "ERROR"
        state.daily_errors += 1
        save_state(state)
        try:
            await sdk.authenticate(force=True)
            state.log("Re-authenticated OK", "green")
        except Exception as re:
            state.log(f"Re-auth failed: {re}", "bright_red")
        return False

    except CantexAPIError as e:
        state.log(f"API error {e.status}: {e.body}", "bright_red")
        state.status = "ERROR"
        state.daily_errors += 1
        save_state(state)
        return False

    except CantexTimeoutError:
        state.log("Timeout — retrying next cycle", "yellow")
        state.status = "ERROR"
        state.daily_errors += 1
        save_state(state)
        return False

    except Exception as e:
        state.log(f"Unexpected: {e}", "bright_red")
        state.status = "ERROR"
        state.daily_errors += 1
        save_state(state)
        return False

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_bot():
    operator_key = os.environ.get("CANTEX_OPERATOR_KEY")
    trading_key  = os.environ.get("CANTEX_TRADING_KEY")
    if not operator_key:
        raise EnvironmentError("CANTEX_OPERATOR_KEY not set")
    if not trading_key:
        raise EnvironmentError("CANTEX_TRADING_KEY not set")

    operator = OperatorKeySigner.from_hex(operator_key)
    intent   = IntentTradingKeySigner.from_hex(trading_key)
    state    = BotState()

    direction = "CC → USDCx" if state.sell_cc else "USDCx → CC"
    state.log("Bot initializing...", "dim white")
    state.log(
        f"Today is a [{direction}] day — target {state.daily_target} swaps",
        "bold cyan"
    )
    state.log(
        f"CC sell: {CC_SELL_MIN}-{CC_SELL_MAX} random | "
        f"USDCx sell: {int(USDC_PCT_MIN*100)}-{int(USDC_PCT_MAX*100)}% of balance",
        "dim white"
    )
    state.log(
        f"Interval: {SLEEP_MIN_MINUTES}-{SLEEP_MAX_MINUTES} min | "
        f"Net fee guard: max {MAX_NETWORK_FEE_CC} CC, retry {FEE_RETRY_SECONDS // 60} min",
        "dim white"
    )
    state.log(
        f"Resumed: {state.daily_swap_count}/{state.daily_target} swaps today | "
        f"all-time: {state.total_swap_count}",
        "cyan"
    )

    async with CantexSDK(operator, intent, base_url=BASE_URL) as sdk:

        with Live(render_ui(state), console=console, refresh_per_second=2, screen=True) as live:

            async def refresh():
                while True:
                    live.update(render_ui(state))
                    await asyncio.sleep(0.5)

            refresh_task = asyncio.create_task(refresh())

            try:
                state.status = "AUTHENTICATING"
                state.log("Authenticating...", "yellow")
                await sdk.authenticate()
                state.log("✅ Authenticated", "bright_green")

                info           = await sdk.get_account_info()
                state.bal_cc   = get_bal(info, CC_ID)
                state.bal_usdc = get_bal(info, USDC_ID)
                state.log(
                    f"Balances: CC={state.bal_cc}  USDCx={state.bal_usdc}",
                    "cyan"
                )

                while True:
                    state.reset_if_new_day()

                    if state.daily_swap_count < state.daily_target:
                        await execute_swap(sdk, state)

                        sleep_secs         = random.randint(
                                                 SLEEP_MIN_MINUTES * 60,
                                                 SLEEP_MAX_MINUTES * 60
                                             )
                        wake_at            = datetime.now(timezone.utc) + timedelta(seconds=sleep_secs)
                        state.next_swap_at = wake_at
                        state.status       = "SLEEPING"
                        swaps_left         = state.daily_target - state.daily_swap_count
                        state.log(
                            f"Sleeping {sleep_secs // 60}m {sleep_secs % 60}s → "
                            f"next ~{wake_at.strftime('%H:%M UTC')} "
                            f"({swaps_left} swaps left today)",
                            "dim white"
                        )
                        await asyncio.sleep(sleep_secs)

                    else:
                        now        = datetime.now(timezone.utc)
                        tomorrow   = (now + timedelta(days=1)).date()
                        rand_hour  = random.randint(6, 11)
                        rand_min   = random.randint(0, 59)
                        wake_at    = datetime(
                                         tomorrow.year, tomorrow.month, tomorrow.day,
                                         rand_hour, rand_min, 0,
                                         tzinfo=timezone.utc
                                     )
                        secs       = max(60.0, (wake_at - now).total_seconds())
                        state.next_swap_at = wake_at
                        state.status       = "SLEEPING"
                        state.log(
                            f"Daily target reached ({state.daily_target} swaps). "
                            f"Resuming tomorrow {wake_at.strftime('%Y-%m-%d %H:%M UTC')} "
                            f"(random 06:00-12:00 UTC)",
                            "dim cyan"
                        )
                        await asyncio.sleep(secs)

            finally:
                refresh_task.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        console.print("\n[bold red]Bot stopped.[/bold red]")