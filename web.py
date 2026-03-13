"""
Cantex Bot Web UI — FastAPI server
Run alongside bot_core.py for multi-wallet management.
Access via http://VPS_IP:8080
"""

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from bot_core import WalletBot, WalletConfig

BASE_DIR    = Path(__file__).parent
WALLETS_DIR = BASE_DIR / "wallets"
STATES_DIR  = BASE_DIR / "states"
WEB_CONFIG  = BASE_DIR / "web_config.json"

WALLETS_DIR.mkdir(exist_ok=True)
STATES_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------

def load_web_config() -> dict:
    if WEB_CONFIG.exists():
        return json.loads(WEB_CONFIG.read_text())
    default = {"password_hash": hashlib.sha256(b"cantex123").hexdigest()}
    WEB_CONFIG.write_text(json.dumps(default, indent=2))
    return default

def check_password(provided: str) -> bool:
    cfg  = load_web_config()
    hashed = hashlib.sha256(provided.encode()).hexdigest()
    return secrets.compare_digest(hashed, cfg["password_hash"])

# ---------------------------------------------------------------------------
# Wallet registry
# ---------------------------------------------------------------------------

wallets: Dict[str, WalletBot] = {}

def discover_wallets():
    for env_file in sorted(WALLETS_DIR.glob("*.env")):
        name = env_file.stem
        if name not in wallets:
            state_path = STATES_DIR / f"{name}.json"
            wallets[name] = WalletBot(name, env_file, state_path)

discover_wallets()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Cantex Bot")

# ---------------------------------------------------------------------------
# WebSocket connections
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

async def broadcast_loop():
    while True:
        if manager.active:
            payload = {
                name: bot.state.to_dict()
                for name, bot in wallets.items()
            }
            await manager.broadcast({"type": "state", "wallets": payload})
        await asyncio.sleep(1)

@app.on_event("startup")
async def startup():
    asyncio.create_task(broadcast_loop())

# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def require_auth(token: str = ""):
    if not check_password(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str

class ConfigUpdate(BaseModel):
    swaps_per_day_min: int
    swaps_per_day_max: int
    cc_sell_min: str
    cc_sell_max: str
    usdc_pct_min: str
    usdc_pct_max: str
    sleep_min: int
    sleep_max: int
    max_fee_cc: str
    fee_retry_sec: int
    balance_settle: int
    max_slip_free: str
    max_slip_paid: str

class StateUpdate(BaseModel):
    daily_target: int

class AddWallet(BaseModel):
    name: str
    operator_key: str
    trading_key: str

class AuthRequest(BaseModel):
    token: str

@app.post("/api/login")
async def login(req: LoginRequest):
    if not check_password(req.password):
        raise HTTPException(status_code=401, detail="Wrong password")
    token = hashlib.sha256(req.password.encode()).hexdigest()
    return {"token": token}

@app.get("/api/wallets")
async def get_wallets(token: str = ""):
    require_auth(token)
    return {
        name: {
            "state":  bot.state.to_dict(),
            "config": bot.cfg.to_dict(),
        }
        for name, bot in wallets.items()
    }

@app.post("/api/wallets/{name}/start")
async def start_wallet(name: str, req: AuthRequest):
    require_auth(req.token)
    if name not in wallets:
        raise HTTPException(status_code=404, detail="Wallet not found")
    await wallets[name].start()
    return {"ok": True}

@app.post("/api/wallets/{name}/stop")
async def stop_wallet(name: str, req: AuthRequest):
    require_auth(req.token)
    if name not in wallets:
        raise HTTPException(status_code=404, detail="Wallet not found")
    await wallets[name].stop()
    return {"ok": True}

@app.put("/api/wallets/{name}/config")
async def update_config(name: str, cfg: ConfigUpdate, token: str = ""):
    require_auth(token)
    if name not in wallets:
        raise HTTPException(status_code=404, detail="Wallet not found")
    bot = wallets[name]
    bot.cfg.swaps_per_day_min = cfg.swaps_per_day_min
    bot.cfg.swaps_per_day_max = cfg.swaps_per_day_max
    bot.cfg.cc_sell_min       = cfg.cc_sell_min
    bot.cfg.cc_sell_max       = cfg.cc_sell_max
    bot.cfg.usdc_pct_min      = cfg.usdc_pct_min
    bot.cfg.usdc_pct_max      = cfg.usdc_pct_max
    bot.cfg.sleep_min         = cfg.sleep_min
    bot.cfg.sleep_max         = cfg.sleep_max
    bot.cfg.max_fee_cc        = cfg.max_fee_cc
    bot.cfg.fee_retry_sec     = cfg.fee_retry_sec
    bot.cfg.balance_settle    = cfg.balance_settle
    bot.cfg.max_slip_free     = cfg.max_slip_free
    bot.cfg.max_slip_paid     = cfg.max_slip_paid
    bot.cfg.save()
    bot.reload_config()
    return {"ok": True}

@app.put("/api/wallets/{name}/state")
async def update_state(name: str, upd: StateUpdate, token: str = ""):
    require_auth(token)
    if name not in wallets:
        raise HTTPException(status_code=404, detail="Wallet not found")
    bot = wallets[name]
    bot.state.daily_target = upd.daily_target
    bot.state.save()
    return {"ok": True}

@app.post("/api/wallets/add")
async def add_wallet(req: AddWallet, token: str = ""):
    require_auth(token)
    name = req.name.replace(" ", "_").lower()
    if name in wallets:
        raise HTTPException(status_code=400, detail="Wallet already exists")
    env_path   = WALLETS_DIR / f"{name}.env"
    state_path = STATES_DIR  / f"{name}.json"
    cfg = WalletConfig(env_path)
    cfg.operator_key = req.operator_key
    cfg.trading_key  = req.trading_key
    cfg.save()
    wallets[name] = WalletBot(name, env_path, state_path)
    return {"ok": True, "name": name}

@app.delete("/api/wallets/{name}")
async def delete_wallet(name: str, token: str = ""):
    require_auth(token)
    if name not in wallets:
        raise HTTPException(status_code=404, detail="Wallet not found")
    await wallets[name].stop()
    del wallets[name]
    env_path = WALLETS_DIR / f"{name}.env"
    if env_path.exists():
        env_path.unlink()
    return {"ok": True}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)

# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return html_path.read_text()
    return "<h1>static/index.html not found</h1>"
