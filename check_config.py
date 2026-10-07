#!/usr/bin/env python3
"""Revisa config.json antes de que llegue al bot (lo lanza el workflow check.yml).

Un error de config no rompe nada a la vista: la tienda simplemente deja de verse
(selector que falta, `limit` olvidado que deja fuera productos, campo mal escrito
que se ignora en silencio). Esto lo convierte en un fallo visible en cada push.
Uso: python3 check_config.py [config.json]
"""
import json
import re
import sys
from urllib.parse import urlparse, parse_qs

GLOBAL_KEYS = {
    "telegram_bot_token", "telegram_chat_id", "user_agent", "sites",
    "bot_emoji", "bot_label", "match_label", "check_interval_minutes", "check_interval_high_minutes",
    "max_workers", "request_timeout_seconds", "degraded_fail_threshold", "degraded_timeout_seconds",
    "backoff_fail_threshold", "backoff_every_passes", "health_fail_threshold", "health_recover_passes",
    "health_digest_cooldown_minutes", "health_empty_threshold", "anomaly_accept_passes",
    "sound_only_for_priority", "sound_for_promo", "avalanche_store_threshold", "max_alerts_avalanche",
    "max_alerts_per_site", "notify_only_in_stock", "notify_new_oos_priority", "silent_first_run",
    "mark_disappeared_oos", "resync_threshold", "cart_buttons", "max_cart_buttons",
    "edit_on_sold_out", "timezone", "official_sources", "official_check_minutes", "official_loud",
    "set_code_pattern", "mass_restock_threshold",
    "required_keywords", "required_any_keywords", "required_patterns", "exclude_keywords",
    "top_priority_keywords", "high_value_keywords", "promo_keywords", "priority_exclude",
}
SITE_KEYS = {
    "name", "url", "type", "priority", "currency", "include_keywords", "exclude_keywords",
    "selector", "title_selector", "link_selector", "price_selector", "cookie_challenge",
}
HTML_KEYS = ("selector", "title_selector", "link_selector", "price_selector")


def revisar(cfg):
    errores = []
    for k in cfg:
        if not k.startswith("_") and k not in GLOBAL_KEYS:
            errores.append(f"clave global desconocida: {k!r} (¿mal escrita?)")
    for i, p in enumerate(cfg.get("required_patterns", [])):
        try:
            re.compile(p)
        except re.error as e:
            errores.append(f"required_patterns[{i}] no es una regex válida: {e}")
    for p in [cfg.get("set_code_pattern")] if cfg.get("set_code_pattern") else []:
        try:
            if re.compile(p).groups != 2:
                errores.append("set_code_pattern necesita 2 grupos: prefijo y número")
        except re.error as e:
            errores.append(f"set_code_pattern no es una regex válida: {e}")
    for f in cfg.get("official_sources", []):
        falta = [k for k in ("name", "url", "selector") if not f.get(k)]
        if falta:
            errores.append(f"fuente oficial {f.get('name', '?')!r}: falta {', '.join(falta)}")
    sites = cfg.get("sites")
    if not isinstance(sites, list) or not sites:
        return errores + ["'sites' falta o está vacío"]
    vistos = set()
    for s in sites:
        nombre = s.get("name", "?")
        donde = f"tienda {nombre!r}"
        for k in s:
            if not k.startswith("_") and k not in SITE_KEYS:
                errores.append(f"{donde}: campo desconocido {k!r} (¿mal escrito?)")
        if not s.get("name"):
            errores.append(f"tienda sin nombre: {s.get('url')}")
        elif nombre in vistos:
            errores.append(f"{donde}: nombre DUPLICADO (el state se indexa por nombre)")
        vistos.add(nombre)
        url = s.get("url", "")
        if not url.startswith(("http://", "https://")):
            errores.append(f"{donde}: url inválida {url!r}")
            continue
        tipo = s.get("type", "html")
        if tipo not in ("api", "html"):
            errores.append(f"{donde}: type {tipo!r} (debe ser 'api' o 'html')")
        if s.get("priority", "medium") not in ("high", "medium"):
            errores.append(f"{donde}: priority {s.get('priority')!r} (debe ser 'high' o 'medium')")
        q = {k.lower(): v for k, v in parse_qs(urlparse(url).query).items()}
        if tipo == "html":
            falta = [k for k in HTML_KEYS if not s.get(k)]
            if falta:
                errores.append(f"{donde}: tienda HTML sin {', '.join(falta)}")
        elif "products.json" in url:
            if q.get("limit") != ["250"]:
                errores.append(f"{donde}: Shopify sin limit=250 (solo se leería una parte del catálogo)")
        elif "/wp-json/" in url:
            if "per_page" not in q:
                errores.append(f"{donde}: WooCommerce sin per_page (solo se leerían 10 productos)")
        for k in ("include_keywords", "exclude_keywords"):
            if k in s and not isinstance(s[k], list):
                errores.append(f"{donde}: {k} debe ser una lista")
    return errores


if __name__ == "__main__":
    ruta = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    try:
        cfg = json.load(open(ruta, encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"❌ {ruta} no es JSON válido: {e}")
        sys.exit(1)
    errores = revisar(cfg)
    for e in errores:
        print(f"❌ {e}")
    print(f"{'❌' if errores else '✅'} {ruta}: {len(cfg.get('sites', []))} tiendas, {len(errores)} errores")
    sys.exit(1 if errores else 0)
