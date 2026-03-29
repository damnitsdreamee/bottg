import asyncio
import logging
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================
# CONFIG
# =========================

BOT_TOKEN = "8672440419:AAHCbJmOkUBdrqioCBHUieQWpQ2gii3sY00"
STAROSTA_CHAT_ID = -1003869910543
ADMIN_IDS = {997225365, 1033734417, 6273760899}
DB_PATH = "support_bot.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)
last_message_time = {}
COOLDOWN = 15

# =========================
# FSM
# =========================

class ReplyState(StatesGroup):
    waiting_for_reply_text = State()

# =========================
# DATABASE
# =========================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # создаём таблицы если нет
        await db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            student_username TEXT,
            student_name TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            assigned_admin_id INTEGER,
            assigned_admin_name TEXT,
            created_at TEXT NOT NULL,
            admin_message_id INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            text TEXT,
            created_at TEXT NOT NULL
        )
        """)

        # 🔥 МИГРАЦИИ (если таблица старая)
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN file_id TEXT")
        except:
            pass

        try:
            await db.execute("ALTER TABLE messages ADD COLUMN file_type TEXT")
        except:
            pass

        try:
            await db.execute("ALTER TABLE requests ADD COLUMN assigned_admin_name TEXT")
        except:
            pass

        await db.commit()

# =========================
# DB FUNCTIONS
# =========================

async def create_request(student_id, username, full_name, text):
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
        INSERT INTO requests (
            student_id, student_username, student_name, text, status, created_at
        ) VALUES (?, ?, ?, ?, 'new', ?)
        """, (student_id, username, full_name, text, created_at))
        await db.commit()
        return cursor.lastrowid

async def get_request(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def get_active_request(student_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM requests
        WHERE student_id = ? AND status != 'closed'
        ORDER BY id DESC LIMIT 1
        """, (student_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def assign_request(request_id, admin_id, admin_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET assigned_admin_id = ?, assigned_admin_name = ?, status = 'in_progress'
        WHERE id = ?
        """, (admin_id, admin_name, request_id))
        await db.commit()

async def close_request(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE requests SET status='closed' WHERE id=?", (request_id,))
        await db.commit()

async def reopen_request(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT student_id FROM requests WHERE id = ?",
            (request_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return

        student_id = row[0]

        await db.execute("""
        UPDATE requests
        SET status = 'closed'
        WHERE student_id = ? AND id != ?
        """, (student_id, request_id))

        await db.execute("""
        UPDATE requests
        SET status = 'in_progress'
        WHERE id = ?
        """, (request_id,))

        await db.commit()

async def add_message(request_id, sender, text=None, file_id=None, file_type=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO messages (request_id, sender, text, file_id, file_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (request_id, sender, text, file_id, file_type,
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        await db.commit()

async def get_messages(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM messages WHERE request_id=? ORDER BY id ASC
        """, (request_id,))
        return [dict(r) for r in await cursor.fetchall()]

# =========================
# KEYBOARD
# =========================

def request_keyboard(request_id: int, status: str):
    kb = InlineKeyboardBuilder()

    if status in ("new", "in_progress"):
        kb.row(
            InlineKeyboardButton(text="🙋 Взять", callback_data=f"take:{request_id}"),
            InlineKeyboardButton(text="💬 Ответить", callback_data=f"reply:{request_id}")
        )
        kb.row(
            InlineKeyboardButton(text="❌ Закрыть", callback_data=f"close:{request_id}")
        )
    else:
        kb.row(
            InlineKeyboardButton(text="🔄 Переоткрыть", callback_data=f"reopen:{request_id}")
        )

    

    return kb.as_markup()

# =========================
# HELPERS
# =========================

def is_admin(user_id: int):
    return user_id in ADMIN_IDS

def format_request_card(r):
    username = f"@{r['student_username']}" if r['student_username'] else "-"
    assigned = r.get("assigned_admin_name") or "никто"

    return (
        f"<b>Тикет #{r['id']}</b>\n"
        f"👤 {r['student_name']} ({username})\n"
        f"🆔 <code>{r['student_id']}</code>\n"
        f"👨‍💼 Староста: {assigned}\n\n"
        f"{r['text']}"
    )

# =========================
# STUDENT
# =========================

@router.message(CommandStart())
async def start(message: Message):
    await message.answer("""👋 Привет !

Это бот для обращений к старостам группы БИСО-03-25.

✉ Если у тебя есть какой-либо вопрос или проблема - напиши его мне и он будет отправлен старостам.

<u>Ты получишь ответ здесь.</u>

❗️<i> Не пиши старостам в личные — используй этот бот.</i>""")

@router.message(F.chat.type == "private")
async def handle_student(message: Message):
    if is_admin(message.from_user.id):
        return

    # ⏳ антиспам
    user_id = message.from_user.id
    now = datetime.now().timestamp()

    if user_id in last_message_time:
        diff = now - last_message_time[user_id]

        if diff < COOLDOWN:
            wait = int(COOLDOWN - diff)
            await message.answer(f"⏳ Подожди {wait} сек")
            return

    last_message_time[user_id] = now

    active = await get_active_request(message.from_user.id)

    text = message.text
    file_id = None
    file_type = None

    if message.voice:
        file_id = message.voice.file_id
        file_type = "voice"
    elif message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"

    # =========================
    # ЕСЛИ ЕСТЬ ТИКЕТ
    # =========================
    if active:
        rid = active["id"]

        await add_message(rid, "user", text, file_id, file_type)

        if file_id:
            if file_type == "photo":
                await bot.send_photo(STAROSTA_CHAT_ID, file_id, caption=f"📎 Вопрос #{rid}")
            elif file_type == "voice":
                await bot.send_voice(STAROSTA_CHAT_ID, file_id)
            elif file_type == "document":
                await bot.send_document(STAROSTA_CHAT_ID, file_id)
        else:
            await bot.send_message(STAROSTA_CHAT_ID, f"💬 #{rid}: {text}")

        await message.answer(f"✉️ Вопрос ушел #{rid}")
        return

    # =========================
    # СОЗДАНИЕ ТИКЕТА
    # =========================
    rid = await create_request(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        text or "[файл]"
    )

    await add_message(rid, "user", text, file_id, file_type)

    req = await get_request(rid)

    if file_id:
        if file_type == "photo":
            await bot.send_photo(
                STAROSTA_CHAT_ID,
                file_id,
                caption=format_request_card(req),
                reply_markup=request_keyboard(rid, "new")
            )
        elif file_type == "voice":
            await bot.send_voice(
                STAROSTA_CHAT_ID,
                file_id,
                caption=format_request_card(req),
                reply_markup=request_keyboard(rid, "new")
            )
        elif file_type == "document":
            await bot.send_document(
                STAROSTA_CHAT_ID,
                file_id,
                caption=format_request_card(req),
                reply_markup=request_keyboard(rid, "new")
            )
    else:
        await bot.send_message(
            STAROSTA_CHAT_ID,
            format_request_card(req),
            reply_markup=request_keyboard(rid, "new")
        )

    await message.answer(f"✅ Вопрос #{rid}")
# =========================
# ADMIN
# =========================

@router.callback_query(F.data.startswith("take:"))
async def take(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])

    await assign_request(rid, callback.from_user.id, callback.from_user.full_name)

    # 🔥 обновляем карточку (ник того кто взял)
    req = await get_request(rid)

    try:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=format_request_card(req),
            reply_markup=request_keyboard(rid, "in_progress")
        )
    except Exception as e:
        print("EDIT TAKE ERROR:", e)

    await callback.answer("Ты отвечаешь на вопрос")

@router.callback_query(F.data.startswith("reply:"))
async def reply(callback: CallbackQuery, state: FSMContext):
    rid = int(callback.data.split(":")[1])
    await state.set_state(ReplyState.waiting_for_reply_text)
    await state.update_data(rid=rid)
    await callback.message.answer("Введите ответ")

@router.message(ReplyState.waiting_for_reply_text)
async def send_reply(message: Message, state: FSMContext):
    data = await state.get_data()
    rid = data["rid"]

    await add_message(rid, "admin", message.text)

    req = await get_request(rid)

    await bot.send_message(
        req["student_id"],
        f"💬 Ответ на вопрос #{rid}:\n{message.text}"
    )

    await message.answer("Отправлено")
    await state.clear()

@router.callback_query(F.data.startswith("close:"))
async def close(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])

    await close_request(rid)

    req = await get_request(rid)

    # уведомляем пользователя
    try:
        await bot.send_message(
            req["student_id"],
            f"✅ Вопрос #{rid} закрыт"
        )
    except Exception as e:
        print("USER SEND ERROR:", e)

    # 🔥 обновляем карточку + кнопки (появится reopen)
    try:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=format_request_card(req),
            reply_markup=request_keyboard(rid, "closed")
        )
    except Exception as e:
        print("EDIT CLOSE ERROR:", e)

    await callback.answer("Закрыто")

@router.callback_query(F.data.startswith("reopen:"))
async def reopen(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    rid = int(callback.data.split(":")[1])

    await reopen_request(rid)

    req = await get_request(rid)

    # уведомляем пользователя
    try:
        await bot.send_message(
            req["student_id"],
            f"🔄 Вопрос #{rid} снова открыт"
        )
    except:
        pass

    # 🔥 ВАЖНО: обновляем кнопки обратно на "закрыть"
    try:
        await bot.edit_message_reply_markup(
            chat_id=STAROSTA_CHAT_ID,
            message_id=callback.message.message_id,
            reply_markup=request_keyboard(rid, "in_progress")
        )
    except:
        pass

    await callback.answer("Переоткрыто")



# =========================
# ADMIN COMMANDS
# =========================

@router.message(F.text.startswith("/close"))
async def close_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    try:
        rid = int(message.text.split()[1])
    except:
        await message.answer("Используй: /close ID")
        return

    await close_request(rid)

    req = await get_request(rid)

    try:
        await bot.send_message(
            req["student_id"],
            f"✅ Вопрос #{rid} закрыт старостой"
        )
    except Exception as e:
        print("USER SEND ERROR:", e)

    await message.answer(f"Вопрос #{rid} закрыт ✅")

# =========================
# MAIN
# =========================

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
