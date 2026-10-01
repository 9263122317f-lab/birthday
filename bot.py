"""
Birthday Bot — Telegram Bot + HTTP сервер на порту 3000
"""

import json
import logging
import os
import sqlite3
import threading
from datetime import date
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Конфиг ────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = "https://birthdaytotime.bothost.tech"
DATA_DIR   = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH    = DATA_DIR / "birthdays.db"
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PORT       = 3000

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── БД ────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                gift TEXT DEFAULT '',
                notify TEXT DEFAULT '[1]',
                folder_id TEXT DEFAULT NULL,
                PRIMARY KEY (id, user_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                color TEXT DEFAULT '#6366f1',
                PRIMARY KEY (id, user_id)
            )
        """)
        conn.commit()

def get_user_id_from_init(init_data: str):
    try:
        from urllib.parse import unquote, parse_qsl
        parsed = dict(parse_qsl(unquote(init_data)))
        user = json.loads(parsed.get("user", "{}"))
        return int(user.get("id", 0)) or None
    except Exception:
        return None

# ── HTTP сервер (статика + API) ───────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE_DIR, **kw)

    def log_message(self, format, *args):
        pass

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def get_uid(self):
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        uid = get_user_id_from_init(init_data)
        if not uid:
            raw = self.headers.get("X-User-Id")
            uid = int(raw) if raw else None
        return uid

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/data":
            uid = self.get_uid()
            if not uid:
                self.send_json(401, {"error": "unauthorized"})
                return
            with get_db() as conn:
                people = [dict(r) for r in conn.execute(
                    "SELECT * FROM people WHERE user_id=?", (uid,)).fetchall()]
                folders = [dict(r) for r in conn.execute(
                    "SELECT * FROM folders WHERE user_id=?", (uid,)).fetchall()]
            for p in people:
                p["notify"] = json.loads(p["notify"])
                p["folderId"] = p.pop("folder_id")
            self.send_json(200, {"people": people, "folders": folders})
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/data":
            uid = self.get_uid()
            if not uid:
                self.send_json(401, {"error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            with get_db() as conn:
                conn.execute("DELETE FROM people WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM folders WHERE user_id=?", (uid,))
                for p in body.get("people", []):
                    conn.execute(
                        "INSERT INTO people VALUES (?,?,?,?,?,?,?)",
                        (p["id"], uid, p["name"], p["date"],
                         p.get("gift", ""), json.dumps(p.get("notify", [1])),
                         p.get("folderId"))
                    )
                for f in body.get("folders", []):
                    conn.execute(
                        "INSERT INTO folders VALUES (?,?,?,?)",
                        (f["id"], uid, f["name"], f.get("color", "#6366f1"))
                    )
                conn.commit()
            self.send_json(200, {"ok": True})
        else:
            self.send_json(404, {"error": "not found"})

def serve():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ── Telegram Bot ──────────────────────────────────
def days_until(date_str):
    p = date_str.split("-")
    m, d = int(p[1]), int(p[2])
    today = date.today()
    nxt = date(today.year, m, d)
    if nxt < today:
        nxt = date(today.year + 1, m, d)
    return (nxt - today).days

def day_word(n):
    if n == 1: return "день"
    if 2 <= n <= 4: return "дня"
    return "дней"

def format_date_ru(date_str):
    p = date_str.split("-")
    m, d = int(p[1]), int(p[2])
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    return f"{d} {months[m-1]}"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу не забыть дни рождения 🎉\n\nДанные синхронизируются между устройствами.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎂 Открыть список", web_app=WebAppInfo(url=WEBAPP_URL))
        ]])
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with get_db() as conn:
        people = [dict(r) for r in conn.execute(
            "SELECT * FROM people WHERE user_id=?", (uid,)).fetchall()]
    if not people:
        await update.message.reply_text("Список пустой.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
            ]]))
        return
    lines = []
    for p in sorted(people, key=lambda x: days_until(x["date"])):
        d = days_until(p["date"])
        badge = "🎂 СЕГОДНЯ!" if d == 0 else f"через {d} {day_word(d)}"
        lines.append(f"• *{p['name']}* — {format_date_ru(p['date'])} ({badge})")
    await update.message.reply_text(
        "📋 *Список:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
        ]]))

async def check_birthdays(app):
    with get_db() as conn:
        uids = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM people").fetchall()]
    for uid in uids:
        with get_db() as conn:
            people = [dict(r) for r in conn.execute(
                "SELECT * FROM people WHERE user_id=?", (uid,)).fetchall()]
        for p in people:
            d = days_until(p["date"])
            notify = json.loads(p["notify"]) if isinstance(p["notify"], str) else p["notify"]
            notify = list(set(notify))  # убираем дубли
            # 0 = сегодня, 1 = "в день рождения" (исторически одно и то же)
            should_notify = d in notify or (d == 0 and 1 in notify)
            if not should_notify:
                continue
            if d == 0:
                text = f"🎂 Сегодня день рождения у *{p['name']}*!\n\nНе забудь поздравить 🎉"
            else:
                gift = f"\n\n🎁 Идея: _{p['gift']}_" if p.get("gift") else ""
                text = f"⏰ Через *{d} {day_word(d)}* ДР у *{p['name']}*\n📅 {format_date_ru(p['date'])}{gift}"
            try:
                await app.bot.send_message(
                    chat_id=uid, text=text, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📋 Список", web_app=WebAppInfo(url=WEBAPP_URL))
                    ]]))
            except Exception as e:
                logger.warning(f"Ошибка {uid}: {e}")

async def post_init(app):
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_birthdays, "cron", hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info("Scheduler started ✓")

# ── Запуск ────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    threading.Thread(target=serve, daemon=True).start()
    logger.info(f"Web server started on port {PORT} ✓")
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    logger.info("Bot started ✓")
    app.run_polling(drop_pending_updates=True)
