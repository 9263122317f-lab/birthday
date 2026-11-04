"""
Birthday Bot — Telegram Bot + HTTP сервер (статика + API) на порту 3000
"""

import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qsl

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Конфиг ────────────────────────────────────────
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
WEBAPP_URL = "https://birthdaytotime.bothost.tech"
DATA_DIR   = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH    = DATA_DIR / "birthdays.db"
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
PORT       = 3000

# ── Лимиты ────────────────────────────────────────
MAX_PEOPLE       = 300
MAX_FOLDERS      = 20
MAX_LINKS        = 10          # активных ссылок на пользователя
MAX_SHARE_PEOPLE = 100
MAX_BODY         = 512 * 1024  # байт
SHARE_TTL_DAYS   = 30
INIT_DATA_TTL    = 7 * 24 * 3600
ALLOWED_NOTIFY   = {0, 1, 3, 7, 14, 30}

ID_RE    = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
DATE_RE  = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# Отдаём ТОЛЬКО эти файлы. Всё остальное (база, bot.py, .git) — 404.
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/Migra-Medium.ttf": "Migra-Medium.ttf",
    "/kinetika-medium.ttf": "kinetika-medium.ttf",
}

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO)
# httpx пишет в логи URL с токеном бота — глушим
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:
    MSK = None

def today():
    return datetime.now(MSK).date() if MSK else date.today()

# ── БД ────────────────────────────────────────────
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                gift TEXT DEFAULT '',
                notify TEXT DEFAULT '[0]',
                folder_id TEXT DEFAULT NULL,
                gifts TEXT DEFAULT '[]',
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_lists (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                people_ids TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(people)")]
        if "gifts" not in cols:
            conn.execute("ALTER TABLE people ADD COLUMN gifts TEXT DEFAULT '[]'")
        # Починка строк, записанных предыдущей версией (колонки были перепутаны:
        # в folder_id попадал список notify, в notify — список подарков, в gifts — id папки).
        # Настоящий folder_id никогда не начинается с '['.
        conn.execute("""
            UPDATE people SET
                notify    = folder_id,
                gifts     = notify,
                folder_id = CASE WHEN gifts IS NULL OR gifts IN ('', '[]') THEN NULL ELSE gifts END
            WHERE folder_id LIKE '[%'
        """)
        conn.execute("UPDATE people SET notify='[0]' WHERE notify IS NULL OR notify IN ('', '[]')")

# ── Авторизация Telegram ──────────────────────────
def user_from_init_data(init_data):
    """Проверяет подпись initData и возвращает id пользователя (или None)."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received = parsed.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, received):
            return None
        if time.time() - int(parsed.get("auth_date", 0)) > INIT_DATA_TTL:
            return None
        return int(json.loads(parsed["user"])["id"]) or None
    except Exception:
        return None

# ── Telegram API без event loop (для HTTP-потока) ─
def tg_api(method, payload=None):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

_bot_username = None
def get_bot_username():
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = tg_api("getMe")["result"]["username"]
        except Exception as e:
            logger.warning(f"getMe failed: {e}")
    return _bot_username

# ── Валидация ─────────────────────────────────────
def clean_str(v, n):
    return str(v if v is not None else "").strip()[:n]

def load_json(s, default):
    try:
        v = json.loads(s) if isinstance(s, str) else s
        return v if isinstance(v, list) else default
    except Exception:
        return default

def clean_notify(raw):
    out = []
    for n in (raw if isinstance(raw, list) else [])[:10]:
        try:
            n = int(n)
        except Exception:
            continue
        if n in ALLOWED_NOTIFY and n not in out:
            out.append(n)
    return out or [0]

def clean_person(p):
    if not isinstance(p, dict):
        return None
    pid = clean_str(p.get("id"), 40)
    name = clean_str(p.get("name"), 100)
    d = clean_str(p.get("date"), 10)
    if not ID_RE.match(pid) or not name or not DATE_RE.match(d):
        return None
    _, m, dd = (int(x) for x in d.split("-"))
    if not (1 <= m <= 12 and 1 <= dd <= 31):
        return None
    gifts = []
    raw_gifts = p.get("gifts")
    for g in (raw_gifts if isinstance(raw_gifts, list) else [])[:20]:
        if isinstance(g, dict) and clean_str(g.get("name"), 200):
            url = clean_str(g.get("url"), 500)
            if not url.lower().startswith(("http://", "https://")):
                url = ""
            gifts.append({
                "name": clean_str(g.get("name"), 200),
                "price": clean_str(g.get("price"), 50),
                "url": url,
            })
    fid = clean_str(p.get("folderId"), 40) or None
    if fid and not ID_RE.match(fid):
        fid = None
    return {
        "id": pid, "name": name, "date": d,
        "gift": clean_str(p.get("gift"), 500),
        "gifts": gifts,
        "notify": clean_notify(p.get("notify")),
        "folder_id": fid,
    }

def clean_folder(f):
    if not isinstance(f, dict):
        return None
    fid = clean_str(f.get("id"), 40)
    name = clean_str(f.get("name"), 50)
    color = clean_str(f.get("color"), 7)
    if not ID_RE.match(fid) or not name:
        return None
    if not COLOR_RE.match(color):
        color = "#6366f1"
    return {"id": fid, "name": name, "color": color}

# ── HTTP сервер ───────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE_DIR, **kw)

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def err(self, code, message):
        self.send_json(code, {"error": True, "message": message})

    def uid(self):
        return user_from_init_data(self.headers.get("X-Telegram-Init-Data", ""))

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY:
            return None
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_HEAD(self):
        f = STATIC_FILES.get(self.path.split("?")[0])
        if not f:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.path = "/" + f
        super().do_HEAD()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/data":
            uid = self.uid()
            if not uid:
                return self.err(401, "unauthorized")
            with db() as conn:
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM people WHERE user_id=?", (uid,))]
                folders = [dict(r) for r in conn.execute(
                    "SELECT id, name, color FROM folders WHERE user_id=?", (uid,))]
            people = [{
                "id": p["id"], "name": p["name"], "date": p["date"],
                "gift": p.get("gift") or "",
                "gifts": load_json(p.get("gifts"), []),
                "notify": load_json(p.get("notify"), [0]),
                "folderId": p.get("folder_id"),
            } for p in rows]
            return self.send_json(200, {"people": people, "folders": folders})

        if path.startswith("/api/share/"):
            if not self.uid():
                return self.err(401, "unauthorized")
            token = path[len("/api/share/"):]
            if not TOKEN_RE.match(token):
                return self.err(404, "Ссылка не найдена")
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM shared_lists WHERE token=?", (token,)).fetchone()
                if not row:
                    return self.err(404, "Ссылка не найдена")
                if row["expires_at"] < datetime.now().isoformat():
                    return self.err(410, "Ссылка устарела")
                people = []
                for pid in load_json(row["people_ids"], []):
                    r = conn.execute(
                        "SELECT name, date FROM people WHERE id=? AND user_id=?",
                        (pid, row["owner_id"])).fetchone()
                    if r:
                        people.append({"name": r["name"], "date": r["date"]})
            return self.send_json(200, {"people": people})

        f = STATIC_FILES.get(path)
        if not f:
            return self.err(404, "not found")
        self.path = "/" + f
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        uid = self.uid()
        if not uid:
            return self.err(401, "unauthorized")
        body = self.read_body()
        if body is None:
            return self.err(400, "Некорректный запрос")

        # ── сохранить весь список ──
        if path == "/api/data":
            raw_people = body.get("people", [])
            raw_folders = body.get("folders", [])
            if not isinstance(raw_people, list) or not isinstance(raw_folders, list):
                return self.err(400, "Некорректные данные")
            if len(raw_people) > MAX_PEOPLE:
                return self.err(400, f"Максимум {MAX_PEOPLE} человек")
            if len(raw_folders) > MAX_FOLDERS:
                return self.err(400, f"Максимум {MAX_FOLDERS} папок")
            people = [clean_person(p) for p in raw_people]
            folders = [clean_folder(f) for f in raw_folders]
            if any(p is None for p in people) or any(f is None for f in folders):
                return self.err(400, "Некорректные данные")
            with db() as conn:
                conn.execute("DELETE FROM people WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM folders WHERE user_id=?", (uid,))
                seen = set()
                for p in people:
                    if p["id"] in seen:
                        continue
                    seen.add(p["id"])
                    conn.execute(
                        "INSERT INTO people (id, user_id, name, date, gift, gifts, notify, folder_id) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (p["id"], uid, p["name"], p["date"], p["gift"],
                         json.dumps(p["gifts"], ensure_ascii=False),
                         json.dumps(p["notify"]), p["folder_id"]))
                seen = set()
                for f in folders:
                    if f["id"] in seen:
                        continue
                    seen.add(f["id"])
                    conn.execute(
                        "INSERT INTO folders (id, user_id, name, color) VALUES (?,?,?,?)",
                        (f["id"], uid, f["name"], f["color"]))
            return self.send_json(200, {"ok": True})

        # ── создать ссылку ──
        if path == "/api/share/create":
            ids = body.get("people_ids")
            if not isinstance(ids, list) or not ids or len(ids) > MAX_SHARE_PEOPLE:
                return self.err(400, "Выбери от 1 до 100 человек")
            username = get_bot_username()
            if not username:
                return self.err(503, "Бот временно недоступен, попробуй позже")
            now = datetime.now()
            with db() as conn:
                conn.execute("DELETE FROM shared_lists WHERE expires_at < ?", (now.isoformat(),))
                own = {r["id"] for r in conn.execute(
                    "SELECT id FROM people WHERE user_id=?", (uid,))}
                ids = sorted({str(i) for i in ids if str(i) in own})
                if not ids:
                    return self.err(400, "Выбери хотя бы одного человека")
                expires = (now + timedelta(days=SHARE_TTL_DAYS)).isoformat()
                existing = conn.execute(
                    "SELECT token, people_ids FROM shared_lists WHERE owner_id=?", (uid,)).fetchall()
                token = None
                for row in existing:
                    if load_json(row["people_ids"], []) == ids:
                        token = row["token"]
                        conn.execute("UPDATE shared_lists SET expires_at=? WHERE token=?",
                                     (expires, token))
                        break
                if token is None:
                    if len(existing) >= MAX_LINKS:
                        return self.err(429, f"Максимум {MAX_LINKS} активных ссылок. "
                                             f"Подожди, пока старые истекут")
                    token = secrets.token_urlsafe(12)
                    conn.execute(
                        "INSERT INTO shared_lists (token, owner_id, people_ids, created_at, expires_at) "
                        "VALUES (?,?,?,?,?)",
                        (token, uid, json.dumps(ids), now.isoformat(), expires))
            link = f"https://t.me/{username}?start=share_{token}"
            sent = False
            try:
                tg_api("sendMessage", {
                    "chat_id": uid,
                    "text": (f"🎂 Список дней рождений ({len(ids)} чел.)\n\n"
                             f"Перешли это сообщение другу — по ссылке он сможет сохранить список себе:\n"
                             f"{link}\n\nСсылка действует {SHARE_TTL_DAYS} дней"),
                    "disable_web_page_preview": True,
                })
                sent = True
            except Exception as e:
                logger.warning(f"share sendMessage failed: {e}")
            return self.send_json(200, {"link": link, "sent": sent})

        # ── сохранить чужой список себе ──
        if path == "/api/share/import":
            token = str(body.get("token", ""))
            if not TOKEN_RE.match(token):
                return self.err(404, "Ссылка не найдена")
            notify = clean_notify(body.get("notify"))
            with db() as conn:
                row = conn.execute(
                    "SELECT * FROM shared_lists WHERE token=?", (token,)).fetchone()
                if not row:
                    return self.err(404, "Ссылка не найдена")
                if row["expires_at"] < datetime.now().isoformat():
                    return self.err(410, "Ссылка устарела")
                if row["owner_id"] == uid:
                    return self.send_json(200, {"ok": True, "imported": 0, "own": True})
                have = conn.execute(
                    "SELECT name, date FROM people WHERE user_id=?", (uid,)).fetchall()
                keys = {(r["name"].strip().lower(), r["date"]) for r in have}
                count = len(have)
                imported, limited = 0, False
                for pid in load_json(row["people_ids"], []):
                    r = conn.execute(
                        "SELECT name, date FROM people WHERE id=? AND user_id=?",
                        (pid, row["owner_id"])).fetchone()
                    if not r:
                        continue
                    key = (r["name"].strip().lower(), r["date"])
                    if key in keys:
                        continue
                    if count >= MAX_PEOPLE:
                        limited = True
                        break
                    # Подарки и папки не передаём — только имя и дату
                    conn.execute(
                        "INSERT INTO people (id, user_id, name, date, gift, gifts, notify, folder_id) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (secrets.token_urlsafe(8), uid, r["name"], r["date"], "", "[]",
                         json.dumps(notify), None))
                    keys.add(key)
                    count += 1
                    imported += 1
            out = {"ok": True, "imported": imported}
            if limited:
                out["message"] = f"Добавлено {imported}. Достигнут лимит {MAX_PEOPLE} человек"
            return self.send_json(200, out)

        return self.err(404, "not found")

def serve():
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

# ── Telegram Bot ──────────────────────────────────
def safe_date(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return date(y, m, 28)  # 29 февраля в невисокосный год

def days_until(date_str):
    p = date_str.split("-")
    m, d = int(p[1]), int(p[2])
    t = today()
    nxt = safe_date(t.year, m, d)
    if nxt < t:
        nxt = safe_date(t.year + 1, m, d)
    return (nxt - t).days

def day_word(n):
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"

def format_date_ru(date_str):
    p = date_str.split("-")
    m, d = int(p[1]), int(p[2])
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    return f"{d} {months[m-1]}"

def open_button(text="🎂 Открыть список", url=WEBAPP_URL):
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, web_app=WebAppInfo(url=url))]])

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Переход по ссылке-приглашению: t.me/бот?start=share_<token>
    if ctx.args and ctx.args[0].startswith("share_"):
        token = ctx.args[0][len("share_"):]
        row = None
        if TOKEN_RE.match(token):
            with db() as conn:
                row = conn.execute(
                    "SELECT people_ids, expires_at FROM shared_lists WHERE token=?",
                    (token,)).fetchone()
        if not row or row["expires_at"] < datetime.now().isoformat():
            await update.message.reply_text("Ссылка устарела или не найдена 😕")
            return
        count = len(load_json(row["people_ids"], []))
        await update.message.reply_text(
            f"🎁 Тебе прислали список дней рождений ({count} чел.)\n\n"
            f"Открой, чтобы посмотреть и сохранить себе.",
            reply_markup=open_button("Открыть список", f"{WEBAPP_URL}/?share={token}"))
        return
    await update.message.reply_text(
        "Привет! Я помогу не забыть дни рождения 🎉\n\nДанные синхронизируются между устройствами.",
        reply_markup=open_button())

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with db() as conn:
        people = [dict(r) for r in conn.execute(
            "SELECT name, date FROM people WHERE user_id=?", (uid,))]
    if not people:
        await update.message.reply_text("Список пустой.", reply_markup=open_button("Открыть"))
        return
    people.sort(key=lambda x: days_until(x["date"]))
    lines = []
    for p in people[:40]:
        d = days_until(p["date"])
        badge = "🎂 СЕГОДНЯ!" if d == 0 else f"через {d} {day_word(d)}"
        lines.append(f"• <b>{html.escape(p['name'])}</b> — {format_date_ru(p['date'])} ({badge})")
    if len(people) > 40:
        lines.append(f"\nи ещё {len(people) - 40}…")
    await update.message.reply_text(
        "📋 <b>Список:</b>\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=open_button("✏️ Открыть"))

async def check_birthdays(app):
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM people")]
    for p in rows:
        try:
            d = days_until(p["date"])
            notify = clean_notify(load_json(p.get("notify"), [0]))
            if not (d in notify or (d == 0 and 1 in notify)):
                continue
            name = html.escape(p["name"])
            if d == 0:
                text = f"🎂 Сегодня день рождения у <b>{name}</b>!\n\nНе забудь поздравить 🎉"
            else:
                gift = f"\n\n🎁 Идея: <i>{html.escape(p['gift'])}</i>" if p.get("gift") else ""
                text = (f"⏰ Через <b>{d} {day_word(d)}</b> день рождения у <b>{name}</b>\n"
                        f"📅 {format_date_ru(p['date'])}{gift}")
            await app.bot.send_message(
                chat_id=p["user_id"], text=text, parse_mode="HTML",
                reply_markup=open_button("📋 Список"))
        except Exception as e:
            logger.warning(f"Уведомление не отправлено ({p.get('user_id')}): {e}")

async def post_init(app):
    global _bot_username
    try:
        _bot_username = (await app.bot.get_me()).username
    except Exception as e:
        logger.warning(f"get_me failed: {e}")
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(check_birthdays, "cron", hour=9, minute=0, args=[app])
    scheduler.start()
    logger.info("Scheduler started ✓")

if __name__ == "__main__":
    init_db()
    threading.Thread(target=serve, daemon=True).start()
    logger.info(f"Server started on port {PORT} ✓")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    logger.info("Bot started ✓")
    app.run_polling(drop_pending_updates=True)
