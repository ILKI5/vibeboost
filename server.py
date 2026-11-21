import asyncio
try:
    asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import os
import hmac
import hashlib
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl
from html import escape
from decimal import Decimal, ROUND_HALF_UP

from aiohttp import web, ClientSession, ClientTimeout
from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    Message,
    CallbackQuery,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("HEUPO_BOOST")


# Переменные окружения
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
WEBAPP_ORIGIN = os.environ["WEBAPP_ORIGIN"].rstrip("/")
SHEETS_API_URL = os.environ["SHEETS_API_URL"].strip()
SHEETS_API_SECRET = os.environ["SHEETS_API_SECRET"].strip()
TWIBOOST_API_KEY = os.environ["TWIBOOST_API_KEY"].strip()
PORT = int(os.getenv("PORT", "8080"))
CATALOG_PATH = Path(os.getenv("CATALOG_PATH", "services.json"))
PAGE_SIZE = 6

# Реестр премиум-эмодзи из каталога tgiosicons
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

# Инициализация MTProto-клиента
app_bot = Client(
    name="HEUPO_BOOST_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)
http: ClientSession | None = None

# Простое хранилище состояний сессий бота (FSM)
USER_STATES: dict[int, dict] = {}


def set_user_state(user_id: int, state: str, **kwargs):
    if user_id not in USER_STATES:
        USER_STATES[user_id] = {}
    USER_STATES[user_id]["state"] = state
    USER_STATES[user_id].update(kwargs)


def get_user_state(user_id: int) -> dict:
    return USER_STATES.get(user_id, {})


def clear_user_state(user_id: int):
    USER_STATES.pop(user_id, None)


def tg_icon(net_name: str) -> str:
    """Генерирует HTML-разметку премиум-эмодзи."""
    emoji_id = PREMIUM_EMOJI_IDS.get(net_name)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">•</tg-emoji>'
    return "•"


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
        user_chat = await app_bot.get_chat(user_id)
        name = escape(user_chat.first_name or "Пользователь")
        username = f"@{user_chat.username}" if user_chat.username else "не указан"
    except Exception:
        name, username = "Пользователь", "не указан"

    admin_text = (
        "<b>Новый заказ принят</b>\n"
        f"Покупатель: {name} ({username})\n"
        f"ID: <code>{user_id}</code>\n"
        f"Соцсеть: {escape(str(item.get('network')))}\n"
        f"Услуга: {escape(str(item.get('name')))} [#{service_id}]\n"
        f"Ссылка: <code>{escape(link)}</code>\n"
        f"Количество: {quantity}\n"
        f"Списано: {format_money(charge_amount)}\n"
        f"Заказ в Twiboost: <code>#{provider_order_id}</code>\n\n#id{user_id}"
    )
    try:
        await app_bot.send_message(ADMIN_CHAT_ID, admin_text)
    except Exception:
        pass

    user_receipt = (
        "<b>Ваш заказ успешно оформлен!</b>\n"
        f"Соцсеть: {escape(str(item.get('network')))}\n"
        f"Услуга: {escape(str(item.get('name')))}\n"
        f"Ссылка: <code>{escape(link)}</code>\n"
        f"Количество: {quantity}\n"
        f"Списано: {format_money(charge_amount)}\n"
        f"Остаток баланса: <b>{format_money(new_balance)}</b>\n"
        f"Номер заказа: <code>#{provider_order_id}</code>"
    )
    try:
        await app_bot.send_message(user_id, user_receipt)
    except Exception:
        pass

    return {"provider_id": provider_order_id, "new_balance": new_balance, "charge": charge_amount}


# --- Клавиатуры бота ---
def main_menu_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Каталог услуг", callback_data="cat:home"),
            InlineKeyboardButton("Мой профиль", callback_data="bot:profile"),
        ],
        [
            InlineKeyboardButton("Пополнить баланс", callback_data="bot:topup"),
            InlineKeyboardButton("Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN)),
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
    """Сетка соцсетей СТРОГО В ДВА СТОЛБЦА."""
    catalog = load_catalog()
    networks = _catalog_networks()
    buttons = []
    row = []
    for index, net in enumerate(networks):
        count = sum(1 for x in catalog.values() if str(x.get("network") or "Прочее") == net)
        row.append(InlineKeyboardButton(f"{net} · {count}", callback_data=f"net:{index}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton("Профиль", callback_data="bot:profile"),
        InlineKeyboardButton("Закрыть", callback_data="cat:close"),
    ])
    return InlineKeyboardMarkup(buttons)


def categories_kb(network: str):
    catalog = load_catalog()
    categories = _catalog_categories(network)
    buttons = []
    for index, cat in enumerate(categories):
        count = sum(1 for x in catalog.values()
                    if str(x.get("network") or "Прочее") == network and str(x.get("category") or "Прочее") == cat)
        buttons.append([InlineKeyboardButton(f"{cat} · {count}", callback_data=f"category:{index}")])
    buttons.append([InlineKeyboardButton("Назад к соцсетям", callback_data="cat:home")])
    return InlineKeyboardMarkup(buttons)


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
        buttons.append([InlineKeyboardButton(f"{name[:36]} · {price}/шт", callback_data=f"srv:{item['service']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Назад", callback_data=f"page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="cat:noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Вперёд", callback_data=f"page:{page+1}"))
    buttons.append(nav)
    buttons.append([
        InlineKeyboardButton("Категории", callback_data="cat:back_cats"),
        InlineKeyboardButton("Соцсети", callback_data="cat:home"),
    ])
    return items, pages, page, InlineKeyboardMarkup(buttons)


# --- Обработчики Pyrogram ---
@app_bot.on_message(filters.command("start") & filters.private)
async def cmd_start(_, message: Message):
    clear_user_state(message.from_user.id)
    text = (
        f"<b>Добро пожаловать в HEUPO_BOOST, {escape(message.from_user.first_name)}!</b>\n\n"
        "Выберите интересующий пункт меню или откройте каталог услуг:"
    )
    await message.reply_text(text, reply_markup=main_menu_kb())


@app_bot.on_message(filters.command(["profile", "баланс"]) & filters.private)
async def cmd_profile(_, message: Message):
    clear_user_state(message.from_user.id)
    await show_profile(message.chat.id, message.from_user)


@app_bot.on_message(filters.command(["catalog", "услуги"]) & filters.private)
async def cmd_catalog(_, message: Message):
    clear_user_state(message.from_user.id)
    await message.reply_text("<b>Выберите социальную сеть:</b>", reply_markup=networks_kb())


async def show_profile(chat_id: int, user, edit_message: Message | None = None):
    try:
        prof = await sheets("get_user", user_id=user.id, username=user.username or "", full_name=user.first_name or "")
        balance = float(prof.get("balance", 0))
    except Exception:
        balance = 0.0

    username = f"@{user.username}" if user.username else "не указан"
    text = (
        "<b>Личный профиль HEUPO_BOOST</b>\n"
        f"Имя: <b>{escape(user.first_name)}</b>\n"
        f"Username: <b>{escape(username)}</b>\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"Баланс: <b>{format_money(balance)}</b>"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Пополнить баланс", callback_data="bot:topup"),
            InlineKeyboardButton("Каталог услуг", callback_data="cat:home"),
        ],
        [
            InlineKeyboardButton("Mini App", web_app=WebAppInfo(url=WEBAPP_ORIGIN)),
            InlineKeyboardButton("Обновить", callback_data="bot:profile"),
        ]
    ])
    if edit_message:
        try:
            await edit_message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
    await app_bot.send_message(chat_id, text, reply_markup=kb)


@app_bot.on_callback_query()
async def on_callback(_, cq: CallbackQuery):
    data = cq.data or ""
    user_id = cq.from_user.id

    if data == "cat:close":
        clear_user_state(user_id)
        await cq.message.delete()
        await cq.answer()
        return

    if data == "bot:profile":
        clear_user_state(user_id)
        await show_profile(cq.message.chat.id, cq.from_user, edit_message=cq.message)
        await cq.answer()
        return

    if data == "cat:home":
        clear_user_state(user_id)
        await cq.message.edit_text("<b>Выберите социальную сеть:</b>", reply_markup=networks_kb())
        await cq.answer()
        return

    if data == "bot:topup":
        set_user_state(user_id, "waiting_topup_amount")
        await cq.message.edit_text(
            "<b>Пополнение баланса</b>\n"
            "Введите сумму в рублях (от 10 до 100 000 ₽):\n\n"
            "Для отмены отправьте /cancel"
        )
        await cq.answer()
        return

    if data.startswith("net:"):
        idx = int(data.split(":")[1])
        nets = _catalog_networks()
        net = nets[idx]
        set_user_state(user_id, "browsing", current_network=net)
        icon = tg_icon(net)
        await cq.message.edit_text(
            f"{icon} <b>{escape(net)}</b> — выберите категорию:",
            reply_markup=categories_kb(net)
        )
        await cq.answer()
        return

    if data == "cat:back_cats":
        st = get_user_state(user_id)
        net = st.get("current_network", "Telegram")
        icon = tg_icon(net)
        await cq.message.edit_text(
            f"{icon} <b>{escape(net)}</b> — выберите категорию:",
            reply_markup=categories_kb(net)
        )
        await cq.answer()
        return

    if data.startswith("category:"):
        idx = int(data.split(":")[1])
        st = get_user_state(user_id)
        net = st.get("current_network", "Telegram")
        cat = _catalog_categories(net)[idx]
        set_user_state(user_id, "browsing", current_network=net, current_category=cat, current_page=0)
        items, pages, page, kb = services_kb(net, cat, 0)
        icon = tg_icon(net)
        await cq.message.edit_text(
            f"{icon} <b>{escape(net)} · {escape(cat)}</b>\nВыберите услугу (стр. {page+1}/{pages}):",
            reply_markup=kb
        )
        await cq.answer()
        return

    if data.startswith("page:"):
        p = int(data.split(":")[1])
        st = get_user_state(user_id)
        net = st.get("current_network", "Telegram")
        cat = st.get("current_category", "")
        set_user_state(user_id, "browsing", current_network=net, current_category=cat, current_page=p)
        items, pages, page, kb = services_kb(net, cat, p)
        icon = tg_icon(net)
        await cq.message.edit_text(
            f"{icon} <b>{escape(net)} · {escape(cat)}</b>\nВыберите услугу (стр. {page+1}/{pages}):",
            reply_markup=kb
        )
        await cq.answer()
        return

    if data.startswith("srv:"):
        srv_id = data.split(":")[1]
        item = load_catalog().get(srv_id)
        if not item:
            await cq.answer("Услуга недоступна", show_alert=True)
            return
        net = str(item.get("network"))
        icon = tg_icon(net)
        price = format_unit_price(item.get("rate", 0))
        desc = str(item.get("description") or "Качественное и быстрое выполнение.")[:800]
        text = (
            f"{icon} <b>{escape(str(item.get('name')))}</b>\n"
            f"Площадка: {escape(net)} · {escape(str(item.get('category')))}\n"
            f"Цена за 1 шт: <code>{price}</code>\n"
            f"Лимиты: {item.get('min')} – {item.get('max')} шт.\n\n"
            f"<b>Описание:</b>\n{escape(desc)}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Заказать услугу", callback_data=f"order:{srv_id}")],
            [
                InlineKeyboardButton("К списку", callback_data="cat:back_cats"),
                InlineKeyboardButton("Соцсети", callback_data="cat:home"),
            ]
        ])
        await cq.message.edit_text(text, reply_markup=kb)
        await cq.answer()
        return

    if data.startswith("order:"):
        srv_id = data.split(":")[1]
        item = load_catalog().get(srv_id)
        if not item:
            await cq.answer("Услуга недоступна", show_alert=True)
            return
        set_user_state(user_id, "waiting_link", order_service_id=srv_id)
        net = str(item.get("network"))
        icon = tg_icon(net)
        await cq.message.reply_text(
            f"{icon} <b>Оформление заказа:</b> «{escape(str(item.get('name')))}»\n\n"
            "Отправьте ссылку на пост, канал или страницу:\n"
            "<i>(Для отмены отправьте /cancel)</i>"
        )
        await cq.answer()
        return

    if data == "botorder:confirm":
        st = get_user_state(user_id)
        item = load_catalog().get(str(st.get("order_service_id")))
        if not item:
            await cq.message.edit_text("Услуга больше недоступна.")
            await cq.answer()
            return
        await cq.message.edit_text("Создаём заказ у поставщика…")
        try:
            res = await execute_provider_order(
                user_id=user_id,
                item=item,
                link=st["order_link"],
                quantity=st["order_quantity"],
                comments=st.get("order_comments"),
                answer_number=st.get("order_answer_number"),
            )
            clear_user_state(user_id)
            await cq.message.edit_text(
                f"<b>Заказ #{res['provider_id']} успешно запущен!</b>\n"
                f"Услуга: {escape(str(item.get('name')))}\n"
                f"Списано: {format_money(res['charge'])}\n"
                f"Остаток: <b>{format_money(res['new_balance'])}</b>",
                reply_markup=main_menu_kb()
            )
        except Exception as exc:
            clear_user_state(user_id)
            await cq.message.edit_text(f"Ошибка заказа: {escape(str(exc))}", reply_markup=main_menu_kb())
        await cq.answer()
        return

    if data == "botorder:cancel":
        clear_user_state(user_id)
        await cq.message.edit_text("Заказ отменён.", reply_markup=main_menu_kb())
        await cq.answer()
        return

    await cq.answer()


@app_bot.on_message(filters.text & filters.private)
async def on_user_text(_, message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()

    if text == "/cancel":
        clear_user_state(user_id)
        await message.reply_text("Действие отменено.", reply_markup=main_menu_kb())
        return

    st = get_user_state(user_id)
    state = st.get("state")

    if state == "waiting_link":
        if not text or len(text) > 700:
            await message.reply_text("Введите корректную ссылку:")
            return
        item = load_catalog().get(str(st.get("order_service_id")))
        srv_type = item.get("type", "Default")
        st["order_link"] = text

        if srv_type in ("Custom Comments", "Mentions"):
            set_user_state(user_id, "waiting_comments", **st)
            await message.reply_text(
                "Пришлите текст комментариев, <b>каждый с новой строки</b>:\n"
                f"Количество строк должно быть от {item['min']} до {item['max']}."
            )
            return

        if srv_type == "Poll":
            set_user_state(user_id, "waiting_poll_answer", **st)
            await message.reply_text("Введите номер или ID варианта ответа в опросе:")
            return

        set_user_state(user_id, "waiting_quantity", **st)
        await message.reply_text(f"Введите количество (от {item['min']} до {item['max']}):")
        return

    if state == "waiting_comments":
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        item = load_catalog().get(str(st.get("order_service_id")))
        if not int(item["min"]) <= len(lines) <= int(item["max"]):
            await message.reply_text(f"Количество строк ({len(lines)}) вне лимита ({item['min']}–{item['max']}). Повторите:")
            return
        st["order_comments"] = "\n".join(lines)
        st["order_quantity"] = len(lines)
        await confirm_wizard(message, st, item, len(lines))
        return

    if state == "waiting_poll_answer":
        st["order_answer_number"] = text
        set_user_state(user_id, "waiting_quantity", **st)
        item = load_catalog().get(str(st.get("order_service_id")))
        await message.reply_text(f"Введите количество голосов (от {item['min']} до {item['max']}):")
        return

    if state == "waiting_quantity":
        if not text.isdigit():
            await message.reply_text("Введите число:")
            return
        qty = int(text)
        item = load_catalog().get(str(st.get("order_service_id")))
        if not int(item["min"]) <= qty <= int(item["max"]):
            await message.reply_text(f"Количество должно быть от {item['min']} до {item['max']}:")
            return
        st["order_quantity"] = qty
        await confirm_wizard(message, st, item, qty)
        return

    if state == "waiting_topup_amount":
        try:
            val = round(float(text.replace(",", ".")), 2)
            if not 10 <= val <= 100000:
                raise ValueError
        except ValueError:
            await message.reply_text("Сумма должна быть числом от 10 до 100 000 ₽:")
            return
        clear_user_state(user_id)
        await app_bot.send_message(
            ADMIN_CHAT_ID,
            f"Запрос пополнения баланса\n"
            f"От: {escape(message.from_user.first_name)} (ID: <code>{user_id}</code>)\n"
            f"Сумма: <b>{format_money(val)}</b>\n\n"
            f"Команда: <code>/addbalance {user_id} {val}</code>\n#id{user_id}"
        )
        await message.reply_text("Запрос отправлен администратору. Ожидайте начисления.")
        return

    # Пересылка входящих сообщений администратору
    if user_id != ADMIN_CHAT_ID:
        admin_note = (
            f"Сообщение в поддержку\n"
            f"От: {escape(message.from_user.first_name)} (ID: <code>{user_id}</code>)\n\n"
            f"{escape(text)}\n\n"
            f"Ответить: <code>/send {user_id} </code>\n#id{user_id}"
        )
        try:
            await app_bot.send_message(ADMIN_CHAT_ID, admin_note)
        except Exception:
            pass


async def confirm_wizard(message: Message, st: dict, item: dict, quantity: int):
    cost = order_cost(item["rate"], quantity)
    try:
        prof = await sheets("get_user", user_id=message.from_user.id)
        bal = float(prof.get("balance", 0))
    except Exception:
        bal = 0.0

    net = str(item.get("network"))
    icon = tg_icon(net)
    summary = (
        "<b>Подтверждение заказа</b>\n"
        f"{icon} Услуга: {escape(str(item.get('name')))}\n"
        f"Ссылка: <code>{escape(st['order_link'])}</code>\n"
        f"Количество: {quantity}\n"
        f"Стоимость: <b>{format_money(cost)}</b>\n"
        f"Ваш баланс: <b>{format_money(bal)}</b>\n\n"
    )
    if bal < cost:
        summary += f"Недостаточно средств (не хватает {format_money(cost - bal)})."
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Пополнить баланс", callback_data="bot:topup")],
            [InlineKeyboardButton("Отменить", callback_data="botorder:cancel")],
        ])
    else:
        summary += "Подтвердите оформление заказа:"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Подтвердить и оплатить", callback_data="botorder:confirm")],
            [InlineKeyboardButton("Отменить", callback_data="botorder:cancel")],
        ])
    set_user_state(message.from_user.id, "confirming", **st)
    await message.reply_text(summary, reply_markup=kb)


# --- REST API aiohttp для WebApp ---
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
        await app_bot.send_message(ADMIN_CHAT_ID, f"Запрос пополнения: ID {u['id']} на {amt} ₽\n#id{u['id']}")
        return web.json_response({"ok": True}, headers=cors_headers())
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400, headers=cors_headers())


async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=25))
    await app_bot.start()


async def on_cleanup(app):
    if http:
        await http.close()
    await app_bot.stop()


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