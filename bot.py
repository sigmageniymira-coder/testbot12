import os
import time
import sqlite3
import asyncio
import threading

from flask import Flask, jsonify

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

# ==================================

# CONFIG

# ==================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
raise Exception("BOT_TOKEN not found")

DB_NAME = "hminer.db"

START_MINERS = 10
HASH_PER_MINER = 5
HASH_TO_COIN = 10

# ==================================

# FLASK FOR RENDER

# ==================================

app = Flask(**name**)

@app.route("/")
def home():
return jsonify({
"status": "ok",
"bot": "HMiner"
})

def run_web():
app.run(
host="0.0.0.0",
port=int(os.getenv("PORT", 10000))
)

# ==================================

# DATABASE

# ==================================

def get_conn():
conn = sqlite3.connect(
DB_NAME,
check_same_thread=False
)

```
conn.row_factory = sqlite3.Row

conn.execute(
    "PRAGMA journal_mode=WAL"
)

return conn
```

def init_db():

```
conn = get_conn()
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY,

    username TEXT,
    first_name TEXT,

    hash INTEGER DEFAULT 0,
    coins INTEGER DEFAULT 0,
    dcoins INTEGER DEFAULT 0,

    miners INTEGER DEFAULT 10,

    last_collect INTEGER DEFAULT 0,

    registered_at INTEGER,

    sub_type TEXT,
    sub_expires INTEGER DEFAULT 0,
    sub_miners INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS transactions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    from_id INTEGER,
    to_id INTEGER,

    amount INTEGER,
    type TEXT,

    created_at INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS payments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER,

    telegram_payment_id TEXT,

    product TEXT,

    stars INTEGER,

    status TEXT,

    created_at INTEGER
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS slot_logs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    user_id INTEGER,

    bet INTEGER,
    result TEXT,
    win INTEGER,

    created_at INTEGER
)
""")

conn.commit()
conn.close()
```

# ==================================

# USERS

# ==================================

def register_user(user):

```
conn = get_conn()

conn.execute(
    """
    INSERT OR IGNORE INTO users(
        user_id,
        username,
        first_name,
        registered_at,
        last_collect,
        miners
    )
    VALUES(?,?,?,?,?,?)
    """,
    (
        user.id,
        user.username,
        user.first_name,
        int(time.time()),
        int(time.time()),
        START_MINERS
    )
)

conn.commit()
conn.close()
```

def get_user(user_id):

```
conn = get_conn()

user = conn.execute(
    """
    SELECT *
    FROM users
    WHERE user_id = ?
    """,
    (user_id,)
).fetchone()

conn.close()

return user
```

def get_multiplier(user_row):

```
now = int(time.time())

if not user_row["sub_type"]:
    return 1.0

if user_row["sub_expires"] <= now:
    return 1.0

multipliers = {
    "fast": 1.5,
    "pro": 2.0,
    "ultra": 2.5,
    "max": 3.0
}

return multipliers.get(
    user_row["sub_type"],
    1.0
)
```

# ==================================

# BOT

# ==================================

router = Router()

bot = Bot(
token=BOT_TOKEN,
default=DefaultBotProperties(
parse_mode=ParseMode.HTML
)
)

dp = Dispatcher()

@router.message(Command("start"))
async def start_cmd(message: Message):

```
register_user(message.from_user)

await message.answer(
    "⛏ Добро пожаловать в HMiner!\n\n"
    "Стартовые майнеры: 10"
)
```

@router.message(
F.text.lower().in_(
[
"профиль",
"/profile"
]
)
)
async def profile_cmd(message: Message):

```
register_user(message.from_user)

user = get_user(
    message.from_user.id
)

multiplier = get_multiplier(user)

sub_text = "Нет"

if (
    user["sub_type"]
    and
    user["sub_expires"] > int(time.time())
):
    sub_text = user["sub_type"].upper()

await message.answer(
    f"👤 {user['first_name']}\n\n"
    f"🧱 HASH: {user['hash']}\n"
    f"🪙 Коины: {user['coins']}\n"
    f"💎 Д-коины: {user['dcoins']}\n\n"
    f"⛏ Майнеры: {user['miners'] + user['sub_miners']}\n"
    f"⚡ Множитель: x{multiplier}\n"
    f"🏷 Подписка: {sub_text}"
)
```

dp.include_router(router)

# ==================================

# START

# ==================================

async def main():

```
init_db()

threading.Thread(
    target=run_web,
    daemon=True
).start()

await dp.start_polling(bot)
```

if **name** == "**main**":
asyncio.run(main())
# ==================================

# MINING

# ==================================

MAX_COLLECT_HOURS = 12

def add_hash(user_id, amount):

```
conn = get_conn()

conn.execute(
    """
    UPDATE users
    SET hash = hash + ?
    WHERE user_id = ?
    """,
    (
        amount,
        user_id
    )
)

conn.commit()
conn.close()
```

def sell_hash_amount(user_id, hash_amount):

```
conn = get_conn()

user = conn.execute(
    """
    SELECT hash, coins
    FROM users
    WHERE user_id = ?
    """,
    (user_id,)
).fetchone()

if not user:
    conn.close()
    return False, "Игрок не найден"

if user["hash"] < hash_amount:
    conn.close()
    return False, "Недостаточно HASH"

coins = hash_amount // HASH_TO_COIN

if coins <= 0:
    conn.close()
    return False, "Минимум 10 HASH"

used_hash = coins * HASH_TO_COIN

conn.execute(
    """
    UPDATE users
    SET
        hash = hash - ?,
        coins = coins + ?
    WHERE user_id = ?
    """,
    (
        used_hash,
        coins,
        user_id
    )
)

conn.commit()
conn.close()

return True, (
    used_hash,
    coins
)
```

def calculate_hash(user):

```
now = int(time.time())

seconds = (
    now -
    user["last_collect"]
)

hours = seconds / 3600

if hours < 1:
    return None

if hours > MAX_COLLECT_HOURS:
    hours = MAX_COLLECT_HOURS

miners = (
    user["miners"] +
    user["sub_miners"]
)

multiplier = get_multiplier(user)

first_hour = (
    miners *
    HASH_PER_MINER *
    multiplier
)

extra_hours = max(
    0,
    hours - 1
)

extra_hash = (
    extra_hours *
    miners *
    HASH_PER_MINER *
    0.5 *
    multiplier
)

total_hash = int(
    first_hour +
    extra_hash
)

return total_hash
```

def collect_hash(user_id):

```
conn = get_conn()

user = conn.execute(
    """
    SELECT *
    FROM users
    WHERE user_id = ?
    """,
    (user_id,)
).fetchone()

if not user:
    conn.close()
    return False, "Игрок не найден"

amount = calculate_hash(user)

if amount is None:

    remain = (
        3600 -
        (
            int(time.time())
            -
            user["last_collect"]
        )
    )

    mins = remain // 60

    conn.close()

    return (
        False,
        f"⏳ Приходи через {mins} мин."
    )

conn.execute(
    """
    UPDATE users
    SET
        hash = hash + ?,
        last_collect = ?
    WHERE user_id = ?
    """,
    (
        amount,
        int(time.time()),
        user_id
    )
)

conn.commit()
conn.close()

return True, amount
```
@router.message(
F.text.lower().in_(
[
"собрать",
"/collect"
]
)
)
async def collect_cmd(message: Message):

```
register_user(
    message.from_user
)

result = collect_hash(
    message.from_user.id
)

if result[0] is False:
    await message.answer(
        result[1]
    )
    return

await message.answer(
    f"⛏ Собрано: {result[1]} HASH"
)
```

@router.message(
F.text.lower().startswith(
"продать "
)
)
async def sell_cmd(message: Message):

```
register_user(
    message.from_user
)

args = (
    message.text
    .lower()
    .split()
)

if len(args) < 2:
    return

user = get_user(
    message.from_user.id
)

if args[1] == "всё":

    amount = user["hash"]

else:

    if not args[1].isdigit():
        await message.answer(
            "Укажи число."
        )
        return

    amount = int(args[1])

result = sell_hash_amount(
    message.from_user.id,
    amount
)

if result[0] is False:
    await message.answer(
        result[1]
    )
    return

used_hash, coins = result[1]

await message.answer(
    f"💰 Продано {used_hash} HASH\n"
    f"🪙 Получено {coins} коинов"
)
```
