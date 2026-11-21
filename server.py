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
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

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
PAGE_SIZE = 6

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
http: ClientSession | None = None

# Сопоставление иконок соцсетей в стиле iOS (2 столбца)
NETWORK_ICONS = {
    "Telegram": "✈️",
    "ВКонтакте": "🔵",
    "Instagram": "📸",
    "TikTok": "🎵",
    "YouTube": "▶️",
    "Twitter X": "✖️",
    "Одноклассники": "🟠",
    "RuTube": "🔴",
    "Twitch": "🟣",
    "Kick": "🟢",
    "MAX": "⚡",
    "Wibes": "💫",
}

class BotOrder(StatesGroup):
    waiting_link = State()
    waiting_comments = State()
    waiting_poll_answer = State()
    waiting_quantity = State()
    confirming = State()

class BotTopup(StatesGroup):
    waiting_amount = State()
    waiting_method = State()


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


def load_catalog() -> dict:
    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Каталог services.json недоступен: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("services.json должен быть JSON-массивом")
    return {str(x["service"]): x for x in data if isinstance(x, dict) and x.get("service") is not None}


def format_money(value):
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}".replace(",", " ") + " ₽"


def format_unit_price(rate_per_1000):
    unit = Decimal(str(rate_per_1000)) / Decimal(1000)
    if unit >= 10:
        return f"{unit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} ₽"
    elif unit >= 1:
        return f"{unit.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)} ₽"
    elif unit >= Decimal("0.01"):
        return f"{unit.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)} ₽"
    return f"{unit.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)} ₽"


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
        return web.json_response({
            "ok": True,
            "balance": float(profile.get("balance", 0)),
            "user": {
                "id": user["id"],
                "first_name": user.get("first_name", ""),
                "username": user.get("username", "")
            }
        }, headers=cors_headers())
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("/api/user/info failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


async def send_order_receipt_to_user(user_id: int, order: dict, provider_order_id: str, charged: float, balance_after: float):
    net_icon = ""
    text = (
        "<b>Ваш заказ успешно оформлен!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Площадка:</b> {escape(str(order.get('network', '—')))}\n"
        f"📁 <b>Категория:</b> {escape(str(order.get('category', '—')))}\n"
        f"⚙️ <b>Услуга:</b> {escape(str(order.get('name', '—')))}\n"
        f"🆔 <b>ID услуги:</b> <code>{escape(str(order.get('service', '—')))}</code>\n"
        f"🔗 <b>Ссылка:</b> <code>{escape(str(order.get('link', '—')))}</code>\n"
        f"📊 <b>Количество:</b> <code>{int(order.get('quantity', 0))}</code>\n"
    )
    if order.get("comments"):
        lines_cnt = len(str(order['comments']).splitlines())
        text += f"💬 <b>Комментариев:</b> <code>{lines_cnt} шт.</code>\n"
    if order.get("answer_number"):
        text += f"🎯 <b>Номер ответа в опросе:</b> <code>{escape(str(order['answer_number']))}</code>\n"

    text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Списано с баланса:</b> <code>{format_money(charged)}</code>\n"
        f"💳 <b>Остаток на балансе:</b> <b>{format_money(balance_after)}</b>\n"
        f"🧾 <b>Номер заказа:</b> <code>#{escape(str(provider_order_id))}</code>\n\n"
        "<i>Статус заказа и запуск происходят в автоматическом режиме.</i>"
    )
    await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")


async def execute_provider_order(user_id: int, item: dict, link: str, quantity: int,
                                 comments: str | None = None, answer_number: str | None = None):
    service_id = str(item["service"])
    charge_amount = order_cost(item["rate"], quantity)
    if charge_amount <= 0:
        raise ValueError("Некорректная стоимость заказа")

    # 1. Атомарное списание в Google Таблице под LockService
    charged = await sheets("charge", user_id=user_id, amount=charge_amount, comment=f"Заказ услуги #{service_id}")
    if http is None:
        await sheets("refund", user_id=user_id, amount=charge_amount, comment="Возврат: API поставщика недоступен")
        raise RuntimeError("API поставщика временно недоступен")

    # 2. Формирование параметров для Twiboost v2 API
    provider_params = {
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": str(quantity),
        "key": TWIBOOST_API_KEY
    }
    if comments:
        provider_params["comments"] = comments
    if answer_number:
        provider_params["answer_number"] = str(answer_number)

    try:
        async with http.get("https://twiboost.com/api/v2", params=provider_params, timeout=ClientTimeout(total=35)) as resp:
            raw_data = await resp.text()
            provider_data = json.loads(raw_data)
    except Exception:
        log.exception("Provider uncertainty for user %s", user_id)
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"⚠️ Не удалось подтвердить ответ API по заказу пользователя <code>{user_id}</code>.\n"
                f"Услуга: #{escape(service_id)}, сумма: {charge_amount:.2f} ₽.\n"
                f"Проверьте кабинет до возврата средств.\n#id{user_id}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        raise RuntimeError("Статус заказа уточняется у поставщика. Средства не потеряны; если заказ не запустится, поддержка вернёт баланс.")

    provider_order_id = provider_data.get("order")
    if provider_order_id is None:
        provider_error = str(provider_data.get("error", "Поставщик отклонил заказ"))
        await sheets("refund", user_id=user_id, amount=charge_amount, comment=f"Возврат: отказ поставщика #{service_id}")
        try:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"❌ Поставщик отклонил заказ пользователя <code>{user_id}</code>.\n"
                f"Услуга: #{escape(service_id)}. Ошибка: {escape(provider_error)}.\n"
                f"Средства ({charge_amount:.2f} ₽) автоматически возвращены пользователю.\n#id{user_id}",
                parse_mode="HTML"
            )
        except Exception:
            pass
        raise RuntimeError(f"Поставщик не смог принять заказ: {provider_error}. Деньги уже возвращены на ваш баланс!")

    provider_order_id = str(provider_order_id)
    new_balance = float(charged.get("balance", 0))

    # 3. Уведомление админа
    try:
        user_chat = await bot.get_chat(user_id)
        name = escape(user_chat.full_name or "Без имени")
        username = "@" + user_chat.username if user_chat.username else "не указан"
    except Exception:
        name = "Пользователь"
        username = "не указан"

    net_icon = ""
    admin_msg = (
        "<b>Новый заказ оформлен</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Покупатель:</b> {name}\n"
        f"🔹 <b>Username:</b> {escape(username)}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"<b>Соцсеть:</b> {escape(str(item.get('network', '—')))}\n"
        f"📁 <b>Категория:</b> {escape(str(item.get('category', '—')))}\n"
        f"⚙️ <b>Услуга:</b> {escape(str(item.get('name', '—')))}\n"
        f"🔢 <b>ID услуги:</b> <code>{escape(service_id)}</code>\n"
        f"🔗 <b>Ссылка:</b> <code>{escape(link)}</code>\n"
        f"📊 <b>Количество:</b> <code>{quantity}</code>\n"
    )
    if comments:
        admin_msg += f"💬 <b>Кастомные комментарии:</b>\n<pre>{escape(comments[:400])}</pre>\n"
    if answer_number:
        admin_msg += f"🎯 <b>Номер ответа:</b> <code>{escape(str(answer_number))}</code>\n"

    admin_msg += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Списано:</b> <code>{format_money(charge_amount)}</code>\n"
        f"🧾 <b>Заказ в Twiboost:</b> <code>#{escape(provider_order_id)}</code>\n\n"
        f"#id{user_id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_msg, parse_mode="HTML")
    except Exception:
        log.exception("Admin notification error")

    # 4. Чек покупателю в чат
    order_info = {
        **item,
        "service": service_id,
        "link": link,
        "quantity": quantity,
        "comments": comments,
        "answer_number": answer_number,
    }
    try:
        await send_order_receipt_to_user(user_id, order_info, provider_order_id, charge_amount, new_balance)
    except Exception:
        log.exception("Receipt delivery failed for user %s", user_id)

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

        comments = order.get("comments")
        answer_number = order.get("answer_number")

        result = await execute_provider_order(int(user["id"]), item, link, quantity, comments, answer_number)
        return web.json_response({"ok": True, **result}, headers=cors_headers())
    except PermissionError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=403, headers=cors_headers())
    except Exception as exc:
        log.exception("submit_order failed")
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
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💳 <b>Запрос на пополнение баланса</b>\n"
            f"👤 ID: <code>{user['id']}</code>\n"
            f"💰 Сумма: <b>{format_money(amount)}</b>\n"
            f"🔹 Способ: <b>{escape(method)}</b>\n\n"
            f"#id{user['id']}",
            parse_mode="HTML"
        )
        return web.json_response({"ok": True, "message": "Запрос отправлен администратору."}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


# =====================================================================
# КЛАВИАТУРЫ И ИНТЕРФЕЙС БОТА
# =====================================================================

def main_menu_kb():
    buttons = [
        [
            InlineKeyboardButton(text="Каталог услуг", callback_data="cat:home"),
            InlineKeyboardButton(text="Мой профиль", callback_data="bot:profile"),
        ],
        [
            InlineKeyboardButton(text="Пополнить баланс", callback_data="bot:topup"),
            InlineKeyboardButton(text="Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN))
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _catalog_networks():
    catalog = load_catalog()
    return sorted({str(x.get("network") or "Прочее") for x in catalog.values()}, key=str.casefold)


def _catalog_categories(network: str):
    catalog = load_catalog()
    return sorted({str(x.get("category") or "Прочее") for x in catalog.values()
                   if str(x.get("network") or "Прочее") == network}, key=str.casefold)


def _callback_token(prefix: str, index: int) -> str:
    return f"{prefix}:{index}"


def networks_kb():
    """Каталог: соцсети в два столбца. Callback передаёт индекс, не длинное имя."""
    catalog = load_catalog()
    networks = sorted({str(x.get("network") or "Прочее") for x in catalog.values()}, key=str.casefold)
    buttons = []
    row = []
    for index, net in enumerate(networks):
        count = sum(1 for x in catalog.values() if str(x.get("network") or "Прочее") == net)
        btn = InlineKeyboardButton(text=f"{net} · {count}", callback_data=_callback_token("net", index))
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="Профиль", callback_data="bot:profile"),
        InlineKeyboardButton(text="Закрыть", callback_data="cat:close")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def categories_kb(network: str):
    catalog = load_catalog()
    categories = sorted({str(x.get("category") or "Прочее") for x in catalog.values()
                         if str(x.get("network") or "Прочее") == network}, key=str.casefold)
    buttons = []
    for index, category in enumerate(categories):
        count = sum(1 for x in catalog.values()
                    if str(x.get("network") or "Прочее") == network
                    and str(x.get("category") or "Прочее") == category)
        # Index callback keeps payload within Telegram's 64-byte limit.
        buttons.append([InlineKeyboardButton(text=f"{category} · {count}", callback_data=_callback_token("category", index))])
    buttons.append([InlineKeyboardButton(text="Назад к соцсетям", callback_data="cat:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_kb(network: str, category: str, page: int = 0):
    catalog = load_catalog()
    items = [x for x in catalog.values()
             if str(x.get("network") or "Прочее") == network
             and str(x.get("category") or "Прочее") == category]
    items.sort(key=lambda x: str(x.get("name", "")).casefold())
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    buttons = []
    for item in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        name = str(item.get("name", "Услуга"))
        unit_price = format_unit_price(item.get("rate", 0))
        btn_text = f"{name[:38]} · {unit_price}/шт"
        buttons.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"srv:{item['service']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="Назад", callback_data=f"page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="cat:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Вперёд", callback_data=f"page:{page+1}"))
    buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="Категории", callback_data="cat:back_cats"),
        InlineKeyboardButton(text="Соцсети", callback_data="cat:home")
    ])
    return items, pages, page, InlineKeyboardMarkup(inline_keyboard=buttons)


def service_card_text(item: dict) -> str:
    net_icon = NETWORK_ICONS.get(str(item.get("network")), "💎")
    unit_price = format_unit_price(item.get("rate", 0))
    desc = str(item.get("description") or "Быстрое и качественное выполнение.")
    if len(desc) > 900:
        desc = desc[:900] + "…"

    service_type = item.get("type", "Default")
    type_badge = ""
    if service_type == "Custom Comments":
        type_badge = "\n💬 <i>Тип услуги: Свои комментарии (каждый с новой строки)</i>"
    elif service_type == "Poll":
        type_badge = "\n🎯 <i>Тип услуги: Голосование в опросе</i>"

    return (
        f"<b>{escape(str(item.get('name', 'Услуга')))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Площадка:</b> {escape(str(item.get('network', '—')))}\n"
        f"📁 <b>Категория:</b> {escape(str(item.get('category', '—')))}\n"
        f"🔢 <b>ID услуги:</b> <code>{escape(str(item['service']))}</code>\n"
        f"💎 <b>Цена за 1 шт:</b> <code>{unit_price}</code>\n"
        f"📊 <b>Лимиты:</b> от <code>{item.get('min')}</code> до <code>{item.get('max')}</code> шт.{type_badge}\n\n"
        f"<b>Описание услуги:</b>\n{escape(desc)}"
    )


def service_card_kb(item: dict) -> InlineKeyboardMarkup:
    service_id = str(item["service"])
    network = str(item.get("network", ""))
    category = str(item.get("category", ""))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Заказать услугу", callback_data=f"cat:order:{service_id}")],
        [
            InlineKeyboardButton(text="К списку", callback_data=f"cat:cat:{category}"),
            InlineKeyboardButton(text="Соцсети", callback_data="cat:home")
        ]
    ])


# =====================================================================
# ХЕНДЛЕРЫ МЕНЮ И ПРОФИЛЯ
# =====================================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    text = (
        f"<b>Добро пожаловать в VibeBoost, {escape(user.first_name)}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Мы предоставляем автоматическое продвижение в топ для всех социальных сетей.\n\n"
        "✨ Выберите действие ниже или откройте удобное Mini App:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


@dp.message(Command("profile", "профиль", "баланс"))
async def cmd_profile(message: types.Message, state: FSMContext):
    await state.clear()
    await show_profile(message.chat.id, message.from_user)


@dp.callback_query(F.data == "bot:profile")
async def cb_profile(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_profile(callback.message.chat.id, callback.from_user, message_to_edit=callback.message)
    await callback.answer()


async def show_profile(chat_id: int, user: types.User, message_to_edit: types.Message | None = None):
    """Полноценная карточка профиля пользователя с балансом и действиями."""
    try:
        profile = await sheets(
            "get_user",
            user_id=user.id,
            username=user.username or "",
            full_name=user.full_name or ""
        )
        balance = float(profile.get("balance", 0))
    except Exception:
        balance = 0.0

    username = f"@{user.username}" if user.username else "не указан"
    text = (
        "👤 <b>Ваш личный профиль VibeBoost</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Имя:</b> {escape(user.full_name)}\n"
        f"🔹 <b>Username:</b> {escape(username)}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.id}</code>\n\n"
        f"💰 <b>Текущий баланс:</b> <b>{format_money(balance)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Пополняйте баланс и запускайте накрутку в 1 клик!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Пополнить баланс", callback_data="bot:topup"),
            InlineKeyboardButton(text="Каталог услуг", callback_data="cat:home")
        ],
        [
            InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN)),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="bot:profile")
        ]
    ])

    if message_to_edit:
        try:
            await message_to_edit.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "bot:topup")
async def cb_start_topup(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotTopup.waiting_amount)
    await callback.message.edit_text(
        "<b>Пополнение баланса</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите желаемую сумму пополнения в рублях (от 10 до 100 000 ₽).\n\n"
        "Для отмены отправьте /cancel",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(BotTopup.waiting_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amount = round(float(raw), 2)
        if not 10 <= amount <= 100000:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите корректную сумму числом от 10 до 100 000 ₽:")
        return

    await state.update_data(topup_amount=amount)
    await state.set_state(BotTopup.waiting_method)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="СБП / Карта РФ", callback_data="topup_m:СБП / Карта РФ")],
        [InlineKeyboardButton(text="Криптовалюта (USDT / TON)", callback_data="topup_m:Криптовалюта")],
        [InlineKeyboardButton(text="🌍 Другой способ", callback_data="topup_m:Другой способ")],
        [InlineKeyboardButton(text="Отмена", callback_data="cat:home")]
    ])
    await message.answer(
        f"💰 Выбрана сумма: <b>{format_money(amount)}</b>\n"
        "Выберите удобный способ оплаты:",
        reply_markup=kb,
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("topup_m:"), StateFilter(BotTopup.waiting_method))
async def process_topup_method(callback: types.CallbackQuery, state: FSMContext):
    method = callback.data.split(":", 1)[1]
    data = await state.get_data()
    amount = data.get("topup_amount", 100)
    user = callback.from_user
    await state.clear()

    # Уведомляем администратора
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💳 <b>Запрос пополнения баланса из бота</b>\n"
            f"👤 Пользователь: {escape(user.full_name)} (@{escape(user.username or '—')})\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"💰 Сумма: <b>{format_money(amount)}</b>\n"
            f"🔹 Способ: <b>{escape(method)}</b>\n\n"
            f"Для зачисления ответьте командой:\n<code>/addbalance {user.id} {amount}</code>\n\n"
            f"#id{user.id}",
            parse_mode="HTML"
        )
    except Exception:
        log.exception("Admin topup alert failed")

    await callback.message.edit_text(
        "✅ <b>Запрос на пополнение отправлен администратору!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сумма: <b>{format_money(amount)}</b>\n"
        f"🔹 Способ: <b>{escape(method)}</b>\n\n"
        "Ожидайте ответа администратора в этом чате. Баланс будет начислен сразу после подтверждения.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )
    await callback.answer()


# =====================================================================
# НАВИГАЦИЯ ПО КАТАЛОГУ
# =====================================================================

@dp.message(Command("catalog", "каталог", "услуги"))
async def cmd_catalog(message: types.Message, state: FSMContext):
    await state.clear()
    kb = networks_kb()
    await message.answer("🌐 <b>Выберите социальную сеть для продвижения:</b>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "cat:home")
async def cb_cat_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = networks_kb()
    await callback.message.edit_text("🌐 <b>Выберите социальную сеть для продвижения:</b>", reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "cat:close")
async def cb_cat_close(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.startswith("net:"))
async def cb_choose_network(callback: types.CallbackQuery, state: FSMContext):
    try:
        index = int(callback.data.split(":", 1)[1])
        networks = _catalog_networks()
        network = networks[index]
    except (ValueError, IndexError):
        await callback.answer("Соцсеть не найдена. Откройте каталог заново.", show_alert=True)
        return
    await state.update_data(current_network=network, current_category=None, current_page=0)
    kb = categories_kb(network)
    await callback.message.edit_text(
        f"<b>{escape(network)}</b> — выберите категорию услуг:",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "cat:back_cats")
async def cb_back_to_cats(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    network = data.get("current_network")
    if not network:
        await callback.answer("Выберите соцсеть заново.", show_alert=True)
        return
    await callback.message.edit_text(
        f"<b>{escape(network)}</b> — выберите категорию услуг:",
        reply_markup=categories_kb(network),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("category:"))
async def cb_choose_category(callback: types.CallbackQuery, state: FSMContext):
    try:
        index = int(callback.data.split(":", 1)[1])
        data = await state.get_data()
        network = data.get("current_network")
        if not network:
            await callback.answer("Сначала выберите соцсеть.", show_alert=True)
            return
        categories = _catalog_categories(network)
        category = categories[index]
    except (ValueError, IndexError):
        await callback.answer("Категория не найдена. Откройте раздел заново.", show_alert=True)
        return

    await state.update_data(current_category=category, current_page=0)
    items, pages, page, kb = services_kb(network, category, 0)
    await callback.message.edit_text(
        f"<b>{escape(network)} · {escape(category)}</b>\n"
        f"Выберите услугу (страница {page+1}/{pages}):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_services_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    network = data.get("current_network", "Telegram")
    category = data.get("current_category", "Подписчики")
    await state.update_data(current_page=page)

    items, pages, page, kb = services_kb(network, category, page)
    icon = NETWORK_ICONS.get(network, "💎")
    await callback.message.edit_text(
        f"{icon} <b>{escape(network)} · {escape(category)}</b>\n"
        f"Выберите интересующую услугу (страница {page+1}/{pages}):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "cat:noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("srv:"))
async def cb_service_card(callback: types.CallbackQuery, state: FSMContext):
    service_id = callback.data.split(":", 1)[1]
    item = load_catalog().get(service_id)
    if not item:
        await callback.answer("Услуга не найдена или устарела.", show_alert=True)
        return

    await state.update_data(current_service_id=service_id,
                            current_network=item.get("network"),
                            current_category=item.get("category"))
    text = service_card_text(item)
    kb = service_card_kb(item)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# =====================================================================
# ПОЛНОЦЕННЫЙ WIZARD ОФОРМЛЕНИЯ ЗАКАЗА
# =====================================================================

@dp.callback_query(F.data.startswith("cat:order:"))
async def cb_start_order(callback: types.CallbackQuery, state: FSMContext):
    service_id = callback.data.split(":", 2)[2]
    item = load_catalog().get(service_id)
    if not item:
        await callback.answer("Услуга недоступна", show_alert=True)
        return

    await state.set_state(BotOrder.waiting_link)
    await state.update_data(order_service_id=service_id)

    net_icon = NETWORK_ICONS.get(str(item.get("network")), "💎")
    unit_price = format_unit_price(item.get("rate", 0))

    await callback.message.answer(
        f"<b>Оформление заказа:</b>\n"
        f"«{escape(str(item.get('name')))}»\n"
        f"Цена за 1 шт: <code>{unit_price}</code>\n"
        f"Лимиты: от <code>{item['min']}</code> до <code>{item['max']}</code> шт.\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>Шаг 1:</b> Отправьте ссылку на пост / канал / страницу или @username:\n\n"
        "<i>Для отмены заказа напишите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("cancel"), StateFilter(BotOrder))
async def cmd_cancel_order(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Оформление заказа отменено.", reply_markup=main_menu_kb())


@dp.message(BotOrder.waiting_link)
async def process_order_link(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link or len(link) > 700:
        await message.answer("⚠️ Пожалуйста, отправьте корректную ссылку (до 700 символов) или @username:")
        return

    data = await state.get_data()
    service_id = data.get("order_service_id")
    item = load_catalog().get(str(service_id))
    if not item:
        await state.clear()
        await message.answer("Услуга устарела. Откройте /catalog заново.")
        return

    await state.update_data(order_link=link)
    service_type = item.get("type", "Default")

    # 1. Если услуга типа Custom Comments / Mentions — просим прислать текст комментариев!
    if service_type in ("Custom Comments", "Mentions"):
        await state.set_state(BotOrder.waiting_comments)
        await message.answer(
            "💬 <b>Шаг 2 (Свои комментарии):</b>\n"
            "Пришлите текст комментариев, <b>каждый с новой строки</b>.\n"
            f"Количество строк должно быть от <code>{item['min']}</code> до <code>{item['max']}</code> шт.",
            parse_mode="HTML"
        )
        return

    # 2. Если услуга типа Poll — запрашиваем номер ответа в опросе
    if service_type == "Poll":
        await state.set_state(BotOrder.waiting_poll_answer)
        await message.answer(
            "🎯 <b>Шаг 2 (Опрос):</b>\n"
            "Введите номер варианта ответа в опросе (например: <code>1</code>, <code>2</code> или ID ответа):",
            parse_mode="HTML"
        )
        return

    # 3. Стандартная услуга — просим количество
    await state.set_state(BotOrder.waiting_quantity)
    await message.answer(
        f"📊 <b>Шаг 2:</b> Введите необходимое количество (от <code>{item['min']}</code> до <code>{item['max']}</code>):",
        parse_mode="HTML"
    )


@dp.message(BotOrder.waiting_comments)
async def process_order_comments(message: types.Message, state: FSMContext):
    comments_text = (message.text or "").strip()
    lines = [line.strip() for line in comments_text.splitlines() if line.strip()]
    count = len(lines)

    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    min_q, max_q = int(item["min"]), int(item["max"])
    if count < min_q or count > max_q:
        await message.answer(
            f"⚠️ Вы прислали <code>{count}</code> строк(и).\n"
            f"Для этой услуги требуется от <code>{min_q}</code> до <code>{max_q}</code> комментариев.\n"
            "Пришлите список комментариев повторно:",
            parse_mode="HTML"
        )
        return

    normalized_comments = "\n".join(lines)
    await state.update_data(order_comments=normalized_comments, order_quantity=count)
    await show_order_confirmation(message, state, item, count)


@dp.message(BotOrder.waiting_poll_answer)
async def process_order_poll_answer(message: types.Message, state: FSMContext):
    answer = (message.text or "").strip()
    if not answer or len(answer) > 50:
        await message.answer("⚠️ Введите корректный номер или ID варианта ответа:")
        return

    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    await state.update_data(order_answer_number=answer)
    await state.set_state(BotOrder.waiting_quantity)
    await message.answer(
        f"📊 <b>Шаг 3:</b> Введите количество голосов (от <code>{item['min']}</code> до <code>{item['max']}</code>):",
        parse_mode="HTML"
    )


@dp.message(BotOrder.waiting_quantity)
async def process_order_quantity(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        quantity = int(raw)
    except ValueError:
        await message.answer("⚠️ Введите целое число:")
        return

    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    min_q, max_q = int(item["min"]), int(item["max"])
    if not min_q <= quantity <= max_q:
        await message.answer(f"⚠️ Количество должно быть в пределах от <code>{min_q}</code> до <code>{max_q}</code>:")
        return

    await state.update_data(order_quantity=quantity)
    await show_order_confirmation(message, state, item, quantity)


async def show_order_confirmation(message: types.Message, state: FSMContext, item: dict, quantity: int):
    """Экран подтверждения заказа со сверкой баланса."""
    data = await state.get_data()
    cost = order_cost(item["rate"], quantity)
    await state.update_data(order_cost=cost)

    # Проверяем текущий баланс
    try:
        prof = await sheets(
            "get_user",
            user_id=message.from_user.id,
            username=message.from_user.username or "",
            full_name=message.from_user.full_name or ""
        )
        current_balance = float(prof.get("balance", 0))
    except Exception:
        current_balance = 0.0

    net_icon = NETWORK_ICONS.get(str(item.get("network")), "💎")
    link = data.get("order_link", "—")

    text = (
        "<b>Проверьте данные вашего заказа:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Услуга:</b> {escape(str(item.get('name')))}\n"
        f"🌐 <b>Площадка:</b> {escape(str(item.get('network')))} · {escape(str(item.get('category')))}\n"
        f"🔗 <b>Ссылка:</b> <code>{escape(link)}</code>\n"
        f"📊 <b>Количество:</b> <code>{quantity} шт.</code>\n"
    )
    if data.get("order_comments"):
        lines_cnt = len(str(data['order_comments']).splitlines())
        text += f"💬 <b>Своих комментариев:</b> <code>{lines_cnt} шт.</code>\n"
    if data.get("order_answer_number"):
        text += f"🎯 <b>Номер ответа:</b> <code>{escape(str(data['order_answer_number']))}</code>\n"

    text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>К списанию:</b> <b>{format_money(cost)}</b>\n"
        f"💳 <b>Ваш баланс:</b> {format_money(current_balance)}\n"
    )

    if current_balance < cost:
        diff = cost - current_balance
        text += (
            f"\n⚠️ <b>Недостаточно средств!</b> Не хватает <b>{format_money(diff)}</b>.\n"
            "Пополните баланс для продолжения."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Пополнить баланс", callback_data="bot:topup")],
            [InlineKeyboardButton(text="Отменить", callback_data="botorder:cancel")]
        ])
    else:
        text += "\nВсе верно? Нажмите кнопку для запуска накрутки:"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить и оплатить", callback_data="botorder:confirm")],
            [InlineKeyboardButton(text="Отменить", callback_data="botorder:cancel")]
        ])

    await state.set_state(BotOrder.confirming)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "botorder:cancel", StateFilter(BotOrder.confirming))
async def cb_cancel_confirmation(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Заказ отменён. Вы можете выбрать другую услугу в /catalog.", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "botorder:confirm", StateFilter(BotOrder.confirming))
async def cb_confirm_order(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    service_id = str(data.get("order_service_id"))
    item = load_catalog().get(service_id)
    if not item:
        await state.clear()
        await callback.message.edit_text("⚠️ Услуга больше недоступна.")
        await callback.answer()
        return

    user_id = callback.from_user.id
    link = str(data.get("order_link"))
    quantity = int(data.get("order_quantity", 0))
    comments = data.get("order_comments")
    answer_number = data.get("order_answer_number")

    await callback.message.edit_text("⏳ <b>Создаём заказ и связываемся с поставщиком…</b>", parse_mode="HTML")
    await state.clear()

    try:
        result = await execute_provider_order(
            user_id=user_id,
            item=item,
            link=link,
            quantity=quantity,
            comments=comments,
            answer_number=answer_number
        )
        net_icon = NETWORK_ICONS.get(str(item.get("network")), "💎")
        await callback.message.edit_text(
            f"<b>Заказ #{escape(result['provider_id'])} успешно запущен!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Услуга:</b> {escape(str(item.get('name')))}\n"
            f"📊 <b>Количество:</b> <code>{quantity} шт.</code>\n"
            f"💰 <b>Списано:</b> <code>{format_money(result['charge'])}</code>\n"
            f"💳 <b>Остаток на балансе:</b> <b>{format_money(result['new_balance'])}</b>\n\n"
            "<i>Чек с подробностями заказа отправлен выше.</i>",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )
    except Exception as exc:
        log.exception("Execution of bot order failed: %s", exc)
        err_msg = str(exc)
        if "недостаточно" in err_msg.lower() or "insufficient" in err_msg.lower():
            text = (
                "<b>Недостаточно средств на балансе!</b>\n\n"
                "Пожалуйста, пополните баланс профиля и повторите попытку."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Пополнить баланс", callback_data="bot:topup")],
                [InlineKeyboardButton(text="В каталог", callback_data="cat:home")]
            ])
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await callback.message.edit_text(
                f"❌ <b>Не удалось оформить заказ</b>\n\n"
                f"Причина: <i>{escape(err_msg)}</i>\n\n"
                "Деньги не списаны (или возвращены). Попробуйте снова или обратитесь в поддержку.",
                reply_markup=main_menu_kb(),
                parse_mode="HTML"
            )
    finally:
        await callback.answer()


# =====================================================================
# АДМИН-КОМАНДЫ И ОБРАТНАЯ СВЯЗЬ
# =====================================================================

@dp.message(Command("addbalance"))
async def cmd_add_balance(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.reply("Формат: <code>/addbalance user_id сумма</code>", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
        amount = round(float(parts[2].replace(",", ".")), 2)
        if amount <= 0 or amount > 1000000:
            await message.reply("Сумма должна быть от 0.01 до 1 000 000 ₽")
            return
        result = await sheets("credit", user_id=target_id, amount=amount, comment="Пополнение администратором")
        new_bal = float(result.get("balance", 0))
        await message.reply(
            f"✅ Баланс пользователя <code>{target_id}</code> успешно пополнен на <b>{format_money(amount)}</b>.\n"
            f"Текущий остаток: <b>{format_money(new_bal)}</b>",
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                target_id,
                f"💎 <b>Ваш баланс пополнен на {format_money(amount)}!</b>\n"
                f"Текущий баланс: <b>{format_money(new_bal)}</b>\n\n"
                "Приятных покупок в VibeBoost!",
                parse_mode="HTML"
            )
        except Exception:
            log.exception("Failed to notify user about balance refill")
    except Exception as exc:
        log.exception("Add balance failed")
        await message.reply(f"Ошибка пополнения: {exc}")


@dp.message(Command("send"))
async def cmd_send_to_user(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Формат: <code>/send user_id текст сообщения</code>", parse_mode="HTML")
        return
    try:
        await bot.send_message(int(parts[1]), parts[2], parse_mode="HTML")
        await message.reply("✅ Сообщение успешно доставлено пользователю.")
    except Exception as exc:
        await message.reply(f"❌ Не удалось отправить: {exc}")


@dp.message(F.chat.type == "private")
async def forward_user_message(message: types.Message):
    """Пересылка входящих сообщений от пользователей администратору с возможностью быстрого ответа."""
    if message.from_user.id == ADMIN_CHAT_ID:
        return
    user = message.from_user
    tag = f"#id{user.id}"
    text = message.text or message.caption or "(сообщение без текста)"
    admin_text = (
        f"📩 <b>Входящее сообщение в поддержку</b>\n"
        f"👤 {escape(user.full_name)} (@{escape(user.username or 'не указан')})\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"{escape(text)}\n\n"
        f"Ответить: <code>/send {user.id} </code>\n"
        f"Пополнить: <code>/addbalance {user.id} </code>\n"
        f"{tag}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
        if message.content_type != types.ContentType.TEXT:
            await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    except Exception:
        log.exception("Could not forward user message to admin")


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
