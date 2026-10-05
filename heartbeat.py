#!/usr/bin/env python3
"""Heartbeat diario — resumen del bot OP a Telegram."""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG = json.load(open(BASE / "config.json"))
STATE_PATH = BASE / "state.json"

HEALTH_KEY = "__health__"  # misma clave reservada que usa monitor.py

bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or CONFIG["telegram_bot_token"]
chat_id = os.environ.get("TELEGRAM_CHAT_ID") or CONFIG["telegram_chat_id"]

state = json.load(open(STATE_PATH)) if STATE_PATH.exists() else {}

# Solo tiendas que siguen en config.json: las claves reservadas ("__health__",
# "__sig__", "__run__"...) empiezan por "__", y una tienda renombrada dejaba su
# entrada vieja contando como tienda vigilada.
_nombres = {s["name"] for s in CONFIG["sites"]}
health = {k: v for k, v in state.get(HEALTH_KEY, {}).items() if k in _nombres}
sites_state = {k: v for k, v in state.items()
               if k in _nombres and isinstance(v, dict)}

n_sites_cfg = len(CONFIG["sites"])
n_sites_tracked = len(sites_state)
total_products = sum(len(v) if isinstance(v, (dict, list)) else 0 for v in sites_state.values())
oos = sum(
    1 for site in sites_state.values() if isinstance(site, dict)
    for p in site.values() if isinstance(p, dict) and not p.get("in_stock", True)
)
in_stock = total_products - oos

# Tiendas que ahora mismo no responden. Con `> 0` salía cualquier fallo suelto de
# la última pasada; con 3 seguidos ya es algo que merece mirarse.
caidas = sorted(
    (name for name, h in health.items() if h.get("fails", 0) >= 3),
    key=lambda n: -health[n].get("fails", 0),
)
# Responden 200 pero llevan pasadas a 0 productos teniendo catálogo antes
vacias = sorted(name for name, h in health.items() if h.get("empty_streak", 0) > 0)
# Configuradas pero sin datos: colección vacía o que nunca ha respondido
sin_datos = [s["name"] for s in CONFIG["sites"] if s["name"] not in sites_state]

# Prueba de vida REAL: monitor.py apunta en "__run__" cuándo completó su última
# pasada. Antes este mensaje decía "bot vivo" siempre, aunque monitor.py petara en
# cada pasada. El state llega por la caché, que el bucle guarda al acabar cada
# bloque de ~5h30m, así que lo normal es que tenga hasta ~6 h; más de 7 h = parado.
_run = state.get("__run__", {})
_edad_h = (time.time() - _run["last_run"]) / 3600 if _run.get("last_run") else None
if _edad_h is None:
    vida = "ℹ️ Sin pasadas registradas todavía (versión nueva recién desplegada)"
elif _edad_h > 7:
    vida = (f"🛑 <b>La última pasada guardada es de hace {_edad_h:.0f} h</b>: "
            f"el bucle puede estar parado. Revisa GitHub Actions.")
else:
    vida = (f"✅ Bot vivo: última pasada guardada hace {_edad_h:.1f} h "
            f"({_run.get('sites_ok', '?')} tiendas OK, {_run.get('sites_failed', '?')} con fallo)")

lines = [
    "💓 <b>Heartbeat One Piece Card Game</b>",
    f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
    "",
    vida,
    f"🏪 Tiendas configuradas: {n_sites_cfg}",
    f"📊 Tiendas con datos: {n_sites_tracked}",
    f"📦 Productos OP tracked: {total_products}",
    f"  • En stock: {in_stock}",
    f"  • Agotados: {oos}",
]

if caidas:
    lines += ["", f"⚠️ Sin responder ({len(caidas)}):"]
    lines += [f"  • {n} ({health[n].get('fails', 0)} fallos)" for n in caidas[:10]]
if vacias:
    lines += ["", f"👻 A 0 productos ({len(vacias)}):"]
    lines += [f"  • {n} ({health[n].get('empty_streak', 0)} pasadas)" for n in vacias[:10]]
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
