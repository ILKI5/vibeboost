import os
import logging
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
import asyncio

# --- Настройка ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "https://vibeboostgo.vercel.app")
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Логика БОТА (Команды) ---
@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    if str(message.from_user.id) != str(ADMIN_CHAT_ID):
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply("Используй: /send ID_ПОЛЬЗОВАТЕЛЯ Текст")
        return

    target_id = parts[1].strip()
    text = parts[2].strip()

    try:
        await bot.send_message(chat_id=int(target_id), text=text)
        await message.reply(f"✅ Доставлено {target_id}")
    except Exception as e:
        await message.reply(f"❌ Ошибка: {e}")

# --- Логика ВЕБ-СЕРВЕРА (Заказы) ---
def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

async def handle_order(request):
    if request.method == "OPTIONS":
        return web.Response(headers=cors_headers())
    
    try:
        data = await request.json()
        order = data.get("order", {})
        
        # Формируем сообщение админу
        text = (
            f"📦 <b>Новый заказ!</b>\n"
            f"Услуга: {order.get('name')}\n"
            f"Ссылка: {order.get('link')}\n"
            f"Количество: {order.get('quantity')}\n"
            f"Сумма: {order.get('total')} ₽\n\n"
            f"👉 <code>/send {data.get('user_id', '???')} </code>"
        )
        
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML")
        
        return web.json_response({"ok": True}, headers=cors_headers())
    except Exception as e:
        logging.error(f"Error: {e}")
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors_headers())

async def health(request):
    return web.json_response({"ok": True})

# --- Запуск ---
async def start_bot(app):
    asyncio.create_task(dp.start_polling(bot))

app = web.Application()
app.router.add_post("/api/order", handle_order)
app.router.add_options("/api/order", handle_order)
app.router.add_get("/health", health)
app.on_startup.append(start_bot)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
