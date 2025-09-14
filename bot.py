import os
import asyncio
import logging
from datetime import datetime
from dateutil.parser import isoparse

import httpx
import aiosqlite
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.client.default import DefaultBotProperties


# ===========================
# Настройки
# ===========================
DB_PATH = "alerts.db"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ===========================
# Инициализация БД
# ===========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            link TEXT NOT NULL,
            shop TEXT,
            product TEXT,
            price REAL,
            threshold REAL
        )
        """)
        await db.commit()


# ===========================
# FSM для добавления правил
# ===========================
class AddAlert(StatesGroup):
    waiting_for_link = State()
    waiting_for_threshold = State()


# ===========================
# Парсинг сайтов
# ===========================
async def fetch_product_info(url: str) -> tuple[str, float, str]:
    shop = "Unknown"
    if "magnit.ru" in url:
        shop = "Магнит"
    elif "lenta.com" in url:
        shop = "Лента"
    elif "5ka.ru" in url:
        shop = "Пятерочка"
    elif "bristol.ru" in url:
        shop = "Бристоль"
    elif "myspar.ru" in url:
        shop = "Спар"
    elif "wildberries.ru" in url:
        shop = "Wildberries"

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, follow_redirects=True)
        if r.status_code == 404:
            raise ValueError("❌ Товар не найден (404)")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

    product, price = None, None

    if shop == "Магнит":
        name_tag = soup.find("span", {"data-test-id": "v-product-details-offer-name"})
        price_tag = soup.find("span", {"data-v-67b88f3b": True})
        product = name_tag.text.strip() if name_tag else None
        price = float(price_tag.text.replace("₽", "").replace(",", ".").strip()) if price_tag else None

    elif shop == "Лента":
        name_tag = soup.find("span", {"_ngcontent-ng-c2436889447": True})
        price_tag = soup.find("span", {"class": "main-price"})
        product = name_tag.text.strip() if name_tag else None
        price = float(price_tag.text.split("₽")[0].replace(",", ".").strip()) if price_tag else None

    elif shop == "Пятерочка":
        name_tag = soup.find("h1", {"class": "chakra-text"})
        price_tag = soup.find("p", {"itemprop": "price"})
        product = name_tag.text.strip() if name_tag else None
        price = float(price_tag["content"]) if price_tag and price_tag.get("content") else None

    elif shop == "Бристоль":
        name_tag = soup.find("h1", {"itemprop": "name"})
        price_tag = soup.find("span", {"class": "product-card__price-tag__price"})
        product = name_tag.text.strip() if name_tag else None
        price = float(price_tag.text.replace("₽", "").replace(",", ".").strip()) if price_tag else None

    elif shop == "Спар":
        name_tag = soup.find("h1", {"class": "catalog-element__title"})
        price_tag = soup.find("span", {"class": "prices__cur"})
        product = name_tag.text.strip() if name_tag else None
        if price_tag:
            price = float(price_tag.text.replace("₽", "").replace(",", ".").replace("\xa0", "").strip())

    elif shop == "Wildberries":
        name_tag = soup.find("h1", {"class": "productTitle--J2W7I"})
        price_tag = soup.find("ins", {"class": "priceBlockFinalPrice--iToZR"})
        product = name_tag.text.strip() if name_tag else None
        if price_tag:
            price = float(price_tag.text.replace("₽", "").replace("\xa0", "").strip())

    return product, price, shop


# ===========================
# Команды
# ===========================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я бот для мониторинга цен.\n\n"
        "📌 Доступные команды:\n"
        "/search – добавить правило\n"
        "/alerts – список правил\n"
    )


@dp.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext):
    await message.answer("🔗 Введите ссылку на товар:")
    await state.set_state(AddAlert.waiting_for_link)


@dp.message(AddAlert.waiting_for_link)
async def process_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text.strip())
    await message.answer("💰 Введите минимальную цену (₽):")
    await state.set_state(AddAlert.waiting_for_threshold)


@dp.message(AddAlert.waiting_for_threshold)
async def process_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите число.")
        return

    data = await state.get_data()
    link = data["link"]

    try:
        product, price, shop = await fetch_product_info(link)
    except Exception as e:
        await message.answer(f"⚠ Ошибка при получении товара: {e}")
        await state.clear()
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO alerts (user_id, link, shop, product, price, threshold) VALUES (?, ?, ?, ?, ?, ?)",
            (message.from_user.id, link, shop, product, price, threshold)
        )
        await db.commit()

    await message.answer(
        f"✅ Правило добавлено!\n"
        f"<b>{shop}</b>: {product}\n"
        f"Текущая цена: {price}₽\n"
        f"Порог уведомления: {threshold}₽"
    )
    await state.clear()


# ===========================
# Inline-UI для управления
# ===========================
def alerts_keyboard(alerts: list[dict]) -> InlineKeyboardMarkup:
    kb = []
    for alert in alerts:
        kb.append([
            InlineKeyboardButton(
                text=f"❌ {alert['product']} ({alert['threshold']}₽)",
                callback_data=f"delete:{alert['id']}"
            ),
            InlineKeyboardButton(
                text="✏ Изменить",
                callback_data=f"edit:{alert['id']}"
            )
        ])
    if alerts:
        kb.append([InlineKeyboardButton(text="🗑 Удалить все", callback_data="delete_all")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


@dp.message(Command("alerts"))
async def cmd_alerts(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, shop, product, threshold FROM alerts WHERE user_id = ?",
            (message.from_user.id,)
        )
        rows = await cursor.fetchall()

    if not rows:
        await message.answer("📭 У вас нет активных правил.")
        return

    alerts = [{"id": r[0], "shop": r[1], "product": r[2], "threshold": r[3]} for r in rows]
    text = "<b>Ваши правила:</b>\n"
    for a in alerts:
        text += f"• <b>{a['shop']}</b>: {a['product']} (порог: {a['threshold']}₽)\n"

    await message.answer(text, reply_markup=alerts_keyboard(alerts))


@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete_alert(callback: CallbackQuery):
    alert_id = int(callback.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, callback.from_user.id))
        await db.commit()
    await callback.answer("✅ Удалено", show_alert=True)
    await callback.message.delete()


@dp.callback_query(F.data == "delete_all")
async def cb_delete_all(callback: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alerts WHERE user_id = ?", (callback.from_user.id,))
        await db.commit()
    await callback.answer("🗑 Все правила удалены", show_alert=True)
    await callback.message.delete()


# FSM для редактирования порога
class EditThreshold(StatesGroup):
    waiting_new_threshold = State()


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit_alert(callback: CallbackQuery, state: FSMContext):
    alert_id = int(callback.data.split(":")[1])
    await state.update_data(alert_id=alert_id)
    await callback.message.answer("Введите новый порог цены (₽):")
    await state.set_state(EditThreshold.waiting_new_threshold)


@dp.message(EditThreshold.waiting_new_threshold)
async def process_new_threshold(message: Message, state: FSMContext):
    try:
        threshold = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("❌ Введите число.")
        return

    data = await state.get_data()
    alert_id = data["alert_id"]

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE alerts SET threshold = ? WHERE id = ? AND user_id = ?",
            (threshold, alert_id, message.from_user.id)
        )
        await db.commit()

    await message.answer(f"✅ Новый порог установлен: {threshold}₽")
    await state.clear()


# ===========================
# Фоновая проверка цен
# ===========================
async def monitor_alerts():
    while True:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT id, user_id, link, shop, product, threshold FROM alerts")
            rows = await cursor.fetchall()

        for row in rows:
            alert_id, user_id, link, shop, product, threshold = row
            try:
                product_name, price, shop_name = await fetch_product_info(link)
                if price is None:
                    continue
                if price <= threshold:
                    await bot.send_message(
                        user_id,
                        f"🔥 Цена упала до {price}₽!\n"
                        f"🛍️ {product_name}\n"
                        f"🔗 {link}"
                    )
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
                        await db.commit()
            except Exception as e:
                logger.warning(f"Ошибка при мониторинге {link}: {e}")

            await asyncio.sleep(RATE_LIMIT_MS / 1000)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ===========================
# Main
# ===========================
async def main():
    await init_db()
    asyncio.create_task(monitor_alerts())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
