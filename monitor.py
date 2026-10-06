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
import unicodedata
import time
import logging
import os
import sys
import argparse
import traceback
import threading
from collections import defaultdict
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
# Vocabulario para leer el stock en los listados HTML (portado del bot de One
# Piece, donde se auditó contra el HTML real). "agotad" cubre agotado/agotada/
# agotados; La Cueva Roja dice "Fuera de stock", no "out of stock".
OOS_KEYWORDS = [
    # Sin "vendido": la etiqueta "Más vendido" marcaba agotado un producto en stock.
    "agotad", "fuera de stock", "sin existencias", "sin stock",
    "no disponible", "sold out", "out of stock", "soon available",
    "rupture de stock", "esgotado", "esaurito", "ausverkauft", "uitverkocht",
]
# El marcador casi nunca está en la raíz de la miniatura: PrestaShop lo cuelga de
# un <span class="product-flag out_of_stock"> hijo, y Dungeon Marvels usa
# "soy_agotado" en el propio botón. Ojo al guion BAJO: la lista vieja solo miraba
# "out-of-stock" con guion y por eso La Cueva Roja salía siempre disponible.
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
HEALTH_KEY = "__health__"  # clave reservada en state para la salud de las tiendas (no es un sitio)
HEALTH_META_KEY = "__health_meta__"  # clave reservada: control del resumen de salud
SIG_KEY = "__sig__"        # clave reservada: firma de URL+filtros con la que se vio cada tienda
RUN_META_KEY = "__run__"   # clave reservada: última pasada completada (lo lee el heartbeat)
# Todas las claves reservadas empiezan por "__": así heartbeat y poda las
# distinguen de las tiendas sin tener que enumerarlas.
CRASH_FLAG = BASE_DIR / ".crash_notified"  # evita repetir el aviso de caída en cada pasada
DEFAULT_RECOVER_PASSES = 3  # pasadas buenas SEGUIDAS para dar por recuperada una tienda
DEFAULT_ANOMALY_ACCEPT = 3  # pasadas seguidas con la misma desaparición masiva para aceptarla
DEFAULT_HEALTH_FAIL_THRESHOLD = 10  # fallos seguidos antes de avisar (~10 min a 1 pasada/min)
DEFAULT_EMPTY_THRESHOLD = 5        # pasadas a 0 productos (habiendo tenido catálogo) antes de avisar
DEFAULT_DIGEST_COOLDOWN_MIN = 30   # minutos mínimos entre dos resúmenes de salud
DEFAULT_AVALANCHE_STORES = 8       # tiendas con alertas a partir de las cuales se agrupa todo
DEFAULT_MAX_ALERTS_AVALANCHE = 40  # productos como mucho en el mensaje de avalancha
DEFAULT_MAX_WORKERS = 12           # peticiones simultáneas
DEFAULT_TIMEOUT = 20               # segundos por petición
# Una tienda caída no debe encarecer TODAS las pasadas. Con 2 intentos y 20s de
# timeout, una sola tienda que agota el timeout mete 42s en cada pasada (medido:
# Friki Galaxy dejaba las pasadas de Naruto en 44s frente a los 8s del resto).
DEFAULT_DEGRADED_AFTER = 5         # fallos seguidos -> timeout corto y 1 solo intento
DEFAULT_DEGRADED_TIMEOUT = 8       # segundos para una tienda ya degradada
DEFAULT_BACKOFF_AFTER = 20         # fallos seguidos -> además se comprueba 1 de cada N pasadas
DEFAULT_BACKOFF_EVERY = 10         # pasadas que se salta una tienda en backoff
DEFAULT_MAX_ALERTS = 20            # productos como mucho por aviso de tienda
TELEGRAM_MAX_CHARS = 3800          # el límite real son 4096; dejamos margen


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


def _record_health(state, name, ok, error=None, n_products=None):
    """Actualiza el contador de fallos consecutivos de una tienda en el state.

    `n_products` es el número CRUDO de productos devueltos (antes del filtro de
    keywords) y sirve para detectar la tienda CIEGA por API: la petición va bien
    (HTTP 200, JSON válido) pero la colección devuelve 0 productos. Si esa tienda
    llegó a tener catálogo alguna vez, es que el handle murió o Shopify Markets
    nos está sirviendo una lista vacía -> 0 avisos para siempre, en silencio y
    con el run en verde. Una colección que SIEMPRE estuvo vacía (p. ej. una
    preventa que aún no ha abierto) no dispara nada: nunca tuvo max_products.
    """
    health = state.setdefault(HEALTH_KEY, {})
    h = health.setdefault(name, {"fails": 0, "alerted": False, "last_error": None})
    h["skips"] = 0  # se acaba de comprobar: el contador de saltos del backoff se reinicia
    h.setdefault("first_seen", time.time())
    if ok:
        h["fails"] = 0
        h["last_error"] = None
        h.pop("down_since", None)
        h["checks"] = h.get("checks", 0) + 1  # pasadas con respuesta (para "nunca ha dado nada")
        # Racha de pasadas buenas: una tienda intermitente (Friki Galaxy) no se da
        # por recuperada con UNA respuesta suelta (ver _collect_health_alerts).
        h["ok_streak"] = h.get("ok_streak", 0) + 1
        if n_products is not None:
            best = h.get("max_products", 0)
            if n_products > 0:
                h["max_products"] = max(best, n_products)
                h["empty_streak"] = 0
            elif best > 0:
                h["empty_streak"] = h.get("empty_streak", 0) + 1
    else:
        h["fails"] = h.get("fails", 0) + 1
        h.setdefault("down_since", time.time())  # para "caída más de 7 días" en el heartbeat
        h["ok_streak"] = 0
        h["last_error"] = error


def _fmt_store_list(names, limit=10):
    """Lista compacta separada por · y recortada, para no llenar la pantalla."""
    shown = [html_mod.escape(n) for n in names[:limit]]
    extra = len(names) - len(shown)
    txt = " · ".join(shown)
    return txt + (f" <i>y {extra} más</i>" if extra else "")


def _collect_health_alerts(state, config):
    """Devuelve (mensajes, deshacer): como mucho UN resumen por pasada.

    Antes salía un mensaje por tienda y por transición. Con 118 tiendas, un bache
    de red del runner producía decenas de mensajes de caída y otras tantas de
    recuperación, y entre ese ruido se perdía un restock de verdad. Ahora todas
    las transiciones de la pasada se agrupan en un único mensaje, que además va
    en silencio (sin notificación en el móvil) y con un tiempo mínimo entre
    resúmenes. `deshacer` restaura los flags si el envío falla, para que el aviso
    se reintente en vez de perderse.
    """
    threshold = config.get("health_fail_threshold", DEFAULT_HEALTH_FAIL_THRESHOLD)
    empty_threshold = config.get("health_empty_threshold", DEFAULT_EMPTY_THRESHOLD)
    cooldown = config.get("health_digest_cooldown_minutes", DEFAULT_DIGEST_COOLDOWN_MIN) * 60
    health = state.get(HEALTH_KEY, {})
    meta = state.setdefault(HEALTH_META_KEY, {})

    caidas, recuperadas, ciegas, vuelven = [], [], [], []
    restores = []

    def flip(h, key, value):
        prev = h.get(key)
        restores.append(lambda: h.__setitem__(key, prev))
        h[key] = value

    # Histéresis: una tienda intermitente (cae y vuelve cada pocas pasadas) generaba
    # un resumen por cada vaivén — 5 en una noche con Friki Galaxy. Solo se da por
    # recuperada tras varias pasadas buenas SEGUIDAS; si recae antes, sigue "caída"
    # (alerted=True) y no se vuelve a avisar.
    recover_passes = config.get("health_recover_passes", DEFAULT_RECOVER_PASSES)
    for name, h in sorted(health.items()):
        fails = h.get("fails", 0)
        if fails >= threshold and not h.get("alerted", False):
            flip(h, "alerted", True)
            caidas.append(name)
        elif fails == 0 and h.get("alerted", False) and h.get("ok_streak", 0) >= recover_passes:
            flip(h, "alerted", False)
            recuperadas.append(name)

        empty = h.get("empty_streak", 0)
        if empty >= empty_threshold and not h.get("empty_alerted", False):
            flip(h, "empty_alerted", True)
            ciegas.append((name, h.get("max_products", 0), empty))
        elif empty == 0 and h.get("empty_alerted", False):
            flip(h, "empty_alerted", False)
            vuelven.append(name)

    def deshacer():
        for r in restores:
            r()

    if not (caidas or recuperadas or ciegas or vuelven):
        return [], deshacer

    # Una tienda CIEGA es pérdida de datos silenciosa y es rara: se salta la espera.
    # Las caídas y recuperaciones son ruido de mantenimiento y sí la respetan.
    ahora = time.time()
    # `or 0`: si falla el envío del PRIMER resumen, deshacer deja last_digest=None
    # y `ahora - None` reventaba la pasada siguiente (y todas las demás).
    if not ciegas and ahora - (meta.get("last_digest") or 0) < cooldown:
        deshacer()
        return [], (lambda: None)

    n_total = len(health)
    lineas = []
    if ciegas:
        for name, best, streak in ciegas:
            lineas.append(f"👻 <b>{html_mod.escape(name)}</b>: responde OK pero lleva {streak} pasadas a "
                          f"<b>0 productos</b> (tenía {best}). Revisa la URL en config.json.")
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
    # cuerpo sin descomprimir (parseo JSON fallaría con "Expecting value").
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
        # activado resuelven el mercado por ese header y, si pides es-ES en una tienda
        # US/UK, devuelven {"products": []} con HTTP 200 -> la tienda queda CIEGA sin
        # que salte ningún error (medido: Flipside Gaming 0 vs 124 productos,
        # Card-Binder 24 vs 28). Sin el header sirven el catálogo completo.
        # Petición tipo XHR: muchas tiendas tras Cloudflare/anti-bot solo sirven
        # el JSON si la cabecera parece una llamada AJAX y no una navegación.
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
    """Botones/enlaces de "añadir al carrito" dentro de la miniatura."""
    controls = []
    for el in item.select("button, a, input"):
        classes = " ".join(el.get("class", [])).lower()
        # La lista de deseos también es un botón con "add" en la clase: fuera.
        if "wishlist" in classes or "compare" in classes:
            continue
        text = el.get_text(" ", strip=True).lower()
        if any(t in classes for t in CART_CLASS_TOKENS) or any(t in text for t in CART_TEXT_TOKENS):
            controls.append(el)
    return controls


def detect_html_stock_signal(item):
    """Tri-estado: False = agotado, True = en stock, None = el listado no lo dice.

    Sustituye a la detección vieja, que solo miraba las clases del elemento RAÍZ
    y buscaba "out-of-stock" con guion: en PrestaShop el marcador es un
    <span class="product-flag out_of_stock"> HIJO, así que La Cueva Roja daba
    todo como disponible y de ahí no podía llegar nunca un restock.
    Las señales NEGATIVAS mandan sobre las positivas: hay temas (Dungeon Marvels)
    que pintan un "Add to Cart" activo también en los productos agotados.
    """
    # 1. Clase de agotado en la propia miniatura o en CUALQUIER descendiente.
    for el in [item] + item.select("[class]"):
        classes = " ".join(el.get("class", [])).lower()
        if any(t in classes for t in OOS_CLASS_TOKENS):
            return False

    # 2. Botón de carrito deshabilitado: la señal más fiable y sin idioma.
    controls = _cart_controls(item)
    if controls and all(_is_disabled(c) for c in controls):
        return False

    # 3. Texto de agotado, en los idiomas de las tiendas del config.
    if any(k in item.get_text(" ", strip=True).lower() for k in OOS_KEYWORDS):
        return False

    # 4. Marca POSITIVA: hay carrito y está activo.
    if controls:
        return True
    return None


def complete_truncated_title(title, link):
    """Completa un título cortado por el tema ("Dragon Ball SCG: Fusion...") con el
    slug del enlace, que lleva el nombre entero (".../dragon-ball-scg-fusion-world-
    case-booster-box-fb12-en").

    Sin esto las keywords de prioridad no casan y un case llega en silencio, o ni
    pasa el filtro (La Cueva Roja: 0 de 17 productos del 30 aniv). El alt de la
    imagen NO sirve: La Cueva Roja lo copia de otros productos. El uid HTML sale
    del enlace, así que completar el título no hace parecer nuevo al producto.
    """
    # ".." y no "...": Distrito Zero corta con dos puntos ("NARUTO..").
    if not link or not title.endswith(("..", "…")):
        return title
    slug = urlparse(link).path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.html?$", "", slug)
    slug = re.sub(r"^\d+-", "", slug)       # PrestaShop antepone a veces el id: "90647-nombre"
    slug = re.sub(r"-\d{8,}$", "", slug)    # Distrito Zero añade el EAN al final
    if not slug:
        return title
    return f"{title.rstrip('.…').strip()} ({slug.replace('-', ' ')})"


def extract_products_html(html, site_cfg):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(site_cfg["selector"])
    signals = [detect_html_stock_signal(it) for it in items]
    # Calibración POR LISTADO: si el tema pinta carrito activo en algún producto,
    # uno que no lo tenga está agotado. Si no lo pinta en NINGUNO, el listado no
    # informa del stock y se asume disponible (no hay dato con el que decir otra cosa).
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
        uid = hashlib.md5((link or f"{title}").encode()).hexdigest()
        legacy = hashlib.md5(f"{title}{link}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                         "link": link, "price": price, "in_stock": in_stock})

    if items:
        n_oos = sum(1 for p in products if not p["in_stock"])
        log.info(f"  stock HTML: {len(items) - n_oos} disponibles / {n_oos} agotados")
        if n_oos == len(items) and len(items) > 5:
            # Puede ser real, pero también un cambio de tema que lo marque todo
            # agotado: con notify_only_in_stock eso deja la tienda muda sin fallar.
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

    # Shopify products.json
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
                    # products.json no dice la divisa: la pone el config de la tienda
                    # (`currency`, por defecto €). Sin esto un case de The Card Vault
                    # salía a "1436.95€" cuando son libras, y Kantocards va en pesos MXN.
                    price = f"{p_raw}{currency}"
                in_stock = any(v.get("available", False) for v in variants)
            # Enlace directo a la cesta: /cart/<variante>:1 crea el carrito con la
            # primera variante DISPONIBLE y lleva al pago. Sin sesión iniciada.
            disponible = next((v for v in variants if v.get("available") and v.get("id")), None)
            cart_url = f"{base}/cart/{disponible['id']}:1" if base and disponible else ""
            # uid ESTABLE: solo el id del producto. Antes incluía el título, así que
            # cualquier retoque del título ("PREVENTA X" -> "X") cambiaba el uid y el
            # producto volvía a parecer nuevo -> el mismo enlace se avisaba otra vez
            # horas después. `legacy_uid` es el esquema viejo y solo sirve para leer
            # el state antiguo sin disparar una tanda de falsos "nuevos".
            pid = item.get("id", "")
            uid = hashlib.md5(f"shopify:{pid}".encode()).hexdigest() if pid else \
                hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
            products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                             "link": link, "price": price, "in_stock": in_stock,
                             "cart_url": cart_url})
        return products

    # WooCommerce Store API
    items = data if isinstance(data, list) else data.get("products", [])
    for item in items:
        title = html_mod.unescape(item.get("name", "Sin título"))
        link = item.get("permalink") or item.get("url", "")
        prices = item.get("prices", {}) or {}
        raw_price = prices.get("price") or "0"
        symbol = html_mod.unescape(prices.get("currency_symbol") or currency)
        try:
            price = f"{int(raw_price) / 100:.2f}{symbol}"
        except (ValueError, TypeError):
            price = "Precio no disponible"
        in_stock = item.get("is_in_stock", item.get("has_stock", True))
        # La Store API dice cuántas quedan ("Solo quedan 1 disponibles"), que en un
        # bot de restock vale tanto como el propio aviso. is_purchasable NO sirve:
        # varias tiendas lo devuelven true incluso con el producto agotado.
        availability = item.get("stock_availability") or {}
        stock_text = ""
        if isinstance(availability, dict):
            stock_text = html_mod.unescape(availability.get("text") or "")
        quedan = item.get("low_stock_remaining")
        if not stock_text and isinstance(quedan, int) and quedan > 0:
            stock_text = f"Quedan {quedan}"
        pid = item.get("id", "")
        # ?add-to-cart=<id> solo vale para productos SIMPLES: los variables
        # necesitan elegir variante, y ahí el botón llevaría a un error.
        cart_url = ""
        if pid and link and in_stock and item.get("type", "simple") == "simple":
            cart_url = f"{link}{'&' if '?' in link else '?'}add-to-cart={pid}"
        uid = hashlib.md5(f"woo:{pid}".encode()).hexdigest() if pid else \
            hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        legacy = hashlib.md5(f"{pid}{title}".encode()).hexdigest()
        products.append({"uid": uid, "legacy_uid": legacy, "title": title,
                         "link": link, "price": price, "in_stock": in_stock,
                         "stock_text": stock_text, "backorder": bool(item.get("is_on_backorder")),
                         "cart_url": cart_url})
    return products


def send_telegram(bot_token, chat_id, message, attempts=4, silent=False, reply_markup=None):
    """Envía un mensaje y devuelve True/False según haya salido.

    Devolver el resultado es lo que permite NO dar por avisado un producto cuyo
    mensaje no llegó: quien llama solo persiste el state si esto devuelve True.
    Reintenta respetando el `retry_after` de los 429 (rate limit), que es el
    fallo más probable cuando un drop grande genera avisos de muchas tiendas.
    """
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": False}
    if silent:
        # Los avisos de salud llegan al chat pero NO hacen sonar el móvil: así el
        # ruido de mantenimiento no compite con un restock de verdad.
        payload["disable_notification"] = True
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for attempt in range(attempts):
        try:
            resp = requests.post(url, json=payload, timeout=20)
        except Exception as e:
            log.warning(f"Telegram, error de red ({e}), reintento {attempt + 1}/{attempts}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            log.info("Notificación Telegram enviada")
            return True
        if resp.status_code == 429:
            try:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            log.warning(f"Telegram rate limit, esperando {wait}s")
            time.sleep(min(wait, 60) + 1)
            continue
        if 500 <= resp.status_code < 600:
            log.warning(f"Telegram {resp.status_code}, reintento {attempt + 1}/{attempts}")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 400 and "reply_markup" in payload:
            # Un botón con un enlace que Telegram no acepta tumbaría el aviso entero:
            # se reintenta SIN botones, que el aviso llegue es lo que importa.
            log.warning(f"Telegram rechazó los botones ({resp.text[:150]}), reenvío sin ellos")
            payload.pop("reply_markup")
            continue
        # 400 y demás: el mensaje es inválido, reintentar no arregla nada
        log.error(f"Error enviando Telegram: {resp.status_code} {resp.text[:300]}")
        return False
    log.error("Telegram: agotados los reintentos, mensaje NO enviado")
    return False


def send_telegram_chunks(bot_token, chat_id, messages, silent=False, reply_markup=None):
    """Envía una lista de trozos. True solo si TODOS salen. Los botones van en el
    último trozo, que es el que queda abajo del todo en el chat."""
    ok = True
    for i, msg in enumerate(messages):
        markup = reply_markup if i == len(messages) - 1 else None
        if not send_telegram(bot_token, chat_id, msg, silent=silent, reply_markup=markup):
            ok = False
    return ok


def cart_keyboard(entradas, config):
    """Botones "🛒 Añadir" para lo que está EN STOCK y tiene enlace de cesta, lo más
    prioritario primero. `entradas` = [(alerta, tienda o None)]. En un drop, abrir
    la ficha y buscar el botón cuesta segundos que deciden si llegas o no."""
    if not config.get("cart_buttons", True):
        return None
    vistos, filas = set(), []
    orden = sorted(entradas, key=lambda t: product_rank(t[0]))
    for a, tienda in orden:
        url = a.get("cart_url")
        if not url or not a.get("in_stock") or url in vistos:
            continue
        vistos.add(url)
        nombre = a["title"] if len(a["title"]) <= 30 else a["title"][:29] + "…"
        texto = f"🛒 {rank_mark(a) + ' ' if rank_mark(a) else ''}{nombre}"
        if tienda:
            texto += f" · {tienda[:18]}"
        filas.append([{"text": texto, "url": url}])
        if len(filas) >= config.get("max_cart_buttons", 8):
            break
    return {"inline_keyboard": filas} if filas else None


def is_priority(p, config=None):
    """¿Es de los que importan? 🔥 y 🚨 siempre; 🎁 solo si el bot lo pide con
    `sound_for_promo` (One Piece: los promos de revista/torneo son caza mayor)."""
    if p.get("top_priority") or p.get("high_value"):
        return True
    return bool(p.get("promo") and (config or {}).get("sound_for_promo", False))


def is_loud(alerts, config=None):
    """¿Merece este aviso hacer sonar el móvil? Solo si lleva algo prioritario
    (UPC, booster box, case, ETB...). Una lata o un blíster llegan al chat en
    silencio: así lo gordo no se pierde entre lo flojo."""
    return any(is_priority(a, config) for a in alerts)


def _chunk_message(title_line, blocks):
    """Trocea por bloques enteros para no pasar del límite de Telegram (un mensaje
    de más de 4096 caracteres se rechaza con un 400 y el aviso se perdía entero).
    Nunca parte un producto por la mitad."""
    msgs, cur = [], title_line
    for b in blocks:
        if len(cur) + len(b) + 1 > TELEGRAM_MAX_CHARS and cur != title_line:
            msgs.append(cur)
            cur = f"{title_line}<i>(continuación {len(msgs) + 1})</i>\n" + b + "\n"
        else:
            cur += b + "\n"
    msgs.append(cur)
    return msgs


def product_rank(p):
    """Orden de interés: 0 = top (🔥), 1 = high_value (🚨), 2 = promo (🎁), 3 = resto.

    El aviso se corta a `max_alerts_per_site` productos, así que sin ordenar lo
    gordo podía quedar fuera del corte por detrás de un llavero. Qué cae en cada
    nivel lo decide el config de cada bot (top_priority_keywords, etc.); un bot
    que no defina un nivel simplemente no lo usa.
    """
    if p.get("top_priority"):
        return 0
    if p.get("high_value"):
        return 1
    if p.get("promo"):
        return 2
    return 3


def rank_mark(p):
    if p.get("top_priority"):
        return "🔥"
    if p.get("high_value"):
        return "🚨"
    if p.get("promo"):
        return "🎁"
    return ""


def matches_keywords(title, keywords):
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


def normalize_title(title):
    """Minúsculas, sin tildes y sin º/°/ª/puntos: "Celebración 30.º" -> "celebracion 30".

    El º se quita ANTES de NFKD, que lo convertiría en una "o" ("30o aniversario").
    """
    t = re.sub(r"[º°ª.]", "", title.lower())
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c))


def matches_patterns(title, patterns):
    """Regex de `required_patterns` sobre el título normalizado. Cubre variantes que
    las keywords literales no ven: "30TH CELEBRACIONES", "30º Aniversario", el typo
    "anniversay" de alguna tienda o "Celebrations 30th" con el orden invertido."""
    t = normalize_title(title)
    return any(re.search(p, t) for p in patterns)


def normalize_state(raw):
    """Migra state antigua (list de uids) al nuevo schema {uid: {in_stock: bool}}."""
    if isinstance(raw, list):
        return {uid: {"in_stock": True} for uid in raw}
    if isinstance(raw, dict):
        return raw
    return {}


def plan_fetch(name, health, config):
    """Decide cómo tratar a una tienda según sus fallos seguidos.

    Devuelve (comprobar, timeout, intentos). Una tienda sana va con el timeout
    normal y 2 intentos; una que lleva fallando baja a timeout corto y 1 intento
    (deja de lastrar la pasada entera); y una caída de forma persistente pasa a
    comprobarse 1 de cada N pasadas, para que siga pudiendo auto-recuperarse sin
    costar una petición por pasada. En cuanto responde, `fails` vuelve a 0 y con
    ello el trato normal.
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


WOO_FIELDS = ("id,name,permalink,prices,is_in_stock,has_stock,is_on_backorder,"
              "low_stock_remaining,stock_availability,type")
_COOKIES = {}


def _cookie_challenge(url, config, timeout):
    """Algunas tiendas (Friki de Nacimiento) no sirven nada hasta que el navegador
    ejecuta un `document.cookie = 'dhd2=<hex>'` de su portada: sin ella la API da
    HTTP 202 con HTML y el bot lo veía como "respuesta no-JSON" en cada pasada.
    Se pide la portada, se saca la cookie y se reutiliza (dura 24 h)."""
    base = "{0.scheme}://{0.netloc}/".format(urlparse(url))
    if base in _COOKIES:
        return _COOKIES[base]
    try:
        r = requests.get(base, headers=build_headers(config["user_agent"]), timeout=timeout)
        m = re.search(r"document\.cookie\s*=\s*['\"]([^=;'\"]+)=([^;'\"]+)", r.text)
    except Exception as e:
        log.warning(f"  cookie de {base}: {e}")
        return None
    if not m:
        return None
    _COOKIES[base] = f"{m.group(1)}={m.group(2)}"
    return _COOKIES[base]


# Una petición a la vez por DOMINIO: con 12 hilos, Sunny Store recibía sus 7
# colecciones a la vez, y Shopify limita más a los bots desde mayo de 2026.
_DOMINIO_LOCKS = defaultdict(threading.Lock)


def fetch_site_serial(site_cfg, config, timeout=None, attempts=2):
    dominio = urlparse(site_cfg["url"]).netloc.lower().removeprefix("www.")
    with _DOMINIO_LOCKS[dominio]:
        return fetch_site(site_cfg, config, timeout=timeout, attempts=attempts)


def fetch_site(site_cfg, config, timeout=None, attempts=2):
    """SOLO red y parseo. No toca el state, así puede correr en paralelo.

    Devuelve (site_cfg, productos|None, error). Sacar la parte de red fuera del
    state es lo que permite lanzar las ~120 tiendas a la vez: una pasada pasa de
    ~60s (secuencial, y varios minutos si hay tiendas caídas reintentando) a ~6s,
    que es lo que de verdad fija la cadencia real del bucle continuo.
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    is_api = site_cfg.get("type", "html") == "api"
    if timeout is None:
        timeout = config.get("request_timeout_seconds", DEFAULT_TIMEOUT)

    headers = build_headers(config["user_agent"], is_api=is_api)
    if is_api:
        p = urlparse(url)
        headers["Referer"] = f"{p.scheme}://{p.netloc}/"
    if "/wp-json/" in url:
        # Varias WooCommerce (Topdeck, Manavortex, Micelion, Esfantasia, TCG
        # Portugal) sirven la Store API desde la caché de LiteSpeed: medido en
        # Topdeck con 1,5 h de antigüedad (`age: 5405`). Un producto nuevo podía
        # tardar eso en verse. Un parámetro que cambia en cada pasada fuerza
        # respuesta fresca (`x-litespeed-cache: miss`); la API lo ignora. Solo se
        # toca la petición: la firma y el tope (`per_page`) salen de la URL del config.
        url = f"{url}{'&' if '?' in url else '?'}_cb={int(time.time())}"
        # Solo los campos que usa el bot: la respuesta baja de ~1 MB a ~55 KB.
        url += "&_fields=" + WOO_FIELDS
    if site_cfg.get("cookie_challenge"):
        cookie = _cookie_challenge(url, config, timeout)
        if cookie:
            headers["Cookie"] = cookie

    last_err = None
    for attempt in range(attempts):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if is_api:
                ctype = resp.headers.get("Content-Type", "").lower()
                if "json" not in ctype:
                    # Cloudflare/anti-bot devolvió HTML en vez del JSON
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


# Lo que decide QUÉ productos ve el bot en una tienda. Si cambia (URL nueva, más
# resultados por página, keywords nuevas...), aparecen de golpe productos que ya
# existían y que el state no conocía -> tanda de falsos "NUEVO". Antes había que
# acordarse de RENOMBRAR la tienda o borrar la caché; ahora la firma lo detecta.
SITE_SIG_FIELDS = ("url", "type", "selector", "title_selector", "link_selector", "exclude_keywords")
GLOBAL_SIG_FIELDS = ("required_keywords", "required_any_keywords", "required_patterns", "exclude_keywords")
# Subir cuando un cambio del MOTOR amplíe lo que se ve (p. ej. completar títulos
# cortados, que hizo pasar el filtro a productos que antes no lo pasaban).
COVERAGE_VERSION = 1


def site_signature(site_cfg, config):
    site = {k: site_cfg.get(k) for k in SITE_SIG_FIELDS}
    # Solo si la tienda lo usa: así las firmas de las tiendas sin include_keywords
    # no cambian respecto a las ya guardadas.
    if site_cfg.get("include_keywords"):
        site["include_keywords"] = site_cfg["include_keywords"]
    data = {"site": site,
            "global": {k: config.get(k) for k in GLOBAL_SIG_FIELDS},
            "engine": COVERAGE_VERSION}
    return hashlib.md5(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def mark_priority(p, config):
    """Marca 🔥 / 🚨 / 🎁 según las keywords del config. `priority_exclude` quita
    la marca (no el producto) a los accesorios que comparten palabra con el
    sellado: fundas "Display 12 unidades", "Card Case", sleeves "Tournament"...
    Así siguen llegando, pero en silencio."""
    if matches_keywords(p["title"], config.get("priority_exclude", [])):
        p["top_priority"] = p["high_value"] = p["promo"] = False
        return p
    p["top_priority"] = matches_keywords(p["title"], config.get("top_priority_keywords", []))
    p["high_value"] = matches_keywords(p["title"], config.get("high_value_keywords", []))
    p["promo"] = matches_keywords(p["title"], config.get("promo_keywords", []))
    return p


def requested_cap(url):
    """Tope de resultados que pide la URL (limit / per_page / resultsPerPage).

    Si el listado vuelve LLENO hasta el tope, lo que sobra entra y sale entre
    pasadas según el orden de la tienda: ahí no se puede deducir nada de que un
    producto "desaparezca".
    """
    query = parse_qs(urlparse(url).query)
    for key, values in query.items():
        # "n" = resultados por página en PrestaShop 1.6 (Nin-Nin-Game)
        if key.lower() in ("limit", "per_page", "resultsperpage", "n") and values:
            try:
                return int(values[0])
            except (TypeError, ValueError):
                pass
    return None


def process_site(site_cfg, products, state, config):
    """Filtra por keywords y compara con el state.

    Devuelve (alertas, nuevo_state_del_sitio, nº absorbidos por re-sync). NO
    escribe en `state`: quien llama solo lo persiste si el aviso de esta tienda
    llegó a Telegram; si el envío falla, el state viejo se conserva y el producto
    se vuelve a avisar en la siguiente pasada en vez de darse por visto.
    """
    name = site_cfg["name"]
    url = site_cfg["url"]
    required_keywords = config.get("required_keywords", [])
    # Segundo filtro opcional (Y lógico con required_keywords): el título debe llevar
    # además un término de juego de cartas. Las búsquedas de tiendas generalistas
    # devuelven sobre todo merch (llaveros, tazas, cómics...).
    required_any_keywords = config.get("required_any_keywords", [])
    # Regex opcionales (O lógico con required_keywords) sobre el título normalizado.
    required_patterns = config.get("required_patterns", [])
    # Filtros propios de una tienda, para feeds mixtos (preventas de varios juegos,
    # colecciones con singles o merch): include obliga, exclude se suma al global.
    include_keywords = site_cfg.get("include_keywords", [])
    exclude_keywords = config.get("exclude_keywords", []) + site_cfg.get("exclude_keywords", [])
    notify_only_in_stock = config.get("notify_only_in_stock", True)
    # Las preventas suelen publicarse agotadas/"Próximamente" antes de abrir la
    # reserva. Con esto, un listado NUEVO de valor alto avisa aunque no haya stock.
    notify_new_oos_priority = config.get("notify_new_oos_priority", False)
    resync_threshold = config.get("resync_threshold")
    match_label = config.get("match_label", "el filtro")

    # El tope se mide sobre el listado CRUDO, antes de filtrar por keywords.
    cap = requested_cap(url)
    truncado = cap is not None and len(products) >= cap

    n_antes = len(products)
    # uids de TODO lo que devuelve la tienda, antes de filtrar: para distinguir un
    # producto que ha DESAPARECIDO del listado de uno que sigue ahí pero el filtro
    # deja fuera.
    uids_crudos = {p["uid"] for p in products} | {p.get("legacy_uid") for p in products}
    if required_keywords or required_patterns:
        def requerido(t):
            return matches_keywords(t, required_keywords) or \
                bool(required_patterns and matches_patterns(t, required_patterns))
        products = [p for p in products if requerido(p["title"])
                    and (not required_any_keywords or matches_keywords(p["title"], required_any_keywords))]
    if include_keywords:
        products = [p for p in products if matches_keywords(p["title"], include_keywords)]
    n_excl = 0
    if exclude_keywords:
        n_excl = sum(1 for p in products if matches_keywords(p["title"], exclude_keywords))
        products = [p for p in products if not matches_keywords(p["title"], exclude_keywords)]
    if len(products) != n_antes or n_excl:
        log.info(f"  {name}: {n_antes} detectados, {len(products)} matchean {match_label}"
                 + (f" ({n_excl} descartados por exclusión)" if n_excl else ""))
    else:
        log.info(f"  {name}: {n_antes} productos detectados")
    if truncado:
        log.warning(f"  {name}: listado LLENO hasta el tope ({cap}), puede haber "
                    f"productos fuera; no se miran desapariciones")

    raw_prev = state.get(name)
    is_first_run = raw_prev is None
    # La URL o los filtros han cambiado desde la última vez (o es el primer
    # despliegue con firmas): lo que aparezca ahora y no estuviera en el state ya
    # existía, solo que el bot no lo veía. Se absorbe en silencio; los restocks de
    # productos conocidos SÍ se avisan.
    rebaseline = not is_first_run and \
        state.get(SIG_KEY, {}).get(name) != site_signature(site_cfg, config)
    # COPIA: normalize_state devuelve el mismo dict que está dentro de `state`
    # cuando ya es un dict. Sin copiar, marcar un producto como visto mutaría el
    # state en el sitio aunque luego el envío a Telegram fallara, y el aviso se
    # perdería igualmente (que es justo lo que esto viene a evitar).
    site_state = dict(normalize_state(raw_prev))
    # Lo que la tienda sigue listando pero el filtro ya no deja pasar (p. ej. merch
    # tras añadir exclusiones) sale del state. Si se quedaba, en las tiendas con
    # `mark_disappeared_oos` contaba como "desaparecido" en cada pasada: Friki
    # Galaxy y AllInTCG daban "listado anómalo" siempre y la detección no servía.
    uids_filtrados = {p["uid"] for p in products} | {p.get("legacy_uid") for p in products}
    for uid in [u for u in site_state if u in uids_crudos and u not in uids_filtrados]:
        del site_state[uid]
    if not products:
        return [], site_state, 0

    # Baseline de la PRIMERA pasada de una tienda. Con `silent_first_run` se absorbe
    # todo lo existente sin avisar (lo que quieren los bots cuyo juego ya tiene
    # catálogo o aún no existe); sin él, se notifica lo que esté en stock.
    if is_first_run and config.get("silent_first_run", False):
        for p in products:
            site_state[p["uid"]] = {"in_stock": p["in_stock"]}
        log.info(f"  {name}: baseline inicial silenciado ({len(products)} productos)")
        return [], site_state, 0

    alerts = []
    absorbidos = 0
    for p in products:
        uid = p["uid"]
        prev = site_state.get(uid)
        if prev is None and p.get("legacy_uid"):
            # Migración silenciosa del esquema viejo de uid: si el producto ya estaba
            # en el state con la clave antigua, se hereda su estado y se reescribe con
            # la nueva. Sin esto, el cambio de esquema haría parecer NUEVO todo el
            # catálogo y dispararía una tanda enorme de avisos falsos.
            prev = site_state.pop(p["legacy_uid"], None)
        mark_priority(p, config)
        if prev is None:
            # Producto nuevo
            if rebaseline:
                absorbidos += 1
            elif not is_first_run:
                if p["in_stock"] or not notify_only_in_stock or \
                        (notify_new_oos_priority and is_priority(p, config)):
                    alerts.append({**p, "alert_type": "new"})
            else:
                # Primera ejecución: solo notifica los que están en stock (baseline)
                if p["in_stock"]:
                    alerts.append({**p, "alert_type": "new"})
        else:
            # Producto conocido — detectar restock
            was_oos = not prev.get("in_stock", True)
            if was_oos and p["in_stock"]:
                alerts.append({**p, "alert_type": "restock"})
        site_state[uid] = {"in_stock": p["in_stock"]}

    if rebaseline:
        log.info(f"  {name}: URL/filtros cambiados -> re-baseline silencioso "
                 f"({absorbidos} productos que antes no se veían)")

    # Tiendas que OCULTAN del listado lo que se agota (Isekai Alcorcón): allí el
    # restock es que el producto REAPARECE. Como el uid no cambia y en el state
    # seguía como disponible, no saltaba nada. Marcándolo agotado al desaparecer,
    # la reaparición dispara el 🔄 RESTOCK por el camino normal. Opcional
    # (`mark_disappeared_oos`). No se aplica con el listado al tope (rotarían
    # productos y darían falsos restocks) ni si desaparece media tienda de golpe.
    if config.get("mark_disappeared_oos", False) and not is_first_run and not truncado:
        desaparecidos = [uid for uid, prev in site_state.items()
                         if uid not in uids_crudos and prev.get("in_stock", True)]
        limite = max(5, len(products) // 2)
        # Una desaparición masiva puede ser un listado roto puntual (no se marca)
        # o la tienda que ha despublicado lo agotado de verdad (AllInTCG pasó de 12
        # cajas a 1 y se quedó así). Si se repite varias pasadas seguidas, se acepta:
        # antes daba "listado anómalo" para siempre y esas cajas nunca avisaban al volver.
        h = state.setdefault(HEALTH_KEY, {}).setdefault(name, {})
        if len(desaparecidos) > limite:
            h["anomalo_streak"] = h.get("anomalo_streak", 0) + 1
            if h["anomalo_streak"] < config.get("anomaly_accept_passes", DEFAULT_ANOMALY_ACCEPT):
                log.warning(f"  {name}: {len(desaparecidos)} productos desaparecidos de golpe "
                            f"(> {limite}), listado anómalo: no se marcan "
                            f"({h['anomalo_streak']}ª pasada seguida)")
                desaparecidos = []
            else:
                log.warning(f"  {name}: {len(desaparecidos)} desaparecidos {h['anomalo_streak']} "
                            f"pasadas seguidas: se aceptan (la tienda ha retirado esos productos)")
        else:
            h["anomalo_streak"] = 0
        if desaparecidos:
            for uid in desaparecidos:
                site_state[uid] = {"in_stock": False, "gone": True}
            log.info(f"  {name}: {len(desaparecidos)} desaparecidos del listado, "
                     f"marcados agotados (avisarán si reaparecen)")

    # Re-sync (opcional, `resync_threshold`): una tienda no publica 20 novedades
    # reales en una pasada de 2 minutos. Si pasa, ha recatalogado o cambiado el
    # orden de la colección: lo flojo se absorbe con UN aviso de una línea, pero lo
    # prioritario y los restocks se avisan SIEMPRE (el día que una tienda sube el
    # set entero de golpe, el case va dentro y no se puede perder).
    n_new = sum(1 for a in alerts if a["alert_type"] == "new")
    if resync_threshold and n_new > resync_threshold:
        keep = [a for a in alerts if a["alert_type"] == "restock"
                or is_priority(a, config) or a.get("promo")]
        n_absorbidos = n_new - sum(1 for a in keep if a["alert_type"] == "new")
        log.info(f"  {name}: {n_new} nuevos de golpe > umbral {resync_threshold}, "
                 f"re-sync: {n_absorbidos} absorbidos, {len(keep)} avisados")
        return keep, site_state, n_absorbidos

    return alerts, site_state, 0


def format_notification(site_name, priority, alerts, config=None):
    """Devuelve una LISTA de mensajes (troceados) con los productos ordenados por
    interés: lo gordo primero, para que no se caiga del corte."""
    config = config or {}
    max_alerts = config.get("max_alerts_per_site", DEFAULT_MAX_ALERTS)
    bot_emoji = config.get("bot_emoji", "🔔")
    bot_label = config.get("bot_label", "MONITOR")
    emoji = PRIORITY_EMOJI.get(priority, "🔔")
    has_restock = any(a["alert_type"] == "restock" for a in alerts)
    header = "🔄 RESTOCK + " if has_restock else ""
    ordered = sorted(alerts, key=product_rank)
    shown, extra = ordered[:max_alerts], len(ordered) - max_alerts

    title_line = (f"{bot_emoji} {header}<b>{bot_label} — {html_mod.escape(site_name)}</b> "
                  f"{emoji} [{priority.upper()}]\n")
    blocks = []
    for p in shown:
        tag = "🔄 VUELVE" if p["alert_type"] == "restock" else "🆕 NUEVO"
        mark = rank_mark(p)
        mark = f"{mark} " if mark else ""
        stock_mark = "" if p["in_stock"] else " ⚠️ AGOTADO"
        # Escapado obligatorio: un '<' o un '&' suelto en el título rompe el
        # parse_mode HTML, Telegram devuelve 400 y el aviso se pierde entero.
        b = [f"• {mark}{tag}{stock_mark} <b>{html_mod.escape(p['title'])}</b>",
             f"  💰 {html_mod.escape(p['price'])}" + (" ⏳ bajo pedido" if p.get("backorder") else "")]
        # "Solo quedan 1 disponibles" (Store API de WooCommerce). Si está agotado
        # ya lo dice AGOTADO.
        if p.get("stock_text") and p["in_stock"]:
            b.append(f"  📊 {html_mod.escape(p['stock_text'])}")
        if p["link"]:
            b.append(f"  🔗 {p['link']}")
        b.append("")
        blocks.append("\n".join(b))
    if extra > 0:
        blocks.append(f"... y {extra} más")
    return _chunk_message(title_line, blocks)


def format_avalanche(entradas, config):
    """UN solo mensaje cuando muchas tiendas avisan en la misma pasada.

    El día que abra un drop del 30 aniversario van a disparar decenas de tiendas
    casi a la vez: con un mensaje por tienda, la booster box se pierde entre
    cuarenta avisos de latas. Aquí va una línea por producto, ordenadas por
    importancia y con la tienda al lado, así lo gordo queda arriba del todo.
    """
    max_items = config.get("max_alerts_avalanche", DEFAULT_MAX_ALERTS_AVALANCHE)
    items = [(a, name) for name, _, alerts, _ in entradas for a in alerts]
    # Una misma tienda con varias colecciones vigiladas (p. ej. Pokemillon en
    # Eternals + Reservas + Novedades) repite el mismo producto. Se colapsa por
    # enlace idéntico: nunca junta tiendas distintas, porque el enlace lleva el
    # dominio. Solo afecta a lo que se muestra; el state de cada tienda se guarda
    # igual, así que ninguna se queda sin registrar el producto.
    vistos, unicos = set(), []
    for a, name in items:
        clave = a["link"] or f"{name}|{a['title']}"
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append((a, name))
    items = unicos
    # Primero lo más gordo; a igual rango, los restock antes que los listados nuevos.
    items.sort(key=lambda t: (product_rank(t[0]), 0 if t[0]["alert_type"] == "restock" else 1))
    total = len(items)
    shown = items[:max_items]

    bot_emoji = config.get("bot_emoji", "🔔")
    bot_label = config.get("bot_label", "MONITOR")
    title_line = (f"{bot_emoji} <b>{bot_label} — {len(entradas)} tiendas con novedades</b> "
                  f"({total} productos)\n")
    blocks = []
    for a, tienda in shown:
        mark = rank_mark(a) or "•"
        tag = "🔄" if a["alert_type"] == "restock" else "🆕"
        stock_mark = "" if a["in_stock"] else " ⚠️ AGOTADO"
        b = [f"{mark} {tag} <b>{html_mod.escape(a['title'])}</b>{stock_mark}",
             f"  💰 {html_mod.escape(a['price'])}" + (" ⏳ bajo pedido" if a.get("backorder") else "")
             + f" — <i>{html_mod.escape(tienda)}</i>"]
        if a.get("stock_text") and a["in_stock"]:
            b.append(f"  📊 {html_mod.escape(a['stock_text'])}")
        if a["link"]:
            b.append(f"  🔗 {a['link']}")
        b.append("")
        blocks.append("\n".join(b))
    if total > len(shown):
        blocks.append(f"... y {total - len(shown)} productos más")
    return _chunk_message(title_line, blocks)


def run_once(priority_filter=None):
    config = load_config()
    state = load_state()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") or config["telegram_bot_token"]
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or config["telegram_chat_id"]

    if bot_token in ("TU_BOT_TOKEN_AQUI", "USE_GITHUB_SECRET", "", None):
        log.error("⚠️  Falta TELEGRAM_BOT_TOKEN (env o config.json)")
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
    for name in saltadas:
        h = health.setdefault(name, {})
        h["skips"] = h.get("skips", 0) + 1
    degradadas = [s["name"] for s in a_consultar if plan[s["name"]][2] == 1]

    workers = max(1, min(config.get("max_workers", DEFAULT_MAX_WORKERS), len(a_consultar) or 1))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda s: fetch_site_serial(s, config, timeout=plan[s["name"]][1], attempts=plan[s["name"]][2]),
            a_consultar))
    extra = ""
    if degradadas:
        extra += f", {len(degradadas)} con timeout corto por fallos"
    if saltadas:
        extra += f", {len(saltadas)} saltadas (caídas persistentes)"
    log.info(f"{len(a_consultar)} tiendas consultadas en {time.time() - t0:.1f}s "
             f"({workers} hilos){extra}")

    # --- 2) Proceso SECUENCIAL contra el state (evita carreras) ---
    pending, resyncs = [], []
    sigs, dominios = {}, {}
    n_ok = 0
    for site_cfg, products, err in results:
        name = site_cfg["name"]
        if products is None:
            _record_health(state, name, ok=False, error=err)
            continue
        n_ok += 1
        _record_health(state, name, ok=True, n_products=len(products))
        sigs[name] = site_signature(site_cfg, config)
        dominios[name] = urlparse(site_cfg["url"]).netloc.lower().removeprefix("www.")
        alerts, new_site_state, n_resync = process_site(site_cfg, products, state, config)
        pending.append((name, site_cfg.get("priority", "medium"), alerts, new_site_state))
        if n_resync:
            resyncs.append((name, n_resync))

    def commit(name, site_state):
        # El state de la tienda y la firma con la que se vio van SIEMPRE juntos: si
        # el aviso no sale, ninguno de los dos se actualiza y se reintenta entero.
        state[name] = site_state
        state.setdefault(SIG_KEY, {})[name] = sigs[name]

    # --- 3) Envío; el state de un sitio solo se persiste si su aviso salió ---
    # El uid de Shopify es md5("shopify:" + id_de_producto): el MISMO artículo
    # listado en varias colecciones de la MISMA tienda (Pokemillon está en Eternals +
    # Reservas + Novedades + Cajas de Sobres) comparte uid. Sin esto llegaban hasta 4
    # Telegram seguidos con el mismo enlace. Como `pending` va en orden de prioridad,
    # avisa la entrada más prioritaria y las demás lo dan por visto sin repetirlo.
    # La clave lleva el DOMINIO: en WooCommerce el uid es md5("woo:" + id) y los ids
    # son enteros pequeños, así que dos tiendas distintas pueden compartirlo; sin el
    # dominio, el aviso de la segunda se descartaba y se daba por visto.
    seen_uids = set()
    n_alertas = 0
    con_alertas = []
    for name, priority, alerts, new_site_state in pending:
        alerts = [a for a in alerts if (dominios[name], a["uid"]) not in seen_uids]
        seen_uids.update((dominios[name], a["uid"]) for a in alerts)
        if not alerts:
            commit(name, new_site_state)
            continue
        n_new = sum(1 for a in alerts if a["alert_type"] == "new")
        n_re = sum(1 for a in alerts if a["alert_type"] == "restock")
        log.info(f"Alertas {name} [{priority}]: {n_new} nuevos + {n_re} restock")
        con_alertas.append((name, priority, alerts, new_site_state))

    solo_prioritarios = config.get("sound_only_for_priority", True)
    umbral_avalancha = config.get("avalanche_store_threshold", DEFAULT_AVALANCHE_STORES)

    if len(con_alertas) > umbral_avalancha:
        # Avalancha: un único mensaje en vez de uno por tienda.
        todas = [a for _, _, alerts, _ in con_alertas for a in alerts]
        log.info(f"AVALANCHA: {len(con_alertas)} tiendas, {len(todas)} productos "
                 f"-> un solo mensaje agrupado")
        msgs = format_avalanche(con_alertas, config)
        silent = solo_prioritarios and not is_loud(todas, config)
        botones = cart_keyboard([(a, name) for name, _, alerts, _ in con_alertas for a in alerts], config)
        if send_telegram_chunks(bot_token, chat_id, msgs, silent=silent, reply_markup=botones):
            for name, _, alerts, new_site_state in con_alertas:
                commit(name, new_site_state)
                n_alertas += len(alerts)
        else:
            log.error("Aviso de avalancha NO enviado -> nada se marca como visto, "
                      "se reintenta en la próxima pasada")
    else:
        for name, priority, alerts, new_site_state in con_alertas:
            msgs = format_notification(name, priority, alerts, config)
            silent = solo_prioritarios and not is_loud(alerts, config)
            botones = cart_keyboard([(a, None) for a in alerts], config)
            if send_telegram_chunks(bot_token, chat_id, msgs, silent=silent, reply_markup=botones):
                commit(name, new_site_state)
                n_alertas += len(alerts)
            else:
                log.error(f"{name}: aviso NO enviado -> no se marca como visto, "
                          f"se reintenta en la próxima pasada")

    # Guardar YA lo avisado: si algo de lo que viene después fallara, la pasada
    # siguiente no repetiría los mismos avisos.
    save_state(state)

    # --- 4) Re-sincronizaciones: ruido de mantenimiento, una línea y en silencio ---
    if resyncs:
        detalle = "\n".join(f"• <b>{html_mod.escape(n)}</b>: {c} listados" for n, c in resyncs)
        send_telegram(
            bot_token, chat_id,
            f"🔁 <b>{html_mod.escape(config.get('bot_label', 'MONITOR'))} — re-sincronización de catálogo</b>\n\n"
            f"{detalle}\n\nAparecieron de golpe (la tienda recatalogó), así que se han "
            f"absorbido sin detallar. Lo prioritario y los restocks se avisan aparte, nunca se absorben.",
            silent=True,
        )

    # --- 5) Salud (caídas y tiendas ciegas): UN resumen, y en silencio ---
    health_msgs, deshacer_salud = _collect_health_alerts(state, config)
    for msg in health_msgs:
        if not send_telegram(bot_token, chat_id, msg, silent=True):
            deshacer_salud()

    # Prueba de vida para el heartbeat: sin esto decía "bot vivo" aunque cada
    # pasada petara (el heartbeat corre aparte y no se enteraba).
    state[RUN_META_KEY] = {"last_run": time.time(), "sites_ok": n_ok,
                           "sites_failed": len(results) - n_ok, "skipped": len(saltadas)}
    save_state(state)
    ping_healthcheck()
    if not n_alertas:
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


def ping_healthcheck(fallo=False):
    """Señal de vida EXTERNA (healthchecks.io). Si GitHub desactiva Actions o se
    para todo, aquí no corre nada que pueda avisar: healthchecks.io avisa solo si
    deja de recibir pings. Sin la variable HEALTHCHECK_URL no hace nada."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    try:
        requests.get(url.rstrip("/") + ("/fail" if fallo else ""), timeout=5)
    except Exception as e:
        log.warning(f"healthcheck no enviado: {e}")


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
    label = "MONITOR"
    try:
        label = load_config().get("bot_label", label)
    except Exception:
        pass
    msg = (f"💥 <b>{html_mod.escape(label)}: monitor.py está fallando</b>\n"
           f"El bot NO está vigilando tiendas hasta que se arregle.\n\n"
           f"<pre>{html_mod.escape(exc_text[-1500:])}</pre>")
    if send_telegram(token, chat, msg, attempts=2):
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
        ping_healthcheck(fallo=True)
        raise
    if CRASH_FLAG.exists():
        try:
            CRASH_FLAG.unlink()
        except OSError:
            pass
        log.info("monitor.py vuelve a funcionar tras un fallo")


def run_loop(priority_filter=None):
    config = load_config()
    if priority_filter == "high":
        interval = config.get("check_interval_high_minutes", 5) * 60
    else:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--priority", choices=["high", "medium"])
    args = parser.parse_args()
    if args.loop:
        run_loop(priority_filter=args.priority)
    else:
        run_once_guarded(priority_filter=args.priority)
