"""
WalletBot — core trading logic, wallet-independent, no TUI.
Used by both bot.py (single wallet + Rich TUI) and manager.py (multi wallet + web UI).
"""

import asyncio
import json
import logging
import os
import random
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Callable, Optional

from cantex_sdk import (
    CantexSDK,
    OperatorKeySigner,
    IntentTradingKeySigner,
    CantexAPIError,
    CantexAuthError,
    CantexTimeoutError,
)
from cantex_sdk._sdk import InstrumentId

CC_ID      = "Amulet"
CC_ADMIN   = "DSO::1220b1431ef217342db44d516bb9befde802be7d8899637d290895fa58880f19accc"
USDC_ID    = "USDCx"
USDC_ADMIN = "decentralized-usdc-interchain-rep::12208115f1e168dd7e792320be9c4ca720c751a02a3053c7606e1c1cd3dad9bf60ef"

CC_INSTRUMENT   = InstrumentId(admin=CC_ADMIN,   id=CC_ID)
USDC_INSTRUMENT = InstrumentId(admin=USDC_ADMIN, id=USDC_ID)

FREE_SWAPS_PER_DAY = 3
MIN_SWAP_AMOUNT    = Decimal("0.01")

logging.getLogger("cantex_sdk").setLevel(logging.WARNING)


def get_bal(info, instrument_id: str) -> Decimal:
    return next(
        (t.unlocked_amount for t in info.tokens if t.instrument.id == instrument_id),
        Decimal(0)
    )

def is_cc_day(date) -> bool:
    epoch = datetime(2026, 3, 8, tzinfo=timezone.utc).date()
    return ((date - epoch).days % 2) == 0


class WalletConfig:
    """Per-wallet config loaded from a .env file."""

    def __init__(self, env_path: Path):
        self.env_path = env_path
        self.load()

    def load(self):
        raw = {}
        if self.env_path.exists():
            for line in self.env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    raw[k.strip()] = v.strip()

        self.operator_key      = raw.get("CANTEX_OPERATOR_KEY", "")
        self.trading_key       = raw.get("CANTEX_TRADING_KEY", "")
        self.base_url          = raw.get("CANTEX_BASE_URL", "https://api.cantex.io")
        self.swaps_per_day_min = int(raw.get("SWAPS_PER_DAY_MIN", "20"))
        self.swaps_per_day_max = int(raw.get("SWAPS_PER_DAY_MAX", "35"))
        self.cc_sell_min       = Decimal(raw.get("CC_SELL_MIN", "4"))
        self.cc_sell_max       = Decimal(raw.get("CC_SELL_MAX", "10"))
        self.usdc_pct_min      = Decimal(raw.get("USDC_PCT_MIN", "0.03"))
        self.usdc_pct_max      = Decimal(raw.get("USDC_PCT_MAX", "0.07"))
        self.sleep_min         = int(raw.get("SLEEP_MIN_MINUTES", "5"))
        self.sleep_max         = int(raw.get("SLEEP_MAX_MINUTES", "15"))
        self.max_fee_cc        = Decimal(raw.get("MAX_NETWORK_FEE_CC", "0.2"))
        self.fee_retry_sec     = int(raw.get("FEE_RETRY_SECONDS", "120"))
        self.balance_settle    = int(raw.get("BALANCE_SETTLE_SEC", "4"))
        self.max_slip_free     = Decimal(raw.get("MAX_SLIPPAGE_FREE", "0.010"))
        self.max_slip_paid     = Decimal(raw.get("MAX_SLIPPAGE_PAID", "0.003"))

    def save(self):
        lines = [
            f"CANTEX_OPERATOR_KEY={self.operator_key}",
            f"CANTEX_TRADING_KEY={self.trading_key}",
            f"CANTEX_BASE_URL={self.base_url}",
            f"SWAPS_PER_DAY_MIN={self.swaps_per_day_min}",
            f"SWAPS_PER_DAY_MAX={self.swaps_per_day_max}",
            f"CC_SELL_MIN={self.cc_sell_min}",
            f"CC_SELL_MAX={self.cc_sell_max}",
            f"USDC_PCT_MIN={self.usdc_pct_min}",
            f"USDC_PCT_MAX={self.usdc_pct_max}",
            f"SLEEP_MIN_MINUTES={self.sleep_min}",
            f"SLEEP_MAX_MINUTES={self.sleep_max}",
            f"MAX_NETWORK_FEE_CC={self.max_fee_cc}",
            f"FEE_RETRY_SECONDS={self.fee_retry_sec}",
            f"BALANCE_SETTLE_SEC={self.balance_settle}",
            f"MAX_SLIPPAGE_FREE={self.max_slip_free}",
            f"MAX_SLIPPAGE_PAID={self.max_slip_paid}",
        ]
        self.env_path.write_text("\n".join(lines) + "\n")

    def to_dict(self) -> dict:
        return {
            "swaps_per_day_min": self.swaps_per_day_min,
            "swaps_per_day_max": self.swaps_per_day_max,
            "cc_sell_min":       str(self.cc_sell_min),
            "cc_sell_max":       str(self.cc_sell_max),
            "usdc_pct_min":      str(self.usdc_pct_min),
            "usdc_pct_max":      str(self.usdc_pct_max),
            "sleep_min":         self.sleep_min,
            "sleep_max":         self.sleep_max,
            "max_fee_cc":        str(self.max_fee_cc),
            "fee_retry_sec":     self.fee_retry_sec,
            "balance_settle":    self.balance_settle,
            "max_slip_free":     str(self.max_slip_free),
            "max_slip_paid":     str(self.max_slip_paid),
        }


class WalletState:
    def __init__(self, state_path: Path, cfg: WalletConfig):
        self.state_path = state_path
        self.cfg        = cfg
        self._load()
        self.bal_cc           = Decimal(0)
        self.bal_usdc         = Decimal(0)
        self.last_price       = Decimal(0)
        self.last_slippage    = Decimal(0)
        self.last_fee_pct     = Decimal(0)
        self.last_network_fee = Decimal(0)
        self.next_swap_at: Optional[datetime] = None
        self.activity         = deque(maxlen=100)
        self.status           = "STARTING"
        self.started_at       = datetime.now(timezone.utc)
        self.running          = False

    def _load(self):
        today = datetime.now(timezone.utc).date().isoformat()
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                if data.get("date") == today:
                    self.daily_swap_count = data["daily_swap_count"]
                    self.daily_target     = data.get("daily_target", self._roll())
                    self.daily_skipped    = data["daily_skipped"]
                    self.daily_errors     = data["daily_errors"]
                    self.daily_vol_cc     = Decimal(data["daily_vol_cc"])
                    self.daily_vol_usdc   = Decimal(data["daily_vol_usdc"])
                    self.total_swap_count = data["total_swap_count"]
                    self.fee_wait_count   = data.get("fee_wait_count", 0)
                    self.daily_fee_cc     = Decimal(data.get("daily_fee_cc", "0"))
                    self.total_fee_cc     = Decimal(data.get("total_fee_cc", "0"))
                    self.day_start        = datetime.now(timezone.utc).date()
                    return
            except Exception:
                pass
        self.daily_swap_count = 0
        self.daily_target     = self._roll()
        self.daily_skipped    = 0
        self.daily_errors     = 0
        self.daily_vol_cc     = Decimal(0)
        self.daily_vol_usdc   = Decimal(0)
        self.total_swap_count = 0
        self.fee_wait_count   = 0
        self.daily_fee_cc     = Decimal(0)
        self.total_fee_cc     = Decimal(0)
        self.day_start        = datetime.now(timezone.utc).date()

    def _roll(self) -> int:
        return random.randint(self.cfg.swaps_per_day_min, self.cfg.swaps_per_day_max)

    def save(self):
        data = {
            "date":             self.day_start.isoformat(),
            "daily_swap_count": self.daily_swap_count,
            "daily_target":     self.daily_target,
            "daily_skipped":    self.daily_skipped,
            "daily_errors":     self.daily_errors,
            "daily_vol_cc":     str(self.daily_vol_cc),
            "daily_vol_usdc":   str(self.daily_vol_usdc),
            "total_swap_count": self.total_swap_count,
            "fee_wait_count":   self.fee_wait_count,
            "daily_fee_cc":     str(self.daily_fee_cc),
            "total_fee_cc":     str(self.total_fee_cc),
        }
        try:
            self.state_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def log(self, msg: str, level: str = "info"):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.activity.appendleft({"ts": ts, "msg": msg, "level": level})

    @property
    def sell_cc(self) -> bool:
        return is_cc_day(self.day_start)

    def reset_if_new_day(self):
        today = datetime.now(timezone.utc).date()
        if today != self.day_start:
            new_target            = self._roll()
            self.daily_swap_count = 0
            self.daily_target     = new_target
            self.daily_skipped    = 0
            self.daily_errors     = 0
            self.daily_vol_cc     = Decimal(0)
            self.daily_vol_usdc   = Decimal(0)
            self.fee_wait_count   = 0
            self.daily_fee_cc     = Decimal(0)
            self.day_start        = today
            direction             = "CC → USDCx" if self.sell_cc else "USDCx → CC"
            self.log(f"New day {today} — {direction} — target {new_target} swaps", "info")
            self.save()

    def to_dict(self) -> dict:
        return {
            "status":           self.status,
            "running":          self.running,
            "day_type":         "CC day" if self.sell_cc else "USDCx day",
            "direction":        "CC → USDCx" if self.sell_cc else "USDCx → CC",
            "bal_cc":           str(self.bal_cc.quantize(Decimal("0.0001"))),
            "bal_usdc":         str(self.bal_usdc.quantize(Decimal("0.0001"))),
            "daily_swap_count": self.daily_swap_count,
            "daily_target":     self.daily_target,
            "daily_skipped":    self.daily_skipped,
            "daily_errors":     self.daily_errors,
            "fee_wait_count":   self.fee_wait_count,
            "total_swap_count": self.total_swap_count,
            "daily_vol_cc":     str(self.daily_vol_cc.quantize(Decimal("0.0001"))),
            "daily_vol_usdc":   str(self.daily_vol_usdc.quantize(Decimal("0.0001"))),
            "last_price":       str(self.last_price),
            "last_network_fee": str(self.last_network_fee),
            "daily_fee_cc":     str(self.daily_fee_cc.quantize(Decimal("0.0001"))),
            "total_fee_cc":     str(self.total_fee_cc.quantize(Decimal("0.0001"))),
            "next_swap_at":     self.next_swap_at.isoformat() if self.next_swap_at else None,
            "activity":         list(self.activity)[:50],
            "day_start":        self.day_start.isoformat(),
        }


class WalletBot:
    def __init__(self, name: str, env_path: Path, state_path: Path):
        self.name       = name
        self.cfg        = WalletConfig(env_path)
        self.state      = WalletState(state_path, self.cfg)
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def reload_config(self):
        self.cfg.load()
        self.state.cfg = self.cfg

    async def start(self):
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except Exception:
                self._task.cancel()
        self.state.status  = "STOPPED"
        self.state.running = False

    async def _run(self):
        self.state.running = True
        self.state.status  = "AUTHENTICATING"
        self.state.log(f"[{self.name}] Starting...", "info")

        if not self.cfg.operator_key or not self.cfg.trading_key:
            self.state.log("Missing CANTEX_OPERATOR_KEY or CANTEX_TRADING_KEY", "error")
            self.state.status  = "ERROR"
            self.state.running = False
            return

        try:
            operator = OperatorKeySigner.from_hex(self.cfg.operator_key)
            intent   = IntentTradingKeySigner.from_hex(self.cfg.trading_key)

            async with CantexSDK(operator, intent, base_url=self.cfg.base_url) as sdk:
                await sdk.authenticate()
                self.state.log("Authenticated", "success")

                info              = await sdk.get_account_info()
                self.state.bal_cc   = get_bal(info, CC_ID)
                self.state.bal_usdc = get_bal(info, USDC_ID)
                self.state.log(
                    f"Balance: CC={self.state.bal_cc:.4f}  USDCx={self.state.bal_usdc:.4f}",
                    "info"
                )

                direction = "CC → USDCx" if self.state.sell_cc else "USDCx → CC"
                self.state.log(
                    f"Today: [{direction}] day — target {self.state.daily_target} swaps",
                    "info"
                )

                while not self._stop_event.is_set():
                    self.state.reset_if_new_day()

                    if self.state.daily_swap_count < self.state.daily_target:
                        await self._execute_swap(sdk)

                        if self._stop_event.is_set():
                            break

                        sleep_secs = random.randint(
                            self.cfg.sleep_min * 60,
                            self.cfg.sleep_max * 60
                        )
                        wake_at              = datetime.now(timezone.utc) + timedelta(seconds=sleep_secs)
                        self.state.next_swap_at = wake_at
                        self.state.status    = "SLEEPING"
                        swaps_left           = self.state.daily_target - self.state.daily_swap_count
                        self.state.log(
                            f"Sleeping {sleep_secs // 60}m → next ~{wake_at.strftime('%H:%M UTC')} "
                            f"({swaps_left} left today)",
                            "info"
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(self._stop_event.wait()),
                                timeout=sleep_secs
                            )
                            break
                        except asyncio.TimeoutError:
                            pass
                    else:
                        now       = datetime.now(timezone.utc)
                        tomorrow  = (now + timedelta(days=1)).date()
                        rand_hour = random.randint(6, 11)
                        rand_min  = random.randint(0, 59)
                        wake_at   = datetime(
                            tomorrow.year, tomorrow.month, tomorrow.day,
                            rand_hour, rand_min, 0, tzinfo=timezone.utc
                        )
                        secs = max(60.0, (wake_at - now).total_seconds())
                        self.state.next_swap_at = wake_at
                        self.state.status = "SLEEPING"
                        self.state.log(
                            f"Daily target reached. Resuming {wake_at.strftime('%Y-%m-%d %H:%M UTC')}",
                            "info"
                        )
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(self._stop_event.wait()),
                                timeout=secs
                            )
                            break
                        except asyncio.TimeoutError:
                            pass

        except Exception as e:
            self.state.log(f"Fatal error: {e}", "error")
            self.state.status = "ERROR"
        finally:
            self.state.running = False

    async def _execute_swap(self, sdk: CantexSDK):
        cfg   = self.cfg
        state = self.state
        is_free    = state.daily_swap_count < FREE_SWAPS_PER_DAY
        max_slip   = cfg.max_slip_free if is_free else cfg.max_slip_paid
        tier_label = "FREE" if is_free else "PAID"

        try:
            state.status = "CHECKING"
            info              = await sdk.get_account_info()
            state.bal_cc      = get_bal(info, CC_ID)
            state.bal_usdc    = get_bal(info, USDC_ID)

            if state.sell_cc:
                sell_instrument = CC_INSTRUMENT
                buy_instrument  = USDC_INSTRUMENT
                label           = "CC → USDCx"
                rand_amt        = Decimal(str(random.uniform(float(cfg.cc_sell_min), float(cfg.cc_sell_max))))
                sell_amount     = min(rand_amt, state.bal_cc).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                state.log(f"CC amount: {sell_amount} [{tier_label}]", "info")
            else:
                sell_instrument = USDC_INSTRUMENT
                buy_instrument  = CC_INSTRUMENT
                label           = "USDCx → CC"
                pct             = Decimal(str(random.uniform(float(cfg.usdc_pct_min), float(cfg.usdc_pct_max))))
                sell_amount     = (state.bal_usdc * pct).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
                state.log(f"USDCx amount: {sell_amount} ({float(pct)*100:.2f}%) [{tier_label}]", "info")

            if sell_amount < MIN_SWAP_AMOUNT:
                state.log(f"Amount too low ({sell_amount}) — skipping", "warning")
                state.status = "SKIPPED"
                state.daily_skipped += 1
                state.save()
                return

            while not self._stop_event.is_set():
                state.status = "QUOTING"
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
                    f"| rate: {quote.trade_price:.6f} "
                    f"| net fee: {quote.fees.network_fee.amount} CC",
                    "info"
                )

                if quote.fees.network_fee.amount > cfg.max_fee_cc:
                    state.fee_wait_count += 1
                    state.status = "FEE_WAIT"
                    state.log(
                        f"Fee {quote.fees.network_fee.amount} CC > limit {cfg.max_fee_cc} CC — "
                        f"waiting {cfg.fee_retry_sec // 60} min (#{state.fee_wait_count})",
                        "warning"
                    )
                    state.save()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._stop_event.wait()),
                            timeout=cfg.fee_retry_sec
                        )
                        return
                    except asyncio.TimeoutError:
                        info2          = await sdk.get_account_info()
                        state.bal_cc   = get_bal(info2, CC_ID)
                        state.bal_usdc = get_bal(info2, USDC_ID)
                        continue

                if quote.slippage > max_slip:
                    state.log(f"Slippage {quote.slippage} too high — skipping", "warning")
                    state.status = "SKIPPED"
                    state.daily_skipped += 1
                    state.save()
                    return

                break

            if self._stop_event.is_set():
                return

            state.status = "EXECUTING"
            state.log(f"Executing {label}...", "info")

            await sdk.swap(
                sell_amount     = sell_amount,
                sell_instrument = sell_instrument,
                buy_instrument  = buy_instrument,
            )

            state.log("Swap completed!", "success")
            state.status = "SETTLING"
            await asyncio.sleep(cfg.balance_settle)

            info2          = await sdk.get_account_info()
            state.bal_cc   = get_bal(info2, CC_ID)
            state.bal_usdc = get_bal(info2, USDC_ID)
            state.log(
                f"New balance: CC={state.bal_cc:.4f}  USDCx={state.bal_usdc:.4f}",
                "success"
            )

            if state.sell_cc:
                state.daily_vol_cc += sell_amount
            else:
                state.daily_vol_usdc += sell_amount

            state.daily_fee_cc     += state.last_network_fee
            state.total_fee_cc     += state.last_network_fee
            state.daily_swap_count += 1
            state.total_swap_count += 1
            state.status = "SUCCESS"
            state.save()

        except CantexAuthError as e:
            state.log(f"Auth error — re-authenticating", "error")
            state.status = "ERROR"
            state.daily_errors += 1
            state.save()
            try:
                await sdk.authenticate(force=True)
                state.log("Re-authenticated", "success")
            except Exception:
                pass

        except CantexAPIError as e:
            state.log(f"API error {e.status}: {e.body}", "error")
            state.status = "ERROR"
            state.daily_errors += 1
            state.save()

        except CantexTimeoutError:
            state.log("Timeout — retrying next cycle", "warning")
            state.status = "ERROR"
            state.daily_errors += 1
            state.save()

        except Exception as e:
            state.log(f"Unexpected: {e}", "error")
            state.status = "ERROR"
            state.daily_errors += 1
            state.save()
