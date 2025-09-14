#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any, List
from datetime import datetime
from decimal import Decimal

import aiosqlite
import httpx
from bs4 import BeautifulSoup
from dateutil.parser import isoparse

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from aiogram import F

# --------------------------
# Configuration (env vars)
# --------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Please set TELEGRAM_BOT_TOKEN environment variable")

# defaults, can override via env
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "900"))  # 15 min
RATE_LIMIT_MS = int(os.getenv("RATE_LIMIT_MS", "400"))  # ms between requests
DB_PATH = os.getenv("DB_PATH", "alerts.db")
USER_AGENT = os.getenv("USER_AGENT", "price-monitor-bot/1.0 (+https://example.com)")

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Make sure we don't accidentally log secrets
logger.propagate = True

# --------------------------
# Aiogram setup
# --------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# --------------------------
# FSM
# --------------------------
class SearchStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_threshold = State()

# --------------------------
# DB helper
# --------------------------
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    link TEXT NOT NULL,
    shop TEXT,
    product TEXT,
    price REAL,
    threshold REAL NOT NULL,
    last_notified_price REAL,
    active INTEGER DEFAULT 1,
    created_at TEXT
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()
    logger.info("Database initialized at %s", DB_PATH)

# --------------------------
# Utilities: parsing helpers
# --------------------------
def parse_price_text_to_float(text: str) -> Optional[float]:
    """
    Extract numeric price from text like "259.99 ₽", "1 123 ₽", "77,99 ₽", "154"
    """
    if not text:
        return None
    # Normalize spaces and non-breaking spaces
    txt = re.sub(r'[\u00A0\s]', '', text)
    # Replace comma as decimal separator
    txt = txt.replace(',', '.')
    # Remove non digit/dot characters except minus
    m = re.search(r'(-?\d+(\.\d+)?)', txt)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

async def fetch_html(client: httpx.AsyncClient, url: str) -> Tuple[int, Optional[str]]:
    try:
        resp = await client.get(url, timeout=20)
        status = resp.status_code
        if status == 200:
            return status, resp.text
        else:
            return status, None
    except httpx.HTTPStatusError as e:
        logger.warning("HTTP error while fetching %s: %s", url, str(e))
        return getattr(e.response, "status_code", 0), None
    except Exception as e:
        logger.exception("Error fetching %s: %s", url, e)
        return 0, None

# --------------------------
# Parsers per shop
# --------------------------
# Each parser returns tuple: (product_name, price_float) or (None, None) if not available.
async def parse_magnit(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    # product name
    name_tag = soup.find("span", {"data-test-id": "v-product-details-offer-name"})
    product = name_tag.get_text(strip=True) if name_tag else None
    # price tag
    price_tag = soup.find("span", attrs={})
    # More robust: find span that contains the ₽ sign near numeric
    candidates = soup.find_all("span")
    price = None
    for t in candidates:
        txt = t.get_text(strip=True)
        if '₽' in txt or re.search(r'\d+[,.\s]*\d*\s*₽', txt):
            price = parse_price_text_to_float(txt)
            if price is not None:
                break
    return product, price

async def parse_lenta(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("span", {"_ngcontent-ng-c2436889447": True})
    if not name_tag:
        # try h1 or title
        name_tag = soup.find("h1")
    product = name_tag.get_text(strip=True) if name_tag else None
    price_tag = soup.find("span", class_=re.compile("main-price"))
    price = parse_price_text_to_float(price_tag.get_text()) if price_tag else None
    return product, price

async def parse_5ka(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("h1", class_=re.compile("mainInformation_name"))
    if not name_tag:
        name_tag = soup.find("h1", itemprop="name")
    product = name_tag.get_text(strip=True) if name_tag else None
    price_tag = soup.find("p", class_=re.compile("priceContainer"))
    price = parse_price_text_to_float(price_tag.get_text()) if price_tag else None
    return product, price

async def parse_bristol(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("h1", itemprop="name")
    product = name_tag.get_text(strip=True) if name_tag else None
    price_tag = soup.find("span", class_=re.compile("product-card__price-tag__price"))
    price = parse_price_text_to_float(price_tag.get_text()) if price_tag else None
    return product, price

async def parse_spar(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("h1", class_=re.compile("catalog-element__title"))
    product = name_tag.get_text(strip=True) if name_tag else None
    price_tag = soup.find("span", class_=re.compile("prices__cur|js-item-price"))
    price = parse_price_text_to_float(price_tag.get_text()) if price_tag else None
    return product, price

async def parse_wildberries(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    name_tag = soup.find("h1", class_=re.compile("productTitle|product-title"))
    if not name_tag:
        name_tag = soup.find("h1")
    product = name_tag.get_text(strip=True) if name_tag else None
    price_tag = soup.find("ins", class_=re.compile("priceBlockFinalPrice"))
    if not price_tag:
        price_tag = soup.find("span", class_=re.compile("price"))
    price = parse_price_text_to_float(price_tag.get_text()) if price_tag else None
    return product, price

# Generic fallback: try common patterns
async def parse_generic(html: str) -> Tuple[Optional[str], Optional[float]]:
    soup = BeautifulSoup(html, "html.parser")
    # try h1
    name_tag = soup.find("h1")
    product = name_tag.get_text(strip=True) if name_tag else None
    # try meta property
    if not product:
        meta_title = soup.find("meta", {"property": "og:title"}) or soup.find("meta", {"name": "twitter:title"})
        if meta_title and meta_title.get("content"):
            product = meta_title.get("content").strip()
    price = None
    text = soup.get_text(" ", strip=True)
    # find first occurrence of price-like pattern
    m = re.search(r'(\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d+)?)[\s\u00A0]*₽', text)
    if m:
        price = parse_price_text_to_float(m.group(0))
    return product, price

# Map domain -> parser
DOMAIN_PARSERS = {
    "magnit.ru": parse_magnit,
    "lenta.com": parse_lenta,
    "5ka.ru": parse_5ka,
    "5ka": parse_5ka,  # fallback
    "5ka": parse_5ka,
    "5": parse_5ka,
    "bristol.ru": parse_bristol,
    "myspar.ru": parse_spar,
    "www.myspar.ru": parse_spar,
    "wildberries.ru": parse_wildberries,
    "www.wildberries.ru": parse_wildberries,
}

def select_parser_by_url(url: str):
    for domain, parser in DOMAIN_PARSERS.items():
        if domain in url:
            return parser
    # fallback for "magnit" mention
    if "magnit.ru" in url or "magnit" in url:
        return parse_magnit
    return parse_generic

# --------------------------
# DB operations
# --------------------------
async def insert_alert(user_id: int, link: str, shop: Optional[str], product: Optional[str],
                       price: Optional[float], threshold: float) -> int:
    created_at = datetime.utcnow().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO alerts (user_id, link, shop, product, price, threshold, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, link, shop, product, price, threshold, created_at)
        )
        await db.commit()
        return cur.lastrowid

async def get_user_alerts(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM alerts WHERE user_id = ? AND active = 1", (user_id,))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_all_active_alerts() -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM alerts WHERE active = 1")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

async def get_alert_by_id(alert_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,))
        r = await cur.fetchone()
        return dict(r) if r else None

async def deactivate_alert(user_id: int, alert_id: int) -> bool:
    # ensure user matches
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE alerts SET active = 0 WHERE id = ? AND user_id = ?", (alert_id, user_id))
        await db.commit()
        return cur.rowcount > 0

async def update_alert_price(alert_id: int, price: Optional[float]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alerts SET price = ? WHERE id = ?", (price, alert_id))
        await db.commit()

async def mark_alert_notified(alert_id: int, price: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE alerts SET last_notified_price = ? WHERE id = ?", (price, alert_id))
        await db.commit()

# --------------------------
# Bot commands / handlers
# --------------------------
START_TEXT = (
    "Привет! Я мониторю цены товаров и пришлю уведомление, когда цена упадёт до указанного порога.\n\n"
    "Доступные команды:\n"
    "/search — пошаговое добавление правила (ссылка -> порог)\n"
    "/alert <link> <Shop> <product> <threshold> — быстрое добавление (все через пробел; product можно взять в кавычках)\n"
    "/alerts — список ваших активных правил\n"
    "/cancel — отменить текущее действие\n"
)

@dp.message(Command(commands=["start"]))
async def cmd_start(message: types.Message):
    await message.answer(START_TEXT)

@dp.message(Command(commands=["cancel"]))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Операция отменена.")

# /search flow
@dp.message(Command(commands=["search"]))
async def cmd_search_start(message: types.Message, state: FSMContext):
    await state.set_state(SearchStates.waiting_for_link)
    await message.answer("Введите ссылку для отслеживания (пример: https://magnit.ru/...)")

@dp.message(SearchStates.waiting_for_link)
async def process_link(message: types.Message, state: FSMContext):
    url = message.text.strip()
    # Basic validation
    if not re.match(r'^https?://', url):
        await message.answer("Пожалуйста, введите валидный URL (должен начинаться с http:// или https://).")
        return
    await state.update_data(link=url)
    await state.set_state(SearchStates.waiting_for_threshold)
    await message.answer("Введите минимальную цену, при которой нужно уведомить (например: 200)")

@dp.message(SearchStates.waiting_for_threshold)
async def process_threshold(message: types.Message, state: FSMContext):
    txt = message.text.strip().replace(',', '.')
    try:
        threshold = float(txt)
        if threshold < 0:
            raise ValueError()
    except Exception:
        await message.answer("Некорректная цена. Введите положительное число (например: 199.99)")
        return

    data = await state.get_data()
    link = data.get("link")
    if not link:
        await message.answer("Ошибка: ссылка не найдена в состоянии. Повторите /search.")
        await state.clear()
        return

    # fetch page and parse
    parser = select_parser_by_url(link)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        status, html = await fetch_html(client, link)
        if status == 404 or html is None:
            await message.answer("Товар не найден (404) или страница недоступна.")
            await state.clear()
            return
        product, price = await parser(html)
    # If price is None => not available
    if price is None:
        await message.answer("Не удалось определить цену — товар может быть недоступен.")
        # still offer to save but user asked to monitor specific threshold: we'll save with price NULL
    shop = None
    if "magnit.ru" in link:
        shop = "Магнит"
    elif "lenta.com" in link:
        shop = "Лента"
    elif "5ka.ru" in link or "5ka" in link or "5%C2%A0ka" in link:
        shop = "Пятерочка"
    elif "bristol.ru" in link:
        shop = "Бристоль"
    elif "myspar.ru" in link or "spar" in link:
        shop = "Спар"
    elif "wildberries" in link:
        shop = "Wildberries"

    # save
    alert_id = await insert_alert(message.from_user.id, link, shop, product, price, threshold)
    await state.clear()

    reply = (
        f"Правило добавлено (id={alert_id}).\n"
        f"Магазин: <b>{shop or 'не определён'}</b>\n"
        f"Товар: <b>{product or 'не определено'}</b>\n"
        f"Текущая цена: <b>{price if price is not None else 'н/д'}</b>\n"
        f"Порог: <b>{threshold}</b>\n"
        f"Ссылка: {link}"
    )
    await message.answer(reply)

# /alert quick command: /alert <link> <Shop> <product> <threshold>
@dp.message(Command(commands=["alert"]))
async def cmd_alert_quick(message: types.Message):
    # Expect: /alert <link> <Shop> <product> <threshold>
    # But product may contain spaces; we try to parse last token as threshold, first token after command as link.
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.answer("Использование: /alert <link> <Shop> <product> <threshold>. Для product с пробелами возьмите в кавычки или используйте /search.")
        return
    rest = message.text[len("/alert"):].strip()
    # try to extract last number as threshold
    m = re.search(r'(-?\d+[.,]?\d*)\s*$', rest)
    if not m:
        await message.answer("Не найден порог. Укажите число в конце сообщения.")
        return
    threshold_txt = m.group(1).replace(',', '.')
    try:
        threshold = float(threshold_txt)
    except:
        await message.answer("Некорректный порог. Укажите число.")
        return
    rest_before_threshold = rest[:m.start()].strip()
    # first token is link
    tokens = rest_before_threshold.split(maxsplit=1)
    if not tokens:
        await message.answer("Не найден URL.")
        return
    link = tokens[0]
    if not re.match(r'^https?://', link):
        await message.answer("URL должен начинаться с http:// или https://")
        return
    remainder = tokens[1].strip() if len(tokens) > 1 else ""
    # try to parse shop and product from remainder: assume first word is Shop, rest is product
    if remainder:
        shop_token, _, prod_token = remainder.partition(" ")
        shop = shop_token.strip()
        product = prod_token.strip() or None
    else:
        shop = None
        product = None

    # fetch current price if possible
    parser = select_parser_by_url(link)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        status, html = await fetch_html(client, link)
        if status == 404 or html is None:
            await message.answer("Страница недоступна или 404. Но правило будет сохранено.")
            product_parsed, price = None, None
        else:
            product_parsed, price = await parser(html)
            product = product or product_parsed

    alert_id = await insert_alert(message.from_user.id, link, shop, product, price, threshold)
    await message.answer(f"Правило добавлено (id={alert_id}). Товар: <b>{product or 'не определено'}</b>. Порог: <b>{threshold}</b>")

# /alerts - list user alerts
@dp.message(Command(commands=["alerts"]))
async def cmd_alerts_list(message: types.Message):
    alerts = await get_user_alerts(message.from_user.id)
    if not alerts:
        await message.answer("У вас нет активных правил.")
        return
    for a in alerts:
        alert_id = a["id"]
        shop = a.get("shop") or "—"
        product = a.get("product") or "—"
        price = a.get("price")
        threshold = a.get("threshold")
        link = a.get("link")
        text = (
            f"ID: <b>{alert_id}</b>\n"
            f"Магазин: <b>{shop}</b>\n"
            f"Товар: <b>{product}</b>\n"
            f"Текущая цена: <b>{price if price is not None else 'н/д'}</b>\n"
            f"Порог: <b>{threshold}</b>\n"
            f"Ссылка: {link}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Удалить", callback_data=f"del:{alert_id}")]
        ])
        await message.answer(text, reply_markup=kb)

# Callback to delete
@dp.callback_query(F.data.startswith("del:"))
async def cb_delete_alert(callback: types.CallbackQuery):
    data = callback.data  # e.g., del:123
    _, sid = data.split(":", 1)
    try:
        alert_id = int(sid)
    except:
        await callback.answer("Некорректный id.", show_alert=True)
        return
    success = await deactivate_alert(callback.from_user.id, alert_id)
    if success:
        await callback.message.edit_text(callback.message.text + "\n\n✅ Удалено.")
        await callback.answer("Правило удалено.")
    else:
        await callback.answer("Не удалось удалить: возможно правило не принадлежит вам или уже удалено.", show_alert=True)

# --------------------------
# Monitor background task
# --------------------------
async def monitor_alerts():
    logger.info("monitor_alerts started (interval %ss, rate limit %sms)", POLL_INTERVAL_SECONDS, RATE_LIMIT_MS)
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=30) as client:
        while True:
            try:
                alerts = await get_all_active_alerts()
                if not alerts:
                    logger.debug("No active alerts to monitor.")
                for alert in alerts:
                    alert_id = alert["id"]
                    user_id = alert["user_id"]
                    link = alert["link"]
                    threshold = alert["threshold"]
                    # choose parser
                    parser = select_parser_by_url(link)
                    status, html = await fetch_html(client, link)
                    if status == 404:
                        logger.info("Alert %s: page 404 -> skip", alert_id)
                        await asyncio.sleep(RATE_LIMIT_MS / 1000)
                        continue
                    if html is None:
                        logger.info("Alert %s: no html -> skip", alert_id)
                        await asyncio.sleep(RATE_LIMIT_MS / 1000)
                        continue
                    product, price = await parser(html)
                    # Update current price in DB (even if None)
                    await update_alert_price(alert_id, price)
                    if price is None:
                        # product unavailable
                        logger.debug("Alert %s: price not found (maybe unavailable)", alert_id)
                    else:
                        try:
                            # Compare floats
                            if price <= threshold:
                                # Send notification
                                shop = alert.get("shop") or ""
                                message = (
                                    f"{shop}\n"
                                    f"🔥 Цена упала до <b>{price} ₽</b>!\n"
                                    f"🛍️ Название товара: \"{product or (alert.get('product') or '—')}\"\n"
                                    f"🔗 {link}"
                                )
                                await bot.send_message(chat_id=user_id, text=message)
                                # mark last notified price
                                await mark_alert_notified(alert_id, price)
                                logger.info("Notified user %s for alert %s (price %s ≤ %s)", user_id, alert_id, price, threshold)
                        except Exception as e:
                            logger.exception("Error while notifying for alert %s: %s", alert_id, e)
                    # rate limit between requests
                    await asyncio.sleep(RATE_LIMIT_MS / 1000)
            except Exception as e:
                logger.exception("Error in monitor loop: %s", e)
            # sleep before next poll
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

# --------------------------
# Startup / shutdown
# --------------------------
async def on_startup():
    logger.info("Bot is starting...")
    await init_db()
    # start monitor task
    asyncio.create_task(monitor_alerts())
    logger.info("Background monitor task started.")

async def on_shutdown():
    logger.info("Bot is shutting down...")
    await bot.session.close()

# --------------------------
# Entrypoint
# --------------------------
if __name__ == "__main__":
    import asyncio

    async def main():
        await on_startup()
        # Start polling. aiogram v3 pattern:
        try:
            await dp.start_polling(bot)
        finally:
            await on_shutdown()

    asyncio.run(main())
