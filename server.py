import os
import hmac
import hashlib
import json
import logging
import asyncio
import time
from urllib.parse import parse_qsl
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
CATALOG_PATH = os.getenv("CATALOG_PATH", "services.json")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
http: ClientSession | None = None


def cors_headers():
    return {
        "Access-Control-Allow-Origin": WEBAPP_ORIGIN,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def validate_init_data(raw: str) -> dict:
    if not raw:
        raise ValueError("Telegram initData отсутствует")
    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("В initData отсутствует hash")
    auth_date = int(pairs.get("auth_date", "0"))
    if not auth_date or time.time() - auth_date > 86400:
        raise ValueError("initData устарел; перезапустите Mini App")
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("Неверная подпись Telegram initData")
    user = json.loads(pairs.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("В initData отсутствует пользователь")
    return user


def load_catalog():
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        raise RuntimeError(f"Каталог недоступен: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("services.json должен быть JSON-массивом")
    return {str(x["service"]): x for x in data if isinstance(x, dict) and x.get("service") is not None}


async def sheets(action: str, **fields):
    if http is None:
        raise RuntimeError("HTTP session не запущена")
    body = {"secret": SHEETS_API_SECRET, "action": action, **fields}
    async with http.post(SHEETS_API_URL, json=body, allow_redirects=True) as response:
        raw = await response.text()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Google Sheets вернул не JSON (HTTP {response.status})") from exc
        if response.status >= 400 or not result.get("ok"):
            raise RuntimeError(str(result.get("error", f"Sheets HTTP {response.status}")))
        return result


async def get_authenticated_user(payload):
    user = validate_init_data(str(payload.get("initData", "")))
    full_name = (str(user.get("first_name", "")) + " " + str(user.get("last_name", ""))).strip()
    profile = await sheets(
        "get_user", user_id=int(user["id"]),
        username=user.get("username", ""), full_name=full_name
    )
    if profile.get("is_banned"):
        raise PermissionError("Профиль заблокирован администратором")
    return user, profile


async def user_info(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        user, profile = await get_authenticated_user(payload)
        return web.json_response({
            "ok": True,
            "balance": float(profile.get("balance", 0)),
            "user": {"id": user["id"], "first_name": user.get("first_name", ""),
                     "username": user.get("username", "")},
        }, headers=cors_headers())
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("/api/user/info failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


async def send_order_receipt_to_user(user_id: int, order: dict, provider_order_id: str, charged: float, balance_after: float):
    """Отправляет покупателю чек в личный чат с ботом. Не отменяет успешный заказ при ошибке уведомления."""
    text = (
        "✅ <b>Ваш заказ оформлен</b>\n\n"
        f"🌐 Площадка: {escape(str(order.get('network', '—')))}\n"
        f"📁 Категория: {escape(str(order.get('category', '—')))}\n"
        f"⚙️ Услуга: {escape(str(order.get('name', '—')))}\n"
        f"🔢 ID услуги: <code>{escape(str(order.get('service', '—')))}</code>\n"
        f"🔗 Ссылка: {escape(str(order.get('link', '—')))}\n"
        f"📊 Количество: {int(order.get('quantity', 0))}\n"
        f"💰 Списано: <b>{charged:.2f} ₽</b>\n"
        f"💳 Остаток: <b>{balance_after:.2f} ₽</b>\n"
        f"🧾 Номер заказа: <code>{escape(str(provider_order_id))}</code>"
    )
    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")


async def submit_order(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())

    try:
        payload = await request.json()
        user, profile = await get_authenticated_user(payload)
        user_id = int(user["id"])
        order_payload = payload.get("order") or {}
        service_id = str(order_payload.get("service", ""))
        item = load_catalog().get(service_id)
        if not item:
            raise ValueError("Услуга не найдена в каталоге")

        quantity = int(order_payload.get("quantity", 0))
        minimum, maximum = int(item["min"]), int(item["max"])
        if quantity < minimum or quantity > maximum:
            raise ValueError(f"Количество должно быть от {minimum} до {maximum}")
        link = str(order_payload.get("link", "")).strip()
        if not link or len(link) > 700:
            raise ValueError("Некорректная ссылка")

        # Авторитетная цена из каталога на сервере; цене, присланной HTML, не доверяем.
        charge_amount = round(float(item["rate"]) * quantity / 1000, 2)
        if charge_amount <= 0:
            raise ValueError("Некорректная стоимость услуги")

        # Списание атомарно выполняется Apps Script под LockService.
        charge = await sheets("charge", user_id=user_id, amount=charge_amount,
                              comment=f"Заказ услуги {service_id}")

        if http is None:
            await sheets("refund", user_id=user_id, amount=charge_amount,
                         comment="Возврат: API поставщика недоступен")
            raise RuntimeError("API поставщика временно недоступен")

        provider_params = {
            "action": "add", "service": service_id, "link": link,
            "quantity": str(quantity), "key": TWIBOOST_API_KEY,
        }
        try:
            async with http.get("https://twiboost.com/api/v2", params=provider_params,
                                timeout=ClientTimeout(total=35)) as response:
                raw_provider = await response.text()
                provider_data = json.loads(raw_provider)
        except Exception as exc:
            # Таймаут не доказывает, что заказ не принят. Не делаем автоматический возврат,
            # чтобы не допустить бесплатного/двойного заказа. Сообщаем админу для проверки.
            log.exception("Provider result uncertain, user=%s", user_id)
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"⚠️ Не удалось подтвердить ответ API по заказу пользователя <code>{user_id}</code>.\n"
                    f"Проверьте заказ в кабинете поставщика до возврата средств.\n#id{user_id}",
                    parse_mode="HTML",
                )
            except Exception:
                log.exception("Could not notify admin about uncertain provider result")
            return web.json_response({
                "ok": False,
                "error": "Статус заказа уточняется. Не отправляйте его повторно; обратитесь в поддержку."
            }, status=502, headers=cors_headers())

        provider_order_id = provider_data.get("order")
        if provider_order_id is None:
            provider_error = str(provider_data.get("error", "Поставщик отклонил заказ"))
            await sheets("refund", user_id=user_id, amount=charge_amount,
                         comment=f"Возврат: поставщик отклонил услугу {service_id}")
            try:
                await bot.send_message(
                    ADMIN_CHAT_ID,
                    f"❌ Поставщик отклонил заказ. Пользователь ID <code>{user_id}</code>; "
                    f"услуга {escape(service_id)}. Средства возвращены.\n#id{user_id}",
                    parse_mode="HTML",
                )
            except Exception:
                log.exception("Could not notify admin about provider rejection")
            return web.json_response({"ok": False, "error": provider_error}, status=502, headers=cors_headers())

        provider_order_id = str(provider_order_id)
        new_balance = float(charge.get("balance", float(profile.get("balance", 0)) - charge_amount))
        full_name = (str(user.get("first_name", "")) + " " + str(user.get("last_name", ""))).strip() or "Без имени"
        username = "@" + user["username"] if user.get("username") else "не указан"
        tag = f"#id{user_id}"
        admin_text = (
            "🛒 <b>Новый заказ принят</b>\n\n"
            f"👤 Пользователь: {escape(full_name)}\n"
            f"🔹 Username: {escape(username)}\n"
            f"🆔 ID: <code>{user_id}</code>\n\n"
            f"🌐 Площадка: {escape(str(item.get('network', '—')))}\n"
            f"📁 Категория: {escape(str(item.get('category', '—')))}\n"
            f"⚙️ Услуга: {escape(str(item.get('name', '—')))}\n"
            f"🔢 ID услуги: <code>{escape(service_id)}</code>\n"
            f"🔗 Ссылка: {escape(link)}\n"
            f"📊 Количество: {quantity}\n"
            f"💰 Списано: {charge_amount:.2f} ₽\n"
            f"🧾 Номер заказа: <code>{escape(provider_order_id)}</code>\n\n{tag}"
        )
        try:
            await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
        except Exception:
            log.exception("Could not notify admin about order")

        # Подробности заказа отправляются также самому покупателю в чат с ботом.
        try:
            await send_order_receipt_to_user(
                user_id, {**item, **order_payload, "link": link, "quantity": quantity,
                          "service": service_id},
                provider_order_id, charge_amount, new_balance
            )
        except Exception:
            # Заказ уже принят и оплачен; ошибка уведомления не должна превращать покупку в ошибку.
            log.exception("Order accepted but customer receipt could not be delivered; user=%s", user_id)

        return web.json_response({
            "ok": True,
            "provider_id": provider_order_id,
            "new_balance": new_balance,
            "message_sent_to_user": True,
        }, headers=cors_headers())

    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("Order creation failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500, headers=cors_headers())


async def topup_request(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        user, _ = await get_authenticated_user(payload)
        amount = round(float(payload.get("amount", 0)), 2)
        method = str(payload.get("method", "manual"))[:40]
        if not 10 <= amount <= 100000:
            raise ValueError("Сумма должна быть от 10 до 100000 ₽")
        tag = f"#id{user['id']}"
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💳 <b>Запрос пополнения</b>\nID: <code>{user['id']}</code>\n"
            f"Сумма: {amount:.2f} ₽\nСпособ: {escape(method)}\n{tag}",
            parse_mode="HTML",
        )
        return web.json_response({"ok": True, "message": "Запрос отправлен администратору."}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Добро пожаловать! Откройте Mini App через меню бота. Подробности заказов будут приходить сюда.")


@dp.message(Command("addbalance"))
async def add_balance(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.reply("Формат: /addbalance user_id сумма")
        return
    amount = round(float(parts[2].replace(",", ".")), 2)
    if amount <= 0 or amount > 1000000:
        await message.reply("Сумма должна быть больше 0 и не более 1000000")
        return
    result = await sheets("credit", user_id=int(parts[1]), amount=amount,
                          comment="Пополнение администратором")
    await message.reply(f"Баланс {parts[1]} пополнен на {amount:.2f} ₽. Остаток: {float(result['balance']):.2f} ₽")
    try:
        await bot.send_message(int(parts[1]), f"Баланс пополнен на {amount:.2f} ₽. Текущий баланс: {float(result['balance']):.2f} ₽")
    except Exception:
        log.exception("Could not notify user about balance credit")


@dp.message(Command("send"))
async def send_message_to_user(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Формат: /send user_id текст")
        return
    try:
        await bot.send_message(int(parts[1]), parts[2])
        await message.reply("✅ Сообщение отправлено")
    except Exception as exc:
        await message.reply("❌ Не удалось отправить: " + str(exc))


@dp.message(F.chat.type == "private")
async def forward_user_message(message: types.Message):
    if message.from_user.id == ADMIN_CHAT_ID:
        return
    user = message.from_user
    tag = f"#id{user.id}"
    text = message.text or message.caption or "(сообщение с медиа)"
    admin_text = (
        f"📩 <b>Сообщение от пользователя</b>\n"
        f"👤 {escape(user.full_name)}\n"
        f"🆔 <code>{user.id}</code>\n\n"
        f"{escape(text)}\n\n"
        f"Ответить: <code>/send {user.id} </code>\n{tag}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
        if message.content_type != types.ContentType.TEXT:
            await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    except Exception:
        log.exception("Could not forward inbound user message")


async def health(request):
    return web.json_response({"ok": True})


async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=25))
    await bot.delete_webhook(drop_pending_updates=True)
    app["polling_task"] = asyncio.create_task(dp.start_polling(bot))


async def on_cleanup(app):
    task = app.get("polling_task")
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
for route, handler in (
    ("/api/user/info", user_info),
    ("/api/order", submit_order),
    ("/api/topup", topup_request),
):
    app.router.add_route("OPTIONS", route, handler)
    app.router.add_post(route, handler)
app.router.add_get("/health", health)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
