import os, hmac, hashlib, json, logging, asyncio
from pathlib import Path
from urllib.parse import parse_qsl, quote
from html import escape

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("vibeboost")
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_ORIGIN = os.environ["WEBAPP_ORIGIN"].rstrip("/")
SHEETS_API_URL = os.environ["SHEETS_API_URL"].strip()
SHEETS_API_SECRET = os.environ["SHEETS_API_SECRET"].strip()
TWIBOOST_API_KEY = os.environ["TWIBOOST_API_KEY"].strip()
PORT = int(os.getenv("PORT", "8080"))
CATALOG_PATH = Path(os.getenv("CATALOG_PATH", "services.json"))

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
http: ClientSession | None = None


def cors():
    return {"Access-Control-Allow-Origin": WEBAPP_ORIGIN,
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
            "Vary": "Origin"}


def validate_init_data(raw: str) -> dict:
    if not raw:
        raise ValueError("Telegram initData отсутствует")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("В initData отсутствует hash")
    auth_date = int(pairs.get("auth_date", "0"))
    import time
    if not auth_date or time.time() - auth_date > 86400 or auth_date - time.time() > 30:
        raise ValueError("initData устарел; перезапустите Mini App")
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    actual = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual, received):
        raise ValueError("Неверная подпись Telegram initData")
    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("В initData нет пользователя")
    return user


def catalog():
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Каталог не загружен ({CATALOG_PATH}): {e}")
    if not isinstance(data, list):
        raise RuntimeError("services.json должен содержать JSON-массив")
    return {str(x["service"]): x for x in data if isinstance(x, dict) and x.get("service") is not None}


async def sheets(action: str, **fields):
    assert http is not None
    body = {"secret": SHEETS_API_SECRET, "action": action, **fields}
    async with http.post(SHEETS_API_URL, json=body, allow_redirects=True) as r:
        text = await r.text()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Sheets вернул не JSON (HTTP {r.status}): {text[:300]}")
        if r.status >= 400 or not result.get("ok"):
            raise RuntimeError(str(result.get("error", f"Sheets HTTP {r.status}")))
        return result


async def auth_user(payload):
    user = validate_init_data(payload.get("initData", ""))
    full_name = (str(user.get("first_name", "")) + " " + str(user.get("last_name", ""))).strip()
    result = await sheets("get_user", user_id=int(user["id"]), username=user.get("username", ""), full_name=full_name)
    if result.get("is_banned"):
        raise PermissionError("Профиль заблокирован")
    return user, result


async def user_info(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors())

    try:
        payload = await request.json()
        log.info(
            "USER_INFO: initData_present=%s, initData_length=%s",
            bool(payload.get("initData")),
            len(payload.get("initData", "")),
        )

        user, data = await auth_user(payload)

        log.info(
            "USER_INFO OK: user_id=%s balance=%s",
            user["id"],
            data.get("balance", 0),
        )

        return web.json_response(
            {
                "ok": True,
                "balance": float(data.get("balance", 0)),
                "user": {
                    "id": user["id"],
                    "first_name": user.get("first_name", ""),
                    "username": user.get("username", ""),
                },
            },
            headers=cors(),
        )

    except PermissionError as e:
        log.warning("USER_INFO FORBIDDEN: %s", e)
        return web.json_response(
            {"ok": False, "error": str(e)},
            status=403,
            headers=cors(),
        )

    except Exception as e:
        log.exception("USER_INFO ERROR")
        return web.json_response(
            {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            },
            status=400,
            headers=cors(),
        )


async def topup_request(request):
    if request.method == "OPTIONS": return web.Response(status=204, headers=cors())
    try:
        payload = await request.json()
        user, _ = await auth_user(payload)
        amount = round(float(payload.get("amount", 0)), 2)
        method = str(payload.get("method", "manual"))[:40]
        if amount < 10 or amount > 100000: raise ValueError("Сумма должна быть от 10 до 100000 ₽")
        await bot.send_message(ADMIN_CHAT_ID,
            f"💳 Запрос пополнения\nID: {user['id']}\nСумма: {amount:.2f} ₽\nМетод: {escape(method)}\n"
            f"Начислить после проверки оплаты: /addbalance {user['id']} {amount:.2f}")
        return web.json_response({"ok": True, "message": "Запрос отправлен администратору. Баланс изменится после подтверждения оплаты."}, headers=cors())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors())


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Откройте каталог через кнопку Mini App. Для связи с поддержкой напишите сообщение сюда.")

@dp.message(Command("addbalance"))
async def add_balance_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID: return
    p = (message.text or "").split(maxsplit=2)
    if len(p) != 3 or not p[1].isdigit():
        await message.reply("Формат: /addbalance user_id сумма"); return
    amount = round(float(p[2].replace(",", ".")), 2)
    if not 0 < amount <= 1000000:
        await message.reply("Сумма должна быть больше 0 и не больше 1000000"); return
    res = await sheets("credit", user_id=int(p[1]), amount=amount, comment="Начисление администратором")
    await message.reply(f"Баланс {p[1]} пополнен на {amount:.2f} ₽. Остаток: {float(res['balance']):.2f} ₽")
    try: await bot.send_message(int(p[1]), f"Баланс пополнен на {amount:.2f} ₽. Текущий баланс: {float(res['balance']):.2f} ₽")
    except Exception: pass

@dp.message(Command("ban"))
async def ban_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID: return
    p=(message.text or "").split(maxsplit=1)
    if len(p)<2 or not p[1].isdigit(): await message.reply("Формат: /ban user_id"); return
    await sheets("ban", user_id=int(p[1])); await message.reply(f"Пользователь {p[1]} заблокирован.")

@dp.message(Command("unban"))
async def unban_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID: return
    p=(message.text or "").split(maxsplit=1)
    if len(p)<2 or not p[1].isdigit(): await message.reply("Формат: /unban user_id"); return
    await sheets("unban", user_id=int(p[1])); await message.reply(f"Пользователь {p[1]} разблокирован.")

@dp.message(Command("send"))
async def send_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID: return
    p=(message.text or "").split(maxsplit=2)
    if len(p)<3 or not p[1].isdigit(): await message.reply("Формат: /send user_id текст"); return
    try: await bot.send_message(int(p[1]), p[2]); await message.reply("Отправлено.")
    except Exception as e: await message.reply("Не отправлено: " + str(e))

@dp.message(F.chat.type == "private")
async def inbound(message: types.Message):
    if message.from_user.id == ADMIN_CHAT_ID: return
    try:
        # Apps Script stores user and enforces bans on protected API actions.
        await sheets("get_user", user_id=message.from_user.id,
                     username=message.from_user.username or "",
                     full_name=message.from_user.full_name or "")
        if message.text:
            await bot.send_message(ADMIN_CHAT_ID,
                f"📩 Сообщение от {escape(message.from_user.full_name)}\nID: <code>{message.from_user.id}</code>\n"
                f"{escape(message.text)}\nОтвет: <code>/send {message.from_user.id} </code>", parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_CHAT_ID,
                f"📩 Медиа от {escape(message.from_user.full_name)}\nID: <code>{message.from_user.id}</code>\n"
                f"Ответ: <code>/send {message.from_user.id} </code>", parse_mode="HTML")
            await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    except Exception: log.exception("Could not forward user message")

async def health(request): return web.json_response({"ok": True})

async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=25))
    await bot.delete_webhook(drop_pending_updates=True)
    app["polling"] = asyncio.create_task(dp.start_polling(bot))

async def on_cleanup(app):
    task=app.get("polling")
    if task:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
    if http: await http.close()
    await bot.session.close()

app=web.Application(client_max_size=32768)
for path, handler in [("/api/user/info", user_info), ("/api/order", submit_order), ("/api/topup", topup_request)]:
    app.router.add_route("OPTIONS", path, handler)
    app.router.add_post(path, handler)
app.router.add_get("/health", health)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__": web.run_app(app, host="0.0.0.0", port=PORT)
