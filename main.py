# =============================================================
#  CodiFather — Telegram bot (aiogram 3 + SQLite)
#  Всё в одном файле. Запуск: python main.py
# =============================================================
import asyncio
import logging
import os
import aiosqlite
import aiohttp
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
)

# =============================================================
#  КОНФИГ
# =============================================================
MAIN_BOT_TOKEN = "9100032188:AAEH-fVC5etqkk-nApd1WLz6K1ovNUR1ZFc"   # ← токен CodiFather от @BotFather
ADMIN_IDS = {6539341659}                            # ← твой ID

DB_PATH = "data/codifather.db"
PIARFLOW_BASE = "https://piarflow.com/v1"

DEFAULT_MAX_BOTS = 3
DEFAULT_MAX_BOTS_PRO = 5
PRO_DAYS = 30

os.makedirs("data", exist_ok=True)
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("CodiFather")

# =============================================================
#  СХЕМА БД
# =============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    full_name   TEXT,
    has_access  INTEGER DEFAULT 0,
    pro_until   TEXT,
    referrer_id INTEGER,
    balance     REAL DEFAULT 0,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS child_bots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     INTEGER NOT NULL,
    bot_token    TEXT NOT NULL,
    bot_username TEXT,
    piarflow_key TEXT NOT NULL,
    reward       REAL DEFAULT 1,
    ref_enabled  INTEGER DEFAULT 1,
    ref_reward   REAL DEFAULT 0.5,
    top_enabled  INTEGER DEFAULT 1,
    min_withdraw REAL DEFAULT 10,
    is_active    INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS child_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    full_name   TEXT,
    balance     REAL DEFAULT 0,
    referrer_id INTEGER,
    subscribed  INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bot_id, user_id)
);
CREATE TABLE IF NOT EXISTS completions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    link       TEXT,
    reward     REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS withdraws (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT,
    full_name  TEXT,
    amount     REAL NOT NULL,
    status     TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS processed_tasks (
    bot_id     INTEGER,
    user_id    INTEGER,
    link       TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(bot_id, user_id, link)
);
"""

# =============================================================
#  DB — ИНИЦИАЛИЗАЦИЯ
# =============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.execute("INSERT OR IGNORE INTO settings VALUES ('access_price','40')")
        await db.commit()

# ---------- SETTINGS ----------
async def get_setting(key, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

async def set_setting(key, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))
        await db.commit()

# ---------- USERS ----------
async def get_user(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        return await cur.fetchone()

async def upsert_user(user_id, username, full_name, referrer_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, username, full_name, referrer_id) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
            (user_id, username, full_name, referrer_id),
        )
        await db.commit()

async def grant_access(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET has_access=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def grant_pro(user_id, days=30):
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET pro_until=?, has_access=1 WHERE user_id=?", (until, user_id))
        await db.commit()

async def is_pro(user_id):
    u = await get_user(user_id)
    if not u or not u["pro_until"]:
        return False
    try:
        return datetime.fromisoformat(u["pro_until"]) > datetime.utcnow()
    except ValueError:
        return False

async def add_balance(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        await db.commit()

# ---------- CHILD BOTS ----------
async def add_child_bot(owner_id, token, username, piar_key):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO child_bots(owner_id, bot_token, bot_username, piarflow_key) VALUES (?,?,?,?)",
            (owner_id, token, username, piar_key),
        )
        await db.commit()
        return cur.lastrowid

async def get_child_bots_by_owner(owner_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM child_bots WHERE owner_id=?", (owner_id,))
        return await cur.fetchall()

async def get_child_bot(bot_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM child_bots WHERE id=?", (bot_id,))
        return await cur.fetchone()

async def update_child_setting(bot_id, field, value):
    allowed = {"reward", "ref_enabled", "ref_reward", "top_enabled", "min_withdraw", "is_active"}
    if field not in allowed:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE child_bots SET {field}=? WHERE id=?", (value, bot_id))
        await db.commit()

# ---------- CHILD USERS ----------
async def get_child_user(bot_id, user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM child_users WHERE bot_id=? AND user_id=?", (bot_id, user_id))
        return await cur.fetchone()

async def upsert_child_user(bot_id, user_id, username, full_name, referrer_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO child_users(bot_id,user_id,username,full_name,referrer_id) VALUES (?,?,?,?,?) "
            "ON CONFLICT(bot_id,user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
            (bot_id, user_id, username, full_name, referrer_id),
        )
        await db.commit()

async def add_child_balance(bot_id, user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE child_users SET balance=balance+? WHERE bot_id=? AND user_id=?",
                         (amount, bot_id, user_id))
        await db.commit()

async def save_completion(bot_id, user_id, link, reward):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO processed_tasks(bot_id,user_id,link) VALUES (?,?,?)",
                             (bot_id, user_id, link))
            await db.execute("INSERT INTO completions(bot_id,user_id,link,reward) VALUES (?,?,?,?)",
                             (bot_id, user_id, link, reward))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def get_top_earners(bot_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT user_id, username, full_name, balance FROM child_users "
            "WHERE bot_id=? ORDER BY balance DESC LIMIT ?", (bot_id, limit))
        return await cur.fetchall()

# ---------- WITHDRAWS ----------
async def create_withdraw(bot_id, user_id, username, full_name, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO withdraws(bot_id,user_id,username,full_name,amount) VALUES (?,?,?,?,?)",
            (bot_id, user_id, username, full_name, amount))
        await db.commit()
        return cur.lastrowid

async def get_withdraw(wid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM withdraws WHERE id=?", (wid,))
        return await cur.fetchone()

async def set_withdraw_status(wid, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE withdraws SET status=? WHERE id=?", (status, wid))
        await db.commit()

# =============================================================
#  PIARFLOW API
# =============================================================
async def pf_get_sponsors(api_key, user_id, chat_id, max_sponsors=5):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{PIARFLOW_BASE}/sponsors",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"user_id": user_id, "chat_id": chat_id, "max_sponsors": max_sponsors},
            timeout=15,
        ) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(data.get("message", f"PiarFlow {r.status}"))
            return data.get("sponsors", [])

async def pf_check_sponsors(api_key, user_id, links):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{PIARFLOW_BASE}/sponsors/check",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"user_id": user_id, "links": links},
            timeout=15,
        ) as r:
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise RuntimeError(data.get("message", f"PiarFlow {r.status}"))
            return data.get("sponsors", [])

def fmt_money(x):
    return f"{x:.2f}".rstrip("0").rstrip(".")

# =============================================================
#  БОТЫ
# =============================================================
main_bot = Bot(MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()

child_bots = {}  # bot_db_id -> (Bot, Dispatcher)

# =============================================================
#  ДОЧЕРНИЙ БОТ
# =============================================================
def make_child_dispatcher(bot_db_id):
    cdp = Dispatcher(storage=MemoryStorage())
    r = Router()

    class WState(StatesGroup):
        amount = State()

    def child_main_kb():
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Задания", callback_data="c_tasks")],
            [InlineKeyboardButton(text="👥 Рефералы", callback_data="c_ref"),
             InlineKeyboardButton(text="🏆 Топ", callback_data="c_top")],
            [InlineKeyboardButton(text="💰 Баланс", callback_data="c_balance"),
             InlineKeyboardButton(text="💸 Вывод", callback_data="c_withdraw")],
        ])

    @r.message(CommandStart())
    async def child_start(msg: Message):
        args = msg.text.split(maxsplit=1)
        ref_id = None
        if len(args) > 1 and args[1].startswith("ref"):
            try:
                ref_id = int(args[1][3:])
            except ValueError:
                ref_id = None

        bot_row = await get_child_bot(bot_db_id)
        if not bot_row or not bot_row["is_active"]:
            await msg.answer("Этот бот отключён владельцем.")
            return

        existing = await get_child_user(bot_db_id, msg.from_user.id)
        if not existing and ref_id and ref_id != msg.from_user.id:
            await upsert_child_user(bot_db_id, msg.from_user.id,
                                    msg.from_user.username or "", msg.from_user.full_name, ref_id)
        else:
            await upsert_child_user(bot_db_id, msg.from_user.id,
                                    msg.from_user.username or "", msg.from_user.full_name)

        await msg.answer(
            "👋 Добро пожаловать!\n\n"
            "Нажми <b>Задания</b>, чтобы заработать GMP.",
            reply_markup=child_main_kb(),
        )

    @r.callback_query(F.data == "c_tasks")
    async def c_tasks(cb: CallbackQuery):
        bot_row = await get_child_bot(bot_db_id)
        try:
            sponsors = await pf_get_sponsors(bot_row["piarflow_key"], cb.from_user.id, cb.message.chat.id)
        except Exception as e:
            log.exception(e)
            await cb.answer("Ошибка PiarFlow", show_alert=True)
            return
        if not sponsors:
            await cb.message.answer("Сейчас нет доступных заданий.")
            await cb.answer()
            return
        kb = [[InlineKeyboardButton(text=f"Подписаться (+{fmt_money(bot_row['reward'])})", url=sp["link"])]
              for sp in sponsors]
        kb.append([InlineKeyboardButton(text="✅ Проверить", callback_data="c_check")])
        await cb.message.answer("Выполни задания, затем нажми Проверить:",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
        await cb.answer()

    @r.callback_query(F.data == "c_check")
    async def c_check(cb: CallbackQuery):
        bot_row = await get_child_bot(bot_db_id)
        try:
            sponsors = await pf_get_sponsors(bot_row["piarflow_key"], cb.from_user.id, cb.message.chat.id)
            links = [s["link"] for s in sponsors]
            result = await pf_check_sponsors(bot_row["piarflow_key"], cb.from_user.id, links)
        except Exception as e:
            log.exception(e)
            await cb.answer("Ошибка проверки", show_alert=True)
            return

        total = 0.0
        done = 0
        for item in result:
            if item.get("status") == "subscribed":
                ok = await save_completion(bot_db_id, cb.from_user.id, item["link"], bot_row["reward"])
                if ok:
                    total += bot_row["reward"]
                    done += 1
        if total > 0:
            await add_child_balance(bot_db_id, cb.from_user.id, total)
            if bot_row["ref_enabled"]:
                cu = await get_child_user(bot_db_id, cb.from_user.id)
                if cu and cu["referrer_id"]:
                    await add_child_balance(bot_db_id, cu["referrer_id"], bot_row["ref_reward"])
            await cb.message.answer(f"✅ Засчитано заданий: {done}\n💰 Начислено: +{fmt_money(total)} GMP")
        else:
            await cb.message.answer("❌ Пока нет выполненных заданий (или уже засчитаны).")
        await cb.answer()

    @r.callback_query(F.data == "c_balance")
    async def c_balance(cb: CallbackQuery):
        cu = await get_child_user(bot_db_id, cb.from_user.id)
        await cb.message.answer(f"💰 Твой баланс: <b>{fmt_money(cu['balance'] if cu else 0)}</b> GMP")
        await cb.answer()

    @r.callback_query(F.data == "c_withdraw")
    async def c_withdraw(cb: CallbackQuery, state: FSMContext):
        bot_row = await get_child_bot(bot_db_id)
        cu = await get_child_user(bot_db_id, cb.from_user.id)
        if not cu or cu["balance"] < bot_row["min_withdraw"]:
            await cb.answer(f"Минимум {fmt_money(bot_row['min_withdraw'])} GMP", show_alert=True)
            return
        await state.set_state(WState.amount)
        await cb.message.answer("Введи сумму вывода:")
        await cb.answer()

    @r.message(WState.amount)
    async def c_withdraw_amount(msg: Message, state: FSMContext):
        bot_row = await get_child_bot(bot_db_id)
        cu = await get_child_user(bot_db_id, msg.from_user.id)
        try:
            amt = float(msg.text.replace(",", "."))
        except ValueError:
            await msg.answer("Введи число.")
            return
        if amt < bot_row["min_withdraw"] or amt > (cu["balance"] if cu else 0):
            await msg.answer("Сумма некорректна.")
            return
        wid = await create_withdraw(bot_db_id, msg.from_user.id,
                                    msg.from_user.username or "", msg.from_user.full_name, amt)
        text = (
            f"💸 <b>Заявка на вывод #{wid}</b>\n"
            f"👤 {msg.from_user.full_name}\n"
            f"🔗 @{msg.from_user.username or '-'}\n"
            f"🆔 <code>{msg.from_user.id}</code>\n"
            f"💵 Сумма: <b>{fmt_money(amt)} GMP</b>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"w_ok:{wid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"w_no:{wid}"),
        ]])
        try:
            await main_bot.send_message(bot_row["owner_id"], text, reply_markup=kb)
        except Exception as e:
            log.warning(f"Не смог отправить владельцу: {e}")
        await msg.answer("✅ Заявка отправлена владельцу.")
        await state.clear()

    @r.callback_query(F.data == "c_ref")
    async def c_ref(cb: CallbackQuery):
        bot_row = await get_child_bot(bot_db_id)
        if not bot_row["ref_enabled"]:
            await cb.answer("Реферальная система отключена", show_alert=True)
            return
        me = await cb.bot.get_me()
        link = f"https://t.me/{me.username}?start=ref{cb.from_user.id}"
        await cb.message.answer(
            f"👥 Твоя ссылка:\n<code>{link}</code>\n\n"
            f"За каждого приглашённого ты получаешь <b>{fmt_money(bot_row['ref_reward'])} GMP</b>."
        )
        await cb.answer()

    @r.callback_query(F.data == "c_top")
    async def c_top(cb: CallbackQuery):
        bot_row = await get_child_bot(bot_db_id)
        if not bot_row["top_enabled"]:
            await cb.answer("Топ отключён", show_alert=True)
            return
        top = await get_top_earners(bot_db_id, 10)
        text = "🏆 <b>Топ по балансу</b>\n\n"
        for i, u in enumerate(top, 1):
            name = u["username"] or u["full_name"] or str(u["user_id"])
            text += f"{i}. {name} — {fmt_money(u['balance'])} GMP\n"
        await cb.message.answer(text or "Пусто")
        await cb.answer()

    # одобрение/отклонение вывода
    @r.callback_query(F.data.startswith("w_ok:"))
    async def w_ok(cb: CallbackQuery):
        wid = int(cb.data.split(":")[1])
        w = await get_withdraw(wid)
        if not w or w["status"] != "pending":
            await cb.answer("Уже обработано", show_alert=True)
            return
        await add_child_balance(bot_db_id, w["user_id"], -w["amount"])
        await set_withdraw_status(wid, "approved")
        await cb.message.edit_text(cb.message.html_text + "\n\n✅ <b>Одобрено</b>")
        await cb.answer("Одобрено")

    @r.callback_query(F.data.startswith("w_no:"))
    async def w_no(cb: CallbackQuery):
        wid = int(cb.data.split(":")[1])
        w = await get_withdraw(wid)
        if not w or w["status"] != "pending":
            await cb.answer("Уже обработано", show_alert=True)
            return
        await set_withdraw_status(wid, "rejected")
        await cb.message.edit_text(cb.message.html_text + "\n\n❌ <b>Отклонено</b>")
        await cb.answer("Отклонено")

    cdp.include_router(r)
    return cdp

async def start_child_bot(bot_db_id):
    row = await get_child_bot(bot_db_id)
    if not row:
        return
    b = Bot(row["bot_token"], default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    d = make_child_dispatcher(bot_db_id)
    child_bots[bot_db_id] = (b, d)
    asyncio.create_task(d.start_polling(b))
    log.info(f"Дочерний бот #{bot_db_id} запущен")

async def start_all_child_bots():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT id FROM child_bots WHERE is_active=1")
        rows = await cur.fetchall()
    for row in rows:
        try:
            await start_child_bot(row["id"])
        except Exception as e:
            log.exception(f"Не запустился бот {row['id']}: {e}")

# =============================================================
#  ГЛАВНЫЙ БОТ — FSM
# =============================================================
class AddBot(StatesGroup):
    token = State()
    piarflow = State()

class AdminState(StatesGroup):
    price = State()
    grant_access = State()
    grant_pro = State()

class CfgState(StatesGroup):
    reward = State()
    ref_reward = State()
    min_withdraw = State()

def main_menu_kb(user_id):
    rows = [
        [InlineKeyboardButton(text="🤖 Мои боты", callback_data="my_bots")],
        [InlineKeyboardButton(text="➕ Создать бота", callback_data="new_bot")],
        [InlineKeyboardButton(text="💰 Баланс", callback_data="my_balance")],
    ]
    if user_id in ADMIN_IDS:
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Цена доступа", callback_data="adm_price")],
        [InlineKeyboardButton(text="✅ Выдать доступ", callback_data="adm_access")],
        [InlineKeyboardButton(text="⭐ Выдать Pro", callback_data="adm_pro")],
    ])

# =============================================================
#  /start (главный бот)
# =============================================================
@router.message(CommandStart())
async def start(msg: Message):
    ref_id = None
    args = msg.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1][3:])
        except ValueError:
            pass

    await upsert_user(
        msg.from_user.id,
        msg.from_user.username or "",
        msg.from_user.full_name,
        ref_id if ref_id != msg.from_user.id else None,
    )
    user = await get_user(msg.from_user.id)

    if not user["has_access"]:
        price = await get_setting("access_price", "40")
        await msg.answer(
            f"🚫 У тебя нет доступа.\n\n"
            f"Купите доступ у @DefaultCoding за <b>{price} GMP</b>."
        )
        return

    await msg.answer("👋 Привет! Это CodiFather.\nВыбери действие:",
                     reply_markup=main_menu_kb(msg.from_user.id))

# =============================================================
#  СОЗДАНИЕ БОТА
# =============================================================
@router.callback_query(F.data == "new_bot")
async def new_bot(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if not user["has_access"]:
        await cb.answer("Нет доступа", show_alert=True)
        return

    bots = await get_child_bots_by_owner(cb.from_user.id)
    limit = DEFAULT_MAX_BOTS_PRO if await is_pro(cb.from_user.id) else DEFAULT_MAX_BOTS
    if len(bots) >= limit:
        await cb.answer(f"Лимит: {limit} ботов", show_alert=True)
        return

    await state.set_state(AddBot.token)
    await cb.message.answer("Отправь <b>токен бота</b> от @BotFather:")
    await cb.answer()

@router.message(AddBot.token)
async def addbot_token(msg: Message, state: FSMContext):
    token = msg.text.strip()
    if ":" not in token:
        await msg.answer("Похоже, это не токен. Попробуй ещё раз.")
        return
    try:
        test = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        me = await test.get_me()
        await test.session.close()
    except Exception:
        await msg.answer("❌ Не могу подключиться. Проверь токен.")
        return
    await state.update_data(token=token, username=me.username)
    await state.set_state(AddBot.piarflow)
    await msg.answer("Теперь отправь <b>PiarFlow API key</b> для этого бота:")

@router.message(AddBot.piarflow)
async def addbot_piar(msg: Message, state: FSMContext):
    data = await state.get_data()
    key = msg.text.strip()
    bot_id = await add_child_bot(msg.from_user.id, data["token"], data["username"], key)
    await state.clear()
    await start_child_bot(bot_id)
    await msg.answer(
        f"✅ Бот @{data['username']} добавлен и запущен!\n"
        f"Теперь владелец может принимать заявки на вывод."
    )

# =============================================================
#  МОИ БОТЫ
# =============================================================
@router.callback_query(F.data == "my_bots")
async def my_bots(cb: CallbackQuery):
    bots = await get_child_bots_by_owner(cb.from_user.id)
    if not bots:
        await cb.message.answer("У тебя нет ботов. Нажми «Создать бота».")
        await cb.answer()
        return
    kb = [[InlineKeyboardButton(text=f"@{b['bot_username'] or b['id']}", callback_data=f"cfg:{b['id']}")]
          for b in bots]
    await cb.message.answer("Твои боты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()

# =============================================================
#  КОНФИГ БОТА
# =============================================================
def bot_cfg_kb(b):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Награда: {fmt_money(b['reward'])}", callback_data=f"cfg_reward:{b['id']}")],
        [InlineKeyboardButton(text=f"👥 Рефы: {'ON' if b['ref_enabled'] else 'OFF'} ({fmt_money(b['ref_reward'])})",
                              callback_data=f"cfg_ref:{b['id']}")],
        [InlineKeyboardButton(text=f"🏆 Топ: {'ON' if b['top_enabled'] else 'OFF'}", callback_data=f"cfg_top:{b['id']}")],
        [InlineKeyboardButton(text=f"💸 Мин. вывод: {fmt_money(b['min_withdraw'])}", callback_data=f"cfg_minw:{b['id']}")],
        [InlineKeyboardButton(text=f"{'🟢 Активен' if b['is_active'] else '🔴 Отключён'}", callback_data=f"cfg_toggle:{b['id']}")],
    ])

@router.callback_query(F.data.startswith("cfg:"))
async def cfg_open(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        await cb.answer("Чужой бот", show_alert=True)
        return
    await cb.message.answer(f"⚙️ Настройки @{b['bot_username']}", reply_markup=bot_cfg_kb(b))
    await cb.answer()

@router.callback_query(F.data.startswith("cfg_reward:"))
async def cfg_reward(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        return
    await state.set_state(CfgState.reward)
    await state.update_data(bid=bid)
    await cb.message.answer(f"Текущая награда: {fmt_money(b['reward'])} GMP\nВведи новую:")
    await cb.answer()

@router.message(CfgState.reward)
async def cfg_reward_set(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(",", "."))
    except ValueError:
        await msg.answer("Число, плиз.")
        return
    data = await state.get_data()
    await update_child_setting(data["bid"], "reward", v)
    await state.clear()
    await msg.answer(f"✅ Награда: {fmt_money(v)} GMP")

@router.callback_query(F.data.startswith("cfg_ref:"))
async def cfg_ref(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        return
    new_val = 0 if b["ref_enabled"] else 1
    await update_child_setting(bid, "ref_enabled", new_val)
    b = await get_child_bot(bid)
    await cb.message.edit_reply_markup(reply_markup=bot_cfg_kb(b))
    await cb.answer("Вкл" if new_val else "Выкл")

@router.callback_query(F.data.startswith("cfg_top:"))
async def cfg_top(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        return
    new_val = 0 if b["top_enabled"] else 1
    await update_child_setting(bid, "top_enabled", new_val)
    b = await get_child_bot(bid)
    await cb.message.edit_reply_markup(reply_markup=bot_cfg_kb(b))
    await cb.answer("Вкл" if new_val else "Выкл")

@router.callback_query(F.data.startswith("cfg_minw:"))
async def cfg_minw(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        return
    await state.set_state(CfgState.min_withdraw)
    await state.update_data(bid=bid)
    await cb.message.answer(f"Текущий мин. вывод: {fmt_money(b['min_withdraw'])} GMP\nВведи новый:")
    await cb.answer()

@router.message(CfgState.min_withdraw)
async def cfg_minw_set(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(",", "."))
    except ValueError:
        await msg.answer("Число, плиз.")
        return
    data = await state.get_data()
    await update_child_setting(data["bid"], "min_withdraw", v)
    await state.clear()
    await msg.answer(f"✅ Мин. вывод: {fmt_money(v)} GMP")

@router.callback_query(F.data.startswith("cfg_toggle:"))
async def cfg_toggle(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b = await get_child_bot(bid)
    if not b or b["owner_id"] != cb.from_user.id:
        return
    new_val = 0 if b["is_active"] else 1
    await update_child_setting(bid, "is_active", new_val)
    b = await get_child_bot(bid)
    await cb.message.edit_reply_markup(reply_markup=bot_cfg_kb(b))
    await cb.answer("Включён" if new_val else "Отключён")

# =============================================================
#  АДМИН-ПАНЕЛЬ
# =============================================================
@router.callback_query(F.data == "admin")
async def admin(cb: CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS:
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.message.answer("⚙️ Админ-панель:", reply_markup=admin_kb())
    await cb.answer()

@router.callback_query(F.data == "adm_price")
async def adm_price(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.price)
    await cb.message.answer("Введи новую цену доступа (GMP):")
    await cb.answer()

@router.message(AdminState.price)
async def set_price(msg: Message, state: FSMContext):
    try:
        v = float(msg.text.replace(",", "."))
    except ValueError:
        await msg.answer("Число, плиз.")
        return
    await set_setting("access_price", str(v))
    await state.clear()
    await msg.answer(f"✅ Новая цена: {fmt_money(v)} GMP")

@router.callback_query(F.data == "adm_access")
async def adm_access(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.grant_access)
    await cb.message.answer("Введи user_id кому выдать доступ:")
    await cb.answer()

@router.message(AdminState.grant_access)
async def do_grant(msg: Message, state: FSMContext):
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("Число.")
        return
    await grant_access(uid)
    await state.clear()
    await msg.answer(f"✅ Доступ выдан {uid}")
    try:
        await main_bot.send_message(uid, "🎉 Тебе выдан доступ к CodiFather!")
    except Exception:
        pass

@router.callback_query(F.data == "adm_pro")
async def adm_pro(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(AdminState.grant_pro)
    await cb.message.answer("Введи user_id кому выдать Pro на 30 дней:")
    await cb.answer()

@router.message(AdminState.grant_pro)
async def do_grant_pro(msg: Message, state: FSMContext):
    try:
        uid = int(msg.text.strip())
    except ValueError:
        await msg.answer("Число.")
        return
    await grant_pro(uid, PRO_DAYS)
    await state.clear()
    await msg.answer(f"⭐ Pro выдан {uid} на {PRO_DAYS} дней")
    try:
        await main_bot.send_message(uid, f"⭐ Тебе выдан Pro на {PRO_DAYS} дней!")
    except Exception:
        pass

# =============================================================
#  КОМАНДЫ БЕЗ "/" :  pro [id]  и  доступ [id]
# =============================================================
@router.message(F.text.regexp(r"^pro\s+(\d+)$"))
async def cmd_pro(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    uid = int(msg.text.split()[1])
    await grant_pro(uid, PRO_DAYS)
    await msg.answer(f"⭐ Pro выдан {uid} на {PRO_DAYS} дней")
    try:
        await main_bot.send_message(uid, f"⭐ Тебе выдан Pro на {PRO_DAYS} дней!")
    except Exception:
        pass

@router.message(F.text.regexp(r"^доступ\s+(\d+)$"))
async def cmd_access(msg: Message):
    if msg.from_user.id not in ADMIN_IDS:
        return
    uid = int(msg.text.split()[1])
    await grant_access(uid)
    await msg.answer(f"✅ Доступ выдан {uid}")
    try:
        await main_bot.send_message(uid, "🎉 Тебе выдан доступ к CodiFather!")
    except Exception:
        pass

# =============================================================
#  БАЛАНС (главный бот)
# =============================================================
@router.callback_query(F.data == "my_balance")
async def my_balance(cb: CallbackQuery):
    u = await get_user(cb.from_user.id)
    pro = await is_pro(cb.from_user.id)
    await cb.message.answer(
        f"💰 Баланс: <b>{fmt_money(u['balance'] or 0)}</b> GMP\n"
        f"⭐ Pro: {'да' if pro else 'нет'}"
    )
    await cb.answer()

# =============================================================
#  MAIN
# =============================================================
async def main():
    await init_db()
    dp.include_router(router)
    await start_all_child_bots()
    log.info("CodiFather запущен")
    await dp.start_polling(main_bot)

if __name__ == "__main__":
    asyncio.run(main())
