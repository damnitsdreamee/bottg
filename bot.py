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
STAROSTA_CHAT_ID = -1003869910543  # чат старост
ADMIN_IDS = {997225365, 1033734417, 6273760899}  # id старост
DB_PATH = "support_bot.db"

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
router = Router()
dp.include_router(router)


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
        await db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            student_username TEXT,
            student_name TEXT,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            assigned_admin_id INTEGER,
            created_at TEXT NOT NULL,
            admin_message_id INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            admin_name TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        await db.commit()


async def create_request(student_id: int, username: str | None, full_name: str, text: str):
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
        INSERT INTO requests (
            student_id, student_username, student_name, text, status, created_at
        ) VALUES (?, ?, ?, ?, 'new', ?)
        """, (student_id, username, full_name, text, created_at))
        await db.commit()
        return cursor.lastrowid


async def set_admin_message_id(request_id: int, admin_message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET admin_message_id = ?
        WHERE id = ?
        """, (admin_message_id, request_id))
        await db.commit()


async def get_request(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM requests WHERE id = ?
        """, (request_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def assign_request(request_id: int, admin_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET status = 'in_progress',
            assigned_admin_id = ?
        WHERE id = ?
        """, (admin_id, request_id))
        await db.commit()


async def close_request(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        UPDATE requests
        SET status = 'closed'
        WHERE id = ?
        """, (request_id,))
        await db.commit()


async def add_reply(request_id: int, admin_id: int, admin_name: str, reply_text: str):
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO replies (
            request_id, admin_id, admin_name, reply_text, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """, (request_id, admin_id, admin_name, reply_text, created_at))
        await db.commit()


async def get_replies(request_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
        SELECT * FROM replies
        WHERE request_id = ?
        ORDER BY id ASC
        """, (request_id,))
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# =========================
# KEYBOARDS
# =========================

def request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Взять в работу", callback_data=f"take:{request_id}"),
        InlineKeyboardButton(text="Ответить", callback_data=f"reply:{request_id}")
    )
    builder.row(
        InlineKeyboardButton(text="Закрыть", callback_data=f"close:{request_id}")
    )
    return builder.as_markup()


# =========================
# HELPERS
# =========================

def format_request_card(request_data: dict, replies: list[dict] | None = None) -> str:
    status_map = {
        "new": "🟡 Новое",
        "in_progress": "🟠 В работе",
        "closed": "✅ Закрыто",
    }

    username = (
        f"@{request_data['student_username']}"
        if request_data["student_username"] else "без username"
    )

    text = (
        f"<b>Обращение #{request_data['id']}</b>\n"
        f"Статус: {status_map.get(request_data['status'], request_data['status'])}\n"
        f"Студент: {request_data['student_name']}\n"
        f"Username: {username}\n"
        f"Student ID: <code>{request_data['student_id']}</code>\n"
        f"Создано: {request_data['created_at']}\n\n"
        f"<b>Сообщение:</b>\n{request_data['text']}"
    )

    if request_data.get("assigned_admin_id"):
        text += f"\n\n👤 Взял в работу: <code>{request_data['assigned_admin_id']}</code>"

    if replies:
        text += "\n\n<b>Ответы:</b>"
        for reply in replies[-5:]:
            text += (
                f"\n— <b>{reply['admin_name']}</b>: {reply['reply_text']}"
            )

    return text


async def refresh_admin_message(request_id: int):
    request_data = await get_request(request_id)
    if not request_data or not request_data.get("admin_message_id"):
        return

    replies = await get_replies(request_id)
    text = format_request_card(request_data, replies)

    try:
        await bot.edit_message_text(
            chat_id=STAROSTA_CHAT_ID,
            message_id=request_data["admin_message_id"],
            text=text,
            reply_markup=request_keyboard(request_id)
        )
    except Exception:
        # если сообщение нельзя отредактировать, просто игнорируем
        pass


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================
# STUDENT SIDE
# =========================

@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет!\n"
        "Это бот для обращений к старостам БИСО-03-25.\n"
        "✉️ Если у тебя есть какой-либо вопрос или проблема - нажми на кнопку <b>\"Задать вопрос\"</b> он будет зарегистрирован и отправлен старостам.\n"
        "<u>Ты получишь ответ здесь.</u>\n"
        "❗️<i> Не пиши старостам в личные — используй этот бот.</i>"
    )


@router.message(F.chat.type == "private", F.text)
async def handle_student_message(message: Message):
    if is_admin(message.from_user.id):
        return

    request_id = await create_request(
        student_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        text=message.text,
    )

    request_data = await get_request(request_id)
    admin_text = format_request_card(request_data)

    sent = await bot.send_message(
        chat_id=STAROSTA_CHAT_ID,
        text=admin_text,
        reply_markup=request_keyboard(request_id)
    )

    await set_admin_message_id(request_id, sent.message_id)

    await message.answer(
        f"✅ Твоё обращение отправлено старостам.\n"
        f"Номер обращения: <b>#{request_id}</b>\n\n"
        f"Когда кто-то из старост ответит, бот пришлёт сообщение сюда."
    )


@router.message(F.chat.type == "private")
async def handle_non_text_student_message(message: Message):
    if is_admin(message.from_user.id):
        return

    await message.answer("Пока что бот принимает только текстовые обращения.")


# =========================
# ADMIN SIDE
# =========================

@router.callback_query(F.data.startswith("take:"))
async def take_request(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    request_id = int(callback.data.split(":")[1])
    request_data = await get_request(request_id)

    if not request_data:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    if request_data["status"] == "closed":
        await callback.answer("Обращение уже закрыто", show_alert=True)
        return

    await assign_request(request_id, callback.from_user.id)
    await refresh_admin_message(request_id)

    await callback.answer("Обращение взято в работу")


@router.callback_query(F.data.startswith("reply:"))
async def reply_request(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    request_id = int(callback.data.split(":")[1])
    request_data = await get_request(request_id)

    if not request_data:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    if request_data["status"] == "closed":
        await callback.answer("Обращение уже закрыто", show_alert=True)
        return

    await state.set_state(ReplyState.waiting_for_reply_text)
    await state.update_data(request_id=request_id)

    await callback.message.answer(
        f"Напиши ответ для обращения #{request_id}.\n"
        f"Он будет отправлен студенту от имени бота."
    )
    await callback.answer()


@router.message(ReplyState.waiting_for_reply_text)
async def process_reply_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    request_id = data["request_id"]

    request_data = await get_request(request_id)
    if not request_data:
        await message.answer("Обращение не найдено.")
        await state.clear()
        return

    if request_data["status"] == "closed":
        await message.answer("Обращение уже закрыто.")
        await state.clear()
        return

    admin_name = message.from_user.full_name
    reply_text = message.text

    await add_reply(request_id, message.from_user.id, admin_name, reply_text)

    # если ещё никто не взял, автоматически назначим отвечающего
    if not request_data["assigned_admin_id"]:
        await assign_request(request_id, message.from_user.id)

    student_text = (
        f"📩 Ответ от старосты по обращению <b>#{request_id}</b>:\n\n"
        f"{reply_text}"
    )

    try:
        await bot.send_message(request_data["student_id"], student_text)
    except Exception:
        await message.answer(
            "Не удалось отправить ответ студенту.\n"
            "Возможно, он ещё не запускал бота или заблокировал его."
        )
        await state.clear()
        return

    await refresh_admin_message(request_id)

    await message.answer(f"Ответ по обращению #{request_id} отправлен.")
    await state.clear()


@router.callback_query(F.data.startswith("close:"))
async def close_request_handler(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    request_id = int(callback.data.split(":")[1])
    request_data = await get_request(request_id)

    if not request_data:
        await callback.answer("Обращение не найдено", show_alert=True)
        return

    if request_data["status"] == "closed":
        await callback.answer("Уже закрыто", show_alert=True)
        return

    await close_request(request_id)
    await refresh_admin_message(request_id)

    try:
        await bot.send_message(
            request_data["student_id"],
            f"✅ Обращение <b>#{request_id}</b> закрыто."
        )
    except Exception:
        pass

    await callback.answer("Обращение закрыто")


# =========================
# MAIN
# =========================

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())