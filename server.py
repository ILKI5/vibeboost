import asyncio
import html
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ----------------- КОНФИГУРАЦИЯ -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН_БОТА")
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))  # Укажите ваш Telegram ID
SERVICES_FILE = Path("services.json")

# Идентификаторы кастомных эмодзи Telegram (Telegram Custom Emoji ID)
EMOJI_CATALOG = "5766994197705921104"
EMOJI_PROFILE = "5879770735999717115"
EMOJI_TOPUP = "5404629224281612936"
EMOJI_MENU = "5850309953293653168"
EMOJI_SUPPORT = "6030400221232501136"

PAGE_SIZE = 6

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ----------------- FSM СОСТОЯНИЯ -----------------
class SupportState(StatesGroup):
    waiting_for_question = State()


# ----------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ КАТАЛОГА -----------------
def load_catalog() -> List[Dict[str, Any]]:
    """Загрузка каталога услуг из services.json."""
    if not SERVICES_FILE.exists():
        return [
            {
                "id": 1,
                "name": "Telegram Подписчики (Быстрые)",
                "network": "Telegram",
                "category": "Подписчики",
                "rate": "150",
                "min": "10",
                "max": "50000",
                "desc": "Быстрый старт, высокое качество.",
            },
            {
                "id": 2,
                "name": "Telegram Просмотры на пост",
                "network": "Telegram",
                "category": "Просмотры",
                "rate": "15",
                "min": "100",
                "max": "100000",
                "desc": "Мгновенное начисление.",
            },
            {
                "id": 3,
                "name": "VK Подписчики в группу",
                "network": "VK",
                "category": "Подписчики",
                "rate": "120",
                "min": "50",
                "max": "20000",
                "desc": "Живые пользователи.",
            },
            {
                "id": 4,
                "name": "YouTube Просмотры",
                "network": "YouTube",
                "category": "Просмотры",
                "rate": "220",
                "min": "500",
                "max": "1000000",
                "desc": "Удержание от 1 мин.",
            },
        ]
    try:
        data = json.loads(SERVICES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return list(data.values())
        return []
    except Exception as e:
        logger.error(f"Ошибка загрузки каталога: {e}")
        return []


def get_networks() -> List[str]:
    catalog = load_catalog()
    nets = []
    for item in catalog:
        net = str(item.get("network") or "Другое").strip()
        if net and net not in nets:
            nets.append(net)
    return sorted(nets)


def get_categories(network: str) -> List[str]:
    catalog = load_catalog()
    cats = []
    for item in catalog:
        if str(item.get("network") or "Другое").strip().lower() == network.lower():
            cat = str(item.get("category") or "Общее").strip()
            if cat and cat not in cats:
                cats.append(cat)
    return sorted(cats)


def get_services(network: str, category: Optional[str] = None) -> List[Dict[str, Any]]:
    catalog = load_catalog()
    result = []
    for item in catalog:
        item_net = str(item.get("network") or "Другое").strip().lower()
        if item_net == network.lower():
            if category is not None:
                item_cat = str(item.get("category") or "Общее").strip().lower()
                if item_cat == category.lower():
                    result.append(item)
            else:
                result.append(item)
    return result


def find_service(service_id: Any) -> Optional[Dict[str, Any]]:
    catalog = load_catalog()
    for item in catalog:
        if str(item.get("id")) == str(service_id):
            return item
    return None


# ----------------- КЛАВИАТУРЫ -----------------
def btn(text: str, callback_data: str, emoji_id: Optional[str] = None) -> InlineKeyboardButton:
    """Создаёт инлайн-кнопку с поддержкой custom_emoji_id."""
    kw = {"text": text, "callback_data": callback_data}
    if emoji_id:
        kw["custom_emoji_id"] = emoji_id
    return InlineKeyboardButton(**kw)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Каталог", "menu:catalog", EMOJI_CATALOG)],
            [
                btn("Профиль", "menu:profile", EMOJI_PROFILE),
                btn("Пополнить баланс", "menu:topup", EMOJI_TOPUP),
            ],
            [btn("Поддержка", "menu:support", EMOJI_SUPPORT)],
        ]
    )


def networks_kb() -> InlineKeyboardMarkup:
    nets = get_networks()
    rows = []
    curr_row = []
    for idx, net in enumerate(nets):
        curr_row.append(btn(net, f"net:{idx}"))
        if len(curr_row) == 2:
            rows.append(curr_row)
            curr_row = []
    if curr_row:
        rows.append(curr_row)

    rows.append([btn("Меню", "menu:home", EMOJI_MENU)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def categories_kb(net_idx: int) -> InlineKeyboardMarkup:
    nets = get_networks()
    if net_idx >= len(nets):
        return networks_kb()
    network = nets[net_idx]
    cats = get_categories(network)

    rows = []
    for c_idx, cat in enumerate(cats):
        count = len(get_services(network, cat))
        rows.append([btn(f"{cat} ({count})", f"cat:{net_idx}:{c_idx}")])

    rows.append([
        btn("Назад к соцсетям", "menu:catalog"),
        btn("Меню", "menu:home", EMOJI_MENU),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def services_kb(net_idx: int, cat_idx: int, page: int = 0) -> InlineKeyboardMarkup:
    nets = get_networks()
    if net_idx >= len(nets):
        return networks_kb()
    network = nets[net_idx]
    cats = get_categories(network)

    cat_name = cats[cat_idx] if cat_idx < len(cats) else None
    items = get_services(network, cat_name)
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    page_items = items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    rows = []
    for item in page_items:
        s_id = item.get("id")
        name = item.get("name", "Услуга")
        rate = item.get("rate") or item.get("price") or "?"
        rows.append([btn(f"{name} — {rate}₽", f"svc:{s_id}:{net_idx}:{cat_idx}:{page}")])

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(btn("◀️ Назад", f"p:{net_idx}:{cat_idx}:{page - 1}"))
    if total_pages > 1:
        nav_row.append(btn(f"{page + 1}/{total_pages}", "noop"))
    if page < total_pages - 1:
        nav_row.append(btn("Вперёд ▶️", f"p:{net_idx}:{cat_idx}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    # Возврат назад и кнопка Меню
    back_target = f"net:{net_idx}" if len(cats) > 1 else "menu:catalog"
    rows.append([
        btn("Назад", back_target),
        btn("Меню", "menu:home", EMOJI_MENU),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def service_detail_kb(service_id: Any, net_idx: int, cat_idx: int, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Купить", f"buy:{service_id}")],
            [
                btn("Назад к списку", f"p:{net_idx}:{cat_idx}:{page}"),
                btn("Меню", "menu:home", EMOJI_MENU),
            ],
        ]
    )


def cancel_support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[btn("Меню", "menu:home", EMOJI_MENU)]]
    )


# ----------------- ХЕНДЛЕРЫ МЕНЮ И НАВИГАЦИИ -----------------
@dp.message(CommandStart())
@dp.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Добро пожаловать в сервис продвижения!</b>\n\n"
        "Выберите нужный раздел в меню ниже:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "menu:home")
async def cb_menu_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "👋 <b>Главное меню</b>\n\n"
        "Выберите нужный раздел:"
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:catalog")
async def cb_catalog(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    nets = get_networks()
    if not nets:
        await callback.answer("Каталог услуг пока пуст.", show_alert=True)
        return
    text = "🌐 <b>Выберите социальную сеть:</b>"
    if callback.message:
        await callback.message.edit_text(text, reply_markup=networks_kb(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:profile")
async def cb_profile(callback: CallbackQuery):
    user = callback.from_user
    text = (
        "👤 <b>Ваш профиль:</b>\n\n"
        f"▫️ ID: <code>{user.id}</code>\n"
        f"▫️ Имя: {html.escape(user.full_name)}\n"
        f"▫️ Баланс: <b>0.00 ₽</b>\n"
        f"▫️ Выполнено заказов: <b>0</b>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Пополнить баланс", "menu:topup", EMOJI_TOPUP)],
            [btn("Меню", "menu:home", EMOJI_MENU)],
        ]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "menu:topup")
async def cb_topup(callback: CallbackQuery):
    text = (
        "💳 <b>Пополнение баланса</b>\n\n"
        "Для пополнения баланса выберите платёжный метод или обратитесь в поддержку:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("Поддержка", "menu:support", EMOJI_SUPPORT)],
            [btn("Меню", "menu:home", EMOJI_MENU)],
        ]
    )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()


# ----------------- ВЫБОР СОЦСЕТИ И КАТЕГОРИИ -----------------
@dp.callback_query(F.data.startswith("net:"))
async def cb_network_selected(callback: CallbackQuery):
    try:
        net_idx = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка запроса.")
        return

    nets = get_networks()
    if net_idx >= len(nets):
        await callback.answer("Соцсеть не найдена.")
        return

    network = nets[net_idx]
    cats = get_categories(network)

    if not cats:
        await callback.answer("В этой категории пока нет услуг.")
        return

    # Если категория одна или пустая — сразу открываем список услуг
    if len(cats) == 1:
        text = f"📦 Услуги в категории <b>{html.escape(network)}</b>:"
        if callback.message:
            await callback.message.edit_text(
                text,
                reply_markup=services_kb(net_idx, 0, 0),
                parse_mode="HTML",
            )
        await callback.answer()
        return

    text = f"📂 <b>{html.escape(network)}</b> — выберите категорию услуг:"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=categories_kb(net_idx),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category_selected(callback: CallbackQuery):
    parts = callback.data.split(":")
    net_idx = int(parts[1])
    cat_idx = int(parts[2])

    nets = get_networks()
    if net_idx >= len(nets):
        await callback.answer("Ошибка перехода.")
        return

    network = nets[net_idx]
    cats = get_categories(network)
    cat_name = cats[cat_idx] if cat_idx < len(cats) else "Услуги"

    text = f"📦 <b>{html.escape(network)}</b> → <b>{html.escape(cat_name)}</b>\nВыберите услугу:"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=services_kb(net_idx, cat_idx, 0),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("p:"))
async def cb_page_selected(callback: CallbackQuery):
    parts = callback.data.split(":")
    net_idx = int(parts[1])
    cat_idx = int(parts[2])
    page = int(parts[3])

    nets = get_networks()
    network = nets[net_idx] if net_idx < len(nets) else ""
    cats = get_categories(network)
    cat_name = cats[cat_idx] if cat_idx < len(cats) else "Услуги"

    text = f"📦 <b>{html.escape(network)}</b> → <b>{html.escape(cat_name)}</b>\nВыберите услугу:"
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=services_kb(net_idx, cat_idx, page),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("svc:"))
async def cb_service_detail(callback: CallbackQuery):
    parts = callback.data.split(":")
    s_id = parts[1]
    net_idx = int(parts[2])
    cat_idx = int(parts[3])
    page = int(parts[4])

    service = find_service(s_id)
    if not service:
        await callback.answer("Услуга не найдена.", show_alert=True)
        return

    name = service.get("name", "Услуга")
    rate = service.get("rate") or service.get("price") or "0"
    min_q = service.get("min", "1")
    max_q = service.get("max", "∞")
    desc = service.get("desc") or service.get("description") or "Нет описания"

    text = (
        f"💎 <b>{html.escape(str(name))}</b>\n\n"
        f"▫️ <b>Цена:</b> {rate} ₽ за 1000 шт.\n"
        f"▫️ <b>Минимум:</b> {min_q}\n"
        f"▫️ <b>Максимум:</b> {max_q}\n\n"
        f"📝 <b>Описание:</b>\n{html.escape(str(desc))}"
    )

    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=service_detail_kb(s_id, net_idx, cat_idx, page),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def cb_buy(callback: CallbackQuery):
    await callback.answer("Недостаточно средств. Пополните баланс!", show_alert=True)


# ----------------- ПОДДЕРЖКА -----------------
@dp.callback_query(F.data == "menu:support")
async def cb_support_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.waiting_for_question)
    text = (
        "💬 <b>Служба поддержки</b>\n\n"
        "Пожалуйста, напишите ваш вопрос или опишите возникшую проблему одним сообщением.\n"
        "Администратор свяжется с вами в ближайшее время."
    )
    if callback.message:
        await callback.message.edit_text(
            text,
            reply_markup=cancel_support_kb(),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.message(SupportState.waiting_for_question)
async def process_support_question(message: Message, state: FSMContext):
    if not message.text:
        await message.answer(
            "Пожалуйста, отправьте текстовый вопрос:",
            reply_markup=cancel_support_kb(),
        )
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "отсутствует"
    user_id = user.id
    user_name = html.escape(user.full_name)
    user_text = html.escape(message.text)

    admin_msg = (
        "📩 <b>Новое обращение в поддержку!</b>\n\n"
        f"👤 <b>От:</b> {user_name} ({username})\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n\n"
        f"❓ <b>Вопрос:</b>\n{user_text}"
    )

    try:
        await bot.send_message(chat_id=ADMIN_ID, text=admin_msg, parse_mode="HTML")
        await message.answer(
            "✅ <b>Ваш вопрос успешно отправлен администратору!</b>\n"
            "Ожидайте ответа в ближайшее время.",
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Не удалось доставить сообщение администратору: {e}")
        await message.answer(
            "⚠️ Произошла ошибка при отправке сообщения администратору. Попробуйте позже.",
            reply_markup=main_menu_kb(),
        )

    await state.clear()


# ----------------- ТОЧКА ВХОДА -----------------
async def main():
    logger.info("Бот запускается...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())