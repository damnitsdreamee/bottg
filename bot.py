import asyncio
from email.mime import message
import logging
import html
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
student_topic_locks = {}

# =========================
# FSM
# =========================

class ReplyState(StatesGroup):
    waiting_for_reply = State()


# =========================
# DATABASE
# =========================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
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
            admin_message_id INTEGER,
            admin_message_kind TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            sender TEXT NOT NULL,
            sender_name TEXT,
            text TEXT,
            file_id TEXT,
            file_type TEXT,
            created_at TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS students (
            student_id INTEGER PRIMARY KEY,
            telegram_full_name TEXT,
            username TEXT,
            custom_name TEXT,
            forum_topic_id INTEGER
        )
        """)

        migrations = [
            "ALTER TABLE messages ADD COLUMN file_id TEXT",
            "ALTER TABLE messages ADD COLUMN file_type TEXT",
            "ALTER TABLE messages ADD COLUMN sender_name TEXT",
            "ALTER TABLE requests ADD COLUMN assigned_admin_name TEXT",
            "ALTER TABLE requests ADD COLUMN admin_message_id INTEGER",
            "ALTER TABLE requests ADD COLUMN admin_message_kind TEXT",
            "ALTER TABLE students ADD COLUMN forum_topic_id INTEGER",
        ]

        for query in migrations:
            try:
                await db.execute(query)
            except Exception:
                pass

        await db.commit()


# =========================
# DB FUNCTIONS
# =========================

def get_student_lock(student_id: int):
    if student_id not in student_topic_locks:
        student_topic_locks[student_id] = asyncio.Lock()
    return student_topic_locks[student_id]

async def upsert_student(student_id, telegram_full_name, username, custom_name=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO students (student_id, telegram_full_name, username, custom_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id) DO UPDATE SET
            telegram_full_name = excluded.telegram_full_name,
            username = excluded.username
        """, (student_id, telegram_full_name, username, custom_name))
        await db.commit()


async def set_student_custom_name(student_id, custom_name):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT student_id FROM students WHERE student_id = ?",
            (student_id,)
        )
        row = await cursor.fetchone()

        if row:
            await db.execute("""
            UPDATE students
            SET custom_name = ?
            WHERE student_id = ?
            """, (custom_name, student_id))
        else:
            await db.execute("""
            INSERT INTO students (student_id, telegram_full_name, username, custom_name)
            VALUES (?, ?, ?, ?)
            """, (student_id, None, None, custom_name))

        await db.commit()


async def get_student(student_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM students WHERE student_id = ?",
            (student_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_student_topic_id(student_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT forum_topic_id FROM students WHERE student_id = ?",
            (student_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else None


async def set_student_topic_id(student_id, forum_topic_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE students
        SET forum_topic_id = ?
        WHERE student_id = ?
        """, (forum_topic_id, student_id))
        await db.commit()


def build_student_topic_name(student: dict, student_id: int):
    custom_name = student.get("custom_name")
    username = student.get("username")

    if custom_name and username:
        base = f"{custom_name} | @{username}"
    elif custom_name:
        base = custom_name
    elif username:
        base = f"@{username}"
    else:
        base = student.get("telegram_full_name") or f"ID {student_id}"

    return (f"{base} | {student_id}")[:128]


def get_student_lock(student_id: int):
    if student_id not in student_topic_locks:
        student_topic_locks[student_id] = asyncio.Lock()
    return student_topic_locks[student_id]


async def get_or_create_student_topic(student_id: int, force_recreate: bool = False):
    lock = get_student_lock(student_id)

    async with lock:
        if force_recreate:
            await set_student_topic_id(student_id, None)

        topic_id = await get_student_topic_id(student_id)
        if topic_id:
            return topic_id

        student = await get_student(student_id)
        if not student:
            return None

        topic_name = build_student_topic_name(student, student_id)

        topic = await bot.create_forum_topic(
            chat_id=STAROSTA_CHAT_ID,
            name=topic_name
        )

        await set_student_topic_id(student_id, topic.message_thread_id)
        return topic.message_thread_id


async def rename_student_topic_if_exists(student_id: int):
    topic_id = await get_student_topic_id(student_id)
    if not topic_id:
        return

    student = await get_student(student_id)
    if not student:
        return

    topic_name = build_student_topic_name(student, student_id)

    try:
        await bot.edit_forum_topic(
            chat_id=STAROSTA_CHAT_ID,
            message_thread_id=topic_id,
            name=topic_name
        )
    except Exception as e:
        print(f"TOPIC RENAME ERROR for {student_id}: {e}")


async def get_student_display(student_id):
    student = await get_student(student_id)

    if not student:
        return f"ID {student_id}"

    custom_name = student.get("custom_name")
    telegram_full_name = student.get("telegram_full_name")
    username = student.get("username")

    if custom_name and username:
        return f"{html.escape(custom_name)} (@{html.escape(username)})"
    if custom_name:
        return html.escape(custom_name)
    if telegram_full_name and username:
        return f"{html.escape(telegram_full_name)} (@{html.escape(username)})"
    if telegram_full_name:
        return html.escape(telegram_full_name)

    return f"ID {student_id}"

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


async def get_open_tickets():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM requests
        WHERE status != 'closed'
        ORDER BY id DESC
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_open_tickets_by_admin(admin_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM requests
        WHERE assigned_admin_id = ? AND status != 'closed'
        ORDER BY id DESC
        """, (admin_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


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
        await db.execute(
            "UPDATE requests SET status = 'closed' WHERE id = ?",
            (request_id,)
        )
        await db.commit()


async def reopen_request(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET status = 'in_progress'
        WHERE id = ?
        """, (request_id,))
        await db.commit()


async def add_message(
    request_id,
    sender,
    sender_name=None,
    text=None,
    file_id=None,
    file_type=None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO messages (request_id, sender, sender_name, text, file_id, file_type, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            request_id,
            sender,
            sender_name,
            text,
            file_id,
            file_type,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        await db.commit()


async def get_messages(request_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM messages
        WHERE request_id = ?
        ORDER BY id ASC
        """, (request_id,))
        return [dict(r) for r in await cursor.fetchall()]


async def set_admin_message_meta(request_id, message_id, message_kind):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET admin_message_id = ?, admin_message_kind = ?
        WHERE id = ?
        """, (message_id, message_kind, request_id))
        await db.commit()


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


def short_text(text, limit=60):
    if not text:
        return "-"
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def extract_message_content(message: Message):
    text = message.text or message.caption or ""
    file_id = None
    file_type = None

    if message.photo:
        file_id = message.photo[-1].file_id
        file_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        file_type = "video"
    elif message.voice:
        file_id = message.voice.file_id
        file_type = "voice"
    elif message.document:
        file_id = message.document.file_id
        file_type = "document"
    elif message.animation:
        file_id = message.animation.file_id
        file_type = "animation"

    return text, file_id, file_type


async def send_content(
    chat_id: int,
    text=None,
    file_id=None,
    file_type=None,
    prefix=None,
    message_thread_id=None
):
    safe_prefix = prefix or ""

    if file_id:
        caption = safe_prefix
        if text:
            caption = f"{safe_prefix}\n{text}" if safe_prefix else text

        if file_type == "photo":
            await bot.send_photo(
                chat_id,
                file_id,
                caption=caption or None,
                message_thread_id=message_thread_id
            )
        elif file_type == "video":
            await bot.send_video(
                chat_id,
                file_id,
                caption=caption or None,
                message_thread_id=message_thread_id
            )
        elif file_type == "voice":
            await bot.send_voice(
                chat_id,
                file_id,
                caption=caption or None,
                message_thread_id=message_thread_id
            )
        elif file_type == "document":
            await bot.send_document(
                chat_id,
                file_id,
                caption=caption or None,
                message_thread_id=message_thread_id
            )
        elif file_type == "animation":
            await bot.send_animation(
                chat_id,
                file_id,
                caption=caption or None,
                message_thread_id=message_thread_id
            )
        else:
            await bot.send_message(
                chat_id,
                caption or "[файл]",
                message_thread_id=message_thread_id
            )
    else:
        if text:
            msg = f"{safe_prefix}\n{text}" if safe_prefix else text
            await bot.send_message(
                chat_id,
                msg,
                message_thread_id=message_thread_id
            )

async def send_to_student_topic(student_id, text=None, file_id=None, file_type=None, prefix=None):
    topic_id = await get_or_create_student_topic(student_id)
    if not topic_id:
        raise RuntimeError("Не удалось получить тему студента")

    try:
        await send_content(
            STAROSTA_CHAT_ID,
            text=text,
            file_id=file_id,
            file_type=file_type,
            prefix=prefix,
            message_thread_id=topic_id
        )
    except Exception as e:
        if "message thread not found" in str(e).lower():
            topic_id = await get_or_create_student_topic(student_id, force_recreate=True)

            await send_content(
                STAROSTA_CHAT_ID,
                text=text,
                file_id=file_id,
                file_type=file_type,
                prefix=prefix,
                message_thread_id=topic_id
            )
        else:
            raise


async def send_ticket_card_to_student_topic(student_id, rid, card_text, file_id=None, file_type=None):
    topic_id = await get_or_create_student_topic(student_id)
    if not topic_id:
        raise RuntimeError("Не удалось получить тему студента")

    try:
        if file_id:
            if file_type == "photo":
                sent = await bot.send_photo(
                    STAROSTA_CHAT_ID,
                    file_id,
                    caption=card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
            elif file_type == "video":
                sent = await bot.send_video(
                    STAROSTA_CHAT_ID,
                    file_id,
                    caption=card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
            elif file_type == "voice":
                sent = await bot.send_voice(
                    STAROSTA_CHAT_ID,
                    file_id,
                    caption=card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
            elif file_type == "document":
                sent = await bot.send_document(
                    STAROSTA_CHAT_ID,
                    file_id,
                    caption=card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
            elif file_type == "animation":
                sent = await bot.send_animation(
                    STAROSTA_CHAT_ID,
                    file_id,
                    caption=card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
            else:
                sent = await bot.send_message(
                    STAROSTA_CHAT_ID,
                    card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )
        else:
            sent = await bot.send_message(
                STAROSTA_CHAT_ID,
                card_text,
                reply_markup=request_keyboard(rid, "new"),
                message_thread_id=topic_id
            )

        return sent

    except Exception as e:
        if "message thread not found" in str(e).lower():
            topic_id = await get_or_create_student_topic(student_id, force_recreate=True)

            if file_id:
                if file_type == "photo":
                    sent = await bot.send_photo(
                        STAROSTA_CHAT_ID,
                        file_id,
                        caption=card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
                elif file_type == "video":
                    sent = await bot.send_video(
                        STAROSTA_CHAT_ID,
                        file_id,
                        caption=card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
                elif file_type == "voice":
                    sent = await bot.send_voice(
                        STAROSTA_CHAT_ID,
                        file_id,
                        caption=card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
                elif file_type == "document":
                    sent = await bot.send_document(
                        STAROSTA_CHAT_ID,
                        file_id,
                        caption=card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
                elif file_type == "animation":
                    sent = await bot.send_animation(
                        STAROSTA_CHAT_ID,
                        file_id,
                        caption=card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
                else:
                    sent = await bot.send_message(
                        STAROSTA_CHAT_ID,
                        card_text,
                        reply_markup=request_keyboard(rid, "new"),
                        message_thread_id=topic_id
                    )
            else:
                sent = await bot.send_message(
                    STAROSTA_CHAT_ID,
                    card_text,
                    reply_markup=request_keyboard(rid, "new"),
                    message_thread_id=topic_id
                )

            return sent
        else:
            raise

    if file_id:
        caption = safe_prefix
        if text:
            caption = f"{safe_prefix}\n{text}" if safe_prefix else text

        if file_type == "photo":
            await bot.send_photo(chat_id, file_id, caption=caption or None)
        elif file_type == "voice":
            await bot.send_voice(chat_id, file_id, caption=caption or None)
        elif file_type == "document":
            await bot.send_document(chat_id, file_id, caption=caption or None)
        elif file_type == "animation":
            await bot.send_animation(chat_id, file_id, caption=caption or None)
        else:
            await bot.send_message(chat_id, caption or "[файл]")
    else:
        if text:
            msg = f"{safe_prefix}\n{text}" if safe_prefix else text
            await bot.send_message(chat_id, msg)


async def format_request_card(r):
    display_name = await get_student_display(r["student_id"])
    assigned = html.escape(r.get("assigned_admin_name") or "никто")
    text_safe = html.escape(r["text"] or "")

    return (
        f"<b>Тикет #{r['id']}</b>\n"
        f"👤 {display_name}\n"
        f"🆔 <code>{r['student_id']}</code>\n"
        f"👨‍💼 Староста: {assigned}\n"
        f"📌 Статус: {html.escape(r['status'])}\n"
        f"🕒 Создан: {html.escape(r['created_at'])}\n\n"
        f"{text_safe}"
    )


async def refresh_admin_card(request_id, chat_id=None, message_id=None):
    req = await get_request(request_id)
    if not req:
        return

    if chat_id is None:
        chat_id = STAROSTA_CHAT_ID
    if message_id is None:
        message_id = req.get("admin_message_id")

    if not message_id:
        return

    text = await format_request_card(req)

    try:
        if req.get("admin_message_kind") == "text":
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=request_keyboard(request_id, req["status"])
            )
        else:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                reply_markup=request_keyboard(request_id, req["status"])
            )
    except Exception as e:
        print(f"REFRESH ERROR #{request_id}: {e}")


async def build_ticket_history_text(request_id):
    req = await get_request(request_id)
    if not req:
        return None

    student_display = await get_student_display(req["student_id"])
    messages = await get_messages(request_id)

    lines = [
        f"<b>История тикета #{req['id']}</b>",
        f"👤 Студент: {student_display}",
        f"🆔 ID: <code>{req['student_id']}</code>",
        f"📌 Статус: {html.escape(req['status'])}",
        f"👨‍💼 Староста: {html.escape(req.get('assigned_admin_name') or 'никто')}",
        f"🕒 Создан: {html.escape(req['created_at'])}",
        "",
        "<b>Сообщения:</b>"
    ]

    if not messages:
        lines.append("— История пуста")
        return "\n".join(lines)

    for msg in messages:
        sender = msg.get("sender")
        sender_name = msg.get("sender_name") or sender
        created_at = html.escape(msg.get("created_at") or "-")
        text = html.escape(msg.get("text") or "")
        file_type = msg.get("file_type")

        if sender == "user":
            role = "Студент"
        else:
            role = f"Староста ({html.escape(sender_name)})"

        body = text if text else "[без текста]"
        if file_type:
            body += f" [файл: {html.escape(file_type)}]"

        lines.append(f"— {role} | {created_at}")
        lines.append(body)
        lines.append("")

    result = "\n".join(lines)
    if len(result) > 4000:
        result = result[:4000] + "\n\n...история обрезана"

    return result


# =========================
# STUDENT
# =========================

@router.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Это бот для обращений к старостам группы БИСО-03-25.\n\n"
        "✉ Если у тебя есть вопрос или проблема — напиши мне, "
        "и сообщение будет отправлено старостам.\n\n"
        "<u>Ты получишь ответ здесь.</u>\n\n"
        "❗️<i>Не пиши старостам в личные — используй этот бот.</i>"
    )


@router.message(F.text.startswith("/setname"))
async def setname_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) < 3:
        await message.answer("Используй: /setname ID Имя")
        return

    try:
        student_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return

    custom_name = parts[2].strip()
    await set_student_custom_name(student_id, custom_name)
    await rename_student_topic_if_exists(student_id)

    student = await get_student(student_id)
    username = student.get("username") if student else None

    if username:
        await message.answer(f"Установлено имя: {html.escape(custom_name)} (@{html.escape(username)})")
    else:
        await message.answer(f"Установлено имя: {html.escape(custom_name)}")


@router.message(F.chat.type == "private")
async def handle_student(message: Message):
    if is_admin(message.from_user.id):
        return

    user_id = message.from_user.id
    now = datetime.now().timestamp()

# если это альбом, не режем его кулдауном
    if not message.media_group_id:
        if user_id in last_message_time:
            diff = now - last_message_time[user_id]
            if diff < COOLDOWN:
                wait = int(COOLDOWN - diff)
                await message.answer(f"⏳ Подожди {wait} сек")
                return

        last_message_time[user_id] = now

    await upsert_student(
        student_id=message.from_user.id,
        telegram_full_name=message.from_user.full_name,
        username=message.from_user.username
    )

    active = await get_active_request(message.from_user.id)
    text, file_id, file_type = extract_message_content(message)

    if active:
        rid = active["id"]

        await add_message(
            rid,
            "user",
            sender_name=message.from_user.full_name,
            text=text,
            file_id=file_id,
            file_type=file_type
        )

        await send_to_student_topic(
            message.from_user.id,
            text=html.escape(text) if text else "",
            file_id=file_id,
            file_type=file_type,
            prefix=f"💬 Дополнение к тикету #{rid}"
        )

        await message.answer(
            f"✉️ У тебя уже есть активный тикет #{rid}.\n"
            f"Сообщение добавлено туда."
        )
        return

    rid = await create_request(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        text or "[файл]"
    )

    await add_message(
        rid,
        "user",
        sender_name=message.from_user.full_name,
        text=text,
        file_id=file_id,
        file_type=file_type
    )

    req = await get_request(rid)
    card_text = await format_request_card(req)

    sent = await send_ticket_card_to_student_topic(
        student_id=message.from_user.id,
        rid=rid,
        card_text=card_text,
        file_id=file_id,
        file_type=file_type
    )

    if sent:
        if file_id:
            await set_admin_message_meta(rid, sent.message_id, "caption")
        else:
            await set_admin_message_meta(rid, sent.message_id, "text")

    await message.answer(f"✅ Вопрос #{rid} отправлен")


# =========================
# ADMIN CALLBACKS
# =========================

@router.callback_query(F.data.startswith("take:"))
async def take(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])

    await assign_request(rid, callback.from_user.id, callback.from_user.full_name)
    await refresh_admin_card(rid, callback.message.chat.id, callback.message.message_id)

    await callback.answer("Ты взял тикет")


@router.callback_query(F.data.startswith("reply:"))
async def reply(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])
    await state.set_state(ReplyState.waiting_for_reply)
    await state.update_data(rid=rid)
    await callback.message.answer(
        f"Отправь ответ для тикета #{rid}.\n"
        f"Можно текст, фото, документ, голосовое или гифку."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("close:"))
async def close(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])

    await close_request(rid)
    req = await get_request(rid)

    # уведомляем пользователя
    if req:
        try:
            await bot.send_message(
                req["student_id"],
                f"✅ Вопрос #{rid} закрыт"
            )
        except Exception as e:
            print("USER SEND ERROR:", e)

    # 🔥 обновляем карточку + кнопки (появится reopen)
    await refresh_admin_card(rid, callback.message.chat.id, callback.message.message_id)
    await callback.answer("Закрыто")


@router.callback_query(F.data.startswith("reopen:"))
async def reopen(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    rid = int(callback.data.split(":")[1])

    await reopen_request(rid)
    req = await get_request(rid)

    # уведомляем пользователя
    if req:
        try:
            await bot.send_message(
                req["student_id"],
                f"🔄 Вопрос #{rid} снова открыт"
            )
        except Exception:
            pass

    await refresh_admin_card(rid, callback.message.chat.id, callback.message.message_id)
    await callback.answer("Переоткрыто")


# =========================
# ADMIN MESSAGES
# =========================

@router.message(ReplyState.waiting_for_reply)
async def send_reply(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    rid = data.get("rid")

    if not rid:
        await message.answer("Не найден ID тикета")
        await state.clear()
        return

    text, file_id, file_type = extract_message_content(message)

    if not text and not file_id:
        await message.answer("Ответ пустой")
        return

    await add_message(
        rid,
        "admin",
        sender_name=message.from_user.full_name,
        text=text,
        file_id=file_id,
        file_type=file_type
    )

    req = await get_request(rid)
    if not req:
        await message.answer("Тикет не найден")
        await state.clear()
        return

    if req["assigned_admin_id"] is None:
        await assign_request(rid, message.from_user.id, message.from_user.full_name)

    try:
        await send_content(
            req["student_id"],
            text=html.escape(text) if text else "",
            file_id=file_id,
            file_type=file_type,
            prefix=f"💬 Ответ на вопрос #{rid}"
        )
    except Exception as e:
        print("SEND TO STUDENT ERROR:", e)
        await message.answer("Не удалось отправить ответ студенту")
        await state.clear()
        return

    await refresh_admin_card(rid)
    await message.answer("Ответ отправлен")
    await state.clear()


# =========================
# ADMIN COMMANDS
# =========================

@router.message(F.text.startswith("/whois"))
async def whois_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("Используй: /whois ID")
        return

    try:
        student_id = int(parts[1])
    except ValueError:
        await message.answer("ID должен быть числом")
        return

    student = await get_student(student_id)

    if not student:
        await message.answer("Студент не найден в базе")
        return

    custom_name = student.get("custom_name") or "-"
    telegram_full_name = student.get("telegram_full_name") or "-"
    username = f"@{student['username']}" if student.get("username") else "-"

    await message.answer(
        f"<b>Информация о студенте</b>\n"
        f"👤 Кастомное имя: {html.escape(custom_name)}\n"
        f"📛 Telegram-имя: {html.escape(telegram_full_name)}\n"
        f"🔗 Username: {html.escape(username)}\n"
        f"🆔 ID: <code>{student_id}</code>"
    )


@router.message(F.text == "/mytickets")
async def mytickets_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    tickets = await get_open_tickets_by_admin(message.from_user.id)

    if not tickets:
        await message.answer("У тебя нет активных тикетов")
        return

    lines = ["<b>Твои активные тикеты:</b>\n"]

    for ticket in tickets:
        student_display = await get_student_display(ticket["student_id"])
        lines.append(
            f"#{ticket['id']} | {student_display}\n"
            f"Статус: {html.escape(ticket['status'])}\n"
            f"Текст: {html.escape(short_text(ticket['text'], 80))}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n...список обрезан"

    await message.answer(text)


@router.message(F.text == "/alltickets")
async def alltickets_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    tickets = await get_open_tickets()

    if not tickets:
        await message.answer("Незакрытых тикетов нет")
        return

    lines = ["<b>Все незакрытые тикеты:</b>\n"]

    for ticket in tickets:
        student_display = await get_student_display(ticket["student_id"])
        assigned = html.escape(ticket.get("assigned_admin_name") or "никто")

        lines.append(
            f"#{ticket['id']} | {student_display}\n"
            f"Статус: {html.escape(ticket['status'])} | Староста: {assigned}\n"
            f"Текст: {html.escape(short_text(ticket['text'], 80))}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n...список обрезан"

    await message.answer(text)


@router.message(F.text.startswith("/ticket"))
async def ticket_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("Используй: /ticket ID")
        return

    try:
        rid = int(parts[1])
    except ValueError:
        await message.answer("ID тикета должен быть числом")
        return

    req = await get_request(rid)
    if not req:
        await message.answer("Тикет не найден")
        return

    history_text = await build_ticket_history_text(rid)
    await message.answer(history_text)


@router.message(F.text.startswith("/close"))
async def close_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    try:
        rid = int(message.text.split()[1])
    except Exception:
        await message.answer("Используй: /close ID")
        return

    await close_request(rid)
    req = await get_request(rid)

    if not req:
        await message.answer("Тикет не найден")
        return

    try:
        await bot.send_message(
            req["student_id"],
            f"✅ Вопрос #{rid} закрыт старостой"
        )
    except Exception as e:
        print("USER SEND ERROR:", e)

    await refresh_admin_card(rid)
    await message.answer(f"Вопрос #{rid} закрыт ✅")


# =========================
# MAIN
# =========================

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())