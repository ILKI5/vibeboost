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

# Реестр премиум-эмодзи из tgiosicons
PREMIUM_EMOJI_IDS = {
    "Авито": "5206315603379373105",
    "Дзен": "5359312848247857963",
    "Одноклассники": "5359535718395812756",
    "Яндекс Маркет": "5206690180362158433",
    "Яндекс Музыка": "5359586068297425068",
    "CoinMarketCap": "5345803067272994307",
    "Facebook": "5355254460635428635",
    "Github": "5357135871059440242",
    "Instagram": "5355097780228470775",
    "Kick": "5289683581474464897",
    "Likee": "5352672553930342216",
    "Linkedin": "5357053089859772828",
    "MAX": "5458653586378469798",
    "Medium": "5456341901835772035",
    "Ozon": "5206507661431947835",
    "Pinterest": "5352595631066071365",
    "Rutube": "5375542803809979254",
    "RuTube": "5375542803809979254",
    "Spotify": "5206361550939506453",
    "Steam": "5206370578960760792",
    "Telegram": "5285350148451344065",
    "Telegram Premium": "5969715206673141221",
    "Threads": "5273863760929372027",
    "TikTok": "5353034628263330616",
    "Twitch": "5352759664457038886",
    "Twitter": "5357203031463050136",
    "Twitter X": "5357203031463050136",
    "VC.ru": "5359557455225299074",
    "VK": "5359619818150436536",
    "ВКонтакте": "5359619818150436536",
    "Wibes": "5445058185230571497",
    "Wildberries": "5420219968451003929",
    "Yappy": "5068989216705217525",
    "YouTube": "5454010052421626926",
}

# Иконки для кнопок в стиле скриншота пользователя
NETWORK_BUTTON_ICONS = {
    "Telegram": "✈️",
    "ВКонтакте": "🔵",
    "VK": "🔵",
    "Instagram": "📸",
    "TikTok": "🎵",
    "YouTube": "▶️",
    "Twitter": "✖️",
    "Twitter X": "✖️",
    "Одноклассники": "🟠",
    "RuTube": "🔴",
    "Rutube": "🔴",
    "Twitch": "🟣",
    "Kick": "🟢",
    "Авито": "🛍️",
    "Ozon": "📦",
    "Wildberries": "🟣",
    "Яндекс Музыка": "🎧",
    "Spotify": "🟢",
    "Steam": "🎮",
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


def tg_icon(name: str) -> str:
    """Генерирует HTML с кастомным премиум-эмодзи."""
    emoji_id = PREMIUM_EMOJI_IDS.get(name)
    fallback = NETWORK_BUTTON_ICONS.get(name, "💎")
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'
    return fallback


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
        raise RuntimeError(f"Каталог недоступен: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError("services.json должен быть массивом")
    return {str(x["service"]): x for x in data if isinstance(x, dict) and x.get("service") is not None}


def format_money(value):
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}".replace(",", " ") + " ₽"


def format_unit_price(rate_per_1000):
    unit = Decimal(str(rate_per_1000)) / Decimal(1000)
    if unit >= 1:
        return f"{unit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} ₽"
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
            raise RuntimeError(f"Google Sheets вернул не JSON ({response.status})") from exc
        if response.status >= 400 or not result.get("ok"):
            raise RuntimeError(str(result.get("error", f"Sheets error {response.status}")))
        return result


async def execute_provider_order(user_id: int, item: dict, link: str, quantity: int,
                                 comments: str | None = None, answer_number: str | None = None):
    service_id = str(item["service"])
    charge_amount = order_cost(item["rate"], quantity)
    if charge_amount <= 0:
        raise ValueError("Некорректная сумма заказа")

    charged = await sheets("charge", user_id=user_id, amount=charge_amount, comment=f"Заказ услуги #{service_id}")
    if http is None:
        await sheets("refund", user_id=user_id, amount=charge_amount, comment="Возврат")
        raise RuntimeError("API поставщика недоступен")

    provider_params = {
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": str(quantity),
        "key": TWIBOOST_API_KEY,
    }
    if comments:
        provider_params["comments"] = comments
    if answer_number:
        provider_params["answer_number"] = str(answer_number)

    try:
        async with http.get("https://twiboost.com/api/v2", params=provider_params, timeout=ClientTimeout(total=35)) as resp:
            raw = await resp.text()
            data = json.loads(raw)
    except Exception:
        log.exception("Twiboost error for %s", user_id)
        raise RuntimeError("Статус заказа уточняется у поставщика.")

    provider_order_id = data.get("order")
    if provider_order_id is None:
        err = str(data.get("error", "Отказ поставщика"))
        await sheets("refund", user_id=user_id, amount=charge_amount, comment=f"Возврат: {service_id}")
        raise RuntimeError(f"Поставщик отклонил заказ: {err}")

    provider_order_id = str(provider_order_id)
    new_balance = float(charged.get("balance", 0))

    try:
        user_chat = await bot.get_chat(user_id)
        name = escape(user_chat.first_name or "Пользователь")
        username = f"@{user_chat.username}" if user_chat.username else "не указан"
    except Exception:
        name, username = "Пользователь", "не указан"

    net_name = str(item.get('network', '—'))
    icon = tg_icon(net_name)

    admin_text = (
        "🛒 <b>Новый заказ принят</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Покупатель: {name} ({username})\n"
        f"🆔 ID: <code>{user_id}</code>\n"
        f"{icon} Площадка: {escape(net_name)}\n"
        f"📁 Категория: {escape(str(item.get('category', '—')))}\n"
        f"⚙️ Услуга: {escape(str(item.get('name')))} [#{service_id}]\n"
        f"🔗 Ссылка: <code>{escape(link)}</code>\n"
        f"📊 Количество: <code>{quantity}</code>\n"
        f"💰 Списано: <code>{format_money(charge_amount)}</code>\n"
        f"🧾 Заказ в Twiboost: <code>#{provider_order_id}</code>\n\n#id{user_id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
    except Exception:
        pass

    user_receipt = (
        "💎 <b>Ваш заказ успешно оформлен!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} <b>Площадка:</b> {escape(net_name)}\n"
        f"📁 <b>Категория:</b> {escape(str(item.get('category', '—')))}\n"
        f"⚙️ <b>Услуга:</b> {escape(str(item.get('name')))}\n"
        f"🔗 <b>Ссылка:</b> <code>{escape(link)}</code>\n"
        f"📊 <b>Количество:</b> <code>{quantity} шт.</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Списано с баланса:</b> <code>{format_money(charge_amount)}</code>\n"
        f"💳 <b>Остаток на балансе:</b> <b>{format_money(new_balance)}</b>\n"
        f"🧾 <b>Номер заказа:</b> <code>#{provider_order_id}</code>\n\n"
        "<i>Заказ запущен в автоматическом режиме!</i>"
    )
    try:
        await bot.send_message(user_id, user_receipt, parse_mode="HTML")
    except Exception:
        pass

    return {"provider_id": provider_order_id, "new_balance": new_balance, "charge": charge_amount}


# ==========================================
# КЛАВИАТУРЫ В СТИЛЕ СКРИНШОТА ПОЛЬЗОВАТЕЛЯ
# ==========================================

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📁 Каталог", callback_data="cat:home"),
            InlineKeyboardButton(text="👤 Профиль", callback_data="bot:profile"),
        ],
        [
            InlineKeyboardButton(text="💳 Пополнить", callback_data="bot:topup"),
            InlineKeyboardButton(text="📱 Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN)),
        ]
    ])


def _catalog_networks():
    catalog = load_catalog()
    return sorted({str(x.get("network") or "Прочее") for x in catalog.values()}, key=str.casefold)


def _catalog_categories(network: str):
    catalog = load_catalog()
    return sorted({str(x.get("category") or "Прочее") for x in catalog.values()
                   if str(x.get("network") or "Прочее") == network}, key=str.casefold)


def networks_kb():
    """Сетка соцсетей СТРОГО В ДВА СТОЛБЦА с аккуратными иконками."""
    catalog = load_catalog()
    networks = _catalog_networks()
    buttons = []
    row = []
    for index, net in enumerate(networks):
        count = sum(1 for x in catalog.values() if str(x.get("network") or "Прочее") == net)
        icon = NETWORK_BUTTON_ICONS.get(net, "💎")
        row.append(InlineKeyboardButton(text=f"{icon} {net} · {count}", callback_data=f"net:{index}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text="👤 Профиль", callback_data="bot:profile"),
        InlineKeyboardButton(text="✖️ Закрыть", callback_data="cat:close"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def categories_kb(network: str):
    catalog = load_catalog()
    categories = _catalog_categories(network)
    buttons = []
    for index, cat in enumerate(categories):
        count = sum(1 for x in catalog.values()
                    if str(x.get("network") or "Прочее") == network and str(x.get("category") or "Прочее") == cat)
        buttons.append([InlineKeyboardButton(text=f"📁 {cat} · {count}", callback_data=f"category:{index}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад к соцсетям", callback_data="cat:home")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def services_kb(network: str, category: str, page: int = 0):
    catalog = load_catalog()
    items = [x for x in catalog.values()
             if str(x.get("network") or "Прочее") == network and str(x.get("category") or "Прочее") == category]
    items.sort(key=lambda x: str(x.get("name", "")).casefold())
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))

    buttons = []
    for item in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        name = str(item.get("name", "Услуга"))
        price = format_unit_price(item.get("rate", 0))
        buttons.append([InlineKeyboardButton(text=f"✨ {name[:34]} · {price}/шт", callback_data=f"srv:{item['service']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"📄 {page+1}/{pages}", callback_data="cat:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([
        InlineKeyboardButton(text="📁 Категории", callback_data="cat:back_cats"),
        InlineKeyboardButton(text="🌐 Соцсети", callback_data="cat:home"),
    ])
    return items, pages, page, InlineKeyboardMarkup(inline_keyboard=buttons)


# ==========================================
# ОБРАБОТЧИКИ КОМАНД И МЕНЮ
# ==========================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user = message.from_user
    log.info("Received /start from %s (@%s)", user.id, user.username)
    text = (
        f"👋 <b>Добро пожаловать в VibeBoost, {escape(user.first_name)}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Автоматическое продвижение и накрутка в топ для всех социальных сетей.\n\n"
        "Выбери действие ниже:"
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
    try:
        prof = await sheets("get_user", user_id=user.id, username=user.username or "", full_name=user.full_name or "")
        balance = float(prof.get("balance", 0))
    except Exception:
        balance = 0.0

    username = f"@{user.username}" if user.username else "не указан"
    text = (
        "👤 <b>Ваш личный профиль VibeBoost</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Имя:</b> {escape(user.full_name)}\n"
        f"🔹 <b>Username:</b> {escape(username)}\n"
        f"🆔 <b>Telegram ID:</b> <code>{user.id}</code>\n\n"
        f"💳 <b>Текущий баланс:</b> <b>{format_money(balance)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Пополняйте баланс и запускайте заказы в 1 клик!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="bot:topup"),
            InlineKeyboardButton(text="📁 Каталог услуг", callback_data="cat:home"),
        ],
        [
            InlineKeyboardButton(text="📱 Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN)),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="bot:profile"),
        ]
    ])
    if message_to_edit:
        try:
            await message_to_edit.edit_text(text, reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("catalog", "каталог", "услуги"))
async def cmd_catalog(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🌐 <b>Выберите социальную сеть для продвижения:</b>", reply_markup=networks_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "cat:home")
async def cb_cat_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🌐 <b>Выберите социальную сеть для продвижения:</b>", reply_markup=networks_kb(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "cat:close")
async def cb_cat_close(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer()


@dp.callback_query(F.data.startswith("net:"))
async def cb_choose_network(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    nets = _catalog_networks()
    net = nets[idx]
    await state.update_data(current_network=net)
    icon = tg_icon(net)
    await callback.message.edit_text(
        f"{icon} <b>{escape(net)}</b> — выберите категорию услуг:",
        reply_markup=categories_kb(net),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "cat:back_cats")
async def cb_back_to_cats(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    net = data.get("current_network", "Telegram")
    icon = tg_icon(net)
    await callback.message.edit_text(
        f"{icon} <b>{escape(net)}</b> — выберите категорию услуг:",
        reply_markup=categories_kb(net),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("category:"))
async def cb_choose_category(callback: types.CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    data = await state.get_data()
    net = data.get("current_network", "Telegram")
    cat = _catalog_categories(net)[idx]
    await state.update_data(current_category=cat, current_page=0)
    items, pages, page, kb = services_kb(net, cat, 0)
    icon = tg_icon(net)
    await callback.message.edit_text(
        f"{icon} <b>{escape(net)} · {escape(cat)}</b>\nВыберите интересующую услугу (стр. {page+1}/{pages}):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("page:"))
async def cb_services_page(callback: types.CallbackQuery, state: FSMContext):
    p = int(callback.data.split(":")[1])
    data = await state.get_data()
    net = data.get("current_network", "Telegram")
    cat = data.get("current_category", "")
    await state.update_data(current_page=p)
    items, pages, page, kb = services_kb(net, cat, p)
    icon = tg_icon(net)
    await callback.message.edit_text(
        f"{icon} <b>{escape(net)} · {escape(cat)}</b>\nВыберите интересующую услугу (стр. {page+1}/{pages}):",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "cat:noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("srv:"))
async def cb_service_card(callback: types.CallbackQuery, state: FSMContext):
    srv_id = callback.data.split(":")[1]
    item = load_catalog().get(srv_id)
    if not item:
        await callback.answer("Услуга не найдена", show_alert=True)
        return
    net = str(item.get("network"))
    icon = tg_icon(net)
    price = format_unit_price(item.get("rate", 0))
    desc = str(item.get("description") or "Быстрое и качественное выполнение.")
    if len(desc) > 800:
        desc = desc[:800] + "…"

    text = (
        f"{icon} <b>{escape(str(item.get('name')))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Площадка:</b> {escape(net)}\n"
        f"📁 <b>Категория:</b> {escape(str(item.get('category')))}\n"
        f"🔢 <b>ID услуги:</b> <code>{escape(srv_id)}</code>\n"
        f"💎 <b>Цена за 1 шт:</b> <code>{price}</code>\n"
        f"📊 <b>Лимиты:</b> от <code>{item.get('min')}</code> до <code>{item.get('max')}</code> шт.\n\n"
        f"<b>Описание услуги:</b>\n{escape(desc)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Заказать услугу", callback_data=f"order:{srv_id}")],
        [
            InlineKeyboardButton(text="🔙 К списку", callback_data="cat:back_cats"),
            InlineKeyboardButton(text="🌐 Соцсети", callback_data="cat:home"),
        ]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ==========================================
# ПОШАГОВЫЙ МАСТЕР ЗАКАЗА (WIZARD)
# ==========================================

@dp.callback_query(F.data.startswith("order:"))
async def cb_start_order(callback: types.CallbackQuery, state: FSMContext):
    srv_id = callback.data.split(":")[1]
    item = load_catalog().get(srv_id)
    if not item:
        await callback.answer("Услуга недоступна", show_alert=True)
        return
    await state.set_state(BotOrder.waiting_link)
    await state.update_data(order_service_id=srv_id)
    net = str(item.get("network"))
    icon = tg_icon(net)
    price = format_unit_price(item.get("rate", 0))

    await callback.message.answer(
        f"{icon} <b>Оформление заказа:</b> «{escape(str(item.get('name')))}»\n"
        f"Цена за 1 шт: <code>{price}</code> | Лимиты: от <code>{item['min']}</code> до <code>{item['max']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>Шаг 1:</b> Отправьте ссылку на пост, канал или страницу:\n\n"
        "<i>Для отмены напишите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("cancel"), StateFilter(BotOrder, BotTopup))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb())


@dp.message(BotOrder.waiting_link)
async def process_link(message: types.Message, state: FSMContext):
    link = (message.text or "").strip()
    if not link or len(link) > 700:
        await message.answer("⚠️ Пожалуйста, отправьте корректную ссылку:")
        return
    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга устарела. Откройте каталог /catalog заново.")
        return

    await state.update_data(order_link=link)
    srv_type = item.get("type", "Default")

    if srv_type in ("Custom Comments", "Mentions"):
        await state.set_state(BotOrder.waiting_comments)
        await message.answer(
            "💬 <b>Шаг 2 (Свои комментарии):</b>\n"
            "Пришлите текст комментариев, <b>каждый с новой строки</b>.\n"
            f"Количество строк должно быть от <code>{item['min']}</code> до <code>{item['max']}</code> шт.",
            parse_mode="HTML"
        )
        return

    if srv_type == "Poll":
        await state.set_state(BotOrder.waiting_poll_answer)
        await message.answer(
            "🎯 <b>Шаг 2 (Опрос):</b>\n"
            "Введите номер или ID варианта ответа в опросе (например: <code>1</code>):",
            parse_mode="HTML"
        )
        return

    await state.set_state(BotOrder.waiting_quantity)
    await message.answer(
        f"📊 <b>Шаг 2:</b> Введите количество (от <code>{item['min']}</code> до <code>{item['max']}</code>):",
        parse_mode="HTML"
    )


@dp.message(BotOrder.waiting_comments)
async def process_comments(message: types.Message, state: FSMContext):
    lines = [x.strip() for x in (message.text or "").splitlines() if x.strip()]
    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    min_q, max_q = int(item["min"]), int(item["max"])
    if not min_q <= len(lines) <= max_q:
        await message.answer(
            f"⚠️ Вы прислали <code>{len(lines)}</code> строк.\n"
            f"Требуется от <code>{min_q}</code> до <code>{max_q}</code> комментариев.\n"
            "Пришлите комментарии повторно:",
            parse_mode="HTML"
        )
        return

    await state.update_data(order_comments="\n".join(lines), order_quantity=len(lines))
    await show_order_confirm(message, state, item, len(lines))


@dp.message(BotOrder.waiting_poll_answer)
async def process_poll_answer(message: types.Message, state: FSMContext):
    ans = (message.text or "").strip()
    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    await state.update_data(order_answer_number=ans)
    await state.set_state(BotOrder.waiting_quantity)
    await message.answer(f"📊 <b>Шаг 3:</b> Введите количество голосов (от <code>{item['min']}</code> до <code>{item['max']}</code>):", parse_mode="HTML")


@dp.message(BotOrder.waiting_quantity)
async def process_qty(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        qty = int(raw)
    except ValueError:
        await message.answer("⚠️ Введите число:")
        return

    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
    if not item:
        await state.clear()
        await message.answer("Услуга недоступна.")
        return

    min_q, max_q = int(item["min"]), int(item["max"])
    if not min_q <= qty <= max_q:
        await message.answer(f"⚠️ Количество должно быть от <code>{min_q}</code> до <code>{max_q}</code>:")
        return

    await state.update_data(order_quantity=qty)
    await show_order_confirm(message, state, item, qty)


async def show_order_confirm(message: types.Message, state: FSMContext, item: dict, qty: int):
    data = await state.get_data()
    cost = order_cost(item["rate"], qty)
    await state.update_data(order_cost=cost)

    try:
        prof = await sheets("get_user", user_id=message.from_user.id)
        current_balance = float(prof.get("balance", 0))
    except Exception:
        current_balance = 0.0

    net = str(item.get("network"))
    icon = tg_icon(net)

    text = (
        "📋 <b>Проверьте данные вашего заказа:</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{icon} <b>Услуга:</b> {escape(str(item.get('name')))}\n"
        f"🌐 <b>Площадка:</b> {escape(net)} · {escape(str(item.get('category')))}\n"
        f"🔗 <b>Ссылка:</b> <code>{escape(data.get('order_link', '—'))}</code>\n"
        f"📊 <b>Количество:</b> <code>{qty} шт.</code>\n"
    )
    if data.get("order_comments"):
        lines_cnt = len(str(data['order_comments']).splitlines())
        text += f"💬 <b>Комментариев:</b> <code>{lines_cnt} шт.</code>\n"
    if data.get("order_answer_number"):
        text += f"🎯 <b>Номер ответа в опросе:</b> <code>{escape(str(data['order_answer_number']))}</code>\n"

    text += (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>К списанию:</b> <b>{format_money(cost)}</b>\n"
        f"💳 <b>Ваш баланс:</b> {format_money(current_balance)}\n"
    )

    if current_balance < cost:
        diff = cost - current_balance
        text += f"\n⚠️ <b>Недостаточно средств!</b> Не хватает <b>{format_money(diff)}</b>."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="bot:topup")],
            [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="botorder:cancel")]
        ])
    else:
        text += "\nВсе верно? Нажмите кнопку для запуска накрутки:"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и оплатить", callback_data="botorder:confirm")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="botorder:cancel")]
        ])

    await state.set_state(BotOrder.confirming)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "botorder:cancel", StateFilter(BotOrder.confirming))
async def cb_order_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Заказ отменён.", reply_markup=main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "botorder:confirm", StateFilter(BotOrder.confirming))
async def cb_order_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    item = load_catalog().get(str(data.get("order_service_id")))
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

    await callback.message.edit_text("⏳ <b>Создаём заказ у поставщика…</b>", parse_mode="HTML")
    await state.clear()

    try:
        res = await execute_provider_order(user_id, item, link, quantity, comments, answer_number)
        net = str(item.get("network"))
        icon = tg_icon(net)
        await callback.message.edit_text(
            f"🎉 <b>Заказ #{escape(res['provider_id'])} успешно запущен!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{icon} <b>Услуга:</b> {escape(str(item.get('name')))}\n"
            f"📊 <b>Количество:</b> <code>{quantity} шт.</code>\n"
            f"💰 <b>Списано:</b> <code>{format_money(res['charge'])}</code>\n"
            f"💳 <b>Остаток на балансе:</b> <b>{format_money(res['new_balance'])}</b>",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )
    except Exception as exc:
        await callback.message.edit_text(
            f"❌ <b>Не удалось оформить заказ:</b> {escape(str(exc))}\n\n"
            "Средства возвращены на ваш баланс.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )
    finally:
        await callback.answer()


# ==========================================
# ПОПОЛНЕНИЕ БАЛАНСА
# ==========================================

@dp.callback_query(F.data == "bot:topup")
async def cb_topup_start(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotTopup.waiting_amount)
    await callback.message.edit_text(
        "💳 <b>Пополнение баланса</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите сумму пополнения в рублях (от 10 до 100 000 ₽):\n\n"
        "<i>Для отмены напишите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(BotTopup.waiting_amount)
async def process_topup_amount(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        amt = round(float(raw), 2)
        if not 10 <= amt <= 100000:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите число от 10 до 100 000 ₽:")
        return

    await state.clear()
    user = message.from_user
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"💳 <b>Запрос на пополнение баланса</b>\n"
            f"👤 От: {escape(user.full_name)} (@{escape(user.username or 'не указан')})\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"💰 Сумма: <b>{format_money(amt)}</b>\n\n"
            f"Зачислить: <code>/addbalance {user.id} {amt}</code>\n#id{user.id}",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await message.answer(
        "✅ <b>Запрос отправлен администратору!</b>\n\n"
        f"Сумма: <b>{format_money(amt)}</b>\n"
        "Баланс будет пополнен сразу после подтверждения платежа.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )


# ==========================================
# АДМИН-КОМАНДЫ И ОБРАТНАЯ СВЯЗЬ
# ==========================================

@dp.message(Command("addbalance"))
async def cmd_addbalance(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3 or not parts[1].isdigit():
        await message.reply("Формат: <code>/addbalance user_id сумма</code>", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
        amt = round(float(parts[2].replace(",", ".")), 2)
        res = await sheets("credit", user_id=target_id, amount=amt, comment="Пополнение администратором")
        new_bal = float(res.get("balance", 0))
        await message.reply(f"✅ Баланс <code>{target_id}</code> пополнен на <b>{format_money(amt)}</b>. Текущий баланс: <b>{format_money(new_bal)}</b>", parse_mode="HTML")
        try:
            await bot.send_message(target_id, f"💎 <b>Ваш баланс пополнен на {format_money(amt)}!</b>\nТекущий баланс: <b>{format_money(new_bal)}</b>", parse_mode="HTML")
        except Exception:
            pass
    except Exception as exc:
        await message.reply(f"Ошибка: {exc}")


@dp.message(Command("send"))
async def cmd_send(message: types.Message):
    if message.from_user.id != ADMIN_CHAT_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.reply("Формат: <code>/send user_id текст</code>", parse_mode="HTML")
        return
    try:
        await bot.send_message(int(parts[1]), parts[2], parse_mode="HTML")
        await message.reply("✅ Доставлено.")
    except Exception as exc:
        await message.reply(f"Ошибка: {exc}")


@dp.message(F.chat.type == "private")
async def forward_to_admin(message: types.Message):
    if message.from_user.id == ADMIN_CHAT_ID:
        return
    user = message.from_user
    text = message.text or message.caption or "(без текста)"
    admin_text = (
        f"📩 <b>Сообщение от пользователя</b>\n"
        f"👤 {escape(user.full_name)} (@{escape(user.username or 'не указан')})\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"{escape(text)}\n\n"
        f"Ответить: <code>/send {user.id} </code>\n"
        f"Пополнить: <code>/addbalance {user.id} </code>\n#id{user.id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
        if message.content_type != types.ContentType.TEXT:
            await bot.copy_message(ADMIN_CHAT_ID, message.chat.id, message.message_id)
    except Exception:
        pass


# ==========================================
# REST API ДЛЯ MINI APP
# ==========================================

async def user_info(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        u = validate_init_data(str(payload.get("initData", "")))
        prof = await sheets("get_user", user_id=int(u["id"]), username=u.get("username", ""))
        return web.json_response({
            "ok": True,
            "balance": float(prof.get("balance", 0)),
            "user": {"id": u["id"], "first_name": u.get("first_name", ""), "username": u.get("username", "")}
        }, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


async def submit_order(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        u = validate_init_data(str(payload.get("initData", "")))
        o = payload.get("order") or {}
        item = load_catalog().get(str(o.get("service", "")))
        if not item:
            raise ValueError("Услуга не найдена")
        qty = int(o.get("quantity", 0))
        link = str(o.get("link", "")).strip()
        res = await execute_provider_order(int(u["id"]), item, link, qty, o.get("comments"), o.get("answer_number"))
        return web.json_response({"ok": True, **res}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500, headers=cors_headers())


async def topup_request(request):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors_headers())
    try:
        payload = await request.json()
        u = validate_init_data(str(payload.get("initData", "")))
        amt = float(payload.get("amount", 0))
        await bot.send_message(ADMIN_CHAT_ID, f"💳 Запрос пополнения: ID {u['id']} на {amt} ₽\n#id{u['id']}")
        return web.json_response({"ok": True}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


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


server_app = web.Application()
for route, handler in (
    ("/api/user/info", user_info),
    ("/api/order", submit_order),
    ("/api/topup", topup_request),
):
    server_app.router.add_route("OPTIONS", route, handler)
    server_app.router.add_post(route, handler)
server_app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
server_app.on_startup.append(on_startup)
server_app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(server_app, host="0.0.0.0", port=PORT)
