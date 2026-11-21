import os
import hmac
import hashlib
import json
import logging
import asyncio
import time
from pathlib import Path
from urllib.parse import parse_qsl
from html import escape
from decimal import Decimal, ROUND_HALF_UP

from aiohttp import web, ClientSession, ClientTimeout
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

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
PAGE_SIZE = 8

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
http: ClientSession | None = None

class BotOrder(StatesGroup):
    waiting_link = State()
    waiting_quantity = State()
    confirming = State()


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
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Каталог недоступен ({CATALOG_PATH}): {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("services.json должен быть JSON-массивом")
    return {str(x["service"]): x for x in data if isinstance(x, dict) and x.get("service") is not None}


def format_money(value):
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}".replace(",", " ") + " ₽"


def order_cost(rate, quantity):
    return float((Decimal(str(rate)) * Decimal(int(quantity)) / Decimal(1000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


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
    profile = await sheets("get_user", user_id=int(user["id"]), username=user.get("username", ""), full_name=full_name)
    if profile.get("is_banned"):
        raise PermissionError("Профиль заблокирован администратором")
    return user, profile


async def user_info(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        user, profile = await get_authenticated_user(payload)
        return web.json_response({"ok": True, "balance": float(profile.get("balance", 0)),
            "user": {"id": user["id"], "first_name": user.get("first_name", ""), "username": user.get("username", "")}}, headers=cors_headers())
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("/api/user/info failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


async def send_order_receipt_to_user(user_id: int, order: dict, provider_order_id: str, charged: float, balance_after: float):
    text = (
        "✅ <b>Ваш заказ оформлен</b>\n\n"
        f"🌐 Площадка: {escape(str(order.get('network', '—')))}\n"
        f"📁 Категория: {escape(str(order.get('category', '—')))}\n"
        f"⚙️ Услуга: {escape(str(order.get('name', '—')))}\n"
        f"🔢 ID услуги: <code>{escape(str(order.get('service', '—')))}</code>\n"
        f"🔗 Ссылка: {escape(str(order.get('link', '—')))}\n"
        f"📊 Количество: {int(order.get('quantity', 0))}\n"
        f"💰 Списано: <b>{format_money(charged)}</b>\n"
        f"💳 Остаток: <b>{format_money(balance_after)}</b>\n"
        f"🧾 Номер заказа: <code>{escape(str(provider_order_id))}</code>\n\n#id{user_id}"
    )
    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")


async def process_order(user_id: int, item: dict, link: str, quantity: int):
    service_id = str(item["service"])
    charge_amount = order_cost(item["rate"], quantity)
    if charge_amount <= 0:
        raise ValueError("Некорректная стоимость услуги")

    # Atomically charge the user's account before placing an order.
    charged = await sheets("charge", user_id=user_id, amount=charge_amount, comment=f"Заказ услуги {service_id}")
    if http is None:
        await sheets("refund", user_id=user_id, amount=charge_amount, comment="Возврат: поставщик недоступен")
        raise RuntimeError("API поставщика временно недоступен")

    provider_params = {"action": "add", "service": service_id, "link": link,
                       "quantity": str(quantity), "key": TWIBOOST_API_KEY}
    try:
        async with http.get("https://twiboost.com/api/v2", params=provider_params,
                            timeout=ClientTimeout(total=35)) as response:
            raw = await response.text()
            provider_data = json.loads(raw)
    except Exception:
        log.exception("Provider status uncertain; user=%s", user_id)
        try:
            await bot.send_message(ADMIN_CHAT_ID,
                f"⚠️ Не удалось подтвердить ответ поставщика по заказу пользователя <code>{user_id}</code>. Проверьте кабинет до возврата.\n#id{user_id}",
                parse_mode="HTML")
        except Exception:
            log.exception("Admin notification failed")
        raise RuntimeError("Статус заказа уточняется. Не отправляйте его повторно; обратитесь в поддержку.")

    provider_order_id = provider_data.get("order")
    if provider_order_id is None:
        provider_error = str(provider_data.get("error", "Поставщик отклонил заказ"))
        await sheets("refund", user_id=user_id, amount=charge_amount, comment=f"Возврат: отказ поставщика, услуга {service_id}")
        raise RuntimeError("Поставщик отклонил заказ. Средства возвращены.")

    provider_order_id = str(provider_order_id)
    new_balance = float(charged.get("balance", 0))
    user_row = await bot.get_chat(user_id)
    name = escape(user_row.full_name or "Без имени")
    username = "@" + user_row.username if user_row.username else "не указан"
    admin_text = (
        "🛒 <b>Новый заказ принят</b>\n\n"
        f"👤 Пользователь: {name}\n🔹 Username: {escape(username)}\n🆔 ID: <code>{user_id}</code>\n\n"
        f"🌐 Площадка: {escape(str(item.get('network', '—')))}\n📁 Категория: {escape(str(item.get('category', '—')))}\n"
        f"⚙️ Услуга: {escape(str(item.get('name', '—')))}\n🔢 ID услуги: <code>{escape(service_id)}</code>\n"
        f"🔗 Ссылка: {escape(link)}\n📊 Количество: {quantity}\n💰 Списано: {format_money(charge_amount)}\n"
        f"🧾 Заказ поставщика: <code>{escape(provider_order_id)}</code>\n\n#id{user_id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
    except Exception:
        log.exception("Admin order notification failed")

    receipt_order = {**item, "service": service_id, "link": link, "quantity": quantity}
    try:
        await send_order_receipt_to_user(user_id, receipt_order, provider_order_id, charge_amount, new_balance)
    except Exception:
        log.exception("Order accepted, customer receipt failed; user=%s", user_id)

    return {"provider_id": provider_order_id, "new_balance": new_balance, "charge": charge_amount}


async def submit_order(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        user, _ = await get_authenticated_user(payload)
        order = payload.get("order") or {}
        service_id = str(order.get("service", ""))
        item = load_catalog().get(service_id)
        if not item:
            raise ValueError("Услуга не найдена в каталоге")
        quantity = int(order.get("quantity", 0))
        if not int(item["min"]) <= quantity <= int(item["max"]):
            raise ValueError(f"Количество должно быть от {item['min']} до {item['max']}")
        link = str(order.get("link", "")).strip()
        if not link or len(link) > 700:
            raise ValueError("Некорректная ссылка")
        result = await process_order(int(user["id"]), item, link, quantity)
        return web.json_response({"ok": True, **result}, headers=cors_headers())
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("Order creation failed")
        status = 402 if "недостаточно" in str(exc).lower() or "insufficient" in str(exc).lower() else 500
        return web.json_response({"ok": False, "error": str(exc)}, status=status, headers=cors_headers())


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
        await bot.send_message(ADMIN_CHAT_ID,
            f"💳 <b>Запрос пополнения</b>\nID: <code>{user['id']}</code>\nСумма: {format_money(amount)}\nСпособ: {escape(method)}\n#id{user['id']}",
            parse_mode="HTML")
        return web.json_response({"ok": True, "message": "Запрос отправлен администратору."}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


# -------------------- BOT CATALOG --------------------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Открыть каталог услуг", callback_data="cat:home")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="cat:balance")],
    ])


def back_kb(target="cat:home"):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=target)]])


def networks_kb():
    catalog = load_catalog()
    networks = sorted({str(x.get("network") or "Прочее") for x in catalog.values()}, key=str.casefold)
    rows = [[InlineKeyboardButton(text=f"{n} · {sum(1 for x in catalog.values() if str(x.get('network') or 'Прочее') == n)}", callback_data=f"cat:net:{i}")]
            for i, n in enumerate(networks)]
    rows.append([InlineKeyboardButton(text="✖ Закрыть", callback_data="cat:close")])
    return networks, InlineKeyboardMarkup(inline_keyboard=rows)


def categories_kb(network: str):
    catalog = load_catalog()
    categories = sorted({str(x.get("category") or "Прочее") for x in catalog.values()
                         if str(x.get("network") or "Прочее") == network}, key=str.casefold)
    rows = []
    for i, category in enumerate(categories):
        count = sum(1 for x in catalog.values()
                    if str(x.get("network") or "Прочее") == network and str(x.get("category") or "Прочее") == category)
        rows.append([InlineKeyboardButton(text=f"{category} · {count}", callback_data=f"cat:category:{i}")])
    rows.append([InlineKeyboardButton(text="← Соцсети", callback_data="cat:home")])
    return categories, InlineKeyboardMarkup(inline_keyboard=rows)


def services_kb(network: str, category: str, page: int = 0):
    catalog = load_catalog()
    items = [x for x in catalog.values()
             if str(x.get("network") or "Прочее") == network and str(x.get("category") or "Прочее") == category]
    items.sort(key=lambda x: str(x.get("name", "")).casefold())
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    rows = []
    for item in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        name = str(item.get("name", "Услуга"))
        # Telegram callback_data has a 64-byte limit; use service ID only.
        unit = Decimal(str(item.get("rate", 0))) / Decimal(1000)
        unit_text = f"{unit.quantize(Decimal('0.0001')).normalize()} ₽/шт"
        label = f"{name[:38]} · {unit_text}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"cat:service:{item['service']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=f"cat:page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="cat:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="›", callback_data=f"cat:page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← Категории", callback_data="cat:network")])
    rows.append([InlineKeyboardButton(text="🏠 Соцсети", callback_data="cat:home")])
    return items, pages, page, InlineKeyboardMarkup(inline_keyboard=rows)


def service_detail_text(item):
    rate = Decimal(str(item.get("rate", 0)))
    unit = rate / Decimal(1000)
    desc = str(item.get("description") or "Описание не предоставлено.")
    if len(desc) > 1800:
        desc = desc[:1800] + "…"
    return (
        f"<b>{escape(str(item.get('name', 'Услуга')))}</b>\n"
        f"{escape(str(item.get('network', '—')))} · {escape(str(item.get('category', '—')))}\n"
        f"ID: <code>{escape(str(item['service']))}</code>\n"
        f"Цена: <b>{unit.quantize(Decimal('0.0001'))} ₽ за 1 шт.</b>\n"
        f"Количество: {item.get('min')}–{item.get('max')}\n\n"
        f"{escape(desc)}"
    )


def service_detail_kb(service_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Заказать", callback_data=f"cat:buy:{service_id}")],
        [InlineKeyboardButton(text="← К услугам", callback_data="cat:services")],
        [InlineKeyboardButton(text="🏠 Соцсети", callback_data="cat:home")],
    ])


@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Здесь можно выбрать соцсеть и услугу через кнопки. Также доступно Mini App.",
        reply_markup=main_menu_kb()
    )


@dp.message(Command("catalog", "услуги", "каталог"))
async def catalog_command(message: types.Message, state: FSMContext):
    await state.clear()
    networks, kb = networks_kb()
    await message.answer("<b>Выберите соцсеть:</b>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "cat:home")
async def catalog_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    networks, kb = networks_kb()
    await callback.message.edit_text("<b>Выберите соцсеть:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:net:"))
async def choose_network(callback: types.CallbackQuery, state: FSMContext):
    try:
        idx = int(callback.data.rsplit(":", 1)[1])
        networks, _ = networks_kb()
        network = networks[idx]
        categories, kb = categories_kb(network)
        await state.update_data(bot_network=network, bot_category=None, bot_page=0)
        await callback.message.edit_text(f"<b>{escape(network)}</b> — выберите категорию:", reply_markup=kb, parse_mode="HTML")
        await callback.answer()
    except Exception as exc:
        log.exception("Network menu error")
        await callback.answer("Не удалось открыть соцсеть", show_alert=True)


@dp.callback_query(F.data == "cat:network")
async def back_to_categories(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    network = data.get("bot_network")
    if not network:
        await callback.answer("Сначала выберите соцсеть", show_alert=True)
        return
    categories, kb = categories_kb(network)
    await callback.message.edit_text(f"<b>{escape(network)}</b> — выберите категорию:", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:category:"))
async def choose_category(callback: types.CallbackQuery, state: FSMContext):
    try:
        data = await state.get_data()
        network = data.get("bot_network")
        if not network:
            await callback.answer("Сначала выберите соцсеть", show_alert=True)
            return
        idx = int(callback.data.rsplit(":", 1)[1])
        categories, _ = categories_kb(network)
        category = categories[idx]
        await state.update_data(bot_category=category, bot_page=0)
        items, pages, page, kb = services_kb(network, category, 0)
        await callback.message.edit_text(
            f"<b>{escape(network)} · {escape(category)}</b>\nВыберите услугу (страница {page+1}/{pages}):",
            reply_markup=kb, parse_mode="HTML")
        await callback.answer()
    except Exception:
        log.exception("Category menu error")
        await callback.answer("Не удалось открыть категорию", show_alert=True)


@dp.callback_query(F.data.startswith("cat:page:"))
async def service_page(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    network, category = data.get("bot_network"), data.get("bot_category")
    if not network or not category:
        await callback.answer("Выберите категорию заново", show_alert=True)
        return
    page = int(callback.data.rsplit(":", 1)[1])
    items, pages, page, kb = services_kb(network, category, page)
    await state.update_data(bot_page=page)
    await callback.message.edit_text(
        f"<b>{escape(network)} · {escape(category)}</b>\nВыберите услугу (страница {page+1}/{pages}):",
        reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "cat:services")
async def back_to_services(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    network, category = data.get("bot_network"), data.get("bot_category")
    if not network or not category:
        await callback.answer("Выберите категорию заново", show_alert=True)
        return
    page = int(data.get("bot_page", 0))
    items, pages, page, kb = services_kb(network, category, page)
    await callback.message.edit_text(
        f"<b>{escape(network)} · {escape(category)}</b>\nВыберите услугу (страница {page+1}/{pages}):",
        reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:service:"))
async def show_service(callback: types.CallbackQuery, state: FSMContext):
    service_id = callback.data.rsplit(":", 1)[1]
    item = load_catalog().get(service_id)
    if not item:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return
    await state.update_data(bot_service_id=service_id)
    await callback.message.edit_text(service_detail_text(item), reply_markup=service_detail_kb(service_id), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "cat:noop")
async def noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:buy:"))
async def start_bot_order(callback: types.CallbackQuery, state: FSMContext):
    service_id = callback.data.rsplit(":", 1)[1]
    item = load_catalog().get(service_id)
    if not item:
        await callback.answer("Услуга больше недоступна", show_alert=True)
        return
    await state.set_state(BotOrder.waiting_link)
    await state.update_data(bot_service_id=service_id)
    await callback.message.answer(
        f"Оформление: <b>{escape(str(item.get('name', 'Услуга')))}</b>\n"
        "Отправьте ссылку на страницу/пост или @username. Для отмены отправьте /cancel.",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("cancel"), StateFilter(BotOrder))
async def cancel_bot_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Оформление отменено.", reply_markup=main_menu_kb())


@dp.message(BotOrder.waiting_link)
async def bot_order_link(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link or len(link) > 700:
        await message.answer("Пришлите корректную ссылку или @username (до 700 символов).")
        return
    data = await state.get_data()
    item = load_catalog().get(str(data.get("bot_service_id", "")))
    if not item:
        await state.clear()
        await message.answer("Услуга больше недоступна. Откройте /catalog заново.")
        return
    await state.update_data(bot_link=link)
    await state.set_state(BotOrder.waiting_quantity)
    await message.answer(f"Введите количество от <b>{item['min']}</b> до <b>{item['max']}</b>:", parse_mode="HTML")


@dp.message(BotOrder.waiting_quantity)
async def bot_order_quantity(message: types.Message, state: FSMContext):
    data = await state.get_data()
    item = load_catalog().get(str(data.get("bot_service_id", "")))
    if not item:
        await state.clear()
        await message.answer("Услуга больше недоступна. Откройте /catalog заново.")
        return
    try:
        quantity = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите целое число.")
        return
    if not int(item["min"]) <= quantity <= int(item["max"]):
        await message.answer(f"Количество должно быть от {item['min']} до {item['max']}.")
        return

    total = order_cost(item["rate"], quantity)
    desc = str(item.get("description") or "Описание не предоставлено.")
    if len(desc) > 900:
        desc = desc[:900] + "…"
    text = (
        f"Проверьте заказ:\n\n<b>{escape(str(item.get('name', 'Услуга')))}</b>\n"
        f"Площадка: {escape(str(item.get('network', '—')))}\n"
        f"Ссылка: {escape(str(data['bot_link']))}\nКоличество: {quantity}\n"
        f"Итого: <b>{format_money(total)}</b>\n\n{escape(desc)}\n\nПодтвердить оформление?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="botorder:confirm")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="botorder:cancel")],
    ])
    await state.update_data(bot_quantity=quantity, bot_total=total)
    await state.set_state(BotOrder.confirming)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "botorder:cancel", StateFilter(BotOrder.confirming))
async def cancel_order_confirmation(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Заказ отменён. Чтобы выбрать услугу, используйте /catalog.")
    await callback.answer()


@dp.callback_query(F.data == "botorder:confirm", StateFilter(BotOrder.confirming))
async def confirm_bot_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    item = load_catalog().get(str(data.get("bot_service_id", "")))
    if not item:
        await state.clear()
        await callback.message.edit_text("Услуга больше недоступна.")
        await callback.answer()
        return

    user_id = callback.from_user.id
    quantity = int(data["bot_quantity"])
    link = str(data["bot_link"])
    try:
        await callback.message.edit_text("⏳ Проверяем баланс и оформляем заказ…")
        result = await process_order(user_id, item, link, quantity)
        await state.clear()
        await callback.message.edit_text(
            f"✅ <b>Заказ оформлен</b>\n\nНомер: <code>{escape(str(result['provider_id']))}</code>\n"
            f"Услуга: {escape(str(item.get('name', 'Услуга')))}\nКоличество: {quantity}\n"
            f"Списано: {format_money(result['charge'])}\nОстаток: {format_money(result['new_balance'])}\n\n"
            f"Подробности отправлены отдельным сообщением.\n#id{user_id}",
            parse_mode="HTML"
        )
    except Exception as exc:
        log.exception("Bot order failed for user=%s", user_id)
        await state.clear()
        msg = str(exc)
        if "недостаточно" in msg.lower() or "insufficient" in msg.lower():
            answer = "Недостаточно средств на балансе. Пополните баланс и попробуйте снова."
        else:
            answer = "Не удалось оформить заказ. Код: ERR-ORDER-500. Обратитесь в поддержку."
        await callback.message.edit_text(answer)
    finally:
        await callback.answer()


@dp.callback_query(F.data == "cat:balance")
async def bot_balance(callback: types.CallbackQuery):
    try:
        # Bot chat messages have no Mini App initData, so lookup by Telegram ID through protected Sheets action.
        result = await sheets("get_user", user_id=callback.from_user.id,
                              username=callback.from_user.username or "",
                              full_name=callback.from_user.full_name or "")
        if result.get("is_banned"):
            await callback.answer("Профиль заблокирован", show_alert=True)
            return
        await callback.message.answer(f"💰 Ваш баланс: <b>{format_money(result.get('balance', 0))}</b>", parse_mode="HTML")
    except Exception:
        log.exception("Bot balance lookup failed")
        await callback.message.answer("Не удалось получить баланс. Код: ERR-BAL-500")
    await callback.answer()


@dp.message(Command("addbalance"))
async def add_balance(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.reply("Формат: /addbalance user_id сумма")
        return
    try:
        amount = round(float(parts[2].replace(",", ".")), 2)
        if amount <= 0 or amount > 1000000:
            await message.reply("Сумма должна быть больше 0 и не более 1000000")
            return
        result = await sheets("credit", user_id=int(parts[1]), amount=amount, comment="Пополнение администратором")
        await message.reply(f"Баланс {parts[1]} пополнен на {format_money(amount)}. Остаток: {format_money(result['balance'])}")
        try:
            await bot.send_message(int(parts[1]), f"Баланс пополнен на {format_money(amount)}. Текущий баланс: {format_money(result['balance'])}")
        except Exception:
            log.exception("Could not notify user about balance credit")
    except Exception as exc:
        log.exception("Add balance failed")
        await message.reply("Ошибка начисления: ERR-ADMIN-BAL-500")


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
        f"📩 <b>Сообщение от пользователя</b>\n👤 {escape(user.full_name)}\n"
        f"🆔 <code>{user.id}</code>\n\n{escape(text)}\n\n"
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
