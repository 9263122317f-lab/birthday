"""
Birthday Bot — Telegram Mini App + уведомления
Зависимости: pip3 install python-telegram-bot apscheduler
"""

import json
import logging
import os, threading
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)




BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # index.html лежит в корне репо
PORT = 3000

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE_DIR, **kw)

def serve():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

threading.Thread(target=serve, daemon=True).start()

# ── Конфиг ────────────────────────────────────────────────────────────────
BOT_TOKEN = ""
WEBAPP_URL = "https://9263122317f-lab.github.io/birthday/"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    parts = date_str.split("-")
    m, d = int(parts[1]), int(parts[2])
    today = date.today()
    next_bd = date(today.year, m, d)
    if next_bd < today:
        next_bd = date(today.year + 1, m, d)
    return (next_bd - today).days


def format_date_ru(date_str: str) -> str:
    parts = date_str.split("-")
    m, d = int(parts[1]), int(parts[2])
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return f"{d} {months[m-1]}"


def day_word(n: int) -> str:
    if n == 1: return "день"
    if 2 <= n <= 4: return "дня"
    return "дней"


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
                people[i] = {**p, **{k: data[k] for k in ("name","date","gift","notify") if k in data}}
        await update.message.reply_text("✏️ Обновлено!")

    elif action == "delete":
        store[uid] = [p for p in store[uid] if p["id"] != data.get("id")]
        await update.message.reply_text("🗑 Удалено.")

    save_store()


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

            if d == 0:
                text = f"🎂 Сегодня день рождения у *{name}*!\n\nНе забудь поздравить 🎉"
            else:
                gift_hint = f"\n\n🎁 Идея подарка: _{p['gift']}_" if p.get("gift") else ""
                text = f"⏰ Через *{d} {day_word(d)}* день рождения у *{name}*\n📅 {bd_str}{gift_hint}"

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


async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_birthdays, "cron", hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info("Scheduler started ✓")


def main():
    global store
    store = load_store()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))

    logger.info("Bot started ✓")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
