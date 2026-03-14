"""
Pantex Bot Manager — starts all wallets + web server in one process.

Usage:
  python3 manager.py

Opens web UI at http://VPS_IP:8080
Default password: pantex123  (change via web_config.json)
"""

import asyncio
import uvicorn
from pathlib import Path
from bot_core import WalletBot
from web import app, wallets, WALLETS_DIR, STATES_DIR

async def main():
    # Discover and auto-start all wallets
    for env_file in sorted(WALLETS_DIR.glob("*.env")):
        name = env_file.stem
        state_path = STATES_DIR / f"{name}.json"
        bot = WalletBot(name, env_file, state_path)
        wallets[name] = bot
        await bot.start()
        print(f"[manager] Started wallet: {name}")

    if not wallets:
        print("[manager] No wallets found in wallets/ — add via web UI")

    # Start web server
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8080,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    print("[manager] Web UI running at http://0.0.0.0:8080")
    print("[manager] Default password: pantex123")
    await server.serve()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[manager] Stopped.")
