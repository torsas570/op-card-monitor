#!/usr/bin/env python3
"""
Monitor de disponibilidad de productos One Piece Card Game.
Revisa varias tiendas online y envía notificación por Telegram cuando detecta productos nuevos.
"""

import json
import hashlib
import time
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# --- Configuración de logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "monitor.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def build_headers(user_agent):
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


def fetch_page(url, user_agent):
    resp = requests.get(url, headers=build_headers(user_agent), timeout=30)
    resp.raise_for_status()
    return resp.text


def extract_products_html(html, site_cfg):
    soup = BeautifulSoup(html, "html.parser")
    products = []

    items = soup.select(site_cfg["selector"])
    for item in items:
        title_el = item.select_one(site_cfg["title_selector"])
        title = title_el.get_text(strip=True) if title_el else "Sin título"

        link_el = item.select_one(site_cfg["link_selector"])
        link = link_el.get("href", "") if link_el else ""
        if link and not link.startswith("http"):
            link = urljoin(site_cfg["url"], link)

        price_el = item.select_one(site_cfg["price_selector"])
        price = price_el.get_text(strip=True) if price_el else "Precio no disponible"

        uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()

        products.append({
            "uid": uid,
            "title": title,
            "link": link,
            "price": price,
        })

    return products


def extract_products_api(data, base_url=""):
    """Detección automática: WooCommerce Store API o Shopify products.json."""
    import html as html_mod
    products = []

    # Shopify products.json → {"products": [{"title", "handle", "variants":[{"price"}]}]}
    if isinstance(data, dict) and "products" in data and data["products"] and "handle" in data["products"][0]:
        from urllib.parse import urlparse
        base = ""
        if base_url:
            p = urlparse(base_url)
            base = f"{p.scheme}://{p.netloc}"
        for item in data["products"]:
            title = html_mod.unescape(item.get("title", "Sin título"))
            handle = item.get("handle", "")
            link = f"{base}/products/{handle}" if handle else ""
            variants = item.get("variants") or []
            price = "Precio no disponible"
            if variants:
                p_raw = variants[0].get("price", "")
                if p_raw:
                    price = f"{p_raw}€"
            uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
            products.append({"uid": uid, "title": title, "link": link, "price": price})
        return products

    # WooCommerce Store API → list of items
    items = data if isinstance(data, list) else data.get("products", [])
    for item in items:
        title = html_mod.unescape(item.get("name", "Sin título"))
        link = item.get("permalink") or item.get("url", "")
        prices = item.get("prices", {}) or {}
        raw_price = prices.get("price") or "0"
        currency = prices.get("currency_symbol", "€")
        try:
            price = f"{int(raw_price) / 100:.2f}{currency}"
        except (ValueError, TypeError):
            price = "Precio no disponible"
        uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price})
    return products


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text}")
    else:
        log.info("Notificación Telegram enviada correctamente")


def check_site(site_cfg, state, config):
    name = site_cfg["name"]
    url = site_cfg["url"]
    site_type = site_cfg.get("type", "html")
    log.info(f"Revisando {name}: {url}")

    try:
        resp = requests.get(url, headers=build_headers(config["user_agent"]), timeout=30)
        resp.raise_for_status()

        if site_type == "api":
            data = resp.json()
            products = extract_products_api(data, base_url=url)
        else:
            products = extract_products_html(resp.text, site_cfg)
    except Exception as e:
        log.error(f"Error al cargar {name}: {e}")
        return []

    log.info(f"  {name}: {len(products)} productos encontrados")

    if not products:
        return []

    prev_uids = set(state.get(name, []))
    current_uids = {p["uid"] for p in products}
    new_products = [p for p in products if p["uid"] not in prev_uids]

    state[name] = list(current_uids)

    if prev_uids:
        return new_products
    else:
        log.info(f"  {name}: Primera ejecución, guardando {len(products)} productos como base")
        return []


def format_notification(site_name, new_products):
    lines = [f"🆕 <b>Nuevos productos en {site_name}!</b>\n"]
    for p in new_products[:10]:
        lines.append(f"• <b>{p['title']}</b>")
        lines.append(f"  💰 {p['price']}")
        if p["link"]:
            lines.append(f"  🔗 {p['link']}")
        lines.append("")
    if len(new_products) > 10:
        lines.append(f"... y {len(new_products) - 10} más")
    return "\n".join(lines)


def run_once():
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]

    if bot_token == "TU_BOT_TOKEN_AQUI":
        log.error("⚠️  Configura tu bot token de Telegram en config.json o como variable de entorno")
        log.error("   Ejecuta: python3 setup_telegram.py")
        sys.exit(1)

    all_new = {}
    for site_cfg in config["sites"]:
        new_products = check_site(site_cfg, state, config)
        if new_products:
            all_new[site_cfg["name"]] = new_products

    save_state(state)

    if all_new:
        for site_name, products in all_new.items():
            msg = format_notification(site_name, products)
            log.info(f"Nuevos productos en {site_name}: {len(products)}")
            send_telegram(bot_token, chat_id, msg)
    else:
        log.info("Sin productos nuevos en esta revisión")


def run_loop():
    config = load_config()
    interval = config.get("check_interval_minutes", 15) * 60
    log.info(f"Iniciando monitor en bucle (cada {interval // 60} minutos)")
    while True:
        run_once()
        log.info(f"Esperando {interval // 60} minutos...")
        time.sleep(interval)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_loop()
    else:
        run_once()
