import os, time, json, hmac, hashlib, urllib.parse, logging
from pathlib import Path
from html import escape
from aiohttp import web
from aiogram import Bot
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_ORIGIN = os.getenv("WEBAPP_ORIGIN", "*")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)

def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Vary": "Origin",
    }

def validate_init_data(init_data: str) -> dict:
    if not init_data:
        raise ValueError("Telegram initData отсутствует")
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("В initData отсутствует hash")
    auth_date = int(pairs.get("auth_date", "0"))
    if not auth_date or not -30 <= time.time() - auth_date <= 3600:
        raise ValueError("initData устарел")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("Неверная подпись Telegram initData")
    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("Пользователь Telegram не найден")
    return user

def clean(value, limit=1500):
    return escape(str(value or "—")[:limit])

async def order(request: web.Request):
    headers = cors_headers()
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)
    try:
        data = await request.json()
        user = validate_init_data(data.get("initData", ""))
        order_data = data.get("order") or {}
        quantity = int(order_data.get("quantity"))
        if quantity <= 0 or quantity > 2_147_483_647:
            raise ValueError("Некорректное количество")
        link = clean(order_data.get("link"), 700)
        username = "@" + user["username"] if user.get("username") else "не указан"
        name = clean((user.get("first_name", "") + " " + user.get("last_name", "")).strip(), 200)
        text = (
            "🛒 <b>Новая заявка VibeBoost</b>\n\n"
            f"👤 <b>Пользователь:</b> {name}\n"
            f"🔹 <b>Username:</b> {username}\n"
            f"🆔 <b>ID:</b> <code>{user['id']}</code>\n\n"
            f"🌐 <b>Площадка:</b> {clean(order_data.get('network'), 100)}\n"
            f"📁 <b>Категория:</b> {clean(order_data.get('category'), 200)}\n"
            f"⚙️ <b>Услуга:</b> {clean(order_data.get('name'), 300)}\n"
            f"🔢 <b>ID услуги:</b> {clean(order_data.get('service'), 30)}\n"
            f"🔗 <b>Ссылка:</b> {link}\n"
            f"📊 <b>Количество:</b> {quantity}\n"
            f"💰 <b>Предварительная сумма (с сайта, требует проверки):</b> {clean(order_data.get('total'), 30)} ₽"
        )
        await bot.send_message(ADMIN_CHAT_ID, text, parse_mode="HTML")
        return web.json_response({"ok": True}, headers=headers)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=headers)
    except Exception:
        logging.exception("Ошибка обработки заявки")
        return web.json_response({"ok": False, "error": "Внутренняя ошибка"}, status=500, headers=headers)

async def health(request):
    return web.json_response({"ok": True})

async def cleanup(app):
    await bot.session.close()

app = web.Application(client_max_size=32768)
app.on_cleanup.append(cleanup)
app.router.add_route("OPTIONS", "/api/order", order)
app.router.add_post("/api/order", order)
app.router.add_get("/health", health)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)