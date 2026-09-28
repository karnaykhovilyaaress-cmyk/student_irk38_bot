from dotenv import load_dotenv
load_dotenv()

import os
import sys
import shutil
import sqlite3
import logging
from datetime import datetime, timezone, timedelta

import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    FSInputFile
)

# ============================================================
# НАСТРОЙКИ
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6014557174"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@tainyi_politex")
DB_PATH = os.getenv("DB_PATH", "podslushano.db")

MAX_PER_HOUR = 5
MAX_TEXT_LEN = 4000

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не задан!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

pending_texts = {}


# ============================================================
# FSM
# ============================================================
class SubmitState(StatesGroup):
    waiting_text = State()


class FeedbackState(StatesGroup):
    waiting_text = State()


class FeedbackReplyState(StatesGroup):
    waiting_reply = State()


# ============================================================
# БАЗА
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        text TEXT,
        author_id INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        text TEXT,
        status TEXT DEFAULT 'new',
        created_at TEXT,
        answered_at TEXT
    )""")
    conn.commit(); conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_irk():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def add_post(text, author_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO posts (text, author_id, status, created_at) VALUES (?, ?, 'pending', ?)",
        (text, author_id, now_iso()))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return pid


def get_post_status(pid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_status(pid, status):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE posts SET status=? WHERE id=?", (status, pid))
    conn.commit(); conn.close()


def count_user_posts(user_id, hours=1):
    conn = sqlite3.connect(DB_PATH)
    threshold = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE author_id=? AND created_at>=?",
        (user_id, threshold)).fetchone()[0]
    conn.close()
    return n


# ---------- ОБРАТНАЯ СВЯЗЬ ----------
def add_feedback(user_id, username, text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO feedback (user_id, username, text, status, created_at) "
        "VALUES (?, ?, ?, 'new', ?)",
        (user_id, username, text, now_iso()))
    fid = cur.lastrowid
    conn.commit(); conn.close()
    return fid


def get_feedback(fid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, user_id, username, text, status FROM feedback WHERE id=?",
        (fid,)).fetchone()
    conn.close()
    return row


def set_feedback_status(fid, status):
    conn = sqlite3.connect(DB_PATH)
    if status == "answered":
        conn.execute("UPDATE feedback SET status=?, answered_at=? WHERE id=?",
                     (status, now_iso(), fid))
    else:
        conn.execute("UPDATE feedback SET status=? WHERE id=?", (status, fid))
    conn.commit(); conn.close()


# ---------- СТАТИСТИКА ДЛЯ БЭКАПА ----------
def count_total_posts():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    conn.close()
    return n


def count_total_feedback():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    conn.close()
    return n


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отправить сообщение")],
            [KeyboardButton(text="Обратная связь")],
            [KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True)


def get_admin_keyboard(post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать анонимно", callback_data=f"pub_{post_id}"),
            InlineKeyboardButton(text="Отменить", callback_data=f"cancel_{post_id}"),
        ],
    ])


def get_feedback_admin_keyboard(fid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Ответить", callback_data=f"fb_reply_{fid}"),
            InlineKeyboardButton(text="Закрыть", callback_data=f"fb_close_{fid}"),
        ],
    ])


# ============================================================
# СТАРТ
# ============================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Привет, админ!\n\n"
            "Сюда приходят анонимные сообщения и обратная связь.\n\n"
            "Анонимка: кнопки «Опубликовать анонимно» и «Отменить».\n"
            "Обратная связь: кнопки «Ответить» и «Закрыть».\n\n"
            "Команды:\n"
            "/backup — прислать файл базы\n"
            "/restore — восстановить базу (пришли .db с подписью /restore)",
            reply_markup=get_user_keyboard())
        return

    await message.answer(
        "Привет! Это бот канала «Тайный политех».\n\n"
        "Здесь можно:\n"
        "— отправить анонимное сообщение в канал\n"
        "— написать админу (обратная связь, жалоба, вопрос)",
        reply_markup=get_user_keyboard())


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur is None:
        await message.answer("Нечего отменять.", reply_markup=get_user_keyboard())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_user_keyboard())


@dp.message(F.text == "Помощь")
async def show_help(message: Message):
    await message.answer(
        "Как пользоваться ботом:\n\n"
        "«Отправить сообщение» — анонимка в канал @tainyi_politex.\n"
        "  Автор не публикуется.\n\n"
        "«Обратная связь» — написать админу лично.\n"
        "  Можно задать вопрос, пожаловаться, предложить идею.\n"
        "  Админ может ответить — ответ придёт сюда.\n\n"
        f"Лимит анонимок: не больше {MAX_PER_HOUR} в час.",
        reply_markup=get_user_keyboard())


# ============================================================
# БЭКАП И ВОССТАНОВЛЕНИЕ
# ============================================================
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Только для админа.")
        return
    try:
        if not os.path.exists(DB_PATH):
            await message.answer("Файл базы не найден.")
            return
        tmp_path = "/tmp/podslushano_backup.db"
        shutil.copyfile(DB_PATH, tmp_path)
        doc = FSInputFile(
            tmp_path,
            filename=f"podslushano_backup_{now_irk().strftime('%Y%m%d_%H%M')}.db"
        )
        await message.answer_document(
            doc,
            caption=(
                f"Резервная копия базы.\n\n"
                f"Анонимок всего: {count_total_posts()}\n"
                f"Обратной связи: {count_total_feedback()}\n"
                f"Дата: {now_irk().strftime('%d.%m.%Y %H:%M')}"
            )
        )
    except Exception as e:
        logging.error(f"[BACKUP] {e}")
        await message.answer(f"Ошибка бэкапа: {e}")


@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Только для админа.")
        return
    if not message.document:
        await message.answer(
            "Как восстановить базу:\n\n"
            "1. Прикрепи файл `.db` (тот, что получил командой /backup).\n"
            "2. В подписи к файлу напиши `/restore`.\n"
            "3. Отправь."
        )
        return
    fname = message.document.file_name or ""
    if not fname.lower().endswith(".db"):
        await message.answer("Нужен файл с расширением `.db`.")
        return
    try:
        file = await bot.get_file(message.document.file_id)
        await bot.download_file(file.file_path, DB_PATH)
        init_db()
        await message.answer(
            f"База восстановлена.\n\n"
            f"Анонимок всего: {count_total_posts()}\n"
            f"Обратной связи: {count_total_feedback()}"
        )
        logging.info("[RESTORE] база восстановлена админом")
    except Exception as e:
        logging.error(f"[RESTORE] {e}")
        await message.answer(f"Ошибка восстановления: {e}")


# ============================================================
# АНОНИМКА — ПРИЁМ
# ============================================================
@dp.message(F.text == "Отправить сообщение")
async def submit_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ты админ. Тебе не нужно отправлять анонимки.")
        return

    cur = await state.get_state()
    if cur == SubmitState.waiting_text.state:
        await message.answer("Ты уже пишешь сообщение. Напиши текст или /cancel.")
        return

    sent = count_user_posts(message.from_user.id, hours=1)
    if sent >= MAX_PER_HOUR:
        await message.answer(
            f"Ты отправил {sent} сообщений за час. "
            f"Лимит — {MAX_PER_HOUR}. Попробуй позже.",
            reply_markup=get_user_keyboard())
        return

    await message.answer(
        "Напиши своё сообщение одним текстом.\n\n"
        "Если передумаешь — /cancel."
    )
    await state.set_state(SubmitState.waiting_text)


@dp.message(SubmitState.waiting_text)
async def submit_receive(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Только текст. Попробуй ещё раз или /cancel.")
        return

    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком коротко. Напиши чуть подробнее.")
        return
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "..."

    sent = count_user_posts(message.from_user.id, hours=1)
    if sent >= MAX_PER_HOUR:
        await message.answer(
            f"Лимит — {MAX_PER_HOUR} сообщений в час. Попробуй позже.",
            reply_markup=get_user_keyboard())
        await state.clear()
        return

    try:
        pid = add_post(text, message.from_user.id)
    except Exception as e:
        logging.error(f"[DB] не смог сохранить пост: {e}")
        await message.answer("Не удалось сохранить. Попробуй позже.")
        await state.clear()
        return

    pending_texts[pid] = text

    admin_text = (
        f"Анонимка #{pid}\n\n"
        f"{text}\n\n"
        f"— от: {message.from_user.full_name} (id {message.from_user.id})\n"
        f"Время: {now_irk().strftime('%d.%m %H:%M')}"
    )

    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=get_admin_keyboard(pid))
    except Exception as e:
        logging.error(f"[ADMIN-NOTIFY] {e}")

    try:
        await message.answer(
            "Отправлено.\n\n"
            "Если админ одобрит — появится в канале @tainyi_politex.",
            reply_markup=get_user_keyboard())
    except Exception as e:
        logging.error(f"[USER-NOTIFY] {e}")

    await state.clear()


# ============================================================
# ОБРАТНАЯ СВЯЗЬ — ПРИЁМ
# ============================================================
@dp.message(F.text == "Обратная связь")
async def feedback_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ты админ. Тебе не нужно писать самому себе.")
        return

    cur = await state.get_state()
    if cur == FeedbackState.waiting_text.state:
        await message.answer("Ты уже пишешь сообщение. Напиши текст или /cancel.")
        return

    await message.answer(
        "Напиши сообщение админу.\n\n"
        "Можно задать вопрос, пожаловаться, предложить идею.\n"
        "Админ может ответить — ответ придёт сюда.\n\n"
        "/cancel — отменить."
    )
    await state.set_state(FeedbackState.waiting_text)


@dp.message(FeedbackState.waiting_text)
async def feedback_receive(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Только текст. Попробуй ещё раз или /cancel.")
        return

    text = message.text.strip()
    if len(text) < 3:
        await message.answer("Слишком коротко. Напиши чуть подробнее.")
        return
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "..."

    username = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name

    try:
        fid = add_feedback(message.from_user.id, username, text)
    except Exception as e:
        logging.error(f"[DB-FB] не смог сохранить обратную связь: {e}")
        await message.answer("Не удалось отправить. Попробуй позже.")
        await state.clear()
        return

    admin_text = (
        f"Обратная связь #{fid}\n\n"
        f"{text}\n\n"
        f"— от: {username} (id {message.from_user.id})\n"
        f"Время: {now_irk().strftime('%d.%m %H:%M')}"
    )

    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=get_feedback_admin_keyboard(fid))
    except Exception as e:
        logging.error(f"[ADMIN-FB] {e}")

    try:
        await message.answer(
            "Сообщение отправлено админу.\n\n"
            "Если он ответит — ответ придёт сюда.",
            reply_markup=get_user_keyboard())
    except Exception as e:
        logging.error(f"[USER-FB] {e}")

    await state.clear()


# ============================================================
# ОБРАТНАЯ СВЯЗЬ — ОТВЕТ АДМИНА
# ============================================================
@dp.callback_query(F.data.startswith("fb_reply_"))
async def fb_reply_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return

    try:
        fid = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка кнопки", show_alert=True)
        return

    row = get_feedback(fid)
    if not row:
        await callback.answer("Не найдено", show_alert=True)
        return
    if row[4] == "answered":
        await callback.answer("Уже отвечено", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if row[4] == "closed":
        await callback.answer("Уже закрыто", show_alert=True)
        return

    await state.update_data(feedback_id=fid, feedback_user=row[1])
    await callback.message.edit_text(
        f"Ответ на обратную связь #{fid}\n\n"
        f"От: {row[2]} (id {row[1]})\n"
        f"Сообщение:\n{row[3]}\n\n"
        f"Напиши ответ одним сообщением.\n\n/cancel"
    )
    await state.set_state(FeedbackReplyState.waiting_reply)
    await callback.answer()


@dp.message(FeedbackReplyState.waiting_reply)
async def fb_reply_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return

    reply = (message.text or "").strip()
    if not reply:
        await message.answer("Пусто. Напиши ответ или /cancel.")
        return
    if len(reply) > MAX_TEXT_LEN:
        reply = reply[:MAX_TEXT_LEN] + "..."

    data = await state.get_data()
    fid = data.get("feedback_id")
    user_id = data.get("feedback_user")
    if not fid or not user_id:
        await state.clear()
        await message.answer("Ошибка. Начни заново.")
        return

    try:
        await bot.send_message(
            user_id,
            f"Ответ администратора на обращение #{fid}:\n\n{reply}")
        set_feedback_status(fid, "answered")
        await state.clear()
        await message.answer(
            f"Ответ отправлен пользователю {user_id}.")
    except Exception as e:
        logging.error(f"[FB-REPLY] {e}")
        await message.answer(
            f"Не удалось отправить: {e}\n\n"
            f"Возможно, пользователь заблокировал бота.")


@dp.callback_query(F.data.startswith("fb_close_"))
async def fb_close(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return

    try:
        fid = int(callback.data.split("_")[-1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка кнопки", show_alert=True)
        return

    row = get_feedback(fid)
    if not row:
        await callback.answer("Не найдено", show_alert=True)
        return
    if row[4] == "answered":
        await callback.answer("Уже отвечено", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    set_feedback_status(fid, "closed")
    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n[ЗАКРЫТО]",
            reply_markup=None)
    except Exception:
        pass
    await callback.answer("Закрыто")


# ============================================================
# FALLBACK — ЛЮБОЙ ТЕКСТ
# ============================================================
@dp.message(F.text & ~F.text.startswith("/"))
async def fallback_text(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur is not None:
        return
    if message.text in ("Отправить сообщение", "Обратная связь", "Помощь"):
        return
    await message.answer(
        "Я принимаю только анонимки и обратную связь.\n\n"
        "Используй кнопки внизу или /help.",
        reply_markup=get_user_keyboard())


# ============================================================
# АНОНИМКА — МОДЕРАЦИЯ
# ============================================================
def extract_channel_text(pid, original_admin_text):
    if pid in pending_texts:
        return pending_texts[pid]
    try:
        body = original_admin_text.split("\n\n", 1)[1]
        body = body.rsplit("\n\n— от:", 1)[0]
        return body
    except Exception:
        return original_admin_text


@dp.callback_query(F.data.startswith("pub_"))
async def admin_publish(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return

    try:
        pid = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка кнопки", show_alert=True)
        return

    status = get_post_status(pid)
    if status == "published":
        await callback.answer("Уже опубликовано", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if status == "rejected":
        await callback.answer("Уже отменено", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    text = extract_channel_text(pid, callback.message.text or "")
    if not text:
        await callback.answer("Текст потерялся", show_alert=True)
        return

    try:
        await bot.send_message(CHANNEL_ID, text)
    except Exception as e:
        logging.error(f"[PUBLISH] {e}")
        await callback.answer(f"Ошибка публикации: {e}", show_alert=True)
        return

    set_status(pid, "published")
    pending_texts.pop(pid, None)

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n[ОПУБЛИКОВАНО]",
            reply_markup=None)
    except Exception as e:
        logging.warning(f"[EDIT] {e}")

    await callback.answer("Опубликовано")


@dp.callback_query(F.data.startswith("cancel_"))
async def admin_cancel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return

    try:
        pid = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        await callback.answer("Ошибка кнопки", show_alert=True)
        return

    status = get_post_status(pid)
    if status == "published":
        await callback.answer("Уже опубликовано — отменить нельзя", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if status == "rejected":
        await callback.answer("Уже отменено", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    set_status(pid, "rejected")
    pending_texts.pop(pid, None)

    try:
        await callback.message.edit_text(
            (callback.message.text or "") + "\n\n[ОТМЕНЕНО]",
            reply_markup=None)
    except Exception as e:
        logging.warning(f"[EDIT] {e}")

    await callback.answer("Отменено")


# ============================================================
# FALLBACK — ЛЮБОЙ CALLBACK
# ============================================================
@dp.callback_query()
async def fallback_callback(callback: CallbackQuery):
    await callback.answer()


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout, force=True)
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Тайный политех: запуск")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
