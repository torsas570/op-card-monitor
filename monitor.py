#!/usr/bin/env python3
"""
Monitor One Piece Card Game — detecta nuevos productos y restocks
en múltiples tiendas online, notifica por Telegram.

Cada URL de `sites` apunta ya a una colección/categoría de One Piece, así que
por defecto NO hay filtro de keywords: todo lo que sale es relevante. Los feeds
mixtos (preventas de varios juegos, colecciones que mezclan singles y sellado)
llevan sus propios `include_keywords` / `exclude_keywords` en config.json.

Features:
- Detección de NUEVO listado y de RESTOCK (agotado que vuelve a stock).
- Baseline SILENCIOSO en la 1ª pasada de cada tienda.
- Re-sync silencioso: si una tienda saca de golpe más de `resync_threshold`
  listados nuevos (recatalogación, ampliación de cobertura, cambio de orden de
  la colección), se absorben sin detallar y se avisa con UNA línea. Evita
  tandas de cientos de mensajes.
- Prioridad de PRODUCTO: 🚨 case/booster box primero, 🎁 promos después.
- Avisos de SALUD: si una tienda deja de responder —o se queda a 0 productos
  cuando antes tenía— avisa una vez (y otra al recuperarse) en vez de quedarse
  ciego en silencio.
- Telegram robusto: escapado de HTML, troceo a 4096 caracteres y reintentos.
"""

import json
import hashlib
import time
import logging
import os
import sys
import argparse
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

PRIORITY_EMOJI = {"high": "🚨", "medium": "📦", "low": "🔍"}
OOS_KEYWORDS = ["agotado", "sold out", "out of stock", "vendido", "no disponible", "rupture de stock"]

HEALTH_KEY = "__health__"  # clave reservada en state para la salud (no es un sitio)
DEFAULT_HEALTH_FAIL_THRESHOLD = 3  # fallos seguidos antes de avisar de bloqueo/caída
DEFAULT_RESYNC_THRESHOLD = 20      # nuevos de golpe a partir de los cuales se absorbe sin detallar
TELEGRAM_LIMIT = 4096              # límite duro de la API de Telegram
MAX_ITEMS_PER_MESSAGE = 12         # productos detallados por aviso


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


def _record_health(state, name, ok, error=None):
    """Cuenta fallos consecutivos por tienda dentro del propio state."""
    health = state.setdefault(HEALTH_KEY, {})
    h = health.setdefault(name, {"fails": 0, "alerted": False, "last_error": None})
    if ok:
        h["fails"] = 0
        h["last_error"] = None
    else:
        h["fails"] = h.get("fails", 0) + 1
        h["last_error"] = error


def _collect_health_alerts(state, config):
    """Mensajes SOLO en las transiciones (cae -> avisa / se recupera -> avisa),
    para no repetir el aviso en cada pasada. Muta los flags 'alerted' del state."""
    threshold = config.get("health_fail_threshold", DEFAULT_HEALTH_FAIL_THRESHOLD)
    health = state.get(HEALTH_KEY, {})
    msgs = []
    for name, h in health.items():
        fails = h.get("fails", 0)
        alerted = h.get("alerted", False)
        if fails >= threshold and not alerted:
            h["alerted"] = True
            msgs.append(
                f"⚠️ <b>Aviso de monitor</b>\n\n"
                f"<b>{html_mod.escape(name)}</b> no responde tras {fails} intentos seguidos "
                f"(posible bloqueo de IP, caída de la web o selectores rotos).\n"
                f"⚠️ Puede que te estés perdiendo restocks de esta tienda.\n"
                f"Último error: <code>{html_mod.escape(str(h.get('last_error')))}</code>"
            )
        elif fails == 0 and alerted:
            h["alerted"] = False
            msgs.append(f"✅ <b>{html_mod.escape(name)}</b> vuelve a responder con normalidad.")
    return msgs


def build_headers(user_agent, is_api=False):
    # Accept-Encoding sin "br": brotli no siempre está instalado y dejaría el
    # cuerpo sin descomprimir (el parseo JSON fallaría con "Expecting value").
    headers = {
        "User-Agent": user_agent,
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if is_api:
        # Petición tipo XHR: muchas tiendas tras Cloudflare solo sirven el JSON
        # si la cabecera parece una llamada AJAX y no una navegación de navegador.
        headers.update({
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        })
    else:
        headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    return headers


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

    # Un elemento sin título es inservible: los bots filtran por keyword sobre el
    # título, así que nunca casaría. Si NINGUNO tiene título, los selectores están
    # obsoletos y la tienda está ciega: fallar para que salte el aviso de salud,
    # en vez de aparentar "0 productos relevantes" para siempre.
    usable = [p for p in products if p["title"] and p["title"] != "Sin título"]
    if products and not usable:
        raise ValueError(
            f"selectores obsoletos: {len(products)} elementos, ninguno con título"
        )
    if len(usable) < len(products):
        log.warning(
            f"  {len(products) - len(usable)} de {len(products)} elementos sin título "
            f"(title_selector incompleto), descartados"
        )
    return usable


def extract_products_api(data, base_url="", currency="€"):
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
                    # Shopify no expone la divisa en products.json: la trae el config
                    # de la tienda (por defecto €), que es cosmético — el link es el bueno.
                    price = f"{p_raw}{currency}"
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
        symbol = html_mod.unescape(prices.get("currency_symbol", currency))
        try:
            price = f"{int(raw_price) / 100:.2f}{symbol}"
        except (ValueError, TypeError):
            price = "Precio no disponible"
        in_stock = item.get("is_in_stock", item.get("has_stock", True))
        uid = hashlib.md5(f"{item.get('id', '')}{title}".encode()).hexdigest()
        products.append({"uid": uid, "title": title, "link": link, "price": price, "in_stock": in_stock})
    return products


def _split_message(message, limit=TELEGRAM_LIMIT):
    """Trocea por líneas para no pasarse del límite de Telegram (si no, la API
    devuelve 400 y el aviso se pierde entero)."""
    if len(message) <= limit:
        return [message]
    chunks, current = [], ""
    for line in message.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for part in _split_message(message):
        payload = {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                   "disable_web_page_preview": False}
        for attempt in range(3):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    log.info("Notificación Telegram enviada")
                    break
                if resp.status_code == 429:
                    # Rate limit: Telegram dice cuántos segundos esperar.
                    try:
                        wait = int(resp.json()["parameters"]["retry_after"])
                    except (ValueError, KeyError, TypeError):
                        wait = 5
                    log.warning(f"Telegram rate limit, esperando {wait}s")
                    time.sleep(wait + 1)
                    continue
                log.error(f"Error enviando Telegram: {resp.status_code} {resp.text}")
            except requests.RequestException as e:
                log.error(f"Error de red enviando Telegram: {e}")
            time.sleep(2)
        else:
            log.error("Telegram: agotados los reintentos, aviso PERDIDO")


def matches_keywords(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def normalize_state(raw):
    if isinstance(raw, list):
        return {uid: {"in_stock": True} for uid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def product_rank(p):
    """Orden de interés: 0 = case/booster box, 1 = promo, 2 = resto."""
    if p.get("high_value"):
        return 0
    if p.get("promo"):
        return 1
    return 2


def check_site(site_cfg, state, config):
    """Devuelve (alertas, nº absorbido por re-sync). Muta `state`."""
    name = site_cfg["name"]
    url = site_cfg["url"]
    site_type = site_cfg.get("type", "html")
    is_api = site_type == "api"
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    resync_threshold = config.get("resync_threshold", DEFAULT_RESYNC_THRESHOLD)
    high_value_keywords = config.get("high_value_keywords", [])
    promo_keywords = config.get("promo_keywords", [])
    # Filtros propios de la tienda: solo para feeds mixtos (preventas de varios
    # juegos, colecciones que mezclan singles/merch con sellado).
    include_keywords = site_cfg.get("include_keywords", [])
    exclude_keywords = site_cfg.get("exclude_keywords", [])
    log.info(f"[{site_cfg.get('priority', 'medium').upper()}] {name}: {url}")

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"

    products = None
    last_err = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            if is_api:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "json" not in ctype:
                    # Cloudflare u otra defensa devolvió HTML en vez del JSON
                    raise ValueError(f"respuesta no-JSON (Content-Type: {ctype or 'desconocido'})")
                products = extract_products_api(
                    resp.json(), base_url=url, currency=site_cfg.get("currency", "€")
                )
            else:
                products = extract_products_html(resp.text, site_cfg)
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(2)

    if products is None:
        # Fallo persistente (normalmente bloqueo por IP de la web): aviso, no error
        log.warning(f"  {name} no disponible: {last_err}")
        _record_health(state, name, ok=False, error=str(last_err))
        return [], 0

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    site_state = normalize_state(raw_prev)

    # Quedarse a 0 productos donde antes había catálogo es el fallo MÁS peligroso:
    # HTTP 200, run en verde y cero avisos para siempre. Cuenta como caída.
    if not products and site_state:
        log.warning(f"  {name}: 0 productos y antes tenía {len(site_state)} — feed roto?")
        _record_health(state, name, ok=False, error="0 productos (antes había catálogo)")
        return [], 0

    _record_health(state, name, ok=True)

    if include_keywords or exclude_keywords:
        filtered = [
            p for p in products
            if (not include_keywords or matches_keywords(p["title"], include_keywords))
            and not (exclude_keywords and matches_keywords(p["title"], exclude_keywords))
        ]
        log.info(f"  {name}: {len(products)} detectados, {len(filtered)} relevantes")
        products = filtered
    else:
        log.info(f"  {name}: {len(products)} productos encontrados")

    if not products:
        return [], 0

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
        return [], 0

    # Una tienda no publica 20 novedades reales en una pasada de 2 minutos. Si pasa,
    # es que ha recatalogado, ha cambiado el orden de la colección o hemos ampliado
    # la cobertura (limit/paginación). Se absorbe en silencio con un aviso de 1 línea.
    n_new = sum(1 for a in alerts if a["alert_type"] == "new")
    if n_new > resync_threshold:
        log.info(f"  {name}: {n_new} nuevos de golpe > umbral {resync_threshold}, "
                 f"re-sync silencioso")
        restocks = [a for a in alerts if a["alert_type"] == "restock"]
        return restocks, n_new

    for p in alerts:
        p["high_value"] = matches_keywords(p["title"], high_value_keywords)
        p["promo"] = matches_keywords(p["title"], promo_keywords)
    alerts.sort(key=product_rank)
    return alerts, 0


def format_notification(site_name, priority, alerts):
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    lines = [f"🆕 {header}<b>{html_mod.escape(site_name)}</b> {emoji}\n"]
    for p in alerts[:MAX_ITEMS_PER_MESSAGE]:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        if p.get("high_value"):
            tag = f"🚨 {tag}"
        elif p.get("promo"):
            tag = f"🎁 {tag}"
        stock = "" if p["in_stock"] else " ⚠️ AGOTADO"
        # Escapado obligatorio: un '&' o un '<' en el título rompe el parse_mode
        # HTML y Telegram devuelve 400 -> el aviso entero se perdería.
        lines.append(f"• {tag}{stock} <b>{html_mod.escape(p['title'])}</b>")
        lines.append(f"  💰 {html_mod.escape(p['price'])}")
        if p["link"]:
            lines.append(f"  🔗 {p['link']}")
        lines.append("")
    if len(alerts) > MAX_ITEMS_PER_MESSAGE:
        lines.append(f"... y {len(alerts) - MAX_ITEMS_PER_MESSAGE} más")
    return "\n".join(lines)


def run_once(priority_filter=None):
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]
    if bot_token in ("TU_BOT_TOKEN_AQUI", "USE_GITHUB_SECRET", "", None):
        log.error("⚠️ Configura Telegram: python3 setup_telegram.py")
        sys.exit(1)

    sites = config["sites"]
    if priority_filter:
        sites = [s for s in sites if s.get("priority", "medium") == priority_filter]
        log.info(f"Filtro de prioridad activo: solo '{priority_filter}' ({len(sites)} sitios)")
    sites_sorted = sorted(sites, key=lambda s: 0 if s.get("priority") == "high" else 1)

    all_alerts = {}
    resyncs = []
    # El uid de Shopify es md5(id_de_producto + título): el MISMO artículo listado en
    # dos colecciones de la MISMA tienda (Pokemillon cajas/sobres, Sunny Store,
    # Card Binder...) comparte uid. Con este set solo avisa la entrada de mayor
    # prioridad y no llegan dos Telegram por el mismo producto.
    seen_uids = set()
    for site_cfg in sites_sorted:
        alerts, n_resync = check_site(site_cfg, state, config)
        alerts = [a for a in alerts if a["uid"] not in seen_uids]
        seen_uids.update(a["uid"] for a in alerts)
        if alerts:
            all_alerts[site_cfg["name"]] = (site_cfg.get("priority", "medium"), alerts)
        if n_resync:
            resyncs.append((site_cfg["name"], n_resync))

    # Avisos de SALUD (tiendas caídas/bloqueadas): solo en las transiciones, no spamea
    health_msgs = _collect_health_alerts(state, config)
    save_state(state)

    for msg in health_msgs:
        send_telegram(bot_token, chat_id, msg)

    if resyncs:
        detail = "\n".join(
            f"• <b>{html_mod.escape(n)}</b>: {c} listados" for n, c in resyncs
        )
        send_telegram(
            bot_token, chat_id,
            "🔁 <b>Re-sincronización de catálogo</b>\n\n"
            f"{detail}\n\n"
            "Aparecieron de golpe (recatalogación o más cobertura del bot), "
            "así que se han absorbido sin detallar. A partir de ahora solo "
            "notifico lo nuevo de verdad."
        )

    if not all_alerts:
        if not health_msgs and not resyncs:
            log.info("Sin alertas en esta revisión")
        return

    for site_name, (priority, alerts) in all_alerts.items():
        msg = format_notification(site_name, priority, alerts)
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {site_name} [{priority}]: {n_new} nuevos + {n_re} restock")
        send_telegram(bot_token, chat_id, msg)


def run_loop(priority_filter=None):
    config = load_config()
    interval = config.get("check_interval_minutes", 15) * 60
    log.info(f"Monitor en bucle (cada {interval // 60} min, filtro={priority_filter or 'todos'})")
    while True:
        run_once(priority_filter=priority_filter)
        log.info(f"Esperando {interval // 60} minutos...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor One Piece Card Game")
    parser.add_argument("--loop", action="store_true", help="bucle infinito local")
    parser.add_argument("--priority", choices=["high", "medium"], help="solo tiendas de esta prioridad")
    args = parser.parse_args()
    if args.loop:
        run_loop(priority_filter=args.priority)
    else:
        run_once(priority_filter=args.priority)
