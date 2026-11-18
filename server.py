import os
import hmac
import hashlib
import json
import logging
import asyncio
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, unquote
from html import escape

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

# --- Настройки окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://vibeboostgo.vercel.app").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = Path("shop.db")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ (SQLite) ---
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0.0,
                is_banned INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                service_id TEXT,
                service_name TEXT,
                network TEXT,
                link TEXT,
                quantity INTEGER,
                total_price REAL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                type TEXT,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

# Операции с пользователями
def get_or_create_user(user_id: int, username: str = "", full_name: str = ""):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, full_name, balance, is_banned) VALUES (?, ?, ?, 0.0, 0)",
                (user_id, username, full_name)
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        else:
            # Обновим имя если изменилось
            if username or full_name:
                conn.execute(
                    "UPDATE users SET username = COALESCE(NULLIF(?, ''), username), full_name = COALESCE(NULLIF(?, ''), full_name) WHERE user_id = ?",
                    (username, full_name, user_id)
                )
                conn.commit()
        return dict(row)

def change_user_balance(user_id: int, delta: float, comment: str = "") -> float:
    with get_db() as conn:
        conn.execute("UPDATE users SET balance = ROUND(balance + ?, 2) WHERE user_id = ?", (delta, user_id))
        conn.execute("INSERT INTO transactions (user_id, amount, type, comment) VALUES (?, ?, ?, ?)",
                     (user_id, delta, "topup" if delta > 0 else "purchase", comment))
        conn.commit()
        row = conn.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance"] if row else 0.0

def set_user_ban(user_id: int, is_banned: bool):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if is_banned else 0, user_id))
        conn.commit()

def is_user_banned(user_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row["is_banned"]) if row else False


# --- Проверка Telegram initData ---
def validate_and_parse_init_data(init_data_str: str, bot_token: str) -> dict:
    if not init_data_str:
        raise ValueError("Telegram initData отсутствует")

    raw_str = init_data_str
    if raw_str.startswith("tgWebAppData="):
        raw_str = raw_str.split("tgWebAppData=", 1)[1]
        raw_str = raw_str.split("&tgWebAppVersion=", 1)[0]
        raw_str = unquote(raw_str)

    parsed = dict(parse_qsl(raw_str, keep_blank_values=True))
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

# 1. Пополнение баланса: /addbalance {ID} {СУММА}
@dp.message(Command("addbalance"))
async def cmd_addbalance(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Используй: <code>/addbalance ID_ПОЛЬЗОВАТЕЛЯ СУММА</code>\nПример: <code>/addbalance 1586986612 500</code>", parse_mode="HTML")
        return
    
    uid = int(parts[1])
    try:
        amount = float(parts[2].replace(",", "."))
    except ValueError:
        await message.reply("❌ Сумма должна быть числом!")
        return

    get_or_create_user(uid)
    new_bal = change_user_balance(uid, amount, comment="Начисление администратором")
    
    await message.reply(f"✅ Баланс пользователя <code>{uid}</code> пополнен на <b>{amount:.2f} ₽</b>.\nТекущий баланс: <b>{new_bal:.2f} ₽</b>", parse_mode="HTML")
    try:
        await bot.send_message(
            chat_id=uid,
            text=f"💳 <b>Ваш баланс пополнен!</b>\n\nСумма: <b>+{amount:.2f} ₽</b>\nТекущий баланс: <b>{new_bal:.2f} ₽</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.reply(f"⚠️ Не удалось уведомить пользователя в ЛС: {e}")

# 2. Инфо о пользователе: /user {ID}
@dp.message(Command("user"))
async def cmd_user(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Используй: <code>/user ID_ПОЛЬЗОВАТЕЛЯ</code>", parse_mode="HTML")
        return
    uid = int(parts[1])
    u = get_or_create_user(uid)
    status = "🚫 Заблокирован" if u["is_banned"] else "✅ Активен"
    text = (
        f"👤 <b>Пользователь:</b> {escape(u['full_name'] or 'Без имени')}\n"
        f"🔹 <b>Username:</b> @{escape(u['username'] or 'не указан')}\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n"
        f"💰 <b>Баланс:</b> <b>{u['balance']:.2f} ₽</b>\n"
        f"Статус: {status}\n\n"
        f"Пополнить: <code>/addbalance {uid} 100</code>\n"
        f"Написать: <code>/send {uid} </code>"
    )
    await message.reply(text, parse_mode="HTML")

# 3. Отправка сообщения: /send {ID} текст
@dp.message(F.chat.id == int(ADMIN_CHAT_ID or 0))
async def handle_admin_messages(message: types.Message):
    full_text = message.text or message.caption or ""
    if not full_text.startswith("/send"):
        return

    parts = full_text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Используй: <code>/send ID_ПОЛЬЗОВАТЕЛЯ Текст</code>", parse_mode="HTML")
        return

    target_id = int(parts[1].strip())
    extra_text = parts[2].strip() if len(parts) > 2 else ""

    try:
        if message.content_type != types.ContentType.TEXT:
            await bot.copy_message(
                chat_id=target_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                caption=f"💬 <b>Поддержка VibeBoost:</b>\n\n{escape(extra_text)}" if extra_text else "💬 <b>Поддержка VibeBoost</b>",
                parse_mode="HTML"
            )
        else:
            if not extra_text:
                await message.reply("⚠️ Введите текст сообщения!")
                return
            await bot.send_message(
                chat_id=target_id,
                text=f"💬 <b>Поддержка VibeBoost:</b>\n\n{escape(extra_text)}",
                parse_mode="HTML"
            )
        await message.reply(f"✅ Доставлено <code>{target_id}</code>!", parse_mode="HTML")
    except Exception as e:
        await message.reply(f"❌ Ошибка отправки: {e}")

# 4. Бан / Разбан
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Используй: <code>/ban ID_ПОЛЬЗОВАТЕЛЯ</code>", parse_mode="HTML")
        return
    uid = int(parts[1].strip())
    set_user_ban(uid, True)
    await message.reply(f"🚫 Пользователь <code>{uid}</code> заблокирован.", parse_mode="HTML")

@dp.message(Command("unban"))
async def cmd_unban(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.reply("Используй: <code>/unban ID_ПОЛЬЗОВАТЕЛЯ</code>", parse_mode="HTML")
        return
    uid = int(parts[1].strip())
    set_user_ban(uid, False)
    await message.reply(f"✅ Пользователь <code>{uid}</code> разблокирован.", parse_mode="HTML")

# ==================== СООБЩЕНИЯ ОТ КЛИЕНТОВ ====================

@dp.message(F.chat.type == "private")
async def handle_user_incoming(message: types.Message):
    uid = message.from_user.id
    if str(uid) == str(ADMIN_CHAT_ID):
        return
    if is_user_banned(uid):
        return

    u = message.from_user
    full_name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "Без имени"
    uname = u.username or ""
    get_or_create_user(uid, username=uname, full_name=full_name)

    if message.text == "/start":
        await message.answer(
            f"👋 <b>Добро пожаловать в VibeBoost!</b>\n\n"
            f"Для оформления заказов откройте каталог внизу экрана.\n"
            f"Ваш ID: <code>{uid}</code>",
            parse_mode="HTML"
        )
        return

    header_text = (
        f"📩 <b>Входящее от клиента!</b>\n\n"
        f"👤 <b>От:</b> {escape(full_name)} (@{escape(uname or 'нет')})\n"
        f"🆔 <b>ID:</b> <code>{uid}</code>\n\n"
        f"✍️ Ответить: <code>/send {uid} </code>\n"
        f"💳 Пополнить: <code>/addbalance {uid} 100</code>\n"
        f"🚫 Забанить: <code>/ban {uid}</code>"
    )

    try:
        if message.content_type == types.ContentType.TEXT:
            await bot.send_message(
                chat_id=int(ADMIN_CHAT_ID),
                text=header_text + f"\n\n💬 <b>Текст:</b>\n{escape(message.text)}",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=header_text, parse_mode="HTML")
            await bot.copy_message(chat_id=int(ADMIN_CHAT_ID), from_chat_id=message.chat.id, message_id=message.message_id)
    except Exception as e:
        logging.error("Ошибка передачи сообщения админу: %s", e)


# ==================== REST API (WEBAPP) ====================

def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
    }

# 1. Авторизация и получение баланса при открытии сайта
async def handle_user_info(request: web.Request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())

    try:
        data = await request.json()
        tg_info = validate_and_parse_init_data(data.get("initData", ""), BOT_TOKEN)
        tg_user = tg_info["user"]
        uid = int(tg_user["id"])
        
        full_name = f"{tg_user.get('first_name', '')} {tg_user.get('last_name', '')}".strip()
        uname = tg_user.get("username", "")
        
        user = get_or_create_user(uid, username=uname, full_name=full_name)
        return web.json_response({
            "ok": True,
            "balance": user["balance"],
            "is_banned": bool(user["is_banned"]),
            "user": {
                "id": uid,
                "full_name": user["full_name"],
                "username": user["username"]
            }
        }, headers=cors_headers())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors_headers())

# 2. Оплата и создание заказа (с проверкой баланса)
async def handle_order(request: web.Request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())

    try:
        payload = await request.json()
        tg_info = validate_and_parse_init_data(payload.get("initData", ""), BOT_TOKEN)
        tg_user = tg_info["user"]
        uid = int(tg_user["id"])

        if is_user_banned(uid):
            return web.json_response({"ok": False, "error": "Ваш профиль заблокирован."}, status=403, headers=cors_headers())

        order = payload.get("order", {})
        price = float(order.get("total", 0.0))

        user = get_or_create_user(uid)
        current_balance = user["balance"]

        if current_balance < price:
            return web.json_response({
                "ok": False,
                "error": f"Недостаточно средств. Ваш баланс: {current_balance:.2f} ₽. К оплате: {price:.2f} ₽"
            }, status=402, headers=cors_headers())

        # СПИСАНИЕ БАЛАНСА
        new_balance = change_user_balance(uid, -price, comment=f"Оплата услуги #{order.get('service')}")

        # Сохранение в БД
        with get_db() as conn:
            conn.execute(
                "INSERT INTO orders (user_id, service_id, service_name, network, link, quantity, total_price) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, str(order.get("service")), str(order.get("name")), str(order.get("network")), str(order.get("link")), int(order.get("quantity")), price)
            )
            conn.commit()

        # Оповещение администратора
        text = (
            f"🛍 <b>НОВЫЙ ОПЛАЧЕННЫЙ ЗАКАЗ!</b>\n\n"
            f"👤 <b>Покупатель:</b> {escape(user['full_name'] or 'Без имени')} (@{escape(user['username'] or 'нет')})\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"💰 <b>Списано:</b> <b>{price:.2f} ₽</b> (Остаток: <b>{new_balance:.2f} ₽</b>)\n\n"
            f"🌐 <b>Площадка:</b> {escape(str(order.get('network', '—')))}\n"
            f"📁 <b>Категория:</b> {escape(str(order.get('category', '—')))}\n"
            f"⚙️ <b>Услуга:</b> {escape(str(order.get('name', '—')))}\n"
            f"🔢 <b>ID услуги:</b> <code>{escape(str(order.get('service', '—')))}</code>\n"
            f"🔗 <b>Ссылка:</b> {escape(str(order.get('link', '—')))}\n"
            f"📊 <b>Количество:</b> {escape(str(order.get('quantity', '—')))} шт.\n\n"
            f"💬 Написать: <code>/send {uid} </code>\n"
            f"💳 Баланс: <code>/addbalance {uid} </code>"
        )

        try:
            await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text, parse_mode="HTML")
        except Exception as e:
            logging.error("Не удалось уведомить админа: %s", e)

        return web.json_response({
            "ok": True,
            "new_balance": new_balance
        }, headers=cors_headers())

    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors_headers())

# 3. Запрос на пополнение баланса
async def handle_topup(request: web.Request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())

    try:
        data = await request.json()
        tg_info = validate_and_parse_init_data(data.get("initData", ""), BOT_TOKEN)
        tg_user = tg_info["user"]
        uid = int(tg_user["id"])
        amount = float(data.get("amount", 0))
        method = str(data.get("method", "manual"))

        u = get_or_create_user(uid)
        
        # Уведомление админу о желании пополнить
        admin_text = (
            f"💳 <b>ЗАПРОС НА ПОПОЛНЕНИЕ БАЛАНСА!</b>\n\n"
            f"👤 <b>Клиент:</b> {escape(u['full_name'] or 'Без имени')} (@{escape(u['username'] or 'нет')})\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"💵 <b>Сумма:</b> <b>{amount:.2f} ₽</b>\n"
            f"Способ: {escape(method)}\n\n"
            f"Для зачисления отправь:\n"
            f"<code>/addbalance {uid} {amount:.0f}</code>"
        )
        await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=admin_text, parse_mode="HTML")

        # Ответ клиенту в боте
        try:
            await bot.send_message(
                chat_id=uid,
                text=(
                    f"💳 <b>Заявка на пополнение: {amount:.2f} ₽</b>\n\n"
                    f"Оператор получил вашу заявку. Напишите в этот чат для получения реквизитов (СБП/Карта/Крипта) или дождитесь ответа оператора!"
                ),
                parse_mode="HTML"
            )
        except Exception:
            pass

        return web.json_response({
            "ok": True,
            "message": f"Заявка на {amount:.2f} ₽ принята! Напишите боту для получения реквизитов."
        }, headers=cors_headers())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors_headers())

async def health(request: web.Request):
    return web.json_response({"ok": True})

# --- Жизненный цикл сервиса ---
async def start_bot(app):
    await bot.delete_webhook(drop_pending_updates=True)
    app["bot_task"] = asyncio.create_task(dp.start_polling(bot))

async def stop_bot(app):
    if "bot_task" in app:
        app["bot_task"].cancel()
    await dp.stop_polling()
    await bot.session.close()

app = web.Application()
app.router.add_post("/api/user/info", handle_user_info)
app.router.add_options("/api/user/info", handle_user_info)
app.router.add_post("/api/order", handle_order)
app.router.add_options("/api/order", handle_order)
app.router.add_post("/api/topup", handle_topup)
app.router.add_options("/api/topup", handle_topup)
app.router.add_get("/health", health)

app.on_startup.append(start_bot)
app.on_cleanup.append(stop_bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
