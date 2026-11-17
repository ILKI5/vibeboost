import os
import hmac
import hashlib
import json
import logging
import asyncio
from urllib.parse import parse_qsl
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://vibeboostgo.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    logging.warning("ВНИМАНИЕ: Переменная BOT_TOKEN не установлена!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- Проверка и парсинг Telegram initData ---
def validate_and_parse_init_data(init_data_str: str, bot_token: str) -> dict:
    """Проверяет HMAC-подпись initData и возвращает распарсенные данные (включая user)."""
    if not init_data_str:
        raise ValueError("Telegram initData отсутствует")

    parsed = dict(parse_qsl(init_data_str, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("В initData отсутствует хэш подписи")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise ValueError("Неверная подпись Telegram initData")

    user_raw = parsed.get("user")
    user = json.loads(user_raw) if user_raw else {}
    return {"user": user, "auth_date": parsed.get("auth_date")}


# --- Команда /send для администратора ---
@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    # Команду может выполнять только администратор
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    # Делим на: ["/send", "USER_ID", "ТЕКСТ"]
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "⚠️ <b>Формат команды:</b>\n"
            "<code>/send ID_ПОЛЬЗОВАТЕЛЯ Текст сообщения</code>\n\n"
            "Пример:\n<code>/send 1586986612 Привет, заявка принята в работу!</code>",
            parse_mode="HTML"
        )
        return

    target_id = parts[1].strip()
    text_to_send = parts[2].strip()

    if not target_id.isdigit():
        await message.reply("❌ ID пользователя должен состоять только из цифр!")
        return

    try:
        await bot.send_message(chat_id=int(target_id), text=text_to_send)
        await message.reply(f"✅ Сообщение доставлено пользователю <code>{target_id}</code>!", parse_mode="HTML")
    except Exception as e:
        await message.reply(
            f"❌ Не удалось отправить: {e}\n\n"
            f"(Убедись, что пользователь запускал бота и не заблокировал его)."
        )


# --- CORS заголовки ---
def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }


# --- Обработка заказов из Mini App ---
async def handle_order(request: web.Request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный JSON"}, status=400, headers=cors_headers())

    init_data_raw = payload.get("initData", "")
    try:
        tg_info = validate_and_parse_init_data(init_data_raw, BOT_TOKEN)
    except ValueError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors_headers())

    user = tg_info.get("user", {})
    order = payload.get("order", {})

    # Формируем имя пользователя
    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() or "Без имени"

    username = user.get("username")
    user_id = user.get("id", "неизвестен")

    # Исходный формат сообщения заявки
    username_str = f"@{username}" if username else "не указан"
    text = (
        f"🛒 <b>Новая заявка VibeBoost</b>\n\n"
        f"👤 <b>Пользователь:</b> {escape(full_name)}\n"
        f"🔹 <b>Username:</b> {escape(username_str)}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"🌐 <b>Площадка:</b> {escape(str(order.get('network', '—')))}\n"
        f"📁 <b>Категория:</b> {escape(str(order.get('category', '—')))}\n"
        f"⚙️ <b>Услуга:</b> {escape(str(order.get('name', '—')))}\n"
        f"🔢 <b>ID услуги:</b> {escape(str(order.get('service', '—')))}\n"
        f"🔗 <b>Ссылка:</b> {escape(str(order.get('link', '—')))}\n"
        f"📊 <b>Количество:</b> {escape(str(order.get('quantity', '—')))}\n"
        f"💰 <b>Предварительная сумма (с сайта, требует проверки):</b> {escape(str(order.get('total', '—')))} ₽\n\n"
        f"💬 Написать клиенту: <code>/send {user_id} </code>"
    )

    try:
        await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode="HTML")
    except Exception as e:
        logging.error("Ошибка отправки в чат администратора: %s", e)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить заявку администратору"},
            status=500,
            headers=cors_headers(),
        )

    return web.json_response({"ok": True}, headers=cors_headers())


async def health(request: web.Request):
    return web.json_response({"ok": True})


# Запуск чтения команд бота
async def start_bot(app):
    asyncio.create_task(dp.start_polling(bot))


app = web.Application()
app.router.add_post("/api/order", handle_order)
app.router.add_options("/api/order", handle_order)
app.router.add_get("/health", health)
app.on_startup.append(start_bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
