import os
import json
import logging
import asyncio
from pathlib import Path
from html import escape
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, List

from aiohttp import web, ClientSession, ClientTimeout
import aiosqlite
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import InlineKeyboardButton as _BaseInlineKeyboardButton

# Расширение модели для icon_custom_emoji_id
class InlineKeyboardButton(_BaseInlineKeyboardButton):
    icon_custom_emoji_id: Optional[str] = None

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("vibeboost")

BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
TWIBOOST_API_KEY = os.environ["TWIBOOST_API_KEY"].strip()
PORT = int(os.getenv("PORT", "8080"))
CATALOG_PATH = Path(os.getenv("CATALOG_PATH", "services.json"))
DB_PATH = Path("database.db")
PAGE_SIZE = 6

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
http: Optional[ClientSession] = None

# =====================================================================
# БАЗА ДАННЫХ SQLITE (МГНОВЕННЫЙ ОТКЛИК)
# =====================================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                balance_after REAL,
                comment TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()
    log.info("База данных SQLite инициализирована.")

async def get_or_create_user(user_id: int, username: str = "", full_name: str = "") -> tuple[dict, bool]:
    """
    Возвращает (user_dict, is_new_user).
    Если пользователь новый — флаг is_new_user = True.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                if username or full_name:
                    await db.execute(
                        "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                        (username, full_name, user_id)
                    )
                    await db.commit()
                return dict(row), False

        # Новый пользователь
        await db.execute(
            "INSERT INTO users (user_id, username, full_name, balance) VALUES (?, ?, ?, 0.0)",
            (user_id, username, full_name)
        )
        await db.commit()
        return {"user_id": user_id, "username": username, "full_name": full_name, "balance": 0.0}, True

async def get_user(user_id: int) -> dict:
    u, _ = await get_or_create_user(user_id)
    return u

async def update_balance(user_id: int, delta_amount: float, comment: str = "") -> tuple[bool, float]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, 0.0

            current_balance = float(row["balance"])
            new_balance = round(current_balance + delta_amount, 2)

            if new_balance < 0:
                return False, current_balance

            await db.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, user_id))
            await db.execute(
                "INSERT INTO transactions (user_id, amount, balance_after, comment) VALUES (?, ?, ?, ?)",
                (user_id, delta_amount, new_balance, comment)
            )
            await db.commit()
            return True, new_balance

async def get_total_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            res = await cursor.fetchone()
            return res[0] if res else 0

# =====================================================================
# IN-MEMORY КЭШ КАТАЛОГА (ОТВЕТ ЗА 0.001 СЕК)
# =====================================================================
CATALOG_BY_ID: Dict[str, dict] = {}
NETWORKS_LIST: List[tuple[str, int]] = []
SERVICES_BY_NET: Dict[str, List[dict]] = {}

def init_catalog_cache():
    global CATALOG_BY_ID, NETWORKS_LIST, SERVICES_BY_NET
    if not CATALOG_PATH.exists():
        log.warning("Каталог %s не найден!", CATALOG_PATH)
        return

    try:
        data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        cat_by_id = {}
        by_net: Dict[str, List[dict]] = {}

        for item in data:
            if isinstance(item, dict) and item.get("service") is not None:
                sid = str(item["service"])
                net = str(item.get("network") or "Прочее")
                cat_by_id[sid] = item
                by_net.setdefault(net, []).append(item)

        for net in by_net:
            by_net[net].sort(key=lambda x: str(x.get("name", "")).casefold())

        sorted_nets = sorted(by_net.keys(), key=str.casefold)
        CATALOG_BY_ID = cat_by_id
        NETWORKS_LIST = [(net, len(by_net[net])) for net in sorted_nets]
        SERVICES_BY_NET = by_net
        log.info("Каталог загружен в RAM: %d услуг, %d соцсетей.", len(cat_by_id), len(sorted_nets))
    except Exception as exc:
        log.exception("Ошибка парсинга каталога: %s", exc)

init_catalog_cache()

MENU_EMOJI_IDS = {
    "catalog": "5766994197705921104",
    "profile": "5879770735999717115",
    "topup": "5404629224281612936",
    "menu": "5850309953293653168",
    "support": "6030400221232501136",
}

PREMIUM_EMOJI_IDS = {
    "Telegram": "5285350148451344065",
    "VK": "5359619818150436536",
    "ВКонтакте": "5359619818150436536",
    "Instagram": "5355097780228470775",
    "TikTok": "5353034628263330616",
    "YouTube": "5454010052421626926",
    "Twitter": "5357203031463050136",
    "Twitter X": "5357203031463050136",
    "Twitch": "5352759664457038886",
    "Дзен": "5359312848247857963",
    "Rutube": "5375542803809979254",
    "RuTube": "5375542803809979254",
}

def tg_icon(name: str) -> str:
    n = name.lower()
    if "telegram" in n: return "✈️"
    if "vk" in n or "вконтакте" in n: return "🟦"
    if "youtube" in n: return "▶️"
    if "instagram" in n: return "📸"
    if "tiktok" in n: return "🎵"
    if "twitter" in n: return "🐦"
    if "twitch" in n: return "🟣"
    return "🌐"

class BotOrder(StatesGroup):
    waiting_link = State()
    waiting_comments = State()
    waiting_poll_answer = State()
    waiting_quantity = State()
    confirming = State()

class BotTopup(StatesGroup):
    waiting_amount = State()

class BotSupport(StatesGroup):
    waiting_question = State()

def format_money(value) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}".replace(",", " ") + " ₽"

def format_unit_price(rate_per_1000) -> str:
    unit = Decimal(str(rate_per_1000)) / Decimal(1000)
    if unit >= 1:
        return f"{unit.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} ₽"
    return f"{unit.quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)} ₽"

def order_cost(rate, quantity) -> float:
    return float((Decimal(str(rate)) * Decimal(int(quantity)) / Decimal(1000)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def make_btn(text: str, callback_data: str | None = None, emoji_id: str | None = None) -> InlineKeyboardButton:
    kwargs = {"text": text}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if emoji_id:
        kwargs["icon_custom_emoji_id"] = str(emoji_id)
    return InlineKeyboardButton(**kwargs)

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            make_btn("Каталог", callback_data="cat:home", emoji_id=MENU_EMOJI_IDS.get("catalog")),
            make_btn("Профиль", callback_data="bot:profile", emoji_id=MENU_EMOJI_IDS.get("profile")),
        ],
        [
            make_btn("Пополнить баланс", callback_data="bot:topup", emoji_id=MENU_EMOJI_IDS.get("topup")),
            make_btn("Поддержка", callback_data="bot:support", emoji_id=MENU_EMOJI_IDS.get("support")),
        ]
    ])

def networks_kb():
    buttons, row = [], []
    for idx, (net, count) in enumerate(NETWORKS_LIST):
        btn = make_btn(f"{net} · {count}", callback_data=f"net:{idx}", emoji_id=PREMIUM_EMOJI_IDS.get(net))
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        make_btn("Профиль", callback_data="bot:profile", emoji_id=MENU_EMOJI_IDS.get("profile")),
        make_btn("Меню", callback_data="bot:menu", emoji_id=MENU_EMOJI_IDS.get("menu")),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def services_by_net_kb(net_idx: int, page: int = 0):
    if not NETWORKS_LIST or not (0 <= net_idx < len(NETWORKS_LIST)):
        net_idx = 0
    net, _ = NETWORKS_LIST[net_idx]
    items = SERVICES_BY_NET.get(net, [])
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))

    buttons = []
    for it in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        p = format_unit_price(it.get("rate", 0))
        buttons.append([make_btn(f"{str(it.get('name'))[:34]} · {p}/шт", callback_data=f"srv:{it['service']}:{net_idx}:{page}")])

    nav = []
    if page > 0:
        nav.append(make_btn("⬅️ Назад", callback_data=f"netpage:{net_idx}:{page-1}"))
    nav.append(make_btn(f"{page+1}/{pages}", callback_data="cat:noop"))
    if page + 1 < pages:
        nav.append(make_btn("Вперёд ➡️", callback_data=f"netpage:{net_idx}:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([
        make_btn("« К соцсетям", callback_data="cat:home"),
        make_btn("Меню", callback_data="bot:menu", emoji_id=MENU_EMOJI_IDS.get("menu")),
    ])
    return net, items, pages, page, InlineKeyboardMarkup(inline_keyboard=buttons)

# =====================================================================
# ВЫПОЛНЕНИЕ ЗАКАЗА В TWIBOOST
# =====================================================================
async def execute_provider_order(user_id: int, item: dict, link: str, quantity: int, comments: str = None, answer_number: str = None):
    service_id = str(item["service"])
    cost = order_cost(item["rate"], quantity)
    
    ok, new_bal = await update_balance(user_id, -cost, f"Заказ услуги #{service_id}")
    if not ok:
        raise ValueError("Недостаточно средств на балансе")

    params = {
        "action": "add",
        "service": service_id,
        "link": link,
        "quantity": str(quantity),
        "key": TWIBOOST_API_KEY,
    }
    if comments: params["comments"] = comments
    if answer_number: params["answer_number"] = str(answer_number)

    try:
        async with http.get("https://twiboost.com/api/v2", params=params, timeout=ClientTimeout(total=35)) as resp:
            data = await resp.json(content_type=None)
    except Exception as exc:
        log.error("Сбой поставщика: %s", exc)
        await update_balance(user_id, cost, f"Возврат по сбою #{service_id}")
        raise RuntimeError("Сервер поставщика не отвечает. Средства возвращены.")

    order_id = data.get("order")
    if not order_id:
        err = data.get("error", "Отказ поставщика")
        await update_balance(user_id, cost, f"Возврат: {err}")
        raise RuntimeError(f"Поставщик отклонил: {err}")

    try:
        u = await bot.get_chat(user_id)
        uname = f"@{u.username}" if u.username else "не указан"
    except Exception:
        uname = "не указан"

    admin_msg = (
        "🛒 <b>Новый заказ принят!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Покупатель: {uname} (ID: <code>{user_id}</code>)\n"
        f"⚙️ Услуга: {escape(item.get('name'))} [#{service_id}]\n"
        f"🔗 Ссылка: <code>{escape(link)}</code>\n"
        f"📊 Кол-во: <code>{quantity}</code> шт.\n"
        f"💰 Списано: <b>{format_money(cost)}</b>\n"
        f"🧾 Заказ Twiboost: <code>#{order_id}</code>\n\n#id{user_id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_msg, parse_mode="HTML")
    except Exception:
        pass

    return order_id, new_bal, cost

# =====================================================================
# ОБРАБОТЧИКИ
# =====================================================================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.clear()
    u = msg.from_user
    username_str = f"@{u.username}" if u.username else "не указан"
    full_name_str = u.full_name or "Пользователь"

    # Проверяем нового пользователя и сохраняем в SQLite
    user_data, is_new = await get_or_create_user(u.id, u.username or "", full_name_str)

    # Если пользователь новый — сразу уведомляем админа в личку
    if is_new:
        admin_alert = (
            "👤 <b>Новый пользователь в боте!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔹 Имя: <b>{escape(full_name_str)}</b>\n"
            f"🔹 Username: {escape(username_str)}\n"
            f"🆔 ID: <code>{u.id}</code>\n\n"
            f"Баланс: <code>0.00 ₽</code>\n#new_user #id{u.id}"
        )
        try:
            await bot.send_message(ADMIN_CHAT_ID, admin_alert, parse_mode="HTML")
        except Exception:
            pass

    text = (
        f"👋 <b>Добро пожаловать в VibeBoost, {escape(u.first_name)}!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Автоматическое продвижение и накрутка во всех социальных сетях.\n\n"
        "Выберите действие в меню:"
    )
    await msg.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "bot:menu")
async def cb_main_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Главное меню VibeBoost</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Автоматическое продвижение и накрутка во всех социальных сетях.\n\n"
        "Выберите действие в меню:"
    )
    try:
        await call.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await call.answer()

@dp.message(Command("profile", "профиль", "баланс"))
@dp.callback_query(F.data == "bot:profile")
async def show_profile(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    u = event.from_user
    user_data, _ = await get_or_create_user(u.id, u.username or "", u.full_name or "")
    bal = user_data["balance"]
    uname = f"@{u.username}" if u.username else "не указан"

    text = (
        "👤 <b>Ваш личный профиль VibeBoost</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔹 <b>Имя:</b> {escape(u.full_name)}\n"
        f"🔹 <b>Username:</b> {escape(uname)}\n"
        f"🆔 <b>Telegram ID:</b> <code>{u.id}</code>\n\n"
        f"💳 <b>Текущий баланс:</b> <b>{format_money(bal)}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Пополняйте баланс и запускайте заказы моментально!</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            make_btn("Пополнить баланс", callback_data="bot:topup", emoji_id=MENU_EMOJI_IDS.get("topup")),
            make_btn("Каталог", callback_data="cat:home", emoji_id=MENU_EMOJI_IDS.get("catalog")),
        ],
        [
            make_btn("Поддержка", callback_data="bot:support", emoji_id=MENU_EMOJI_IDS.get("support")),
            make_btn("Обновить", callback_data="bot:profile"),
        ],
        [make_btn("Меню", callback_data="bot:menu", emoji_id=MENU_EMOJI_IDS.get("menu"))]
    ])

    if isinstance(event, types.CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            pass
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("catalog", "каталог"))
@dp.callback_query(F.data == "cat:home")
async def show_catalog(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🌐 <b>Выберите социальную сеть для продвижения:</b>"
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=networks_kb(), parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=networks_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("net:"))
async def cb_net(call: types.CallbackQuery):
    idx = int(call.data.split(":")[1])
    net, items, pages, page, kb = services_by_net_kb(idx, 0)
    icon = tg_icon(net)
    text = (
        f"{icon} <b>{escape(net)}</b> · Услуги продвижения\n"
        f"Найдено услуг: <b>{len(items)}</b> (стр. {page+1}/{pages})\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите услугу из списка ниже:"
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data.startswith("netpage:"))
async def cb_netpage(call: types.CallbackQuery):
    _, idx, page = call.data.split(":")
    net, items, pages, p, kb = services_by_net_kb(int(idx), int(page))
    icon = tg_icon(net)
    text = (
        f"{icon} <b>{escape(net)}</b> · Услуги продвижения\n"
        f"Найдено услуг: <b>{len(items)}</b> (стр. {p+1}/{pages})\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Выберите услугу из списка ниже:"
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "cat:noop")
async def cb_noop(call: types.CallbackQuery):
    await call.answer()

@dp.callback_query(F.data.startswith("srv:"))
async def cb_srv(call: types.CallbackQuery):
    _, sid, net_idx, page = call.data.split(":")
    item = CATALOG_BY_ID.get(sid)
    if not item:
        await call.answer("Услуга не найдена", show_alert=True)
        return

    net = str(item.get("network"))
    desc = str(item.get("description") or "Быстрое и качественное выполнение.")
    if len(desc) > 800: desc = desc[:800] + "…"

    text = (
        f"{tg_icon(net)} <b>{escape(str(item.get('name')))}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Площадка:</b> {escape(net)}\n"
        f"📁 <b>Категория:</b> {escape(str(item.get('category', '—')))}\n"
        f"🔢 <b>ID:</b> <code>{escape(sid)}</code>\n"
        f"💎 <b>Цена:</b> <code>{format_unit_price(item.get('rate', 0))}</code> за 1 шт.\n"
        f"📊 <b>Лимиты:</b> от <code>{item.get('min')}</code> до <code>{item.get('max')}</code> шт.\n\n"
        f"<b>Описание:</b>\n{escape(desc)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [make_btn("🚀 Заказать услугу", callback_data=f"order:{sid}")],
        [
            make_btn("« К списку услуг", callback_data=f"netpage:{net_idx}:{page}"),
            make_btn("Меню", callback_data="bot:menu", emoji_id=MENU_EMOJI_IDS.get("menu")),
        ]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()

# =====================================================================
# WIZARD ЗАКАЗА
# =====================================================================
@dp.callback_query(F.data.startswith("order:"))
async def cb_order(call: types.CallbackQuery, state: FSMContext):
    sid = call.data.split(":")[1]
    item = CATALOG_BY_ID.get(sid)
    if not item:
        await call.answer("Услуга недоступна", show_alert=True)
        return

    await state.set_state(BotOrder.waiting_link)
    await state.update_data(sid=sid)
    await call.message.answer(
        f"🔗 <b>Оформление заказа:</b> «{escape(item.get('name'))}»\n"
        f"Цена за 1 шт: <code>{format_unit_price(item.get('rate', 0))}</code>\n\n"
        "Отправьте ссылку на пост, канал или страницу:\n"
        "<i>(Отмена — /cancel)</i>",
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(Command("cancel"), StateFilter(BotOrder, BotTopup, BotSupport))
async def cmd_cancel(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("❌ Действие отменено.", reply_markup=main_menu_kb())

@dp.message(BotOrder.waiting_link)
async def step_link(msg: types.Message, state: FSMContext):
    link = (msg.text or "").strip()
    if not link:
        await msg.answer("⚠️ Введите корректную ссылку:")
        return

    data = await state.get_data()
    item = CATALOG_BY_ID.get(data["sid"])
    await state.update_data(link=link)

    stype = item.get("type", "Default")
    if stype in ("Custom Comments", "Mentions"):
        await state.set_state(BotOrder.waiting_comments)
        await msg.answer(f"💬 Пришлите комментарии (каждый с новой строки, от {item['min']} до {item['max']} шт.):")
        return
    if stype == "Poll":
        await state.set_state(BotOrder.waiting_poll_answer)
        await msg.answer("🎯 Введите номер или ID варианта ответа в опросе:")
        return

    await state.set_state(BotOrder.waiting_quantity)
    await msg.answer(f"📊 Введите количество (от {item['min']} до {item['max']} шт.):")

@dp.message(BotOrder.waiting_comments)
async def step_comments(msg: types.Message, state: FSMContext):
    lines = [x.strip() for x in (msg.text or "").splitlines() if x.strip()]
    data = await state.get_data()
    item = CATALOG_BY_ID.get(data["sid"])
    if not (int(item["min"]) <= len(lines) <= int(item["max"])):
        await msg.answer(f"⚠️ Нужно от {item['min']} до {item['max']} строк. Попробуйте еще раз:")
        return
    await state.update_data(comments="\n".join(lines), qty=len(lines))
    await show_confirm(msg, state, item, len(lines))

@dp.message(BotOrder.waiting_poll_answer)
async def step_poll(msg: types.Message, state: FSMContext):
    ans = (msg.text or "").strip()
    await state.update_data(answer_number=ans)
    data = await state.get_data()
    item = CATALOG_BY_ID.get(data["sid"])
    await state.set_state(BotOrder.waiting_quantity)
    await msg.answer(f"📊 Введите количество голосов (от {item['min']} до {item['max']}):")

@dp.message(BotOrder.waiting_quantity)
async def step_qty(msg: types.Message, state: FSMContext):
    try:
        qty = int((msg.text or "").strip())
    except ValueError:
        await msg.answer("⚠️ Введите целое число:")
        return
    data = await state.get_data()
    item = CATALOG_BY_ID.get(data["sid"])
    if not (int(item["min"]) <= qty <= int(item["max"])):
        await msg.answer(f"⚠️ Количество должно быть от {item['min']} до {item['max']}:")
        return
    await state.update_data(qty=qty)
    await show_confirm(msg, state, item, qty)

async def show_confirm(msg: types.Message, state: FSMContext, item: dict, qty: int):
    cost = order_cost(item["rate"], qty)
    await state.update_data(cost=cost)
    udata = await get_user(msg.from_user.id)
    bal = udata["balance"]

    text = (
        "📋 <b>Подтверждение заказа</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Услуга:</b> {escape(item.get('name'))}\n"
        f"📊 <b>Количество:</b> {qty} шт.\n"
        f"💰 <b>К оплате:</b> <b>{format_money(cost)}</b>\n"
        f"💳 <b>Ваш баланс:</b> {format_money(bal)}\n"
    )

    if bal < cost:
        text += f"\n⚠️ Недостаточно средств! Не хватает <b>{format_money(cost - bal)}</b>."
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [make_btn("Пополнить баланс", callback_data="bot:topup", emoji_id=MENU_EMOJI_IDS.get("topup"))],
            [make_btn("Отмена", callback_data="botorder:cancel")]
        ])
    else:
        text += "\nЗапустить накрутку?"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [make_btn("✅ Подтвердить и оплатить", callback_data="botorder:confirm")],
            [make_btn("❌ Отмена", callback_data="botorder:cancel")]
        ])

    await state.set_state(BotOrder.confirming)
    await msg.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "botorder:cancel", StateFilter(BotOrder.confirming))
async def cb_cancel_order(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Заказ отменён.", reply_markup=main_menu_kb())
    await call.answer()

@dp.callback_query(F.data == "botorder:confirm", StateFilter(BotOrder.confirming))
async def cb_confirm_order(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    item = CATALOG_BY_ID.get(data["sid"])
    await call.message.edit_text("⏳ <i>Оформляем заказ...</i>", parse_mode="HTML")
    await state.clear()

    try:
        oid, new_bal, cost = await execute_provider_order(
            call.from_user.id, item, data["link"], data["qty"],
            data.get("comments"), data.get("answer_number")
        )
        await call.message.edit_text(
            f"🎉 <b>Заказ #{oid} запущен!</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Услуга:</b> {escape(item.get('name'))}\n"
            f"💰 <b>Списано:</b> {format_money(cost)}\n"
            f"💳 <b>Остаток:</b> {format_money(new_bal)}",
            reply_markup=main_menu_kb(),
            parse_mode="HTML"
        )
    except Exception as e:
        await call.message.edit_text(f"❌ Ошибка: {e}", reply_markup=main_menu_kb())
    await call.answer()

# =====================================================================
# ПОПОЛНЕНИЕ И ПОДДЕРЖКА
# =====================================================================
@dp.callback_query(F.data == "bot:topup")
async def cb_topup(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotTopup.waiting_amount)
    await call.message.edit_text(
        "💳 <b>Пополнение баланса</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Введите сумму пополнения в рублях (от 10 до 100 000 ₽):\n"
        "<i>(Отмена — /cancel)</i>",
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(BotTopup.waiting_amount)
async def process_topup(msg: types.Message, state: FSMContext):
    try:
        amt = round(float(msg.text.replace(",", ".").strip()), 2)
        if not (10 <= amt <= 100000): raise ValueError
    except ValueError:
        await msg.answer("⚠️ Введите сумму от 10 до 100 000 ₽:")
        return

    await state.clear()
    u = msg.from_user
    admin_text = (
        "💳 <b>Запрос на пополнение баланса!</b>\n"
        f"👤 От: {escape(u.full_name)} (@{escape(u.username or '—')})\n"
        f"🆔 ID: <code>{u.id}</code>\n"
        f"💰 Сумма: <b>{format_money(amt)}</b>\n\n"
        f"Зачислить: <code>/addbalance {u.id} {amt}</code>\n#id{u.id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_text, parse_mode="HTML")
    except Exception:
        pass

    await msg.answer(
        f"✅ <b>Запрос на {format_money(amt)} отправлен администратору!</b>\n"
        "Баланс обновится сразу после подтверждения платежа.",
        reply_markup=main_menu_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "bot:support")
async def cb_support(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(BotSupport.waiting_question)
    kb = InlineKeyboardMarkup(inline_keyboard=[[make_btn("Отмена", callback_data="bot:menu", emoji_id=MENU_EMOJI_IDS.get("menu"))]])
    await call.message.edit_text(
        "💬 <b>Служба поддержки VibeBoost</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "Напишите ваш вопрос или опишите проблему одним сообщением:\n"
        "<i>(Отмена — /cancel)</i>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await call.answer()

@dp.message(BotSupport.waiting_question)
async def process_support(msg: types.Message, state: FSMContext):
    txt = (msg.text or msg.caption or "").strip()
    if not txt:
        await msg.answer("⚠️ Напишите вопрос текстом:")
        return
    await state.clear()
    u = msg.from_user
    admin_msg = (
        "🆘 <b>Новое обращение в поддержку!</b>\n"
        f"👤 От: {escape(u.full_name)} (@{escape(u.username or '—')})\n"
        f"🆔 ID: <code>{u.id}</code>\n\n"
        f"<b>Вопрос:</b>\n{escape(txt)}\n\n"
        f"Ответить: <code>/send {u.id} </code>\n#id{u.id}"
    )
    try:
        await bot.send_message(ADMIN_CHAT_ID, admin_msg, parse_mode="HTML")
    except Exception:
        pass
    await msg.answer("✅ <b>Ваш вопрос передан администратору!</b> Скоро вам ответят.", reply_markup=main_menu_kb(), parse_mode="HTML")

# =====================================================================
# АДМИН-КОМАНДЫ
# =====================================================================
@dp.message(Command("addbalance"))
async def admin_addbalance(msg: types.Message):
    if msg.from_user.id != ADMIN_CHAT_ID: return
    parts = msg.text.split()
    if len(parts) != 3 or not parts[1].isdigit():
        await msg.reply("Формат: <code>/addbalance user_id сумма</code>", parse_mode="HTML")
        return
    tid, amt = int(parts[1]), round(float(parts[2].replace(",", ".")), 2)
    ok, nbal = await update_balance(tid, amt, "Пополнение администратором")
    if ok:
        await msg.reply(f"✅ Баланс <code>{tid}</code> пополнен на <b>{format_money(amt)}</b>. Баланс: <b>{format_money(nbal)}</b>", parse_mode="HTML")
        try:
            await bot.send_message(tid, f"💎 <b>Ваш баланс пополнен на {format_money(amt)}!</b>\nТекущий баланс: <b>{format_money(nbal)}</b>", parse_mode="HTML")
        except Exception: pass
    else:
        await msg.reply("❌ Пользователь не найден.")

@dp.message(Command("send"))
async def admin_send(msg: types.Message):
    if msg.from_user.id != ADMIN_CHAT_ID: return
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await msg.reply("Формат: <code>/send user_id текст</code>", parse_mode="HTML")
        return
    try:
        await bot.send_message(int(parts[1]), parts[2], parse_mode="HTML")
        await msg.reply("✅ Доставлено.")
    except Exception as e:
        await msg.reply(f"Ошибка: {e}")

@dp.message(Command("stats"))
async def admin_stats(msg: types.Message):
    if msg.from_user.id != ADMIN_CHAT_ID: return
    cnt = await get_total_users_count()
    await msg.reply(f"📊 Пользователей в базе данных: <b>{cnt}</b>", parse_mode="HTML")

@dp.message(Command("reload"))
async def admin_reload(msg: types.Message):
    if msg.from_user.id != ADMIN_CHAT_ID: return
    init_catalog_cache()
    await msg.reply(f"🔄 Каталог перезагружен в RAM: {len(CATALOG_BY_ID)} услуг.")

# =====================================================================
# ВЕБ-СЕРВЕР ДЛЯ RENDER + НАДЁЖНЫЙ ЗАПУСК БОТА
# =====================================================================
async def start_bot_polling():
    # Даем 3 секунды старому процессу Render гарантированно закрыться
    await asyncio.sleep(3)
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Вебхук очищен.")
    except Exception as e:
        log.warning("delete_webhook пропущен: %s", e)

    # Вечный цикл поллинга: если соединение сбросится — бот переподключится сам
    while True:
        try:
            log.info("Запуск Telegram Polling...")
            await dp.start_polling(bot, handle_signals=False)
        except asyncio.CancelledError:
            log.info("Polling остановлен штатно.")
            break
        except Exception as e:
            log.error("Сбой polling: %s. Перезапуск через 5 сек...", e, exc_info=True)
            await asyncio.sleep(5)

async def on_startup(app):
    global http
    http = ClientSession(timeout=ClientTimeout(total=25))
    await init_db()
    app["bot_task"] = asyncio.create_task(start_bot_polling())

async def on_cleanup(app):
    t = app.get("bot_task")
    if t:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    if http:
        await http.close()
    await bot.session.close()

app = web.Application()
app.router.add_get("/", lambda r: web.json_response({"status": "live", "service": "VibeBoost Bot"}))
app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
