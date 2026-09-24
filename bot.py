"""
Birthday Bot — Telegram Mini App + уведомления
Зависимости: pip install python-telegram-bot apscheduler
"""

import json
import logging
from datetime import date, datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)

# ── Конфиг ────────────────────────────────────────────────────────────────
BOT_TOKEN = "ВАШ_TOKEN_ЗДЕСЬ"
WEBAPP_URL = "https://your-domain.com/index.html"  # Где хостится index.html

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Хранилище (в продакшне → БД) ─────────────────────────────────────────
# Структура: { user_id: [ {id, name, date "MM-DD", year, gift, notify:[1,7,14]}, ... ] }
store: dict[int, list] = {}


def load_store():
    try:
        with open("data.json") as f:
            raw = json.load(f)
            return {int(k): v for k, v in raw.items()}
    except FileNotFoundError:
        return {}


def save_store():
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)


def days_until(date_str: str) -> int:
    """date_str = '0000-MM-DD'"""
    _, m, d = date_str.split("-")
    today = date.today()
    next_bd = date(today.year, int(m), int(d))
    if next_bd < today:
        next_bd = date(today.year + 1, int(m), int(d))
    return (next_bd - today).days


def format_date_ru(date_str: str) -> str:
    _, m, d = date_str.split("-")
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return f"{int(d)} {months[int(m)-1]}"


def day_word(n: int) -> str:
    if n == 1: return "день"
    if 2 <= n <= 4: return "дня"
    return "дней"


# ── /start ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🎂 Открыть список дней рождений",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]])
    await update.message.reply_text(
        "Привет! Я помогу не забыть дни рождения близких 🎉\n\n"
        "Добавляй людей, ставь напоминания — я пришлю уведомление заранее.",
        reply_markup=keyboard
    )


# ── /list — список через бота ─────────────────────────────────────────────
async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    people = store.get(uid, [])
    if not people:
        await update.message.reply_text(
            "Список пустой. Добавь людей через приложение 👇",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
            ]])
        )
        return

    sorted_p = sorted(people, key=lambda p: days_until(p["date"]))
    lines = []
    for p in sorted_p:
        d = days_until(p["date"])
        if d == 0:
            badge = "🎂 СЕГОДНЯ!"
        elif d <= 7:
            badge = f"🎉 через {d} {day_word(d)}"
        else:
            badge = f"через {d} {day_word(d)}"
        lines.append(f"• *{p['name']}* — {format_date_ru(p['date'])} ({badge})")

    await update.message.reply_text(
        "📋 *Твой список:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Редактировать", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
    )


# ── Получение данных из Mini App ──────────────────────────────────────────
async def on_webapp_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception:
        return

    action = data.get("action")
    if uid not in store:
        store[uid] = []

    if action == "add":
        store[uid].append({
            "id": data.get("id") or str(int(datetime.now().timestamp())),
            "name": data["name"],
            "date": data["date"],
            "year": data.get("year", ""),
            "gift": data.get("gift", ""),
            "notify": data.get("notify", [1]),
        })
        await update.message.reply_text(
            f"✅ *{data['name']}* добавлен!\n"
            f"День рождения {format_date_ru(data['date'])} — через {days_until(data['date'])} {day_word(days_until(data['date']))}.",
            parse_mode="Markdown"
        )

    elif action == "update":
        people = store[uid]
        for i, p in enumerate(people):
            if p["id"] == data.get("id"):
                people[i] = {**p, **{k: data[k] for k in ("name","date","year","gift","notify") if k in data}}
        await update.message.reply_text("✏️ Обновлено!", parse_mode="Markdown")

    elif action == "delete":
        store[uid] = [p for p in store[uid] if p["id"] != data.get("id")]
        await update.message.reply_text("🗑 Удалено.")

    save_store()


# ── Планировщик уведомлений ───────────────────────────────────────────────
async def check_birthdays(app: Application):
    today = date.today()
    for uid, people in store.items():
        for p in people:
            d = days_until(p["date"])
            notify_days = p.get("notify", [1])

            if d not in notify_days:
                continue

            name = p["name"]
            bd_str = format_date_ru(p["date"])
            age_part = ""
            if p.get("year"):
                age = today.year + (1 if d > 0 else 0) - int(p["year"])
                age_part = f" — исполнится *{age}*"

            if d == 0:
                text = (
                    f"🎂 Сегодня день рождения у *{name}*{age_part}!\n\n"
                    f"Не забудь поздравить 🎉"
                )
            else:
                gift_hint = f"\n\n🎁 Твоя идея подарка: _{p['gift']}_" if p.get("gift") else ""
                text = (
                    f"⏰ Через *{d} {day_word(d)}* день рождения у *{name}*{age_part}\n"
                    f"📅 {bd_str}"
                    f"{gift_hint}"
                )

            try:
                await app.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Открыть список", web_app=WebAppInfo(url=WEBAPP_URL))
                    ]])
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить {uid}: {e}")


# ── Запуск ────────────────────────────────────────────────────────────────
def main():
    global store
    store = load_store()

    app = Application.builder().token(BOT_TOKEN).build()

    # Хэндлеры
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))

    # Планировщик — проверка каждый день в 09:00
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        check_birthdays,
        "cron",
        hour=9, minute=0,
        args=[app]
    )
    scheduler.start()

    logger.info("Bot started ✓")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
