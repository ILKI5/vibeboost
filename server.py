import os
import hmac
import hashlib
import json
import logging
import asyncio
from pathlib import Path
from urllib.parse import parse_qsl
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

# --- Переменные окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://vibeboostgo.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Файл хранения заблокированных пользователей ---
BAN_FILE = Path("banned.json")

def load_banned_users() -> set:
    if BAN_FILE.exists():
        try:
            with open(BAN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_banned_users(banned_set: set):
    try:
        with open(BAN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(banned_set), f)
    except Exception as e:
        logging.error("Ошибка сохранения banned.json: %s", e)

banned_users = load_banned_users()


# --- Проверка Telegram initData ---
def validate_and_parse_init_data(init_data_str: str, bot_token: str) -> dict:
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


# ==================== КОМАНДЫ АДМИНИСТРАТОРА ====================

# 1. Отправка сообщения клиенту: /send ID текст
@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply(
            "⚠️ <b>Формат команды:</b>\n"
            "<code>/send ID_ПОЛЬЗОВАТЕЛЯ Текст сообщения</code>\n\n"
            "Пример:\n<code>/send 1586986612 Привет, ваш заказ принят!</code>",
            parse_mode="HTML"
        )
        return

    target_id = parts[1].strip()
    text_to_send = parts[2].strip()

    if not target_id.isdigit():
        await message.reply("❌ ID пользователя должен состоять только из цифр!")
        return

    try:
        await bot.send_message(
            chat_id=int(target_id),
            text=f"💬 <b>Сообщение от поддержки:</b>\n\n{escape(text_to_send)}",
            parse_mode="HTML"
        )
        await message.reply(f"✅ Сообщение доставлено клиенту <code>{target_id}</code>!", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}")


# 2. Бан пользователя: /ban ID
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Используй: <code>/ban ID_ПОЛЬЗОВАТЕЛЯ</code>", parse_mode="HTML")
        return

    target_id = int(parts[1].strip())
    banned_users.add(target_id)
    save_banned_users(banned_users)
    await message.reply(f"🚫 Пользователь <code>{target_id}</code> <b>заблокирован</b>. Заказы и сообщения от него больше не принимаются.", parse_mode="HTML")


# 3. Разбан пользователя: /unban ID
@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Используй: <code>/unban ID_ПОЛЬЗОВАТЕЛЯ</code>", parse_mode="HTML")
        return

    target_id = int(parts[1].strip())
    if target_id in banned_users:
        banned_users.remove(target_id)
        save_banned_users(banned_users)
        await message.reply(f"✅ Пользователь <code>{target_id}</code> <b>разблокирован</b>.", parse_mode="HTML")
    else:
        await message.reply(f"ℹ️ Пользователь <code>{target_id}</code> не был в бане.", parse_mode="HTML")


# 4. Список забаненных: /banlist
@dp.message(Command("banlist"))
async def cmd_banlist(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    if not banned_users:
        await message.reply("Список заблокированных пользователей пуст.")
        return

    text = "🚫 <b>Список заблокированных пользователей:</b>\n\n"
    for uid in sorted(banned_users):
        text += f"• <code>{uid}</code> (разбан: <code>/unban {uid}</code>)\n"
    await message.reply(text, parse_mode="HTML")


# ==================== СООБЩЕНИЯ ОТ КЛИЕНТОВ ====================

@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message):
    user_id = message.from_user.id

    # Игнорируем команды админа
    if str(user_id) == str(ADMIN_CHAT_ID):
        return

    # Если пользователь в бане — ничего не шлем админу
    if user_id in banned_users:
        return

    # Если клиент нажал /start
    if message.text == "/start":
        await message.answer("👋 Добро пожаловать в VibeBoost! Откройте меню, чтобы выбрать услуги, или напишите свой вопрос сюда.")
        return

    # Пересылаем сообщение клиента администратору
    u = message.from_user
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "Без имени"
    uname = f"@{u.username}" if u.username else "не указан"

    text = (
        f"📩 <b>Новое сообщение от клиента!</b>\n\n"
        f"👤 <b>От:</b> {escape(full_name)} ({uname})\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"💬 <b>Текст:</b>\n{escape(message.text or '[Медиа/Файл]')}\n\n"
        f"✍️ Ответить: <code>/send {user_id} </code>\n"
        f"🚫 Забанить: <code>/ban {user_id}</code>"
    )

    try:
        await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode="HTML")
    except Exception as e:
        logging.error("Ошибка пересылки сообщения админу: %s", e)


# ==================== СЕРВЕР ЗАКАЗОВ (WEB API) ====================

def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }

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
    user_id = user.get("id")

    # ПРОВЕРКА НА БАН: забаненным запрещено оформлять заявки
    if user_id and int(user_id) in banned_users:
        return web.json_response(
            {"ok": False, "error": "Ваш аккаунт заблокирован администратором."},
            status=403,
            headers=cors_headers()
        )

    order = payload.get("order", {})

    first_name = user.get("first_name", "")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip() or "Без имени"

    username = user.get("username")
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
        f"💬 Написать: <code>/send {user_id} </code>\n"
        f"🚫 Забанить: <code>/ban {user_id}</code>"
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

# --- Запуск бота и веб-сервера ---
async def start_bot(app):
    await bot.delete_webhook(drop_pending_updates=True)
    app["bot_task"] = asyncio.create_task(dp.start_polling(bot))

async def stop_bot(app):
    if "bot_task" in app:
        app["bot_task"].cancel()
    await dp.stop_polling()
    await bot.session.close()

app = web.Application()
app.router.add_post("/api/order", handle_order)
app.router.add_options("/api/order", handle_order)
app.router.add_get("/health", health)

app.on_startup.append(start_bot)
app.on_cleanup.append(stop_bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
