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

# --- База заблокированных пользователей ---
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


# ==================== ОТПРАВКА СООБЩЕНИЙ ЮЗЕРУ (ОТ АДМИНА) ====================

# Поддерживает как обычный текст, так и ФОТО / ДОКУМЕНТЫ / ГОЛОСОВЫЕ с подписью /send ID текст
@dp.message(F.chat.id == int(ADMIN_CHAT_ID or 0))
async def handle_admin_messages(message: types.Message):
    # Текст команды может быть либо в message.text, либо в message.caption (если прикреплено фото)
    full_text = message.text or message.caption or ""
    
    if not full_text.startswith("/send"):
        # Если это служебные команды бана, пропускаем их дальше
        if full_text.startswith(("/ban", "/unban", "/banlist")):
            return
        return

    parts = full_text.split(maxsplit=2)
    if len(parts) < 2:
        await message.reply(
            "⚠️ <b>Формат:</b>\n"
            "• Текст: <code>/send ID Текст</code>\n"
            "• Фото/файл: прикрепи файл и в подписи напиши <code>/send ID Текст</code>",
            parse_mode="HTML"
        )
        return

    target_id = parts[1].strip()
    if not target_id.isdigit():
        await message.reply("❌ ID пользователя должен быть числом!")
        return

    extra_text = parts[2].strip() if len(parts) > 2 else ""

    try:
        # Если админ прикрепил фото/видео/документ
        if message.content_type != types.ContentType.TEXT:
            await bot.copy_message(
                chat_id=int(target_id),
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"💬 <b>Сообщение от поддержки:</b>\n\n{escape(extra_text)}" if extra_text else "💬 <b>Сообщение от поддержки</b>",
                parse_mode="HTML"
            )
        else:
            # Обычное текстовое сообщение
            if not extra_text:
                await message.reply("⚠️ Напиши текст сообщения после ID!")
                return
            await bot.send_message(
                chat_id=int(target_id),
                text=f"💬 <b>Сообщение от поддержки:</b>\n\n{escape(extra_text)}",
                parse_mode="HTML"
            )
            
        await message.reply(f"✅ Успешно отправлено клиенту <code>{target_id}</code>!", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}\n(Возможно, клиент заблокировал бота)")


# ==================== УПРАВЛЕНИЕ БАНОМ ====================

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
    await message.reply(f"🚫 Пользователь <code>{target_id}</code> <b>заблокирован</b>.", parse_mode="HTML")


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


# ==================== СООБЩЕНИЯ И МЕДИА ОТ КЛИЕНТОВ ====================

@dp.message(F.chat.type == "private")
async def handle_user_incoming(message: types.Message):
    user_id = message.from_user.id

    # Игнорируем админа
    if str(user_id) == str(ADMIN_CHAT_ID):
        return

    # Забаненные игнорируются
    if user_id in banned_users:
        return

    if message.text == "/start":
        await message.answer("👋 Добро пожаловать в VibeBoost! Выберите услуги в меню или задайте свой вопрос прямо сюда.")
        return

    u = message.from_user
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "Без имени"
    uname = f"@{u.username}" if u.username else "не указан"

    # Шапка карточки
    header_text = (
        f"📩 <b>Входящее от клиента!</b>\n\n"
        f"👤 <b>От:</b> {escape(full_name)} ({uname})\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"✍️ Ответить: <code>/send {user_id} </code>\n"
        f"🚫 Забанить: <code>/ban {user_id}</code>"
    )

    try:
        # Если юзер прислал чисто текст
        if message.content_type == types.ContentType.TEXT:
            full_card = (
                f"📩 <b>Входящее от клиента!</b>\n\n"
                f"👤 <b>От:</b> {escape(full_name)} ({uname})\n"
                f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
                f"💬 <b>Текст:</b>\n{escape(message.text)}\n\n"
                f"✍️ Ответить: <code>/send {user_id} </code>\n"
                f"🚫 Забанить: <code>/ban {user_id}</code>"
            )
            await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=full_card, parse_mode="HTML")
        else:
            # Если юзер прислал ФОТО, ВИДЕО, ЧЕК, ФАЙЛ, ГОЛОСОВОЕ
            # 1. Шлем карточку с кнопками
            await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=header_text, parse_mode="HTML")
            # 2. Пересылаем/копируем сам файл админу в исходном качестве
            await bot.copy_message(
                chat_id=int(ADMIN_CHAT_ID),
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
    except Exception as e:
        logging.error("Ошибка передачи сообщения от клиента админу: %s", e)


# ==================== ЗАКАЗЫ С САЙТА ====================

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
        logging.error("Ошибка отправки заявки админу: %s", e)
        return web.json_response(
            {"ok": False, "error": "Не удалось отправить заявку администратору"},
            status=500,
            headers=cors_headers(),
        )

    return web.json_response({"ok": True}, headers=cors_headers())

async def health(request: web.Request):
    return web.json_response({"ok": True})

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
