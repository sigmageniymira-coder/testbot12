"""
╔══════════════════════════════════════════╗
║         HMiner Bot — Single File         ║
║  Установка: pip install python-telegram-bot flask
║  Запуск:    python hminer_bot_single.py  ║
╚══════════════════════════════════════════╝
"""

# ════════════════════════════════════════════
#  ИМПОРТЫ
# ════════════════════════════════════════════

import sqlite3, time, json, threading, random, logging
from collections import defaultdict
from flask import Flask
from telegram import Update, LabeledPrice, Chat
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    PreCheckoutQueryHandler, filters
)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════
#  КОНФИГ — меняй здесь
# ════════════════════════════════════════════

BOT_TOKEN  = "ВАШ_ТОКЕН_ЗДЕСЬ"   # ← вставь токен от BotFather
ADMIN_ID   = 123456789            # ← вставь свой Telegram ID
PORT       = 8080
DB_PATH    = "hminer.db"

# Экономика
HASH_PER_MINER_PER_HOUR = 1      # 1 HASH/час с 1 майнера
HASH_TO_COIN_RATE        = 10    # 10 HASH → 1 коин
COIN_PER_MINER           = 5     # 5 коинов → 1 майнер
DCOIN_PER_STAR           = 10    # 10 д-коинов = 1 звезда
MINERS_PER_DCOIN         = 10    # 1 д-коин = 10 майнеров
DCOIN_HASH_RATE          = 100   # 1 д-коин = 100 HASH
START_MINERS             = 10    # стартовые майнеры

# Таймер
COLLECT_INTERVAL_SEC = 3600      # сбор раз в час
HALF_RATE_AFTER_SEC  = 3600      # после 1 ч без сбора — половина скорости
MAX_ACCUMULATE_HOURS = 12        # макс. накопление

# Лимиты
TRANSFER_DAILY_LIMIT  = 10_000
MIN_ACCOUNT_AGE_HOURS = 24

# Топ
TOP_UPDATE_INTERVAL = 300        # 5 минут

# Антиспам (секунды)
COOLDOWNS = {"collect": 3, "transfer": 5, "buy": 2, "sell": 2, "profile": 2, "top": 5}

# Подписки
SUBSCRIPTIONS = {
    "fast": {
        "name": "HMiner Fast", "emoji": "⚡",
        "multiplier": 1.5, "bonus_miners": 100,
        "stars": 15, "dcoins": 150, "duration_days": 30,
    },
    "pro": {
        "name": "HMiner PRO", "emoji": "🔥",
        "multiplier": 2.0, "bonus_miners": 500,
        "stars": 25, "dcoins": 250, "duration_days": 30,
    },
    "ultra": {
        "name": "HMiner Ultra", "emoji": "💎",
        "multiplier": 2.5, "bonus_miners": 1500,
        "stars": 50, "dcoins": 500, "duration_days": 30,
    },
    "max": {
        "name": "HMiner Max", "emoji": "👑",
        "multiplier": 3.0, "bonus_miners": 10000,
        "stars": 100, "dcoins": 1000, "duration_days": 30,
    },
}

CONTAINER_STARS   = 40
CONTAINER_DCOINS  = 400
CONTAINER_WEIGHTS = {"fast": 50, "pro": 25, "ultra": 15, "max": 10}

# ════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ════════════════════════════════════════════

_db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id         INTEGER PRIMARY KEY,
            username        TEXT    DEFAULT '',
            first_name      TEXT    DEFAULT '',
            hash_balance    INTEGER DEFAULT 0  CHECK(hash_balance  >= 0),
            coins           INTEGER DEFAULT 0  CHECK(coins         >= 0),
            dcoins          INTEGER DEFAULT 0  CHECK(dcoins        >= 0),
            miners          INTEGER DEFAULT 10 CHECK(miners        >= 0),
            sub_miners      INTEGER DEFAULT 0  CHECK(sub_miners    >= 0),
            last_collect    INTEGER DEFAULT 0,
            registered_at   INTEGER DEFAULT 0,
            sub_type        TEXT    DEFAULT '',
            sub_expires     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER, to_id INTEGER, amount INTEGER,
            type TEXT, note TEXT DEFAULT '', created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, telegram_payment_id TEXT DEFAULT '',
            product TEXT, stars INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS top_cache (
            cache_key TEXT PRIMARY KEY,
            data TEXT, updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transfer_daily (
            user_id INTEGER, date_str TEXT, total INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date_str)
        );
        CREATE INDEX IF NOT EXISTS idx_users_hash   ON users(hash_balance DESC);
        CREATE INDEX IF NOT EXISTS idx_users_coins  ON users(coins DESC);
        """)


def get_user(user_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def get_user_by_username(username):
    uname = username.lstrip("@")
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()


def register_user(user_id, username, first_name):
    now = int(time.time())
    with _db_lock:
        with get_conn() as conn:
            existing = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                    (username or "", first_name or "", user_id)
                )
                return False
            conn.execute(
                """INSERT INTO users
                   (user_id,username,first_name,hash_balance,coins,dcoins,
                    miners,sub_miners,last_collect,registered_at,sub_type,sub_expires)
                   VALUES (?,?,?,0,0,0,?,0,?,?,'',0)""",
                (user_id, username or "", first_name or "", START_MINERS, now, now)
            )
            return True


def calculate_pending_hash(user, multiplier):
    now     = int(time.time())
    elapsed = now - user["last_collect"]
    total_miners = user["miners"] + user["sub_miners"]
    if elapsed < COLLECT_INTERVAL_SEC:
        return 0, False
    elapsed = min(elapsed, MAX_ACCUMULATE_HOURS * 3600)
    full_sec = min(elapsed, HALF_RATE_AFTER_SEC)
    half_sec = max(0, elapsed - HALF_RATE_AFTER_SEC)
    rate = (HASH_PER_MINER_PER_HOUR * total_miners * multiplier) / 3600
    total = int(rate * full_sec + rate * 0.5 * half_sec)
    return total, half_sec > 0


def collect_hash(user_id, amount):
    now = int(time.time())
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT hash_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False
                conn.execute(
                    "UPDATE users SET hash_balance=hash_balance+?, last_collect=? WHERE user_id=?",
                    (amount, now, user_id)
                )
                conn.execute("COMMIT"); return True
            except Exception:
                conn.execute("ROLLBACK"); return False


def sell_hash(user_id, amount):
    coins_gained = amount // HASH_TO_COIN_RATE
    if coins_gained == 0:
        return False, 0
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT hash_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None or row["hash_balance"] < amount:
                    conn.execute("ROLLBACK"); return False, 0
                conn.execute(
                    "UPDATE users SET hash_balance=hash_balance-?, coins=coins+? WHERE user_id=?",
                    (coins_gained * HASH_TO_COIN_RATE, coins_gained, user_id)
                )
                conn.execute("COMMIT"); return True, coins_gained
            except Exception:
                conn.execute("ROLLBACK"); return False, 0


def buy_miners_coins(user_id, count):
    cost = count * COIN_PER_MINER
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT coins FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False, "Ты не зарегистрирован."
                if row["coins"] < cost:
                    conn.execute("ROLLBACK")
                    return False, f"Нужно {cost:,} коинов, у тебя {row['coins']:,}."
                conn.execute(
                    "UPDATE users SET coins=coins-?, miners=miners+? WHERE user_id=?",
                    (cost, count, user_id)
                )
                conn.execute("COMMIT"); return True, ""
            except Exception:
                conn.execute("ROLLBACK"); return False, "Ошибка БД."


def buy_miners_dcoins(user_id, count):
    dcoin_cost = (count + MINERS_PER_DCOIN - 1) // MINERS_PER_DCOIN
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT dcoins FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False, "Ты не зарегистрирован."
                if row["dcoins"] < dcoin_cost:
                    conn.execute("ROLLBACK")
                    return False, f"Нужно {dcoin_cost:,} д-коинов, у тебя {row['dcoins']:,}."
                conn.execute(
                    "UPDATE users SET dcoins=dcoins-?, miners=miners+? WHERE user_id=?",
                    (dcoin_cost, count, user_id)
                )
                conn.execute("COMMIT"); return True, ""
            except Exception:
                conn.execute("ROLLBACK"); return False, "Ошибка БД."


def buy_hash_dcoins(user_id, dcoin_amount):
    hash_gain = dcoin_amount * DCOIN_HASH_RATE
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT dcoins FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False, 0, "Не зарегистрирован."
                if row["dcoins"] < dcoin_amount:
                    conn.execute("ROLLBACK")
                    return False, 0, f"Нужно {dcoin_amount:,} д-коинов, у тебя {row['dcoins']:,}."
                conn.execute(
                    "UPDATE users SET dcoins=dcoins-?, hash_balance=hash_balance+? WHERE user_id=?",
                    (dcoin_amount, hash_gain, user_id)
                )
                conn.execute("COMMIT"); return True, hash_gain, ""
            except Exception:
                conn.execute("ROLLBACK"); return False, 0, "Ошибка БД."


def transfer_coins(from_id, to_id, amount):
    if from_id == to_id:
        return False, "Нельзя переводить самому себе."
    if amount <= 0:
        return False, "Сумма должна быть больше 0."
    today = time.strftime("%Y-%m-%d")
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                sender   = conn.execute("SELECT coins FROM users WHERE user_id=?", (from_id,)).fetchone()
                receiver = conn.execute("SELECT coins, registered_at FROM users WHERE user_id=?", (to_id,)).fetchone()
                if sender is None:
                    conn.execute("ROLLBACK"); return False, "Ты не зарегистрирован."
                if receiver is None:
                    conn.execute("ROLLBACK"); return False, "Получатель не найден."
                age_h = (time.time() - receiver["registered_at"]) / 3600
                if age_h < MIN_ACCOUNT_AGE_HOURS:
                    conn.execute("ROLLBACK"); return False, "Получатель зарегистрировался менее 24 ч назад."
                if sender["coins"] < amount:
                    conn.execute("ROLLBACK"); return False, f"Недостаточно коинов. У тебя {sender['coins']:,}."
                daily = conn.execute(
                    "SELECT total FROM transfer_daily WHERE user_id=? AND date_str=?",
                    (from_id, today)
                ).fetchone()
                sent_today = daily["total"] if daily else 0
                if sent_today + amount > TRANSFER_DAILY_LIMIT:
                    left = TRANSFER_DAILY_LIMIT - sent_today
                    conn.execute("ROLLBACK")
                    return False, f"Суточный лимит {TRANSFER_DAILY_LIMIT:,}. Осталось: {left:,}."
                conn.execute("UPDATE users SET coins=coins-? WHERE user_id=?", (amount, from_id))
                conn.execute("UPDATE users SET coins=coins+? WHERE user_id=?", (amount, to_id))
                conn.execute(
                    """INSERT INTO transfer_daily(user_id,date_str,total) VALUES(?,?,?)
                       ON CONFLICT(user_id,date_str) DO UPDATE SET total=total+?""",
                    (from_id, today, amount, amount)
                )
                conn.execute(
                    "INSERT INTO transactions(from_id,to_id,amount,type,created_at) VALUES(?,?,?,'transfer',?)",
                    (from_id, to_id, amount, int(time.time()))
                )
                conn.execute("COMMIT"); return True, ""
            except Exception as e:
                conn.execute("ROLLBACK"); return False, f"Ошибка: {e}"


def get_multiplier(user):
    now = int(time.time())
    key = user["sub_type"] if user["sub_type"] else ""
    if key and key in SUBSCRIPTIONS and user["sub_expires"] > now:
        return SUBSCRIPTIONS[key]["multiplier"]
    return 1.0


def check_and_expire_sub(user_id):
    now = int(time.time())
    with _db_lock:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT sub_type, sub_expires FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            if row and row["sub_type"] and row["sub_expires"] < now:
                conn.execute("UPDATE users SET sub_type='', sub_expires=0 WHERE user_id=?", (user_id,))


def apply_subscription(user_id, sub_key):
    sub    = SUBSCRIPTIONS[sub_key]
    now    = int(time.time())
    expires = now + sub["duration_days"] * 86400
    bonus  = sub["bonus_miners"]
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT sub_type, sub_expires FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False
                current = row["sub_type"]
                if current and current in SUBSCRIPTIONS:
                    if SUBSCRIPTIONS[current]["multiplier"] >= sub["multiplier"]:
                        conn.execute(
                            "UPDATE users SET sub_expires=?, sub_miners=sub_miners+? WHERE user_id=?",
                            (expires, bonus, user_id)
                        )
                    else:
                        conn.execute(
                            "UPDATE users SET sub_type=?, sub_expires=?, sub_miners=sub_miners+? WHERE user_id=?",
                            (sub_key, expires, bonus, user_id)
                        )
                else:
                    conn.execute(
                        "UPDATE users SET sub_type=?, sub_expires=?, sub_miners=sub_miners+? WHERE user_id=?",
                        (sub_key, expires, bonus, user_id)
                    )
                conn.execute("COMMIT"); return True
            except Exception:
                conn.execute("ROLLBACK"); return False


def add_dcoins(user_id, amount):
    with get_conn() as conn:
        conn.execute("UPDATE users SET dcoins=dcoins+? WHERE user_id=?", (amount, user_id))


def spend_dcoins(user_id, amount):
    with _db_lock:
        with get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT dcoins FROM users WHERE user_id=?", (user_id,)).fetchone()
                if row is None:
                    conn.execute("ROLLBACK"); return False, "Не зарегистрирован."
                if row["dcoins"] < amount:
                    conn.execute("ROLLBACK")
                    return False, f"Нужно {amount:,} д-коинов, у тебя {row['dcoins']:,}."
                conn.execute("UPDATE users SET dcoins=dcoins-? WHERE user_id=?", (amount, user_id))
                conn.execute("COMMIT"); return True, ""
            except Exception:
                conn.execute("ROLLBACK"); return False, "Ошибка БД."


def create_payment(user_id, product, stars):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO payments(user_id,product,stars,status,created_at) VALUES(?,?,?,'pending',?)",
            (user_id, product, stars, int(time.time()))
        )
        return cur.lastrowid


def confirm_payment(pay_id, tg_pay_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE payments SET status='done', telegram_payment_id=? WHERE id=?",
            (tg_pay_id, pay_id)
        )


def get_top(field, chat_members=None, limit=10):
    cache_key = f"{field}:{'global' if chat_members is None else ','.join(map(str, sorted(chat_members)))}"
    now = int(time.time())
    with get_conn() as conn:
        cached = conn.execute(
            "SELECT data, updated_at FROM top_cache WHERE cache_key=?", (cache_key,)
        ).fetchone()
        if cached and (now - cached["updated_at"]) < TOP_UPDATE_INTERVAL:
            return json.loads(cached["data"])
        if chat_members is None:
            rows = conn.execute(
                f"SELECT user_id,username,first_name,{field} AS value "
                f"FROM users ORDER BY {field} DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            ph = ",".join("?" * len(chat_members))
            rows = conn.execute(
                f"SELECT user_id,username,first_name,{field} AS value "
                f"FROM users WHERE user_id IN ({ph}) ORDER BY {field} DESC LIMIT ?",
                (*chat_members, limit)
            ).fetchall()
        result = [dict(r) for r in rows]
        conn.execute(
            """INSERT INTO top_cache(cache_key,data,updated_at) VALUES(?,?,?)
               ON CONFLICT(cache_key) DO UPDATE SET data=?,updated_at=?""",
            (cache_key, json.dumps(result), now, json.dumps(result), now)
        )
        return result


def wipe_all():
    now = int(time.time())
    with _db_lock:
        with get_conn() as conn:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            conn.execute(
                "UPDATE users SET hash_balance=0, coins=0, miners=?, last_collect=?",
                (START_MINERS, now)
            )
            conn.execute("DELETE FROM transfer_daily")
            conn.execute("DELETE FROM top_cache")
            return count


# ════════════════════════════════════════════
#  УТИЛИТЫ
# ════════════════════════════════════════════

def fmt(n):
    return f"{int(n):,}"


def seconds_to_hms(secs):
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:   return f"{h}ч {m:02d}м {s:02d}с"
    if m:   return f"{m}м {s:02d}с"
    return f"{s}с"


def sub_info(user):
    now = int(time.time())
    key = user["sub_type"] if user["sub_type"] else ""
    if not key or key not in SUBSCRIPTIONS:
        return "❌ Нет подписки"
    if user["sub_expires"] < now:
        return "❌ Подписка истекла"
    sub  = SUBSCRIPTIONS[key]
    left = seconds_to_hms(user["sub_expires"] - now)
    return f"{sub['emoji']} {sub['name']} (истекает через {left})"


def multiplier_str(mult):
    return f"×{mult:.1f}"


# ════════════════════════════════════════════
#  АНТИСПАМ
# ════════════════════════════════════════════

_last_call: dict = defaultdict(float)


def check_cooldown(user_id, cmd):
    cooldown = COOLDOWNS.get(cmd, 2)
    key = (user_id, cmd)
    now = time.time()
    diff = now - _last_call[key]
    if diff < cooldown:
        return int(cooldown - diff) + 1
    _last_call[key] = now
    return 0


# ════════════════════════════════════════════
#  ХЭНДЛЕРЫ
# ════════════════════════════════════════════

# ── /start ──────────────────────────────────

async def cmd_start(update: Update, ctx):
    user = update.effective_user
    is_new = register_user(user.id, user.username or "", user.first_name or "")
    if is_new:
        text = (
            f"⛏️ *Добро пожаловать в HMiner Bot!*\n\n"
            f"Привет, {user.first_name}!\n\n"
            f"🖥️ Тебе выдано *{START_MINERS} майнеров*\n"
            f"💰 Каждый майнер приносит *1 HASH/час*\n\n"
            f"*Команды:*\n"
            f"⛏️ `собрать` — забрать намайненный HASH\n"
            f"💱 `продать [кол-во]` — продать HASH → коины\n"
            f"🖥️ `купить майнеры [кол-во]` — купить майнеры\n"
            f"💸 `перевести @user [кол-во]` — перевести коины\n"
            f"👤 `профиль` — посмотреть профиль\n"
            f"🏆 `топ hash` / `топ коины` — таблица лидеров\n"
            f"🛒 `магазин` — подписки и д-коины\n\n"
            f"Удачи! 🚀"
        )
    else:
        text = f"👋 С возвращением, *{user.first_name}*!\nНапиши `собрать` чтобы забрать HASH."
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Профиль ─────────────────────────────────

def build_profile_text(row):
    now  = int(time.time())
    mult = get_multiplier(row)
    total_miners = row["miners"] + row["sub_miners"]
    elapsed = now - row["last_collect"]
    if elapsed >= COLLECT_INTERVAL_SEC:
        collect_info = "✅ Готово к сбору!"
    else:
        collect_info = f"⏱ Сбор через {seconds_to_hms(COLLECT_INTERVAL_SEC - elapsed)}"
    name   = row["first_name"] or "Игрок"
    uname  = f"@{row['username']}" if row["username"] else "—"
    return "\n".join([
        f"👤 *Профиль: {name}* ({uname})",
        f"",
        f"⛏️ HASH:      `{fmt(row['hash_balance'])}`",
        f"🪙 Коины:    `{fmt(row['coins'])}`",
        f"💎 Д-коины:  `{fmt(row['dcoins'])}`",
        f"",
        f"🖥️ Майнеры:  `{fmt(total_miners)}`",
        f"  ├ Обычные: `{fmt(row['miners'])}`",
        f"  └ Донат:   `{fmt(row['sub_miners'])}`",
        f"",
        f"⚡ Скорость: `{fmt(int(total_miners * mult))} HASH/час`",
        f"📈 Множитель: *{multiplier_str(mult)}*",
        f"",
        f"🎫 Подписка: {sub_info(row)}",
        f"",
        f"{collect_info}",
        f"",
        f"📅 В игре с: `{time.strftime('%d.%m.%Y', time.localtime(row['registered_at']))}`",
    ])


async def handle_profile_reply(update: Update, ctx):
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    if (msg.text or "").strip().lower() != "профиль":
        return
    wait = check_cooldown(update.effective_user.id, "profile")
    if wait:
        await msg.reply_text(f"⏳ Подожди {wait} сек."); return
    tu = msg.reply_to_message.from_user
    if not tu:
        await msg.reply_text("❌ Не могу найти пользователя."); return
    register_user(tu.id, tu.username or "", tu.first_name or "")
    check_and_expire_sub(tu.id)
    row = get_user(tu.id)
    if not row:
        await msg.reply_text("❌ Игрок не найден."); return
    await msg.reply_text(build_profile_text(row), parse_mode="Markdown")


async def cmd_profile(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "profile")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    if ctx.args:
        row = get_user_by_username(ctx.args[0])
        if not row:
            await update.message.reply_text(f"❌ Игрок {ctx.args[0]} не найден."); return
    else:
        check_and_expire_sub(user.id)
        row = get_user(user.id)
        if not row:
            await update.message.reply_text("❌ Напиши /start"); return
    check_and_expire_sub(row["user_id"])
    row = get_user(row["user_id"])
    await update.message.reply_text(build_profile_text(row), parse_mode="Markdown")


# ── Сбор HASH ───────────────────────────────

async def handle_collect(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "collect")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    check_and_expire_sub(user.id)
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("❌ Напиши /start"); return
    elapsed = int(time.time()) - row["last_collect"]
    if elapsed < COLLECT_INTERVAL_SEC:
        left = COLLECT_INTERVAL_SEC - elapsed
        await update.message.reply_text(
            f"⏱ Рано! Следующий сбор через *{seconds_to_hms(left)}*",
            parse_mode="Markdown"
        )
        return
    mult   = get_multiplier(row)
    earned, half_rate = calculate_pending_hash(row, mult)
    if earned == 0:
        await update.message.reply_text("⚠️ Нечего собирать."); return
    if not collect_hash(user.id, earned):
        await update.message.reply_text("❌ Ошибка. Попробуй снова."); return
    total_miners = row["miners"] + row["sub_miners"]
    lines = [
        f"⛏️ *HASH собран!*", f"",
        f"💰 Получено: `+{fmt(earned)} HASH`",
        f"🖥️ Майнеров: `{fmt(total_miners)}`",
        f"📈 Множитель: `{multiplier_str(mult)}`",
        f"⏱ Прошло: `{seconds_to_hms(elapsed)}`",
    ]
    if half_rate:
        lines += [f"", f"⚠️ _После 1 часа без сбора скорость снижена вдвое._\n_Собирай каждый час для максимального дохода!_"]
    updated = get_user(user.id)
    lines += [f"", f"💼 Баланс HASH: `{fmt(updated['hash_balance'])}`"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Продажа HASH ────────────────────────────

async def handle_sell(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "sell")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    row  = get_user(user.id)
    if not row:
        await update.message.reply_text("❌ Напиши /start"); return
    parts = update.message.text.strip().split()
    if len(parts) >= 2 and parts[1].lower() in ("всё","все","all"):
        amount = row["hash_balance"]
    elif len(parts) >= 2:
        try:
            amount = int(parts[1].replace(",","").replace("_",""))
        except ValueError:
            await update.message.reply_text("❌ Формат: `продать 100` или `продать всё`", parse_mode="Markdown"); return
    else:
        await update.message.reply_text("❌ Укажи кол-во: `продать 100`", parse_mode="Markdown"); return
    if amount <= 0:
        await update.message.reply_text("❌ Нечего продавать."); return
    if amount > row["hash_balance"]:
        await update.message.reply_text(f"❌ У тебя только `{fmt(row['hash_balance'])} HASH`.", parse_mode="Markdown"); return
    if amount < HASH_TO_COIN_RATE:
        await update.message.reply_text(f"❌ Минимум `{HASH_TO_COIN_RATE} HASH`.", parse_mode="Markdown"); return
    ok, coins = sell_hash(user.id, amount)
    if not ok:
        await update.message.reply_text("❌ Ошибка. Попробуй снова."); return
    remainder = amount - coins * HASH_TO_COIN_RATE
    updated = get_user(user.id)
    lines = [
        f"💱 *Продажа HASH*", f"",
        f"📤 Продано: `{fmt(amount)} HASH`",
        f"🪙 Получено: `+{fmt(coins)} коинов`",
    ]
    if remainder:
        lines.append(f"📌 Остаток: `{remainder} HASH` (не кратно 10)")
    lines += [f"", f"💼 HASH: `{fmt(updated['hash_balance'])}`", f"🪙 Коины: `{fmt(updated['coins'])}`"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Покупка майнеров ────────────────────────

async def handle_buy_miners(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "buy")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    parts = update.message.text.strip().split()
    count = None
    for p in parts:
        try:
            count = int(p.replace(",","").replace("_",""))
            break
        except ValueError:
            continue
    if not count or count <= 0:
        await update.message.reply_text(
            f"❌ Укажи кол-во: `купить майнеры 100`\n\n"
            f"💰 Цены:\n• `{COIN_PER_MINER} коинов` за 1 майнер\n• `1 д-коин` за {MINERS_PER_DCOIN} майнеров",
            parse_mode="Markdown"
        ); return
    row = get_user(user.id)
    if not row:
        await update.message.reply_text("❌ Напиши /start"); return
    coin_cost  = count * COIN_PER_MINER
    dcoin_cost = (count + MINERS_PER_DCOIN - 1) // MINERS_PER_DCOIN
    if row["coins"] >= coin_cost:
        ok, err = buy_miners_coins(user.id, count)
        currency, spent = "коинов", coin_cost
    elif row["dcoins"] >= dcoin_cost:
        ok, err = buy_miners_dcoins(user.id, count)
        currency, spent = "д-коинов", dcoin_cost
    else:
        await update.message.reply_text(
            f"❌ Недостаточно средств!\n"
            f"• `{coin_cost:,} коинов` — у тебя `{fmt(row['coins'])}`\n"
            f"• `{dcoin_cost:,} д-коинов` — у тебя `{fmt(row['dcoins'])}`",
            parse_mode="Markdown"
        ); return
    if not ok:
        await update.message.reply_text(f"❌ {err}"); return
    updated = get_user(user.id)
    total   = updated["miners"] + updated["sub_miners"]
    await update.message.reply_text(
        f"✅ *Куплено {fmt(count)} майнеров!*\n\n"
        f"💸 Потрачено: `{fmt(spent)} {currency}`\n"
        f"🖥️ Всего майнеров: `{fmt(total)}`",
        parse_mode="Markdown"
    )


# ── Покупка HASH ────────────────────────────

async def handle_buy_hash(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "buy")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    parts = update.message.text.strip().split()
    count = None
    for p in parts:
        try:
            count = int(p.replace(",",""))
            break
        except ValueError:
            continue
    if not count or count <= 0:
        await update.message.reply_text(
            f"❌ Укажи кол-во д-коинов: `купить hash 10`\n1 д-коин = {DCOIN_HASH_RATE} HASH",
            parse_mode="Markdown"
        ); return
    ok, gained, err = buy_hash_dcoins(user.id, count)
    if not ok:
        await update.message.reply_text(f"❌ {err}"); return
    updated = get_user(user.id)
    await update.message.reply_text(
        f"✅ *Куплено {fmt(gained)} HASH!*\n\n"
        f"💸 Потрачено: `{count} д-коинов`\n"
        f"⛏️ HASH баланс: `{fmt(updated['hash_balance'])}`",
        parse_mode="Markdown"
    )


# ── Магазин ─────────────────────────────────

async def handle_shop(update: Update, ctx):
    lines = ["🛒 *Магазин HMiner*", ""]
    lines.append("━━━ 🎫 *Подписки* ━━━")
    for key, sub in SUBSCRIPTIONS.items():
        lines.append(
            f"\n{sub['emoji']} *{sub['name']}*\n"
            f"  ├ Множитель: ×{sub['multiplier']}\n"
            f"  ├ Бонус майнеров: +{fmt(sub['bonus_miners'])}\n"
            f"  ├ 💫 Stars: `{sub['stars']}` | 💎 Д-коины: `{sub['dcoins']}`\n"
            f"  └ 👉 `купить {key}` / `купить {key} дкоины`"
        )
    lines += [
        f"\n━━━ 📦 *Контейнер* ━━━",
        f"🎰 Случайная подписка | Fast 50% PRO 25% Ultra 15% Max 10%",
        f"  ├ 💫 `{CONTAINER_STARS} Stars` | 💎 `{CONTAINER_DCOINS} д-коинов`",
        f"  └ 👉 `купить контейнер`",
        f"\n━━━ 💎 *Д-коины* ━━━",
        f"10 д-коинов = 1 ⭐ | 1 д-коин = 10 майнеров | 1 д-коин = 100 HASH",
        f"  👉 `купить дкоины [кол-во]`",
        f"\n━━━ 🖥️ *Майнеры* ━━━",
        f"{COIN_PER_MINER} коинов = 1 майнер | 1 д-коин = {MINERS_PER_DCOIN} майнеров",
        f"  👉 `купить майнеры [кол-во]`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Перевод ─────────────────────────────────

async def handle_transfer(update: Update, ctx):
    user = update.effective_user
    wait = check_cooldown(user.id, "transfer")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    parts = update.message.text.strip().split()
    target_id = None
    amount    = None
    for p in parts[1:]:
        if p.startswith("@"):
            row = get_user_by_username(p)
            if row:
                target_id = row["user_id"]
        else:
            try:
                amount = int(p.replace(",","").replace("_",""))
            except ValueError:
                pass
    if not target_id and update.message.reply_to_message:
        tu = update.message.reply_to_message.from_user
        if tu:
            row = get_user(tu.id)
            if row:
                target_id = tu.id
    if not target_id:
        await update.message.reply_text(
            "❌ Формат: `перевести @username 100`\nили ответь на сообщение игрока: `перевести 100`",
            parse_mode="Markdown"
        ); return
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Укажи сумму: `перевести @username 100`", parse_mode="Markdown"); return
    ok, err = transfer_coins(user.id, target_id, amount)
    if not ok:
        await update.message.reply_text(f"❌ {err}"); return
    receiver = get_user(target_id)
    rname    = receiver["first_name"] if receiver else "Игрок"
    await update.message.reply_text(
        f"✅ *Перевод выполнен!*\n\n💸 `{fmt(amount)} коинов` → *{rname}*",
        parse_mode="Markdown"
    )
    try:
        await ctx.bot.send_message(
            target_id,
            f"💌 *Тебе перевели коины!*\n👤 От: *{user.first_name}*\n🪙 `+{fmt(amount)} коинов`",
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ── Топ ─────────────────────────────────────

MEDALS = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]


def build_top_text(rows, field_label, emoji):
    if not rows:
        return f"{emoji} Топ пока пуст."
    lines = [f"{emoji} *Топ по {field_label}:*\n"]
    for i, row in enumerate(rows):
        medal = MEDALS[i] if i < len(MEDALS) else f"{i+1}."
        name  = row.get("first_name") or "Игрок"
        uname = f" (@{row['username']})" if row.get("username") else ""
        lines.append(f"{medal} *{name}*{uname} — `{fmt(row['value'])}`")
    lines += ["", "🔄 _Обновляется раз в 5 минут_"]
    return "\n".join(lines)


async def handle_top_hash(update: Update, ctx):
    wait = check_cooldown(update.effective_user.id, "top")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    chat = update.effective_chat
    if chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        try:
            members = [m.user.id async for m in ctx.bot.get_chat_members(chat.id)]
        except Exception:
            members = None
        rows  = get_top("hash_balance", members)
        scope = "чата"
    else:
        rows  = get_top("hash_balance", None)
        scope = "всех игроков"
    await update.message.reply_text(build_top_text(rows, f"HASH ({scope})", "⛏️"), parse_mode="Markdown")


async def handle_top_coins(update: Update, ctx):
    wait = check_cooldown(update.effective_user.id, "top")
    if wait:
        await update.message.reply_text(f"⏳ Подожди {wait} сек."); return
    chat = update.effective_chat
    if chat.type in (Chat.GROUP, Chat.SUPERGROUP):
        try:
            members = [m.user.id async for m in ctx.bot.get_chat_members(chat.id)]
        except Exception:
            members = None
        rows  = get_top("coins", members)
        scope = "чата"
    else:
        rows  = get_top("coins", None)
        scope = "всех игроков"
    await update.message.reply_text(build_top_text(rows, f"коинам ({scope})", "🪙"), parse_mode="Markdown")


# ── Платежи ─────────────────────────────────

def _weighted_random_sub():
    keys    = list(CONTAINER_WEIGHTS.keys())
    weights = [CONTAINER_WEIGHTS[k] for k in keys]
    return random.choices(keys, weights=weights, k=1)[0]


async def _send_sub_invoice(update, ctx, sub_key):
    sub    = SUBSCRIPTIONS[sub_key]
    user   = update.effective_user
    pay_id = create_payment(user.id, f"sub_{sub_key}", sub["stars"])
    await ctx.bot.send_invoice(
        chat_id=user.id,
        title=f"{sub['emoji']} {sub['name']}",
        description=f"Подписка 30 дней | ×{sub['multiplier']} | +{fmt(sub['bonus_miners'])} майнеров",
        payload=f"sub_{sub_key}:{pay_id}",
        currency="XTR",
        prices=[LabeledPrice(label=sub["name"], amount=sub["stars"])],
    )


async def _buy_sub_dcoins(update, ctx, sub_key):
    sub  = SUBSCRIPTIONS[sub_key]
    cost = sub["dcoins"]
    ok, err = spend_dcoins(update.effective_user.id, cost)
    if not ok:
        await update.message.reply_text(
            f"❌ {err}\n\n💫 Или купи за `{sub['stars']} Stars`: `купить {sub_key}`",
            parse_mode="Markdown"
        ); return
    apply_subscription(update.effective_user.id, sub_key)
    await update.message.reply_text(
        f"{sub['emoji']} *{sub['name']} активирована!*\n\n"
        f"💸 Потрачено: `{cost} д-коинов`\n"
        f"📈 Множитель: `×{sub['multiplier']}`\n"
        f"🖥️ Майнеров: `+{fmt(sub['bonus_miners'])}`\n⏱ Срок: 30 дней",
        parse_mode="Markdown"
    )


async def handle_buy_dcoins(update: Update, ctx):
    parts  = update.message.text.strip().split()
    amount = None
    for p in parts:
        try:
            amount = int(p.replace(",",""))
            break
        except ValueError:
            pass
    if not amount or amount <= 0 or amount % DCOIN_PER_STAR != 0:
        await update.message.reply_text(
            f"❌ Укажи кол-во д-коинов (кратно {DCOIN_PER_STAR}):\n`купить дкоины 100` = 10 Stars",
            parse_mode="Markdown"
        ); return
    stars  = amount // DCOIN_PER_STAR
    user   = update.effective_user
    pay_id = create_payment(user.id, f"dcoins_{amount}", stars)
    await ctx.bot.send_invoice(
        chat_id=user.id,
        title=f"💎 {amount} Д-коинов",
        description=f"Покупка {amount} д-коинов ({DCOIN_PER_STAR} д-коинов = 1 Star)",
        payload=f"dcoins:{amount}:{pay_id}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} д-коинов", amount=stars)],
    )


async def pre_checkout(update: Update, ctx):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, ctx):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    tg_id   = payment.telegram_payment_charge_id
    user    = update.effective_user
    parts   = payload.split(":")
    if parts[0].startswith("sub_") and len(parts) == 2:
        sub_key = parts[0][4:]
        confirm_payment(int(parts[1]), tg_id)
        apply_subscription(user.id, sub_key)
        sub = SUBSCRIPTIONS[sub_key]
        await update.message.reply_text(
            f"{sub['emoji']} *{sub['name']} активирована!*\n\n"
            f"📈 Множитель: `×{sub['multiplier']}`\n"
            f"🖥️ Майнеров: `+{fmt(sub['bonus_miners'])}`\n"
            f"⏱ 30 дней\n\nСпасибо! 💫",
            parse_mode="Markdown"
        )
    elif parts[0] == "container" and len(parts) == 2:
        confirm_payment(int(parts[1]), tg_id)
        won = _weighted_random_sub()
        sub = SUBSCRIPTIONS[won]
        apply_subscription(user.id, won)
        await update.message.reply_text(
            f"📦 *Контейнер открыт!*\n\n🎉 {sub['emoji']} *{sub['name']}*\n"
            f"📈 ×{sub['multiplier']} | 🖥️ +{fmt(sub['bonus_miners'])} майнеров\n\nСпасибо! 💫",
            parse_mode="Markdown"
        )
    elif parts[0] == "dcoins" and len(parts) == 3:
        amount = int(parts[1])
        confirm_payment(int(parts[2]), tg_id)
        add_dcoins(user.id, amount)
        updated = get_user(user.id)
        await update.message.reply_text(
            f"💎 *+{fmt(amount)} д-коинов!*\n💼 Баланс: `{fmt(updated['dcoins'])}`\n\nСпасибо! 💫",
            parse_mode="Markdown"
        )


# ── Админ ───────────────────────────────────

async def cmd_wipe(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа."); return
    if not ctx.args or ctx.args[0] != "CONFIRM":
        await update.message.reply_text(
            "⚠️ Вайп сбросит HASH, коины и обычные майнеры всем.\n"
            "Д-коины и подписки сохраняются.\n\nПодтверди: `/вайп CONFIRM`",
            parse_mode="Markdown"
        ); return
    count = wipe_all()
    await update.message.reply_text(f"🗑️ *Вайп выполнен!* Затронуто: `{count}` игроков.", parse_mode="Markdown")
    with get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM users").fetchall()
    for row in rows:
        try:
            await ctx.bot.send_message(
                row["user_id"],
                "🔄 *Ежемесячный вайп!*\nHASH, коины и обычные майнеры сброшены.\n"
                "Д-коины, подписки и донат-майнеры сохранены. Удачи! ⛏️",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def cmd_stats(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа."); return
    with get_conn() as conn:
        tu   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        th   = conn.execute("SELECT SUM(hash_balance) FROM users").fetchone()[0] or 0
        tc   = conn.execute("SELECT SUM(coins) FROM users").fetchone()[0] or 0
        td   = conn.execute("SELECT SUM(dcoins) FROM users").fetchone()[0] or 0
        tm   = conn.execute("SELECT SUM(miners+sub_miners) FROM users").fetchone()[0] or 0
        sc   = conn.execute("SELECT COUNT(*) FROM users WHERE sub_type!='' AND sub_expires>strftime('%s','now')").fetchone()[0]
        pp   = conn.execute("SELECT COUNT(*),SUM(stars) FROM payments WHERE status='done'").fetchone()
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"👥 Игроков: `{fmt(tu)}` | 🎫 С подпиской: `{fmt(sc)}`\n"
        f"⛏️ HASH: `{fmt(th)}` | 🪙 Коины: `{fmt(tc)}`\n"
        f"💎 Д-коины: `{fmt(td)}` | 🖥️ Майнеры: `{fmt(tm)}`\n"
        f"💫 Платежей: `{pp[0]}` на `{fmt(pp[1] or 0)} Stars`",
        parse_mode="Markdown"
    )


async def cmd_give(update: Update, ctx):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Нет доступа."); return
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text("Формат: `/дать @username hash 1000`", parse_mode="Markdown"); return
    row = get_user_by_username(ctx.args[0])
    if not row:
        await update.message.reply_text(f"❌ Игрок {ctx.args[0]} не найден."); return
    field_map = {"hash":"hash_balance","coins":"coins","dcoins":"dcoins","miners":"miners"}
    res = ctx.args[1].lower()
    if res not in field_map:
        await update.message.reply_text("❌ Тип: hash / coins / dcoins / miners"); return
    try:
        amount = int(ctx.args[2])
    except ValueError:
        await update.message.reply_text("❌ Неверная сумма."); return
    db_field = field_map[res]
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {db_field}={db_field}+? WHERE user_id=?", (amount, row["user_id"]))
    await update.message.reply_text(f"✅ Выдано `{fmt(amount)} {res}` → {ctx.args[0]}", parse_mode="Markdown")


# ════════════════════════════════════════════
#  РОУТЕР ТЕКСТОВЫХ СООБЩЕНИЙ
# ════════════════════════════════════════════

async def text_router(update: Update, ctx):
    if not update.message or not update.message.text:
        return
    user = update.effective_user
    register_user(user.id, user.username or "", user.first_name or "")
    text = update.message.text.strip()
    low  = text.lower()

    if low == "профиль":
        await handle_profile_reply(update, ctx); return
    if low in ("собрать","collect","⛏️ собрать"):
        await handle_collect(update, ctx); return
    if low.startswith("продать") or low.startswith("sell"):
        await handle_sell(update, ctx); return
    if ("купить" in low or "buy" in low) and "майнер" in low:
        await handle_buy_miners(update, ctx); return
    if ("купить" in low or "buy" in low) and "hash" in low:
        await handle_buy_hash(update, ctx); return
    if low.startswith("перевести") or low.startswith("transfer"):
        await handle_transfer(update, ctx); return
    if low in ("топ hash","топ хэш","top hash"):
        await handle_top_hash(update, ctx); return
    if low in ("топ коины","топ коин","top coins"):
        await handle_top_coins(update, ctx); return
    if low in ("магазин","shop","🛒 магазин"):
        await handle_shop(update, ctx); return

    # Подписки за Stars / д-коины
    for key in ("fast","pro","ultra","max"):
        if low in (f"купить {key}", f"купить {key} stars"):
            await _send_sub_invoice(update, ctx, key); return
        if low in (f"купить {key} дкоины", f"купить {key} д-коины"):
            await _buy_sub_dcoins(update, ctx, key); return

    # Контейнер
    if low in ("купить контейнер","купить контейнер stars"):
        pay_id = create_payment(user.id, "container", CONTAINER_STARS)
        await ctx.bot.send_invoice(
            chat_id=user.id,
            title="📦 Контейнер с подпиской",
            description="Случайная подписка 30 дней | Fast 50% PRO 25% Ultra 15% Max 10%",
            payload=f"container:{pay_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Контейнер", amount=CONTAINER_STARS)],
        ); return
    if low in ("купить контейнер дкоины","купить контейнер д-коины"):
        ok, err = spend_dcoins(user.id, CONTAINER_DCOINS)
        if not ok:
            await update.message.reply_text(f"❌ {err}"); return
        won = _weighted_random_sub()
        sub = SUBSCRIPTIONS[won]
        apply_subscription(user.id, won)
        await update.message.reply_text(
            f"📦 *Контейнер открыт!*\n\n🎉 {sub['emoji']} *{sub['name']}*\n"
            f"📈 ×{sub['multiplier']} | 🖥️ +{fmt(sub['bonus_miners'])} майнеров",
            parse_mode="Markdown"
        ); return

    # Д-коины за Stars
    if ("купить" in low or "buy" in low) and ("дкоин" in low or "д-коин" in low or "dcoins" in low):
        await handle_buy_dcoins(update, ctx); return

    # Русские варианты slash-команд (Telegram не принимает кириллицу в CommandHandler)
    if low.startswith("/профиль") or low.startswith("/profile"):
        parts = text.split()
        ctx.args = parts[1:] if len(parts) > 1 else []
        await cmd_profile(update, ctx); return
    if low.startswith("/вайп") or low.startswith("/wipe"):
        parts = text.split()
        ctx.args = parts[1:] if len(parts) > 1 else []
        await cmd_wipe(update, ctx); return
    if low.startswith("/дать") or low.startswith("/give"):
        parts = text.split()
        ctx.args = parts[1:] if len(parts) > 1 else []
        await cmd_give(update, ctx); return
    if low.startswith("/stats") or low.startswith("/стат"):
        await cmd_stats(update, ctx); return


# ════════════════════════════════════════════
#  ЗАПУСК
# ════════════════════════════════════════════

flask_app = Flask(__name__)

@flask_app.route("/")
def health():
    return {"status": "ok", "bot": "HMiner"}


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    init_db()
    logger.info("БД инициализирована.")
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"Flask запущен на порту {PORT}")
    app = Application.builder().token(BOT_TOKEN).build()
    # Только латинские команды — Telegram не принимает кириллицу в CommandHandler
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("wipe",    cmd_wipe))
    app.add_handler(CommandHandler("give",    cmd_give))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    # Все текстовые сообщения (включая русские команды) — через роутер
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    logger.info("HMiner Bot запущен! 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
