from dotenv import load_dotenv
load_dotenv()

import os
import re
import sys
import json
import random
import shutil
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

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
BOT_USERNAME = os.getenv("BOT_USERNAME", "tainyi_politex_bot")
DB_PATH = os.getenv("DB_PATH", "podslushano.db")

MAX_LINKS = 3
MAX_PER_HOUR = 3
MAX_PER_DAY = 10
REJECTS_TO_NOTIFY = 5
POST_INTERVAL_HOURS = 2
POST_WORK_START = 9
POST_WORK_END = 21
POST_MAX_PER_DAY = 7
QUOTE_HOUR = 12

if not BOT_TOKEN:
    logging.error("BOT_TOKEN не задан!")
    sys.exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# id постов, для которых уже запрошено подтверждение /draw
_pending_draw = {"confirm": False}

STOP_WORDS = [
    "кредит", "займ", "заработок", "казино", "ставки", "букмекер",
    "наркотик", "мефедрон", "соль", "спайс", "закладк",
    "проститут", "интим", "18+",
    "крипт", "биткоин", "инвест", "пассивный доход",
]

DAILY_QUESTIONS = [
    "Что бесит в Политехе больше всего?",
    "Самый странный препод за всю историю?",
    "Лучшая столовая в кампусе?",
    "Самая бесполезная пара в этом семестре?",
    "Что бы ты изменил в Политехе, если бы был ректором?",
    "Твой самый провальный экзамен?",
    "Самое странное, что ты слышал на лекции?",
    "Где в Политехе можно нормально поесть за 150 рублей?",
    "Какая пара начинается слишком рано?",
    "Самый добрый препод?",
    "Что нужно сдать, но ты всё откладываешь?",
    "Твой главный лайфхак для сессии?",
    "Что бесит в общаге?",
    "Какая аудитория самая холодная?",
    "Самый смешной случай на паре?",
    "Куда идти после пар в пятницу?",
    "Что помогает не уснуть на лекции?",
    "Твой самый нелюбимый предмет?",
    "Что бы ты сказал себе на первом курсе?",
    "Какую профессию хочешь после выпуска?",
    "Что делать, если проспал пару?",
    "Самое странное объявление, которое ты видел в кампусе?",
    "Какой корпус самый запутанный?",
    "Твой топ-1 совет первокурсникам?",
    "Что бесит в расписании?",
    "Самый бесполезный предмет?",
    "Как готовишься к зачёту за ночь?",
    "Самая дорогая еда в кампусе?",
    "Кого из преподов запомнил навсегда?",
    "О чём мечтаешь после сессии?",
]


# ============================================================
# FSM
# ============================================================
class SubmitState(StatesGroup):
    waiting_category = State()
    waiting_text = State()
    waiting_edit = State()
    waiting_answer = State()
    waiting_broadcast = State()


# ============================================================
# БАЗА ДАННЫХ
# ============================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT,
        text TEXT,
        author_id INTEGER,
        author_name TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT,
        published_at TEXT,
        published_msg_id INTEGER,
        reject_count INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS queue (
        post_id INTEGER PRIMARY KEY,
        added_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS spam_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        kind TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rejects (
        user_id INTEGER PRIMARY KEY,
        count INTEGER DEFAULT 0,
        notified INTEGER DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS stats (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS banned (
        user_id INTEGER PRIMARY KEY,
        banned_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS giveaway (
        user_id INTEGER PRIMARY KEY,
        joined_at TEXT
    )""")
    conn.commit(); conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def now_irk():
    return datetime.now(timezone.utc) + timedelta(hours=8)


# ---------- БАНЫ ----------
def is_banned(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def ban_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO banned (user_id, banned_at) VALUES (?, ?)",
                 (user_id, now_iso()))
    conn.commit(); conn.close()


def unban_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM banned WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()


# ---------- СПАМ-ЛОГ ----------
def log_spam(user_id, kind):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO spam_log (user_id, kind, created_at) VALUES (?, ?, ?)",
                 (user_id, kind, now_iso()))
    conn.commit(); conn.close()


def count_user_posts(user_id, hours=None):
    conn = sqlite3.connect(DB_PATH)
    if hours:
        threshold = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    else:
        threshold = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    n = conn.execute("SELECT COUNT(*) FROM posts WHERE author_id=? AND created_at>=?",
                     (user_id, threshold)).fetchone()[0]
    conn.close()
    return n


# ---------- ПОСТЫ ----------
def add_post(category, text, author_id, author_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO posts (category, text, author_id, author_name, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (category, text, author_id, author_name, now_iso()))
    pid = cur.lastrowid
    conn.commit(); conn.close()
    return pid


def get_post(post_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, category, text, author_id, author_name, status, published_msg_id "
        "FROM posts WHERE id=?", (post_id,)).fetchone()
    conn.close()
    return row


def get_pending_posts(limit=30):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, category, text, author_id, author_name FROM posts "
        "WHERE status='pending' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


def set_post_text(post_id, new_text):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE posts SET text=? WHERE id=?", (new_text, post_id))
    conn.commit(); conn.close()


def set_post_status(post_id, status, published_msg_id=None):
    conn = sqlite3.connect(DB_PATH)
    if published_msg_id is not None:
        conn.execute("UPDATE posts SET status=?, published_at=?, published_msg_id=? WHERE id=?",
                     (status, now_iso(), published_msg_id, post_id))
    else:
        conn.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))
    conn.commit(); conn.close()


# ---------- ОТКЛОНЕНИЯ ----------
def inc_reject(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT count FROM rejects WHERE user_id=?", (user_id,)).fetchone()
    if row:
        new_count = row[0] + 1
        conn.execute("UPDATE rejects SET count=?, notified=0 WHERE user_id=?",
                     (new_count, user_id))
    else:
        new_count = 1
        conn.execute("INSERT INTO rejects (user_id, count, notified) VALUES (?, ?, 0)",
                     (user_id, new_count))
    conn.commit(); conn.close()
    return new_count


def reset_rejects(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM rejects WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()


def mark_reject_notified(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE rejects SET notified=1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()


def was_reject_notified(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT notified FROM rejects WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return bool(row and row[0])


# ---------- ОЧЕРЕДЬ ----------
def add_to_queue(post_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO queue (post_id, added_at) VALUES (?, ?)",
                 (post_id, now_iso()))
    conn.commit(); conn.close()


def pop_next_from_queue():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT post_id FROM queue ORDER BY added_at ASC LIMIT 1").fetchone()
    if not row:
        conn.close()
        return None
    pid = row[0]
    conn.execute("DELETE FROM queue WHERE post_id=?", (pid,))
    conn.commit(); conn.close()
    return pid


def queue_size():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM queue").fetchone()[0]
    conn.close()
    return n


def get_queue_list(limit=20):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT p.id, p.category, p.text FROM queue q JOIN posts p ON p.id=q.post_id "
        "ORDER BY q.added_at ASC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows


# ---------- СТАТИСТИКА ----------
def stat_get(key, default=0):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM stats WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row[0])
    except ValueError:
        return default


def stat_set(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO stats (key, value) VALUES (?, ?)",
                 (key, str(value)))
    conn.commit(); conn.close()


def stat_inc(key, by=1):
    cur = stat_get(key, 0)
    stat_set(key, cur + by)


def published_today_count():
    conn = sqlite3.connect(DB_PATH)
    today_irk = now_irk().strftime("%Y-%m-%d")
    threshold = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = conn.execute(
        "SELECT published_at FROM posts WHERE status='published' AND published_at>=?",
        (threshold,)).fetchall()
    conn.close()
    n = 0
    for (pub,) in rows:
        try:
            pub_dt = datetime.fromisoformat(pub) + timedelta(hours=8)
            if pub_dt.strftime("%Y-%m-%d") == today_irk:
                n += 1
        except Exception:
            pass
    return n


# ---------- РОЗЫГРЫШ ----------
def add_giveaway_participant(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO giveaway (user_id, joined_at) VALUES (?, ?)",
                 (user_id, now_iso()))
    conn.commit(); conn.close()


def is_giveaway_participant(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT 1 FROM giveaway WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def get_giveaway_participants():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT user_id FROM giveaway").fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_giveaway_count():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM giveaway").fetchone()[0]
    conn.close()
    return n


def clear_giveaway():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM giveaway")
    conn.commit(); conn.close()


# ============================================================
# ФИЛЬТРЫ
# ============================================================
URL_RE = re.compile(r"https?://\S+|t\.me/\S+|@\w+", re.IGNORECASE)


def count_links(text):
    return len(URL_RE.findall(text))


def find_stop_word(text):
    low = text.lower()
    for w in STOP_WORDS:
        if w in low:
            return w
    return None


def validate_text(text):
    if len(text) < 5:
        return False, "Слишком коротко. Напиши чуть подробнее."
    if len(text) > 2000:
        return False, "Слишком длинно (макс. 2000 символов)."
    if count_links(text) > MAX_LINKS:
        return False, "Слишком много ссылок. Убери лишние и попробуй снова."
    sw = find_stop_word(text)
    if sw:
        return False, "Сообщение не прошло фильтр. Переформулируй без спама и запрещённых тем."
    return True, ""


def check_limits(user_id):
    hour_count = count_user_posts(user_id, hours=1)
    day_count = count_user_posts(user_id, hours=24)
    if hour_count >= MAX_PER_HOUR:
        return False, f"Ты отправил {hour_count} сообщений за час. Попробуй позже."
    if day_count >= MAX_PER_DAY:
        return False, f"Дневной лимит — {MAX_PER_DAY} сообщений. Возвращайся завтра."
    return True, ""


# ============================================================
# КЛАВИАТУРЫ
# ============================================================
CATEGORIES = {
    "q": ("вопрос", "Вопрос к студентам"),
    "s": ("история", "Анонимная история"),
    "b": ("барахолка", "Барахолка (продам/куплю)"),
    "d": ("знакомства", "Знакомства и компания"),
    "l": ("слух", "Слух (непроверенное)"),
}


def get_user_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Отправить анонимно")],
            [KeyboardButton(text="Правила"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True)


def get_categories_keyboard():
    buttons = []
    for code, (tag, label) in CATEGORIES.items():
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"cat_{code}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_keyboard(post_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Опубликовать", callback_data=f"pub_now_{post_id}"),
            InlineKeyboardButton(text="В очередь", callback_data=f"pub_q_{post_id}"),
        ],
        [
            InlineKeyboardButton(text="Редактировать", callback_data=f"edit_{post_id}"),
            InlineKeyboardButton(text="Отклонить", callback_data=f"rej_{post_id}"),
        ],
        [
            InlineKeyboardButton(text="Ответить от имени канала", callback_data=f"ans_{post_id}"),
        ],
    ])


def get_admin_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="Очередь", callback_data="admin_queue")],
        [InlineKeyboardButton(text="Ожидают модерации", callback_data="admin_pending")],
        [InlineKeyboardButton(text="Розыгрыш — участники", callback_data="giveaway_stats")],
        [InlineKeyboardButton(text="Опубликовать вопрос дня", callback_data="daily_q_now")],
    ])


# ============================================================
# СТАРТ
# ============================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    args = message.text.split(maxsplit=1)
    payload = args[1].strip() if len(args) > 1 else ""

    if is_banned(message.from_user.id):
        await message.answer("Ты забанен и не можешь отправлять сообщения.")
        return

    if payload == "giveaway":
        if message.from_user.id == ADMIN_ID:
            await message.answer(
                "Ты админ — тебе не нужно участвовать в розыгрыше.\n"
                "Админ-панель:",
                reply_markup=get_admin_main_keyboard())
            return
        already = is_giveaway_participant(message.from_user.id)
        add_giveaway_participant(message.from_user.id)
        if already:
            await message.answer(
                "Ты уже участвуешь в розыгрыше.\n\n"
                "Следи за каналом @tainyi_politex — победителя выберем случайно.",
                reply_markup=get_user_keyboard())
        else:
            await message.answer(
                "Ты участвуешь в розыгрыше!\n\n"
                "Победителя выберем случайно. Следи за каналом @tainyi_politex.\n\n"
                "Хочешь отправить анонимку? Жми «Отправить анонимно».",
                reply_markup=get_user_keyboard())
        return

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Привет, админ!\n\n"
            "Команды:\n"
            "/admin — админ-панель\n"
            "/pending — посты, ожидающие модерации\n"
            "/queue — очередь на публикацию\n"
            "/backup — прислать файл базы\n"
            "/restore — восстановить базу (пришли .db с подписью /restore)\n"
            "/draw — провести розыгрыш\n"
            "/ban <id> — забанить пользователя\n"
            "/unban <id> — разбанить\n"
            "/post <текст> — опубликовать текст в канал\n"
            "/question — опубликовать вопрос дня\n\n"
            "Админ-панель:",
            reply_markup=get_admin_main_keyboard())
        return

    await message.answer(
        "Привет! Это бот канала «Тайный политех».\n\n"
        "Здесь можно анонимно отправить:\n"
        "— вопрос к студентам (#вопрос)\n"
        "— историю (#история)\n"
        "— барахолку (#барахолка)\n"
        "— знакомства (#знакомства)\n"
        "— слух (#слух)\n\n"
        "Твоё имя и username нигде не публикуются.\n"
        "Нажми «Отправить анонимно», чтобы начать.",
        reply_markup=get_user_keyboard())


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Только для админа.")
        return
    await message.answer(
        f"Админ-панель\n\n"
        f"Опубликовано всего: {stat_get('published_total', 0)}\n"
        f"Сегодня: {published_today_count()}\n"
        f"В очереди: {queue_size()}\n"
        f"Участников розыгрыша: {get_giveaway_count()}\n\n"
        f"Команды:\n"
        f"/pending — посты на модерации\n"
        f"/queue — очередь\n"
        f"/backup — бэкап\n"
        f"/restore — восстановление\n"
        f"/draw — розыгрыш\n"
        f"/ban <id> — забанить\n"
        f"/unban <id> — разбанить\n"
        f"/post <текст> — пост в канал\n"
        f"/question — вопрос дня",
        reply_markup=get_admin_main_keyboard())


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur is None:
        await message.answer("Нечего отменять.", reply_markup=get_user_keyboard())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_user_keyboard())


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
                f"Опубликовано всего: {stat_get('published_total', 0)}\n"
                f"В очереди: {queue_size()}\n"
                f"Участников розыгрыша: {get_giveaway_count()}\n"
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
            f"Опубликовано всего: {stat_get('published_total', 0)}\n"
            f"В очереди: {queue_size()}"
        )
        logging.info("[RESTORE] база восстановлена админом")
    except Exception as e:
        logging.error(f"[RESTORE] {e}")
        await message.answer(f"Ошибка восстановления: {e}")


# ============================================================
# ПРАВИЛА / ПОМОЩЬ
# ============================================================
@dp.message(F.text == "Правила")
async def show_rules(message: Message):
    await message.answer(
        "Правила канала «Тайный политех»\n\n"
        "1. Без имён, @username и ссылок на личные профили.\n"
        "2. Без травли, угроз и оскорблений.\n"
        "3. Без спама, рекламы, кредитов, казино, 18+.\n"
        "4. Без личных данных третьих лиц (номера, адреса).\n"
        "5. Всё, что нарушает правила, не публикуется.\n\n"
        "Лимиты: 3 сообщения в час, 10 в сутки.",
        reply_markup=get_user_keyboard())


@dp.message(F.text == "Помощь")
async def show_help(message: Message):
    await message.answer(
        "Как отправить анонимку:\n\n"
        "1. Жми «Отправить анонимно».\n"
        "2. Выбери категорию.\n"
        "3. Напиши текст одним сообщением.\n"
        "4. Дождись модерации.\n\n"
        "Если админ одобрит — появится в канале @tainyi_politex.\n\n"
        "Автор остаётся полностью анонимным.",
        reply_markup=get_user_keyboard())


# ============================================================
# ПРИЁМ АНОНИМКИ
# ============================================================
@dp.message(F.text == "Отправить анонимно")
async def submit_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Ты админ. Используй /admin для панели.")
        return
    if is_banned(message.from_user.id):
        await message.answer("Ты забанен.")
        return
    ok, err = check_limits(message.from_user.id)
    if not ok:
        await message.answer(err)
        return
    await message.answer("Выбери категорию:", reply_markup=get_categories_keyboard())
    await state.set_state(SubmitState.waiting_category)


@dp.callback_query(SubmitState.waiting_category, F.data == "cancel")
async def cancel_submit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@dp.callback_query(SubmitState.waiting_category, F.data.startswith("cat_"))
async def choose_category(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split("_")[1]
    if code not in CATEGORIES:
        await callback.answer("Неизвестная категория")
        return
    tag, label = CATEGORIES[code]
    await state.update_data(category=tag, category_label=label)
    await callback.message.edit_text(
        f"Категория: {label}\n\n"
        f"Теперь напиши текст одним сообщением.\n\n"
        f"Если передумаешь — /cancel."
    )
    await state.set_state(SubmitState.waiting_text)
    await callback.answer()


@dp.message(SubmitState.waiting_text)
async def submit_receive(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Только текст. Попробуй ещё раз или /cancel.")
        return

    text = message.text.strip()
    ok, err = validate_text(text)
    if not ok:
        log_spam(message.from_user.id, "filter")
        await message.answer(err)
        return

    ok, err = check_limits(message.from_user.id)
    if not ok:
        log_spam(message.from_user.id, "limit")
        await message.answer(err)
        return

    data = await state.get_data()
    category = data.get("category", "вопрос")
    category_label = data.get("category_label", "Вопрос")

    pid = add_post(category, text, message.from_user.id, message.from_user.full_name)

    admin_text = (
        f"Анонимка #{pid} [{category_label}]\n\n"
        f"{text}\n\n"
        f"— от: {message.from_user.full_name} (id {message.from_user.id})\n"
        f"Отправлено: {now_irk().strftime('%d.%m %H:%M')}"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=get_admin_keyboard(pid))
        await message.answer(
            "Отправлено на модерацию.\n\n"
            "Если админ одобрит — появится в канале @tainyi_politex.",
            reply_markup=get_user_keyboard())
    except Exception as e:
        logging.error(f"[SUBMIT] {e}")
        await message.answer("Не удалось отправить. Попробуй позже.")

    await state.clear()


# ============================================================
# МОДЕРАЦИЯ
# ============================================================
def format_channel_post(category, text, post_id):
    return f"{text}\n\n#{category}  ·  №{post_id}"


async def publish_post(post_id):
    row = get_post(post_id)
    if not row:
        return None, "Пост не найден"
    pid, category, text, author_id, author_name, status, _ = row
    if status == "published":
        return None, "Уже опубликован"

    final_text = format_channel_post(category, text, pid)
    try:
        msg = await bot.send_message(CHANNEL_ID, final_text)
    except Exception as e:
        logging.error(f"[PUBLISH] {e}")
        return None, f"Ошибка публикации: {e}"

    set_post_status(pid, "published", msg.message_id)
    stat_inc("published_total")
    reset_rejects(author_id)
    return msg.message_id, None


@dp.callback_query(F.data.startswith("pub_now_"))
async def admin_pub_now(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return
    pid = int(callback.data.split("_")[2])
    msg_id, err = await publish_post(pid)
    if err:
        await callback.answer(err, show_alert=True)
        return
    await callback.message.edit_text(callback.message.text + "\n\n[ОПУБЛИКОВАНО СРАЗУ]")
    await callback.answer("Опубликовано")


@dp.callback_query(F.data.startswith("pub_q_"))
async def admin_pub_queue(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return
    pid = int(callback.data.split("_")[2])
    row = get_post(pid)
    if not row or row[5] == "published":
        await callback.answer("Уже обработано", show_alert=True)
        return
    add_to_queue(pid)
    set_post_status(pid, "queued")
    await callback.message.edit_text(
        callback.message.text + f"\n\n[В ОЧЕРЕДИ #{queue_size()}]")
    await callback.answer("Добавлено в очередь")


@dp.callback_query(F.data.startswith("edit_"))
async def admin_edit(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return
    pid = int(callback.data.split("_")[1])
    row = get_post(pid)
    if not row:
        await callback.answer("Не найдено")
        return
    await state.update_data(edit_post_id=pid)
    await callback.message.edit_text(
        f"Редактирование #{pid}\n\n"
        f"Текущий текст:\n{row[2]}\n\n"
        f"Пришли новый текст одним сообщением.\n\n/cancel"
    )
    await state.set_state(SubmitState.waiting_edit)
    await callback.answer()


@dp.message(SubmitState.waiting_edit)
async def admin_edit_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("Пусто.")
        return
    data = await state.get_data()
    pid = data.get("edit_post_id")
    if not pid:
        await state.clear()
        await message.answer("Ошибка. Начни заново.")
        return
    set_post_text(pid, new_text)
    await state.clear()
    await message.answer(
        f"Текст #{pid} обновлён.\n\nОпубликовать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Опубликовать сейчас", callback_data=f"pub_now_{pid}")],
            [InlineKeyboardButton(text="В очередь", callback_data=f"pub_q_{pid}")],
            [InlineKeyboardButton(text="Отклонить", callback_data=f"rej_{pid}")],
        ]))


@dp.callback_query(F.data.startswith("rej_"))
async def admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return
    pid = int(callback.data.split("_")[1])
    row = get_post(pid)
    if not row:
        await callback.answer("Не найдено")
        return
    if row[5] == "published":
        await callback.answer("Уже опубликовано")
        return
    set_post_status(pid, "rejected")
    author_id = row[3]
    author_name = row[4]
    cnt = inc_reject(author_id)

    await callback.message.edit_text(callback.message.text + "\n\n[ОТКЛОНЕНО]")
    await callback.answer("Отклонено")

    try:
        await bot.send_message(
            author_id,
            "Твоё сообщение не прошло модерацию.\n"
            "Попробуй переформулировать или отправь другое.",
            reply_markup=get_user_keyboard())
    except Exception as e:
        logging.error(f"[REJ-NOTIFY] {e}")

    if cnt >= REJECTS_TO_NOTIFY and not was_reject_notified(author_id):
        mark_reject_notified(author_id)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"У пользователя {author_name} (id {author_id}) "
                f"{cnt} отклонённых постов подряд.\n\nЗабанить?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Забанить", callback_data=f"banid_{author_id}")],
                    [InlineKeyboardButton(text="Не банить", callback_data="noop")],
                ]))
        except Exception as e:
            logging.error(f"[REJ-ADMIN-NOTIFY] {e}")


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("banid_"))
async def ban_by_id_cb(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    uid = int(callback.data.split("_")[1])
    ban_user(uid)
    await callback.message.edit_text(callback.message.text + f"\n\n[ЗАБАНЕН {uid}]")
    await callback.answer("Забанен")


@dp.callback_query(F.data.startswith("ans_"))
async def admin_answer_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Не для тебя", show_alert=True)
        return
    pid = int(callback.data.split("_")[1])
    row = get_post(pid)
    if not row:
        await callback.answer("Не найдено")
        return
    await state.update_data(answer_to_post=pid)
    await callback.message.edit_text(
        f"Ответ на #{pid}\n\n"
        f"Оригинал:\n{row[2]}\n\n"
        f"Пришли текст ответа — он будет опубликован в канале со ссылкой на #{pid}.\n\n/cancel"
    )
    await state.set_state(SubmitState.waiting_answer)
    await callback.answer()


@dp.message(SubmitState.waiting_answer)
async def admin_answer_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пусто.")
        return
    data = await state.get_data()
    pid = data.get("answer_to_post")
    if not pid:
        await state.clear()
        await message.answer("Ошибка.")
        return
    try:
        await bot.send_message(CHANNEL_ID, f"{text}\n\n#{pid}  ·  ответ")
    except Exception as e:
        await message.answer(f"Не удалось: {e}")
        await state.clear()
        return
    await state.clear()
    await message.answer("Ответ опубликован.", reply_markup=get_admin_main_keyboard())


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    total_published = stat_get("published_total", 0)
    q_size = queue_size()
    today = published_today_count()
    conn = sqlite3.connect(DB_PATH)
    total_pending = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE status='pending'").fetchone()[0]
    total_rejected = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE status='rejected'").fetchone()[0]
    total_all = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    banned_count = conn.execute("SELECT COUNT(*) FROM banned").fetchone()[0]
    conn.close()

    text = (
        f"Статистика «Тайного политеха»\n\n"
        f"Всего получено анонимок: {total_all}\n"
        f"Опубликовано: {total_published}\n"
        f"  из них сегодня: {today}\n"
        f"В ожидании модерации: {total_pending}\n"
        f"В очереди на публикацию: {q_size}\n"
        f"Отклонено: {total_rejected}\n"
        f"Забанено: {banned_count}\n"
        f"Участников розыгрыша: {get_giveaway_count()}\n"
    )
    await callback.message.answer(text)
    await callback.answer()


@dp.callback_query(F.data == "admin_queue")
async def admin_queue(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    rows = get_queue_list(limit=20)
    if not rows:
        await callback.message.answer("Очередь пуста.")
        await callback.answer()
        return
    lines = [f"Очередь: {len(rows)} постов\n"]
    for pid, cat, txt in rows:
        short = txt[:80] + ("..." if len(txt) > 80 else "")
        lines.append(f"#{pid} [{cat}] {short}")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data == "admin_pending")
async def admin_pending(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    rows = get_pending_posts(limit=30)
    if not rows:
        await callback.message.answer("Нет постов, ожидающих модерации.")
        await callback.answer()
        return
    lines = [f"Ожидают модерации: {len(rows)}\n"]
    for pid, cat, txt, uid, uname in rows:
        short = txt[:80] + ("..." if len(txt) > 80 else "")
        lines.append(f"#{pid} [{cat}] {short}")
    lines.append("")
    lines.append("Открывай каждую в чате и жми «Опубликовать / В очередь / Отклонить».")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


# ============================================================
# РОЗЫГРЫШ
# ============================================================
@dp.callback_query(F.data == "giveaway_stats")
async def giveaway_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    count = get_giveaway_count()
    if count == 0:
        await callback.message.answer(
            "Участников пока нет.\n\n"
            "Как запустить розыгрыш:\n"
            "1. Опубликуй в канале анонс с ссылкой:\n"
            f"   t.me/{BOT_USERNAME}?start=giveaway\n"
            "2. Люди жмут ссылку → попадают в бота → автоматически записываются.\n"
            "3. Когда решишь — жми /draw, бот выберет случайного.\n\n"
            "После /draw список участников очищается."
        )
        await callback.answer()
        return
    participants = get_giveaway_participants()
    lines = [f"Участников: {count}\n"]
    for uid in participants[:30]:
        lines.append(f"  • {uid}")
    if count > 30:
        lines.append(f"  ... и ещё {count - 30}")
    lines.append("")
    lines.append("Когда готов — /draw")
    await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.message(Command("draw"))
async def cmd_draw(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    participants = get_giveaway_participants()
    if not participants:
        await message.answer(
            "Никто не участвует.\n\n"
            f"Опубликуй в канале ссылку t.me/{BOT_USERNAME}?start=giveaway"
        )
        return

    count = len(participants)
    await message.answer(
        f"Подтверди розыгрыш.\n\n"
        f"Участников: {count}\n"
        f"Будет выбран один случайный, ему придёт уведомление, "
        f"а результат опубликуется в канал.\n\n"
        f"После розыгрыша список участников очистится.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Провести розыгрыш", callback_data="draw_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="draw_cancel")],
        ]))


@dp.callback_query(F.data == "draw_cancel")
async def draw_cancel(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await callback.message.edit_text("Розыгрыш отменён.")
    await callback.answer()


@dp.callback_query(F.data == "draw_confirm")
async def draw_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    participants = get_giveaway_participants()
    if not participants:
        await callback.answer("Никто не участвует", show_alert=True)
        return

    winner = random.choice(participants)
    count = len(participants)

    await callback.message.edit_text(
        f"Победитель: {winner}\n\n"
        f"Всего участников: {count}\n\n"
        f"Список участников очищен — можно запускать новый розыгрыш."
    )
    await callback.answer("Розыгрыш проведён")

    try:
        await bot.send_message(
            winner,
            "Поздравляем! Ты выиграл розыгрыш «Тайного политеха».\n\n"
            "Напиши администратору, чтобы забрать приз."
        )
    except Exception as e:
        logging.error(f"[DRAW-NOTIFY] {e}")

    try:
        await bot.send_message(
            CHANNEL_ID,
            f"Розыгрыш завершён!\n\n"
            f"Победитель выбран среди {count} участников.\n"
            f"Поздравляем!"
        )
    except Exception as e:
        logging.error(f"[DRAW-CHANNEL] {e}")

    clear_giveaway()


# ============================================================
# БЫСТРЫЕ КОМАНДЫ
# ============================================================
@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = get_pending_posts(limit=30)
    if not rows:
        await message.answer("Нет постов, ожидающих модерации.")
        return
    lines = [f"Ожидают модерации: {len(rows)}\n"]
    for pid, cat, txt, uid, uname in rows:
        short = txt[:80] + ("..." if len(txt) > 80 else "")
        lines.append(f"#{pid} [{cat}] {short}")
    await message.answer("\n".join(lines))


@dp.message(Command("queue"))
async def cmd_queue(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = get_queue_list(limit=20)
    if not rows:
        await message.answer("Очередь пуста.")
        return
    lines = [f"Очередь: {len(rows)} постов\n"]
    for pid, cat, txt in rows:
        short = txt[:80] + ("..." if len(txt) > 80 else "")
        lines.append(f"#{pid} [{cat}] {short}")
    await message.answer("\n".join(lines))


@dp.message(Command("ban"))
async def cmd_ban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban <user_id>")
        return
    uid = int(parts[1])
    ban_user(uid)
    await message.answer(f"Пользователь {uid} забанен.")


@dp.message(Command("unban"))
async def cmd_unban(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban <user_id>")
        return
    uid = int(parts[1])
    unban_user(uid)
    await message.answer(f"Пользователь {uid} разбанен.")


@dp.message(Command("post"))
async def cmd_post(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.replace("/post", "", 1).strip()
    if not text:
        await message.answer("Использование: /post Текст")
        return
    try:
        await bot.send_message(CHANNEL_ID, text)
        await message.answer("Опубликовано.")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.message(Command("question"))
async def cmd_question(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    q = random.choice(DAILY_QUESTIONS)
    try:
        await bot.send_message(CHANNEL_ID, f"Вопрос дня:\n\n{q}\n\n#вопрос")
        await message.answer(f"Опубликовано:\n{q}")
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@dp.callback_query(F.data == "daily_q_now")
async def daily_q_now(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    q = random.choice(DAILY_QUESTIONS)
    try:
        await bot.send_message(CHANNEL_ID, f"Вопрос дня:\n\n{q}\n\n#вопрос")
        await callback.message.answer(f"Опубликовано:\n{q}")
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")
    await callback.answer()


# ============================================================
# ФОНОВЫЕ ЗАДАЧИ
# ============================================================
async def queue_loop():
    while True:
        try:
            now = now_irk()
            if POST_WORK_START <= now.hour < POST_WORK_END:
                if published_today_count() < POST_MAX_PER_DAY:
                    pid = pop_next_from_queue()
                    if pid:
                        msg_id, err = await publish_post(pid)
                        if err:
                            logging.error(f"[QUEUE] {err}")
                        else:
                            logging.info(f"[QUEUE] опубликован #{pid}")
        except Exception as e:
            logging.exception(f"[QUEUE] {e}")
        await asyncio.sleep(POST_INTERVAL_HOURS * 3600)


async def daily_q_loop():
    last_date = None
    while True:
        try:
            now = now_irk()
            cur_date = now.strftime("%Y-%m-%d")
            if now.hour == QUOTE_HOUR and now.minute < 10 and cur_date != last_date:
                q = random.choice(DAILY_QUESTIONS)
                try:
                    await bot.send_message(CHANNEL_ID, f"Вопрос дня:\n\n{q}\n\n#вопрос")
                    last_date = cur_date
                except Exception as e:
                    logging.error(f"[DAILY-Q] {e}")
        except Exception as e:
            logging.exception(f"[DAILY-Q] {e}")
        await asyncio.sleep(60)


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
    asyncio.create_task(queue_loop())
    asyncio.create_task(daily_q_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
