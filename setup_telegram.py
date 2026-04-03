#!/usr/bin/env python3
"""
Guía interactiva para configurar el bot de Telegram.
"""

import json
import requests
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def main():
    print("=" * 50)
    print("  CONFIGURACIÓN DE TELEGRAM")
    print("=" * 50)
    print()
    print("PASO 1: Crear un bot de Telegram")
    print("-" * 40)
    print("1. Abre Telegram y busca @BotFather")
    print("2. Envíale: /newbot")
    print("3. Elige un nombre (ej: 'OP Card Monitor')")
    print("4. Elige un username (ej: 'op_card_monitor_bot')")
    print("5. BotFather te dará un token como:")
    print("   123456789:ABCdefGHIjklMNOpqrsTUVwxyz")
    print()

    bot_token = input("Pega aquí tu BOT TOKEN: ").strip()

    print()
    print("PASO 2: Obtener tu Chat ID")
    print("-" * 40)
    print(f"1. Abre Telegram y busca tu bot por su username")
    print(f"2. Envíale cualquier mensaje (ej: 'hola')")
    print(f"3. Pulsa Enter aquí y lo detectaré automáticamente...")
    print()
    input("Pulsa Enter después de enviar un mensaje al bot...")

    # Obtener chat_id automáticamente
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("result"):
            chat_id = str(data["result"][-1]["message"]["chat"]["id"])
            chat_name = data["result"][-1]["message"]["chat"].get("first_name", "")
            print(f"✅ Chat ID detectado: {chat_id} ({chat_name})")
        else:
            print("No se detectaron mensajes. Introduce el Chat ID manualmente:")
            chat_id = input("Chat ID: ").strip()
    except Exception as e:
        print(f"Error: {e}")
        chat_id = input("Introduce el Chat ID manualmente: ").strip()

    # Guardar en config.json
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    config["telegram_bot_token"] = bot_token
    config["telegram_chat_id"] = chat_id

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    print()
    print("Guardado en config.json. Enviando mensaje de prueba...")

    # Mensaje de prueba
    test_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "✅ Bot configurado correctamente!\nRecibirás notificaciones cuando haya nuevos productos de One Piece Card Game.",
    }
    resp = requests.post(test_url, json=payload, timeout=10)
    if resp.status_code == 200:
        print("✅ ¡Mensaje de prueba enviado! Revisa Telegram.")
    else:
        print(f"❌ Error: {resp.status_code} - {resp.text}")

    print()
    print("=" * 50)
    print("  ¡LISTO!")
    print("=" * 50)
    print()
    print("Ejecuta el monitor con:")
    print("  python3 monitor.py          (una vez)")
    print("  python3 monitor.py --loop   (en bucle continuo)")
    print()
    print("O configura un cron para que se ejecute cada 15 min:")
    print(f"  */15 * * * * cd {Path(__file__).parent} && python3 monitor.py >> monitor.log 2>&1")


if __name__ == "__main__":
    main()
