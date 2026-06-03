#!/usr/bin/env python3
"""Heartbeat diario — resumen del bot OP a Telegram."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG = json.load(open(BASE / "config.json"))
STATE_PATH = BASE / "state.json"

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG["telegram_bot_token"]
chat_id = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG["telegram_chat_id"]

state = json.load(open(STATE_PATH)) if STATE_PATH.exists() else {}

n_sites_cfg = len(CONFIG["sites"])
n_sites_tracked = len(state)
total_products = sum(len(v) if isinstance(v, (dict, list)) else 0 for v in state.values())
oos = sum(
    1 for site in state.values() if isinstance(site, dict)
    for p in site.values() if isinstance(p, dict) and not p.get("in_stock", True)
)
in_stock = total_products - oos

msg = (
    f"💓 <b>Heartbeat One Piece Card Game</b>\n"
    f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
    f"✅ Bot vivo y funcionando\n"
    f"🏪 Tiendas configuradas: {n_sites_cfg}\n"
    f"📊 Tiendas con datos: {n_sites_tracked}\n"
    f"📦 Productos OP tracked: {total_products}\n"
    f"  • En stock: {in_stock}\n"
    f"  • Agotados: {oos}\n\n"
    f"Si esto no te llega cada noche → el bot está caído. Revisa GitHub Actions."
)

resp = requests.post(
    f"https://api.telegram.org/bot{bot_token}/sendMessage",
    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
    timeout=15,
)
if resp.status_code != 200:
    print(f"Error: {resp.text}")
    sys.exit(1)
print("Heartbeat enviado")
