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

HEALTH_KEY = "__health__"  # misma clave reservada que usa monitor.py

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG["telegram_bot_token"]
chat_id = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG["telegram_chat_id"]

state = json.load(open(STATE_PATH)) if STATE_PATH.exists() else {}

health = state.get(HEALTH_KEY, {})
# __health__ no es una tienda: si se cuenta, infla el nº de tiendas y de productos.
sites_state = {k: v for k, v in state.items() if k != HEALTH_KEY}

n_sites_cfg = len(CONFIG["sites"])
n_sites_tracked = len(sites_state)
total_products = sum(len(v) if isinstance(v, (dict, list)) else 0 for v in sites_state.values())
oos = sum(
    1 for site in sites_state.values() if isinstance(site, dict)
    for p in site.values() if isinstance(p, dict) and not p.get("in_stock", True)
)
in_stock = total_products - oos

# Tiendas que ahora mismo no responden (fallos consecutivos acumulados)
caidas = sorted(
    (name for name, h in health.items() if h.get("fails", 0) > 0),
    key=lambda n: -health[n].get("fails", 0),
)
# Configuradas pero sin datos: colección vacía o que nunca ha respondido
sin_datos = [s["name"] for s in CONFIG["sites"] if s["name"] not in sites_state]

lines = [
    "💓 <b>Heartbeat One Piece Card Game</b>",
    f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    "",
    "✅ Bot vivo y funcionando",
    f"🏪 Tiendas configuradas: {n_sites_cfg}",
    f"📊 Tiendas con datos: {n_sites_tracked}",
    f"📦 Productos OP tracked: {total_products}",
    f"  • En stock: {in_stock}",
    f"  • Agotados: {oos}",
]

if caidas:
    lines += ["", f"⚠️ Sin responder ({len(caidas)}):"]
    lines += [f"  • {n} ({health[n].get('fails', 0)} fallos)" for n in caidas[:10]]
if sin_datos:
    lines += ["", f"🔍 Sin datos todavía ({len(sin_datos)}):"]
    lines += [f"  • {n}" for n in sin_datos[:10]]

lines += ["", "Si esto no te llega cada noche → el bot está caído. Revisa GitHub Actions."]
msg = "\n".join(lines)

resp = requests.post(
    f"https://api.telegram.org/bot{bot_token}/sendMessage",
    json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
    timeout=15,
)
if resp.status_code != 200:
    print(f"Error: {resp.text}")
    sys.exit(1)
print("Heartbeat enviado")
