import os, hmac, hashlib, json, logging, asyncio, sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, unquote
from html import escape

from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://vibeboostgo.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
TWIBOOST_KEY = "gFcXlTobDo74fLZqIX3IECSjgGp8Xejn79gxyPl71SYE7KQCQExEGI86HlqC"
DB_PATH = Path("shop.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Логика провайдера ---
async def send_to_provider(service, link, quantity):
    """Отправка заказа в Twiboost"""
    async with ClientSession() as session:
        params = {
            "action": "add",
            "service": str(service),
            "link": link,
            "quantity": str(quantity),
            "key": TWIBOOST_KEY
        }
        async with session.get("https://twiboost.com/api/v2", params=params) as resp:
            data = await resp.json()
            return data

# --- БАЗА ДАННЫХ (SQLite) ---
# [Функции БД остались прежними, добавим только логику покупки]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ... (код get_or_create_user, change_user_balance, is_user_banned такой же) ...

# --- ОБНОВЛЕННАЯ ЛОГИКА ЗАКАЗА ---
async def handle_order(request: web.Request):
    # ... (валидация initData) ...
    try:
        payload = await request.json()
        tg_info = validate_and_parse_init_data(payload.get("initData", ""), BOT_TOKEN)
        user_id = int(tg_info["user"]["id"])
        order = payload.get("order", {})
        price = float(order.get("total", 0.0))

        # 1. Проверка баланса
        user = get_or_create_user(user_id)
        if user["balance"] < price:
            return web.json_response({"ok": False, "error": "Недостаточно средств"}, status=402)

        # 2. Списание
        change_user_balance(user_id, -price, comment=f"Заказ {order.get('service')}")

        # 3. Отправка провайдеру
        prov = await send_to_provider(order.get('service'), order.get('link'), order.get('quantity'))
        
        if "order" in prov:
            prov_id = prov["order"]
            # Успех: сохраняем в базу
            with get_db() as conn:
                conn.execute("INSERT INTO orders (user_id, service_id, link, total_price, status) VALUES (?,?,?,?,?)",
                             (user_id, prov_id, order.get('link'), price, 'accepted'))
            
            await bot.send_message(ADMIN_CHAT_ID, f"✅ Заказ принят провайдером! ID провайдера: {prov_id}", parse_mode="HTML")
            return web.json_response({"ok": True, "provider_id": prov_id})
        else:
            # Ошибка провайдера: ВОЗВРАТ ДЕНЕГ
            change_user_balance(user_id, price, comment="Возврат из-за ошибки провайдера")
            err_msg = prov.get("error", "Неизвестная ошибка провайдера")
            await bot.send_message(ADMIN_CHAT_ID, f"❌ Ошибка провайдера: {err_msg}. Деньги возвращены пользователю {user_id}")
            return web.json_response({"ok": False, "error": f"Ошибка сервиса: {err_msg}"}, status=500)

    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)
