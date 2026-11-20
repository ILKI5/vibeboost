import os, hmac, hashlib, json, logging, asyncio
from pathlib import Path
from urllib.parse import parse_qsl, unquote
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
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin"
    }


def validate_init_data(raw: str) -> dict:
    if not raw:
        raise ValueError("Telegram initData отсутствует (пустая строка)")

    # Support raw hash string or tgWebAppData prefix
    raw_str = raw
    if "tgWebAppData=" in raw_str:
        raw_str = raw_str.split("tgWebAppData=", 1)[1].split("&tgWebAppVersion=", 1)[0]
        raw_str = unquote(raw_str)

    pairs = dict(parse_qsl(raw_str, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise ValueError("В initData отсутствует параметр hash")

    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    actual = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(actual, received):
        raise ValueError("Неверная подпись Telegram initData (токен в Render не совпадает с ботом!)")

    user_raw = pairs.get("user", "{}")
    user = json.loads(user_raw)
    if not user.get("id"):
        raise ValueError("В initData нет данных пользователя")
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
            raise RuntimeError(f"Google Таблица вернула не JSON (HTTP {r.status}): {text[:300]}")
        if r.status >= 400 or not result.get("ok"):
            err = result.get("error", f"Sheets HTTP {r.status}")
            raise RuntimeError(f"Google Таблица: {err}")
        return result


async def auth_user(payload):
    init_data = payload.get("initData", "")
    user = validate_init_data(init_data)
    full_name = (str(user.get("first_name", "")) + " " + str(user.get("last_name", ""))).strip()
    result = await sheets("get_user", user_id=int(user["id"]), username=user.get("username", ""), full_name=full_name)
    if result.get("is_banned"):
        raise PermissionError("Ваш профиль заблокирован администратором")
    return user, result


async def user_info(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors())

    try:
        payload = await request.json()
        user, data = await auth_user(payload)
        log.info("Успешная авторизация пользователя %s, баланс: %s", user['id'], data.get('balance', 0))
        return web.json_response({
            "ok": True,
            "balance": float(data.get("balance", 0)),
            "user": {
                "id": user["id"],
                "first_name": user.get("first_name", ""),
                "username": user.get("username", "")
            }
        }, headers=cors())
    except PermissionError as e:
        log.warning("Заблокированный пользователь: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=403, headers=cors())
    except Exception as e:
        log.error("Ошибка в /api/user/info: %s", e)
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors())


async def submit_order(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors())

    charged = False
    uid = None
    amount = 0.0
    try:
        payload = await request.json()
        user, profile = await auth_user(payload)
        uid = int(user["id"])
        order = payload.get("order") or {}
        sid = str(order.get("service", ""))
        
        cat = catalog()
        item = cat.get(sid)
        if not item:
            raise ValueError(f"Услуга ID {sid} отсутствует в серверном каталоге")

        quantity = int(order.get("quantity", 0))
        minimum, maximum = int(item["min"]), int(item["max"])
        if quantity < minimum or quantity > maximum:
            raise ValueError(f"Количество должно быть от {minimum} до {maximum}")

        link = str(order.get("link", "")).strip()
        if not link or len(link) > 700:
            raise ValueError("Некорректная ссылка")

        # Точный расчет цены на сервере
        amount = round(float(item["rate"]) * quantity / 1000, 2)
        if amount <= 0:
            raise ValueError("Некорректная стоимость услуги")

        # Списание средств
        charge = await sheets("charge", user_id=uid, amount=amount, comment=f"Заказ услуги {sid}")
        charged = True

        # Отправка поставщику
        assert http is not None
        params = {
            "action": "add",
            "service": sid,
            "link": link,
            "quantity": str(quantity),
            "key": TWIBOOST_API_KEY
        }
        async with http.get("https://twiboost.com/api/v2", params=params, timeout=ClientTimeout(total=35)) as r:
            provider_text = await r.text()
            try:
                provider = json.loads(provider_text)
            except json.JSONDecodeError:
                log.error("Поставщик вернул не JSON. uid=%s ответ=%s", uid, provider_text[:300])
                return web.json_response({
                    "ok": False,
                    "error": "Не удалось однозначно подтвердить заказ. Напишите поддержке; повторно не нажимайте."
                }, status=502, headers=cors())

        if provider.get("order") is None:
            # Ошибка у поставщика — возвращаем баланс
            await sheets("refund", user_id=uid, amount=amount, comment=f"Возврат по услуге {sid}")
            charged = False
            err_msg = str(provider.get("error", "неизвестная ошибка"))
            return web.json_response({
                "ok": False,
                "error": f"Поставщик отклонил заказ: {err_msg}"
            }, status=502, headers=cors())

        provider_id = str(provider["order"])
        full_name = (str(user.get("first_name", "")) + " " + str(user.get("last_name", ""))).strip() or "Без имени"
        username = "@" + user["username"] if user.get("username") else "не указан"

        msg = (
            f"🛒 <b>Новый заказ принят поставщиком!</b>\n\n"
            f"👤 <b>Пользователь:</b> {escape(full_name)}\n"
            f"🔹 <b>Username:</b> {escape(username)}\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n\n"
            f"🌐 <b>Площадка:</b> {escape(str(item.get('network','—')))}\n"
            f"📁 <b>Категория:</b> {escape(str(item.get('category','—')))}\n"
            f"⚙️ <b>Услуга:</b> {escape(str(item.get('name','—')))}\n"
            f"🔢 <b>ID услуги:</b> {sid}\n"
            f"🔗 <b>Ссылка:</b> {escape(link)}\n"
            f"📊 <b>Количество:</b> {quantity}\n"
            f"💰 <b>Списано:</b> {amount:.2f} ₽\n"
            f"🧾 <b>ID у поставщика:</b> <code>{escape(provider_id)}</code>\n\n"
            f"💬 Написать: <code>/send {uid} </code>"
        )
        try:
            await bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
        except Exception:
            log.exception("Не удалось отправить сообщение админу")

        new_bal = float(charge.get("balance", profile.get("balance", 0) - amount))
        return web.json_response({
            "ok": True,
            "provider_id": provider_id,
            "new_balance": new_bal
        }, headers=cors())

    except PermissionError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=403, headers=cors())
    except Exception as e:
        log.exception("Ошибка при создании заказа")
        return web.json_response({"ok": False, "error": str(e)}, status=500, headers=cors())


async def topup_request(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors())

    try:
        payload = await request.json()
        user, _ = await auth_user(payload)
        amount = round(float(payload.get("amount", 0)), 2)
        method = str(payload.get("method", "manual"))[:40]
        if amount < 10 or amount > 100000:
            raise ValueError("Сумма должна быть от 10 до 100000 ₽")

        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💳 <b>Запрос на пополнение баланса!</b>\n\n"
            f"👤 ID: <code>{user['id']}</code>\n"
            f"💵 Сумма: <b>{amount:.2f} ₽</b>\n"
            f"Способ: {escape(method)}\n\n"
            f"Зачислить после оплаты:\n"
            f"<code>/addbalance {user['id']} {amount:.2f}</code>",
            parse_mode="HTML"
        )
        return web.json_response({
            "ok": True,
            "message": "Запрос отправлен администратору. Ожидайте подтверждения."
        }, headers=cors())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=cors())


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("👋 Добро пожаловать! Откройте меню для заказа услуг или напишите вопрос сюда.")


@dp.message(Command("addbalance"))
async def add_balance_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    p = (message.text or "").split(maxsplit=2)
    if len(p) != 3 or not p[1].isdigit():
        await message.reply("Используй: /addbalance user_id сумма")
        return
    amount = round(float(p[2].replace(",", ".")), 2)
    if not 0 < amount <= 1000000:
        await message.reply("Сумма должна быть больше 0")
        return
    res = await sheets("credit", user_id=int(p[1]), amount=amount, comment="Пополнение администратором")
    await message.reply(f"✅ Баланс {p[1]} пополнен на {amount:.2f} ₽. Остаток: {float(res['balance']):.2f} ₽")
    try:
        await bot.send_message(int(p[1]), f"💳 Ваш баланс пополнен на {amount:.2f} ₽. Текущий баланс: {float(res['balance']):.2f} ₽")
    except Exception:
        pass


@dp.message(Command("ban"))
async def ban_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    p = (message.text or "").split(maxsplit=1)
    if len(p) < 2 or not p[1].isdigit():
        await message.reply("Используй: /ban user_id")
        return
    await sheets("ban", user_id=int(p[1]))
    await message.reply(f"🚫 Пользователь {p[1]} заблокирован.")


@dp.message(Command("unban"))
async def unban_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    p = (message.text or "").split(maxsplit=1)
    if len(p) < 2 or not p[1].isdigit():
        await message.reply("Используй: /unban user_id")
        return
    await sheets("unban", user_id=int(p[1]))
    await message.reply(f"✅ Пользователь {p[1]} разблокирован.")


@dp.message(Command("send"))
async def send_cmd(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    p = (message.text or "").split(maxsplit=2)
    if len(p) < 3 or not p[1].isdigit():
        await message.reply("Используй: /send user_id текст")
        return
    try:
        await bot.send_message(int(p[1]), p[2])
        await message.reply("✅ Сообщение отправлено.")
    except Exception as e:
        await message.reply(f"❌ Не отправлено: {e}")


@dp.message(F.chat.type == "private")
async def inbound(message: types.Message):
    if message.from_user.id == ADMIN_CHAT_ID:
        return
    try:
        await sheets(
            "get_user",
            user_id=message.from_user.id,
            username=message.from_user.username or "",
            full_name=message.from_user.full_name or ""
        )
        if message.text:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"📩 Сообщение от {escape(message.from_user.full_name)}\n"
                f"ID: <code>{message.from_user.id}</code>\n"
                f"{escape(message.text)}\n\n"
                f"Ответить: <code>/send {message.from_user.id} </code>",
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"📩 Медиа от {escape(message.from_user.full_name)}\n"
                f"ID: <code>{message.from_user.id}</code>\n"
                f"Ответить: <code>/send {message.from_user.id} </code>",
                parse_mode="HTML"
            )
            await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    except Exception:
        log.exception("Не удалось переслать сообщение админу")


async def health(request):
    return web.json_response({"ok": True})


async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=25))
    await bot.delete_webhook(drop_pending_updates=True)
    app["polling"] = asyncio.create_task(dp.start_polling(bot))


async def on_cleanup(app):
    task = app.get("polling")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    if http:
        await http.close()
    await bot.session.close()


app = web.Application(client_max_size=32768)
for path, handler in [
    ("/api/user/info", user_info),
    ("/api/order", submit_order),
    ("/api/topup", topup_request)
]:
    app.router.add_route("OPTIONS", path, handler)
    app.router.add_post(path, handler)

app.router.add_get("/health", health)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
