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

Cómo se decide si un producto está AGOTADO, según el motor:
- Shopify: `available` por variante. Es la verdad del inventario. Las preventas
  salen `available: false` hasta que abren, así que abrir reserva = 🔄 RESTOCK.
- WooCommerce Store API: `is_in_stock` (+ `stock_availability.text`, que dice
  cuántas quedan, y `is_on_backorder`). `is_purchasable` NO sirve: devuelve true
  incluso con el producto agotado.
- HTML: `detect_html_stock_signal` en tres señales negativas (clase de agotado en
  cualquier descendiente, carrito deshabilitado, texto en varios idiomas) y una
  positiva (carrito activo). Las negativas MANDAN: hay temas que pintan un
  "Add to Cart" activo también en lo agotado. Si un listado no enseña carrito en
  ningún producto, no informa del stock y se asume disponible.
"""

import json
import hashlib
import re
import time
import logging
import os
import sys
import argparse
import traceback
import html as html_mod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

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
# Vocabulario para leer el stock en los listados HTML. "agotad" cubre
# agotado/agotada/agotados/agotadas; el resto son las formas que de verdad usan
# las tiendas del config (La Cueva Roja dice "Fuera de stock", no "out of stock").
OOS_KEYWORDS = [
    "agotad", "fuera de stock", "sin existencias", "sin stock",
    "no disponible", "sold out", "out of stock", "vendido",
    "rupture de stock", "esgotado", "esaurito", "ausverkauft", "uitverkocht",
]
# El marcador casi nunca esta en la raiz de la miniatura: PrestaShop lo cuelga de
# un <span class="product-flag out_of_stock"> hijo, y Dungeon Marvels usa
# "soy_agotado" en el propio boton. Ojo al guion BAJO: la lista vieja solo
# miraba "out-of-stock" con guion y por eso La Cueva Roja salia siempre disponible.
OOS_CLASS_TOKENS = [
    "out-of-stock", "out_of_stock", "outofstock",
    "sold-out", "sold_out", "soldout",
    "agotado", "product-unavailable", "no-stock", "nostock",
]
CART_CLASS_TOKENS = ["add-to-cart", "add_to_cart", "addtocart", "ajax_add_to_cart"]
CART_TEXT_TOKENS = [
    "anadir al carrito", "añadir al carrito", "añadir a la cesta",
    "add to cart", "add to basket", "aggiungi al carrello", "ajouter au panier",
]

HEALTH_KEY = "__health__"  # clave reservada en state para la salud (no es un sitio)
HEALTH_META_KEY = "__health_meta__"  # clave reservada: control del resumen de salud
SIG_KEY = "__sig__"        # clave reservada: firma de URL+filtros con la que se vio cada tienda
RUN_META_KEY = "__run__"   # clave reservada: última pasada completada (lo lee el heartbeat)
# Todas las claves reservadas empiezan por "__": así heartbeat y poda las
# distinguen de las tiendas sin tener que enumerarlas.
CRASH_FLAG = BASE_DIR / ".crash_notified"  # evita repetir el aviso de caída en cada pasada
DEFAULT_RECOVER_PASSES = 3  # pasadas buenas SEGUIDAS para dar por recuperada una tienda
DEFAULT_HEALTH_FAIL_THRESHOLD = 10  # fallos seguidos antes de avisar de bloqueo/caída
DEFAULT_DIGEST_COOLDOWN_MIN = 30    # minutos mínimos entre dos resúmenes de salud
DEFAULT_EMPTY_THRESHOLD = 10        # pasadas a 0 productos (habiendo tenido catálogo) antes de avisar
DEFAULT_MAX_WORKERS = 12            # peticiones simultáneas
DEFAULT_TIMEOUT = 20                # segundos por petición
# Una tienda caída no debe encarecer TODAS las pasadas: con 2 intentos y 20s de
# timeout, una sola tienda que agota el timeout mete 42s en cada pasada.
DEFAULT_DEGRADED_AFTER = 5          # fallos seguidos -> timeout corto y 1 solo intento
DEFAULT_DEGRADED_TIMEOUT = 8        # segundos para una tienda ya degradada
DEFAULT_BACKOFF_AFTER = 20          # fallos seguidos -> además se comprueba 1 de cada N pasadas
DEFAULT_BACKOFF_EVERY = 10          # pasadas que se salta una tienda en backoff
DEFAULT_AVALANCHE_STORES = 8        # tiendas con alertas a partir de las cuales se agrupa
DEFAULT_MAX_ALERTS_AVALANCHE = 40   # productos como mucho en el mensaje de avalancha
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


def _record_health(state, name, ok, error=None, empty=False):
    """Cuenta fallos consecutivos por tienda dentro del propio state.

    `empty` = la tienda respondió bien pero con 0 productos teniendo catálogo antes.
    NO es un fallo: una colección de preventas se vacía de verdad cuando se sirve
    lo reservado, y contarlo como caída la mandaba a backoff (1 de cada 10 pasadas)
    justo cuando más importa ver lo primero que suba. Se lleva aparte en
    `empty_streak` para avisar de tienda "ciega" sin degradar el chequeo.
    """
    health = state.setdefault(HEALTH_KEY, {})
    h = health.setdefault(name, {"fails": 0, "alerted": False, "last_error": None})
    h["skips"] = 0  # se acaba de comprobar: el contador de saltos del backoff se reinicia
    if ok:
        h["fails"] = 0
        h["last_error"] = None
        # Racha de pasadas buenas: una tienda intermitente (Friki Galaxy) no se da
        # por recuperada con UNA respuesta suelta (ver _collect_health_alerts).
        h["ok_streak"] = h.get("ok_streak", 0) + 1
        h["empty_streak"] = h.get("empty_streak", 0) + 1 if empty else 0
    else:
        h["fails"] = h.get("fails", 0) + 1
        h["ok_streak"] = 0
        h["last_error"] = error


def _fmt_store_list(names, limit=10):
    """Lista compacta separada por · y recortada, para no llenar la pantalla."""
    shown = [html_mod.escape(n) for n in names[:limit]]
    extra = len(names) - len(shown)
    return " · ".join(shown) + (f" <i>y {extra} más</i>" if extra else "")


def _collect_health_alerts(state, config):
    """Devuelve (mensajes, deshacer): como mucho UN resumen por pasada.

    Antes salía un mensaje por tienda y por transición; con ~130 tiendas, un bache
    de red del runner producía decenas de mensajes de caída y otras tantas de
    recuperación, y entre ese ruido se perdía un restock de verdad. Ahora todas
    las transiciones se agrupan, el mensaje va en silencio (sin notificación en el
    móvil) y hay un tiempo mínimo entre resúmenes. `deshacer` restaura los flags
    si el envío falla, para que el aviso se reintente en vez de perderse.
    """
    threshold = config.get("health_fail_threshold", DEFAULT_HEALTH_FAIL_THRESHOLD)
    cooldown = config.get("health_digest_cooldown_minutes", DEFAULT_DIGEST_COOLDOWN_MIN) * 60
    health = state.get(HEALTH_KEY, {})
    meta = state.setdefault(HEALTH_META_KEY, {})

    empty_threshold = config.get("health_empty_threshold", DEFAULT_EMPTY_THRESHOLD)
    caidas, recuperadas, ciegas, vuelven, restores = [], [], [], [], []

    def flip(h, key, value):
        prev = h.get(key)
        restores.append(lambda: h.__setitem__(key, prev))
        h[key] = value

    for name, h in sorted(health.items()):
        fails = h.get("fails", 0)
        if fails >= threshold and not h.get("alerted", False):
            flip(h, "alerted", True)
            caidas.append(name)
        # Histéresis: una tienda intermitente generaba un resumen por cada vaivén
        # (5 en una noche). Solo se da por recuperada tras varias pasadas buenas
        # SEGUIDAS; si recae antes, sigue "caída" y no se vuelve a avisar.
        elif fails == 0 and h.get("alerted", False) and \
                h.get("ok_streak", 0) >= config.get("health_recover_passes", DEFAULT_RECOVER_PASSES):
            flip(h, "alerted", False)
            recuperadas.append(name)

        empty = h.get("empty_streak", 0)
        if empty >= empty_threshold and not h.get("empty_alerted", False):
            flip(h, "empty_alerted", True)
            ciegas.append((name, empty))
        elif empty == 0 and h.get("empty_alerted", False):
            flip(h, "empty_alerted", False)
            vuelven.append(name)

    def deshacer():
        for r in restores:
            r()

    if not (caidas or recuperadas or ciegas or vuelven):
        return [], deshacer

    ahora = time.time()
    # Una tienda vacía de golpe es rara y puede ser un feed roto: se salta la espera.
    if not ciegas and ahora - meta.get("last_digest", 0) < cooldown:
        deshacer()
        return [], (lambda: None)

    n_total = len(health)
    lineas = []
    for name, streak in ciegas:
        lineas.append(f"👻 <b>{html_mod.escape(name)}</b>: responde OK pero lleva {streak} "
                      f"pasadas a <b>0 productos</b> (antes tenía catálogo). Puede ser una "
                      f"colección vaciada de verdad o un feed roto: revisa la URL.")
    if caidas:
        cabecera = f"⚠️ <b>{len(caidas)} tiendas no responden</b>"
        if n_total and len(caidas) >= max(5, n_total // 3):
            cabecera += " — son muchas a la vez, probablemente sea la red del runner"
        lineas.append(f"{cabecera}\n{_fmt_store_list(caidas)}")
    if recuperadas:
        lineas.append(f"✅ <b>Recuperadas ({len(recuperadas)})</b>: {_fmt_store_list(recuperadas)}")
    if vuelven:
        lineas.append(f"✅ <b>Vuelven a dar productos</b>: {_fmt_store_list(vuelven)}")

    flip(meta, "last_digest", ahora)
    return ["\n\n".join(lineas)], deshacer


def build_headers(user_agent, is_api=False):
    # Accept-Encoding sin "br": brotli no siempre está instalado y dejaría el
    # cuerpo sin descomprimir (el parseo JSON fallaría con "Expecting value").
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
    }
    if is_api:
        # OJO: aquí NO se manda Accept-Language. Las tiendas Shopify con "Markets"
        # activado resuelven el mercado por ese header y, si pides es-ES en una
        # tienda US/UK, devuelven {"products": []} con HTTP 200 -> la tienda queda
        # CIEGA sin que salte ningún error. Sin el header sirven el catálogo entero.
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
        # En HTML sí interesa el idioma (tiendas ES con páginas traducidas).
        headers.update({
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })
    return headers


def _is_disabled(el):
    if el.has_attr("disabled") or el.get("aria-disabled") == "true":
        return True
    return "disabled" in " ".join(el.get("class", [])).lower()


def _cart_controls(item):
    """Botones/enlaces de "anadir al carrito" dentro de la miniatura."""
    controls = []
    for el in item.select("button, a, input"):
        classes = " ".join(el.get("class", [])).lower()
        # La lista de deseos tambien es un boton con "add" en la clase: fuera.
        if "wishlist" in classes or "compare" in classes:
            continue
        text = el.get_text(" ", strip=True).lower()
        if any(t in classes for t in CART_CLASS_TOKENS) or any(t in text for t in CART_TEXT_TOKENS):
            controls.append(el)
    return controls


def detect_html_stock_signal(item):
    """Tri-estado: False = agotado, True = en stock, None = el listado no lo dice.

    Las senales NEGATIVAS mandan sobre las positivas: hay temas (Dungeon Marvels)
    que pintan un "Add to Cart" activo tambien en los productos agotados, asi que
    fiarse del boton positivo antes de mirar los marcadores seria un error.
    """
    # 1. Clase de agotado en la propia miniatura o en CUALQUIER descendiente.
    for el in [item] + item.select("[class]"):
        classes = " ".join(el.get("class", [])).lower()
        if any(t in classes for t in OOS_CLASS_TOKENS):
            return False

    # 2. Boton de carrito deshabilitado: la senal mas fiable y sin idioma.
    controls = _cart_controls(item)
    if controls and all(_is_disabled(c) for c in controls):
        return False

    # 3. Texto de agotado, en los idiomas de las tiendas del config.
    if any(k in item.get_text(" ", strip=True).lower() for k in OOS_KEYWORDS):
        return False

    # 4. Marca POSITIVA: hay carrito y esta activo.
    if controls:
        return True
    return None


def complete_truncated_title(title, link):
    """Completa un título cortado por el tema ("OPCG: Case Booster Box...") con el
    slug del enlace, que lleva el nombre entero (".../opcg-case-booster-box-peb01-en").

    Sin esto el aviso no dice qué producto es y las keywords (case, box...) pueden
    quedarse fuera del corte. NO se usa el alt de la imagen: en La Cueva Roja es
    genérico o viene copiado de otros productos. El uid HTML sale del enlace, así
    que cambiar el título no hace parecer nuevo al producto.
    """
    # ".." y no "...": Distrito Zero corta con dos puntos ("NARUTO..").
    if not link or not (title.endswith("..") or title.endswith("…")):
        return title
    slug = urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.html?$", "", slug)
    slug = re.sub(r"^\d+-", "", slug)  # PrestaShop antepone a veces el id: "90647-nombre"
    if not slug:
        return title
    base = title.rstrip(".…").rstrip()
    return f"{base} ({slug.replace('-', ' ')})"


def extract_products_html(html, site_cfg):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(site_cfg["selector"])
    signals = [detect_html_stock_signal(it) for it in items]

    # Calibracion POR LISTADO: si el tema pinta carrito activo en algun producto,
    # entonces uno que no lo tenga esta agotado. Si no lo pinta en NINGUNO (Isekai
    # no saca botones en la miniatura), el listado no informa del stock y se asume
    # disponible, que es lo unico que se puede hacer sin inventarse datos.
    tiene_marca_positiva = any(s is True for s in signals)
    # ...pero solo si el listado es mayoritariamente explícito. Isekai pinta
    # "añadir al carrito" únicamente en los productos SIN variantes (3 de 21) y el
    # resto, en stock según su ficha, salía agotado: sus novedades no avisaban.
    if sum(1 for s in signals if s is not None) * 2 < len(signals):
        tiene_marca_positiva = False

    products = []
    for item, signal in zip(items, signals):
        title_el = item.select_one(site_cfg["title_selector"])
        title = title_el.get_text(strip=True) if title_el else "Sin título"
        link_el = item.select_one(site_cfg["link_selector"])
        link = link_el.get("href", "") if link_el else ""
        if link and not link.startswith("http"):
            link = urljoin(site_cfg["url"], link)
        title = complete_truncated_title(title, link)
        price_el = item.select_one(site_cfg["price_selector"])
        price = price_el.get_text(strip=True) if price_el else "Precio no disponible"
        in_stock = signal if signal is not None else not tiene_marca_positiva
        # uid ESTABLE: el enlace, que sobrevive a que la tienda retoque el título.
        # Antes el uid incluía el título, así que un retoque hacía parecer NUEVO al
        # producto y el mismo enlace se avisaba otra vez horas después.
        uid = hashlib.md5((link or f"{title}").encode()).hexdigest()
        legacy = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title, "link": link,
                         "price": price, "in_stock": in_stock, "stock_text": "",
                         "backorder": False})

    if items:
        n_oos = sum(1 for p in products if not p["in_stock"])
        n_ciegos = sum(1 for s in signals if s is None)
        log.info(f"  stock HTML: {len(items) - n_oos} disponibles / {n_oos} agotados"
                 + (f" ({n_ciegos} sin senal, marca positiva en el listado: "
                    f"{'si' if tiene_marca_positiva else 'no'})" if n_ciegos else ""))
        if n_oos == len(items) and len(items) > 5:
            # Puede ser real, pero tambien un cambio de tema que marque todo agotado:
            # con notify_only_in_stock eso deja la tienda muda sin fallar.
            log.warning(f"  el listado entero sale AGOTADO ({len(items)}), revisar si es real")

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
            pid = item.get("id", "")
            uid = hashlib.md5(f"shopify:{pid}".encode()).hexdigest() if pid else \
                hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            products.append({"uid": uid, "legacy_uid": legacy, "title": title, "link": link,
                             "price": price, "in_stock": in_stock, "stock_text": "",
                             "backorder": False})
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
        # La Store API dice cuantas quedan ("Solo quedan 1 disponibles"), que en un
        # bot de restock vale tanto como el propio aviso. is_purchasable NO sirve:
        # las cuatro tiendas lo devuelven true incluso con el producto agotado.
        availability = item.get("stock_availability") or {}
        stock_text = ""
        if isinstance(availability, dict):
            stock_text = html_mod.unescape(availability.get("text") or "")
        pid = item.get("id", "")
        uid = hashlib.md5(f"woo:{pid}".encode()).hexdigest() if pid else \
            hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title, "link": link,
                         "price": price, "in_stock": in_stock, "stock_text": stock_text,
                         "backorder": bool(item.get("is_on_backorder"))})
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


def send_telegram(bot_token, chat_id, message, silent=False):
    """Envía un mensaje (troceándolo si hace falta) y devuelve True/False.

    Devolver el resultado es lo que permite NO dar por avisado un producto cuyo
    mensaje no llegó: quien llama solo persiste el state si esto devuelve True.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    todo_ok = True
    for part in _split_message(message):
        payload = {"chat_id": chat_id, "text": part, "parse_mode": "HTML",
                   "disable_web_page_preview": False}
        if silent:
            # Ruido de mantenimiento: llega al chat pero no hace sonar el móvil.
            payload["disable_notification"] = True
        enviado = False
        for attempt in range(4):
            try:
                resp = requests.post(url, json=payload, timeout=20)
            except requests.RequestException as e:
                log.warning(f"Telegram, error de red ({e}), reintento {attempt + 1}/4")
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 200:
                log.info("Notificación Telegram enviada")
                enviado = True
                break
            if resp.status_code == 429:
                # Rate limit: Telegram dice cuántos segundos esperar.
                try:
                    wait = int(resp.json()["parameters"]["retry_after"])
                except (ValueError, KeyError, TypeError):
                    wait = 5
                log.warning(f"Telegram rate limit, esperando {wait}s")
                time.sleep(min(wait, 60) + 1)
                continue
            if 500 <= resp.status_code < 600:
                log.warning(f"Telegram {resp.status_code}, reintento {attempt + 1}/4")
                time.sleep(2 * (attempt + 1))
                continue
            # 400 y demás: el mensaje es inválido, reintentar no arregla nada
            log.error(f"Error enviando Telegram: {resp.status_code} {resp.text[:300]}")
            break
        if not enviado:
            log.error("Telegram: aviso NO enviado")
            todo_ok = False
    return todo_ok


def is_loud(alerts):
    """¿Merece este aviso hacer sonar el móvil? Solo si lleva algo marcado 🚨/🎁
    (case, booster box, promo). Un accesorio suelto llega al chat en silencio."""
    return any(a.get("high_value") or a.get("promo") for a in alerts)


def requested_cap(url):
    """Tope de resultados que pide la URL (limit / per_page / resultsPerPage).

    Si el listado vuelve LLENO hasta el tope, lo que sobra entra y sale entre
    pasadas segun el orden de la tienda: ahi no se puede deducir nada de que un
    producto "desaparezca".
    """
    query = parse_qs(urlparse(url).query)
    for key, values in query.items():
        if key.lower() in ("limit", "per_page", "resultsperpage") and values:
            try:
                return int(values[0])
            except (TypeError, ValueError):
                pass
    return None


def matches_keywords(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def normalize_state(raw):
    if isinstance(raw, list):
        return {uid: {"in_stock": True} for uid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def mark_priority(p, config):
    """Marca 🚨 (case/caja/box) y 🎁 (promo). `priority_exclude` quita la marca (no
    el producto) a los accesorios que comparten palabra con el sellado: "deck box",
    "card case", "caja de mazo", "caja protectora"... Siguen llegando, en silencio."""
    if matches_keywords(p["title"], config.get("priority_exclude", [])):
        p["high_value"] = p["promo"] = False
        return p
    p["high_value"] = matches_keywords(p["title"], config.get("high_value_keywords", []))
    p["promo"] = matches_keywords(p["title"], config.get("promo_keywords", []))
    return p


# Lo que decide QUÉ productos ve el bot en una tienda. Si cambia (URL nueva, más
# resultados por página, filtros nuevos...), aparecen de golpe productos que ya
# existían y que el state no conocía -> tanda de falsos "NUEVO". Antes había que
# acordarse de RENOMBRAR la tienda; ahora la firma lo detecta y los absorbe.
SITE_SIG_FIELDS = ("url", "type", "selector", "title_selector", "link_selector",
                   "include_keywords", "exclude_keywords")
# Subir cuando un cambio del MOTOR amplíe lo que se ve.
COVERAGE_VERSION = 1


def site_signature(site_cfg):
    data = {"site": {k: site_cfg.get(k) for k in SITE_SIG_FIELDS}, "engine": COVERAGE_VERSION}
    return hashlib.md5(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def product_rank(p):
    """Orden de interés: 0 = case/booster box, 1 = promo, 2 = resto."""
    if p.get("high_value"):
        return 0
    if p.get("promo"):
        return 1
    return 2


def plan_fetch(name, health, config):
    """Decide cómo tratar a una tienda según sus fallos seguidos.

    Devuelve (comprobar, timeout, intentos). Una tienda sana va con el timeout
    normal y 2 intentos; una que lleva fallando baja a timeout corto y 1 intento
    (deja de lastrar la pasada entera); y una caída de forma persistente pasa a
    comprobarse 1 de cada N pasadas, para que siga pudiendo auto-recuperarse sin
    costar una petición por pasada. En cuanto responde vuelve al trato normal.
    """
    h = health.get(name, {})
    fails = h.get("fails", 0)
    timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)
    if fails < config.get("degraded_fail_threshold", DEFAULT_DEGRADED_AFTER):
        return True, timeout, 2
    degradado = (True, config.get("degraded_timeout_seconds", DEFAULT_DEGRADED_TIMEOUT), 1)
    if fails < config.get("backoff_fail_threshold", DEFAULT_BACKOFF_AFTER):
        return degradado
    if h.get("skips", 0) >= config.get("backoff_every_passes", DEFAULT_BACKOFF_EVERY):
        return degradado
    return (False, 0, 0)


def fetch_site(site_cfg, config, timeout=None, attempts=2):
    """SOLO red y parseo. No toca el state, así puede correr en paralelo.

    Devuelve (site_cfg, productos|None, error). Sacar la red fuera del state es lo
    que permite lanzar todas las tiendas a la vez: la pasada baja de ~1 min (y
    varios minutos si hay tiendas caídas reintentando) a unos segundos.
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    is_api = site_cfg.get("type", "html") == "api"
    if timeout is None:
        timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)
    log.info(f"[{site_cfg.get('priority', 'medium').upper()}] {name}: {url}")

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"

    last_err = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if is_api:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "json" not in ctype:
                    # Cloudflare u otra defensa devolvió HTML en vez del JSON
                    raise ValueError(f"respuesta no-JSON (Content-Type: {ctype or 'desconocido'})")
                return site_cfg, extract_products_api(
                    resp.json(), base_url=url, currency=site_cfg.get("currency", "€")
                ), None
            return site_cfg, extract_products_html(resp.text, site_cfg), None
        except Exception as e:
            last_err = e
            if attempt + 1 < attempts:
                time.sleep(2)
    log.warning(f"  {name} no disponible: {last_err}")
    return site_cfg, None, str(last_err)


def process_site(site_cfg, products, state, config):
    """Devuelve (alertas, nº absorbido por re-sync, nuevo state del sitio).

    NO escribe en `state[name]`: quien llama solo lo persiste si el aviso de esta
    tienda llegó a Telegram. Si el envío falla, el state viejo se conserva y el
    producto se vuelve a avisar en la siguiente pasada en vez de darse por visto.
    (Sí toca la salud, que es independiente y tiene su propio deshacer.)
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    # Las preventas se publican agotadas ("reserva no abierta": La Cueva Roja tiene
    # el case PEB01 y los ST39-44 así, con fecha de 2027). Con esto, un listado NUEVO
    # marcado 🚨/🎁 avisa ya; al abrirse la reserva llegará otra vez como RESTOCK.
    notify_new_oos_priority = config.get("notify_new_oos_priority", False)
    resync_threshold = config.get("resync_threshold", DEFAULT_RESYNC_THRESHOLD)
    # Filtros propios de la tienda: solo para feeds mixtos (preventas de varios
    # juegos, colecciones que mezclan singles/merch con sellado).
    include_keywords = site_cfg.get("include_keywords", [])
    exclude_keywords = site_cfg.get("exclude_keywords", [])

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    # URL o filtros cambiados desde la última vez (o primer despliegue con firmas):
    # lo que aparezca ahora y no estuviera en el state ya existía, solo que no se
    # veía. Se absorbe en silencio; los restocks de lo conocido SÍ se avisan.
    rebaseline = not is_first_run and \
        state.get(SIG_KEY, {}).get(name) != site_signature(site_cfg)
    # COPIA: normalize_state devuelve el mismo dict que está dentro de `state`
    # cuando ya es un dict. Sin copiar, marcar un producto como visto mutaría el
    # state aunque luego el envío a Telegram fallara, y el aviso se perdería igual.
    site_state = dict(normalize_state(raw_prev))

    # Quedarse a 0 productos donde antes había catálogo puede ser un feed roto
    # (HTTP 200, run en verde y cero avisos para siempre) o una colección de
    # preventas vaciada de verdad. NO cuenta como caída: degradarla a backoff
    # retrasaría ~10 pasadas lo primero que suba. Va a `empty_streak`, que avisa
    # de tienda "ciega" si se prolonga.
    if not products and site_state:
        log.warning(f"  {name}: 0 productos y antes tenía {len(site_state)} — ¿vaciada o feed roto?")
        _record_health(state, name, ok=True, empty=True)
        return [], 0, site_state

    _record_health(state, name, ok=True)

    # El tope se mide sobre el listado CRUDO, antes de filtrar por keywords.
    cap = requested_cap(url)
    truncado = cap is not None and len(products) >= cap

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
    if truncado:
        log.warning(f"  {name}: listado LLENO hasta el tope ({cap}), puede haber "
                    f"productos fuera; no se miran desapariciones")

    if not products:
        return [], 0, site_state

    alerts = []
    absorbidos = 0
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        if prev is None and p.get("legacy_uid"):
            # Migración silenciosa del esquema viejo de uid: si el producto ya estaba
            # con la clave antigua se hereda su estado y se reescribe con la nueva.
            # Sin esto, el cambio de esquema haría parecer NUEVO todo el catálogo.
            prev = site_state.pop(p["legacy_uid"], None)
        if prev is None:
            if rebaseline:
                absorbidos += 1
            elif not is_first_run:
                marcado = mark_priority(dict(p), config)
                prioritario = marcado["high_value"] or marcado["promo"]
                if p["in_stock"] or not notify_only_in_stock or \
                        (notify_new_oos_priority and prioritario):
                    alerts.append({**p, "alert_type": "new"})
        else:
            if not prev.get("in_stock", True) and p["in_stock"]:
                alerts.append({**p, "alert_type": "restock"})
        site_state[uid] = {"in_stock": p["in_stock"]}

    if is_first_run:
        log.info(f"  {name}: primera ejecución, guardando baseline de {len(products)} productos")
        return [], 0, site_state
    if rebaseline:
        log.info(f"  {name}: URL/filtros cambiados -> re-baseline silencioso "
                 f"({absorbidos} productos que antes no se veían)")

    # Tiendas que OCULTAN del listado lo que se agota (Isekai Alcorcón): allí el
    # restock es que el producto REAPARECE. Como el uid no cambia y en el state
    # seguía como disponible, no saltaba nada. Marcándolo agotado al desaparecer,
    # la reaparición dispara el 🔄 RESTOCK por el camino normal.
    # No se aplica con el listado al tope (rotarían productos y darían falsos
    # restocks) ni cuando desaparece media tienda de golpe (listado anómalo).
    if not truncado:
        vistos = {p["uid"] for p in products} | {p.get("legacy_uid") for p in products}
        desaparecidos = [
            uid for uid, prev in site_state.items()
            if uid not in vistos and prev.get("in_stock", True)
        ]
        limite = max(5, len(products) // 2)
        if len(desaparecidos) > limite:
            log.warning(f"  {name}: {len(desaparecidos)} productos desaparecidos de golpe "
                        f"(> {limite}), listado anómalo: no se marcan")
        elif desaparecidos:
            for uid in desaparecidos:
                site_state[uid] = {"in_stock": False, "gone": True}
            log.info(f"  {name}: {len(desaparecidos)} desaparecidos del listado, "
                     f"marcados agotados (avisarán si reaparecen)")

    for p in alerts:
        mark_priority(p, config)

    # Una tienda no publica 20 novedades reales en una pasada de 2 minutos. Si pasa,
    # es que ha recatalogado, ha cambiado el orden de la colección o hemos ampliado
    # la cobertura (limit/paginación). Lo flojo se absorbe en silencio con un aviso
    # de 1 línea, pero lo marcado 🚨/🎁 se avisa SIEMPRE: el día que una tienda sube
    # el set entero de golpe (cajas, case, sobres, starters...) el case iba dentro
    # del re-sync y se perdía.
    n_new = sum(1 for a in alerts if a["alert_type"] == "new")
    if n_new > resync_threshold:
        keep = [a for a in alerts
                if a["alert_type"] == "restock" or a["high_value"] or a["promo"]]
        n_absorbidos = n_new - sum(1 for a in keep if a["alert_type"] == "new")
        log.info(f"  {name}: {n_new} nuevos de golpe > umbral {resync_threshold}, "
                 f"re-sync: {n_absorbidos} absorbidos, {len(keep)} avisados (🚨/🎁/restock)")
        keep.sort(key=product_rank)
        return keep, n_absorbidos, site_state

    alerts.sort(key=product_rank)
    return alerts, 0, site_state


def format_avalanche(entradas, config):
    """UN solo mensaje cuando muchas tiendas avisan en la misma pasada.

    El día que abra una preventa gorda disparan decenas de tiendas casi a la vez:
    con un mensaje por tienda, el case se pierde entre cuarenta avisos de fundas.
    Aquí va una línea por producto, ordenadas por importancia y con la tienda al
    lado, así lo gordo queda arriba del todo.
    """
    max_items = config.get("max_alerts_avalanche", DEFAULT_MAX_ALERTS_AVALANCHE)
    items = [(a, name) for name, _, alerts, _, _ in entradas for a in alerts]
    # El dedup por uid ya lo hace run_once; aquí solo se ordena y se corta.
    items.sort(key=lambda t: (product_rank(t[0]), 0 if t[0]["alert_type"] == "restock" else 1))
    total = len(items)
    shown = items[:max_items]

    lines = [f"🆕 <b>{len(entradas)} tiendas con novedades</b> ({total} productos)\n"]
    for a, tienda in shown:
        mark = "🚨" if a.get("high_value") else ("🎁" if a.get("promo") else "•")
        tag = "🔄" if a["alert_type"] == "restock" else "🆕"
        stock = "" if a["in_stock"] else " ⚠️ AGOTADO"
        lines.append(f"{mark} {tag} <b>{html_mod.escape(a['title'])}</b>{stock}")
        precio = f"  💰 {html_mod.escape(a['price'])} — <i>{html_mod.escape(tienda)}</i>"
        if a.get("backorder"):
            precio += " ⏳ bajo pedido"
        lines.append(precio)
        if a["link"]:
            lines.append(f"  🔗 {a['link']}")
        lines.append("")
    if total > len(shown):
        lines.append(f"... y {total - len(shown)} productos más")
    return "\n".join(lines)


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
        precio = f"  💰 {html_mod.escape(p['price'])}"
        if p.get("backorder"):
            precio += " ⏳ bajo pedido"
        lines.append(precio)
        # "Solo quedan 1 disponibles" — lo da la Store API de WooCommerce. Solo
        # tiene sentido enseñarlo si está disponible: si no, ya lo dice AGOTADO.
        if p.get("stock_text") and p["in_stock"]:
            lines.append(f"  📊 {html_mod.escape(p['stock_text'])}")
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

    prune_state(state, config)

    sites = config["sites"]
    if priority_filter:
        sites = [s for s in sites if s.get("priority", "medium") == priority_filter]
        log.info(f"Filtro de prioridad activo: solo '{priority_filter}' ({len(sites)} sitios)")
    sites_sorted = sorted(sites, key=lambda s: 0 if s.get("priority") == "high" else 1)

    # --- 1) Red en PARALELO (sin tocar el state) ---
    # A las tiendas que llevan fallando se les acorta el timeout, y a las caídas de
    # forma persistente se las salta la mayoría de pasadas: así una sola tienda
    # muerta deja de marcar el ritmo de todas las pasadas.
    health = state.get(HEALTH_KEY, {})
    plan = {s["name"]: plan_fetch(s["name"], health, config) for s in sites_sorted}
    a_consultar = [s for s in sites_sorted if plan[s["name"]][0]]
    saltadas = [s["name"] for s in sites_sorted if not plan[s["name"]][0]]
    for nombre in saltadas:
        h = health.setdefault(nombre, {})
        h["skips"] = h.get("skips", 0) + 1
    degradadas = [s["name"] for s in a_consultar if plan[s["name"]][2] == 1]

    workers = max(1, min(config.get("max_workers", DEFAULT_MAX_WORKERS), len(a_consultar) or 1))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda s: fetch_site(s, config, timeout=plan[s["name"]][1], attempts=plan[s["name"]][2]),
            a_consultar))
    extra = ""
    if degradadas:
        extra += f", {len(degradadas)} con timeout corto por fallos"
    if saltadas:
        extra += f", {len(saltadas)} saltadas (caídas persistentes)"
    log.info(f"{len(a_consultar)} tiendas consultadas en {time.time() - t0:.1f}s "
             f"({workers} hilos){extra}")

    # --- 2) Proceso SECUENCIAL contra el state (evita carreras) ---
    # El uid de Shopify es md5("shopify:" + id_de_producto): el MISMO artículo listado en
    # dos colecciones de la MISMA tienda comparte uid. Con este set solo avisa la
    # entrada de mayor prioridad y no llegan dos Telegram por el mismo producto.
    # La clave lleva el DOMINIO: en WooCommerce el uid es md5("woo:" + id) y los ids
    # son enteros pequeños, así que dos tiendas distintas pueden compartirlo; sin el
    # dominio, el aviso de la segunda se descartaba y se daba por visto.
    seen_uids = set()
    pending, resyncs = [], []
    sigs = {}
    n_ok = 0
    for site_cfg, products, err in results:
        name = site_cfg["name"]
        if products is None:
            _record_health(state, name, ok=False, error=err)
            continue
        n_ok += 1
        sigs[name] = site_signature(site_cfg)
        dominio = urlparse(site_cfg["url"]).netloc.lower().removeprefix("www.")
        alerts, n_resync, new_site_state = process_site(site_cfg, products, state, config)
        alerts = [a for a in alerts if (dominio, a["uid"]) not in seen_uids]
        seen_uids.update((dominio, a["uid"]) for a in alerts)
        pending.append((name, site_cfg.get("priority", "medium"), alerts, n_resync, new_site_state))
        if n_resync:
            resyncs.append((name, n_resync))

    def commit(name, site_state):
        # El state de la tienda y la firma con la que se vio van SIEMPRE juntos: si
        # el aviso no sale, ninguno de los dos se actualiza y se reintenta entero.
        state[name] = site_state
        state.setdefault(SIG_KEY, {})[name] = sigs[name]

    # --- 3) Envío; el state de un sitio solo se persiste si su aviso salió ---
    con_alertas = []
    for name, priority, alerts, n_resync, new_site_state in pending:
        if not alerts:
            commit(name, new_site_state)
            continue
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {name} [{priority}]: {n_new} nuevos + {n_re} restock")
        con_alertas.append((name, priority, alerts, n_resync, new_site_state))

    solo_prioritarios = config.get("sound_only_for_priority", True)
    umbral_avalancha = config.get("avalanche_store_threshold", DEFAULT_AVALANCHE_STORES)

    if len(con_alertas) > umbral_avalancha:
        todas = [a for _, _, alerts, _, _ in con_alertas for a in alerts]
        log.info(f"AVALANCHA: {len(con_alertas)} tiendas, {len(todas)} productos "
                 f"-> un solo mensaje agrupado")
        silent = solo_prioritarios and not is_loud(todas)
        if send_telegram(bot_token, chat_id, format_avalanche(con_alertas, config), silent=silent):
            for name, _, _, _, new_site_state in con_alertas:
                commit(name, new_site_state)
        else:
            log.error("Aviso de avalancha NO enviado -> nada se marca como visto, "
                      "se reintenta en la próxima pasada")
    else:
        for name, priority, alerts, _, new_site_state in con_alertas:
            msg = format_notification(name, priority, alerts)
            silent = solo_prioritarios and not is_loud(alerts)
            if send_telegram(bot_token, chat_id, msg, silent=silent):
                commit(name, new_site_state)
            else:
                log.error(f"{name}: aviso NO enviado -> no se marca como visto, "
                          f"se reintenta en la próxima pasada")

    # --- 4) Re-sincronizaciones: ruido de mantenimiento, en silencio ---
    if resyncs:
        detail = "\n".join(
            f"• <b>{html_mod.escape(n)}</b>: {c} listados" for n, c in resyncs
        )
        send_telegram(
            bot_token, chat_id,
            "🔁 <b>Re-sincronización de catálogo</b>\n\n"
            f"{detail}\n\n"
            "Aparecieron de golpe (recatalogación o más cobertura del bot), "
            "así que se han absorbido sin detallar (lo marcado 🚨/🎁 se avisa "
            "aparte, nunca se absorbe). A partir de ahora solo notifico lo nuevo "
            "de verdad.",
            silent=True,
        )

    # --- 5) Salud (caídas y feeds rotos): UN resumen, y en silencio ---
    health_msgs, deshacer_salud = _collect_health_alerts(state, config)
    for msg in health_msgs:
        if not send_telegram(bot_token, chat_id, msg, silent=True):
            deshacer_salud()

    # Prueba de vida para el heartbeat: sin esto decía "bot vivo" aunque cada
    # pasada petara (el heartbeat corre aparte y no se enteraba).
    state[RUN_META_KEY] = {"last_run": time.time(), "sites_ok": n_ok,
                           "sites_failed": len(results) - n_ok, "skipped": len(saltadas)}
    save_state(state)
    if not con_alertas:
        log.info("Sin alertas en esta revisión")


def prune_state(state, config):
    """Borra del state las tiendas que ya no están en config.json.

    Al reapuntar o quitar una tienda su entrada se quedaba huérfana para siempre:
    engordaba el state y el heartbeat la seguía contando como tienda vigilada.
    Solo se mira config["sites"] completo (nunca el filtrado por prioridad), así
    que un `--priority high` no borra las tiendas medium.
    """
    nombres = {s["name"] for s in config["sites"]}
    huerfanas = [k for k in state if not k.startswith("__") and k not in nombres]
    for k in huerfanas:
        del state[k]
    for key in (HEALTH_KEY, SIG_KEY):
        sub = state.get(key, {})
        for k in [k for k in sub if k not in nombres]:
            del sub[k]
    if huerfanas:
        log.info(f"State: {len(huerfanas)} tiendas que ya no están en config, borradas "
                 f"({', '.join(huerfanas[:5])}{'...' if len(huerfanas) > 5 else ''})")


def notify_crash(exc_text):
    """Avisa UNA vez si monitor.py peta (config mal escrita, bug tras un push...).

    El bucle del workflow hace `python3 monitor.py || echo ...` y sigue: un error
    en cada pasada dejaba el bot ciego horas sin que nadie se enterase, con el run
    en verde y el heartbeat diciendo "vivo". El fichero marca evita repetir el
    mismo aviso en cada pasada; vive en el disco del runner, así que como mucho
    se repite una vez por bloque de 5h30m mientras siga roto.
    """
    firma = hashlib.md5(exc_text.strip().splitlines()[-1].encode()).hexdigest()
    try:
        if CRASH_FLAG.exists() and CRASH_FLAG.read_text().strip() == firma:
            return
    except OSError:
        pass
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        try:
            cfg = load_config()
            token, chat = token or cfg.get("telegram_bot_token"), chat or cfg.get("telegram_chat_id")
        except Exception:
            return
    label = "ONE PIECE"
    msg = (f"💥 <b>{html_mod.escape(label)}: monitor.py está fallando</b>\n"
           f"El bot NO está vigilando tiendas hasta que se arregle.\n\n"
           f"<pre>{html_mod.escape(exc_text[-1500:])}</pre>")
    if send_telegram(token, chat, msg):
        try:
            CRASH_FLAG.write_text(firma)
        except OSError:
            pass


def run_once_guarded(priority_filter=None):
    try:
        run_once(priority_filter=priority_filter)
    except SystemExit:
        raise
    except Exception:
        notify_crash(traceback.format_exc())
        raise
    if CRASH_FLAG.exists():
        try:
            CRASH_FLAG.unlink()
        except OSError:
            pass
        log.info("monitor.py vuelve a funcionar tras un fallo")


def run_loop(priority_filter=None):
    config = load_config()
    interval = config.get("check_interval_minutes", 15) * 60
    log.info(f"Monitor en bucle (cada {interval // 60} min, filtro={priority_filter or 'todos'})")
    while True:
        try:
            run_once_guarded(priority_filter=priority_filter)
        except Exception:
            log.exception("Pasada fallida, sigo con la siguiente")
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
        run_once_guarded(priority_filter=args.priority)
