#!/usr/bin/env python3
"""
Monitor One Piece Card Game — detecta nuevos productos y restocks
en múltiples tiendas online, notifica por Telegram.
"""

import json
import hashlib
import time
import logging
import os
import sys
import html as html_mod
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

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

OOS_KEYWORDS = ["agotado", "sold out", "out of stock", "vendido", "no disponible", "rupture de stock"]


def load_config():
    return json.load(open(CONFIG_PATH))


def load_state():
    return json.load(open(STATE_PATH)) if STATE_PATH.exists() else {}


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


def detect_html_in_stock(item):
    classes = " ".join(item.get("class", [])).lower()
    if any(k in classes for k in ["out-of-stock", "sold-out", "outofstock", "agotado"]):
        return False
    text = item.get_text(" ", strip=True).lower()
    if any(k in text for k in OOS_KEYWORDS):
        return False
    return True


def extract_products_html(html, site_cfg):
    soup = BeautifulSoup(html, "html.parser")
    products = []
    for item in soup.select(site_cfg["selector"]):
        title_el = item.select_one(site_cfg["title_selector"])
        title = title_el.get_text(strip=True) if title_el else "Sin título"
        link_el = item.select_one(site_cfg["link_selector"])
        link = link_el.get("href", "") if link_el else ""
        if link and not link.startswith("http"):
            link = urljoin(site_cfg["url"], link)
        price_el = item.select_one(site_cfg["price_selector"])
        price = price_el.get_text(strip=True) if price_el else "Precio no disponible"
        in_stock = detect_html_in_stock(item)
        uid = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
    return products


def extract_products_api(data, base_url=""):
    """Detección automática: Shopify products.json o WooCommerce Store API."""
    products = []
    if isinstance(data, dict) and "products" in data and data["products"] and "handle" in data["products"][0]:
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
            in_stock = False
            if variants:
                p_raw = variants[0].get("price", "")
                if p_raw:
                    price = f"{p_raw}€"
                in_stock = any(v.get("available", False) for v in variants)
            uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
            products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
        return products

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
        in_stock = item.get("is_in_stock", item.get("has_stock", True))
        uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
    return products


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
    resp = requests.post(url, json=payload, timeout=15)
    if resp.status_code != 200:
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text}")
    else:
        log.info("Notificación Telegram enviada")


def normalize_state(raw):
    if isinstance(raw, list):
        return {uid: {"in_stock": True} for uid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def check_site(site_cfg, state, config):
    name = site_cfg["name"]
    url = site_cfg["url"]
    site_type = site_cfg.get("type", "html")
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    log.info(f"Revisando {name}: {url}")

    try:
        resp = requests.get(url, headers=build_headers(config["user_agent"]), timeout=30)
        resp.raise_for_status()
        if site_type == "api":
            products = extract_products_api(resp.json(), base_url=url)
        else:
            products = extract_products_html(resp.text, site_cfg)
    except Exception as e:
        log.error(f"  Error {name}: {e}")
        return []

    log.info(f"  {name}: {len(products)} productos encontrados")
    if not products:
        return []

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    site_state = normalize_state(raw_prev)

    alerts = []
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        if prev is None:
            if not is_first_run:
                if p["in_stock"] or not notify_only_in_stock:
                    alerts.append({**p, "alert_type": "new"})
        else:
            if not prev.get("in_stock", True) and p["in_stock"]:
                alerts.append({**p, "alert_type": "restock"})
        site_state[uid] = {"in_stock": p["in_stock"]}

    state[name] = site_state

    if is_first_run:
        log.info(f"  {name}: primera ejecución, guardando baseline de {len(products)} productos")
    return alerts


def format_notification(site_name, alerts):
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    lines = [f"🆕 {header}<b>{site_name}</b>\n"]
    for p in alerts[:10]:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        stock = "" if p["in_stock"] else " ⚠️ AGOTADO"
        lines.append(f"• {tag}{stock} <b>{p['title']}</b>")
        lines.append(f"  💰 {p['price']}")
        if p["link"]:
            lines.append(f"  🔗 {p['link']}")
        lines.append("")
    if len(alerts) > 10:
        lines.append(f"... y {len(alerts) - 10} más")
    return "\n".join(lines)


def run_once():
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]
    if bot_token == "TU_BOT_TOKEN_AQUI":
        log.error("⚠️ Configura Telegram: python3 setup_telegram.py")
        sys.exit(1)

    all_alerts = {}
    for site_cfg in config["sites"]:
        alerts = check_site(site_cfg, state, config)
        if alerts:
            all_alerts[site_cfg["name"]] = alerts

    save_state(state)

    if not all_alerts:
        log.info("Sin alertas en esta revisión")
        return

    for site_name, alerts in all_alerts.items():
        msg = format_notification(site_name, alerts)
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {site_name}: {n_new} nuevos + {n_re} restock")
        send_telegram(bot_token, chat_id, msg)


def run_loop():
    config = load_config()
    interval = config.get("check_interval_minutes", 15) * 60
    log.info(f"Iniciando monitor en bucle (cada {interval // 60} min)")
    while True:
        run_once()
        log.info(f"Esperando {interval // 60} minutos...")
        time.sleep(interval)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        run_loop()
    else:
        run_once()
