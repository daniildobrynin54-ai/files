"""
Telegram-бот для бронирования временных слотов в чате.

Бот реагирует ТОЛЬКО на слова-триггеры (точное совпадение текста сообщения,
без команд со слешем):

  бронь                — создать бронь (выбор даты: сегодня/завтра/послезавтра,
                          затем времени через кнопки)
  бронь HH:MM-HH:MM    — забронировать время сразу (на сегодня)
  брони                — показать расписание (все брони) на сегодня картинкой
  брони завтра         — показать расписание на завтра картинкой
  мои брони            — список своих бронирований на сегодня
  отмена брони         — выбор даты (сегодня/завтра/послезавтра), затем список
                          своих броней на эту дату с кнопками отмены
  отмена броней        — то же самое
  отмена HH:MM-HH:MM   — отменить конкретную свою бронь на сегодня
  ник <имя>            — задать свой ник
  id                   — показать chat_id и message_thread_id текущей темы

Правила длительности брони:
  • с 8:00 до 24:00 — от 30 до 120 минут;
  • с 00:00 до 8:00 (ночь) — без ограничения сверху, но строго больше часа.

Не больше 2 бронирований на один день на одного человека.
Бронировать и отменять брони можно на сегодня, завтра и послезавтра.

Запуск: см. README.md
"""

import os
import io
import re
import sqlite3
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from PIL import Image, ImageDraw, ImageFont

# ====================== НАСТРОЙКИ ======================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8906083939:AAGk-ppgkXh1av6ze1VTJuh0atWC1nkQZmg")
TIMEZONE = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Europe/Sofia"))
DB_PATH = os.environ.get("BOT_DB_PATH", "bookings.db")
ALLOWED_CHAT_ID = int(os.environ.get("ALLOWED_CHAT_ID", "-1002234810541"))
ALLOWED_THREAD_ID = os.environ.get("ALLOWED_THREAD_ID")
ALLOWED_THREAD_ID = int(ALLOWED_THREAD_ID) if ALLOWED_THREAD_ID else None
TIME_STEP_MINUTES = 30

# Дневное окно: с 8:00 до 24:00 — длительность брони от 30 до 120 минут.
DAY_START_MIN = 8 * 60          # 480  (08:00)
DAY_END_MIN = 24 * 60           # 1440 (24:00)
DAY_MIN_DURATION = 30
DAY_MAX_DURATION = 120

# Ночное окно: с 00:00 до 8:00 — без ограничения сверху, но больше часа за раз.
NIGHT_MIN_DURATION = 61         # строго больше 60 минут
NIGHT_MAX_DURATION = None       # без ограничения

# Максимум бронирований на один день для одного человека.
MAX_BOOKINGS_PER_DAY = 2


def is_night_start(start_min: int) -> bool:
    return start_min < DAY_START_MIN


def min_duration_for_start(start_min: int) -> int:
    return NIGHT_MIN_DURATION if is_night_start(start_min) else DAY_MIN_DURATION


def max_duration_for_start(start_min: int):
    return NIGHT_MAX_DURATION if is_night_start(start_min) else DAY_MAX_DURATION


def validate_duration(start_min: int, end_min: int):
    """Возвращает (ok: bool, error_message: str | None)."""
    duration = end_min - start_min
    if is_night_start(start_min):
        if duration <= NIGHT_MIN_DURATION - 1:
            return False, "Ночью (до 8:00) длительность брони должна быть больше 60 минут."
        return True, None
    else:
        if duration < DAY_MIN_DURATION or duration > DAY_MAX_DURATION:
            return False, (
                f"С 8:00 до 24:00 длительность брони — от {DAY_MIN_DURATION} "
                f"до {DAY_MAX_DURATION} минут."
            )
        return True, None

TRIGGER_BOOK = "бронь"
TRIGGER_SCHEDULE = "брони"
TRIGGER_SCHEDULE_TOMORROW = "брони завтра"
TRIGGER_MY_BOOKINGS = "мои брони"
TRIGGER_CANCEL = ("отмена брони", "отмена броней")
TRIGGER_ID = "id"
TRIGGER_NICK_PREFIX = "ник "

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_font(filename: str) -> str | None:
    for candidate in (
        os.path.join(BASE_DIR, filename),
        os.path.join(BASE_DIR, "fonts", filename),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


FONT_REGULAR = _find_font("DejaVuSans.ttf")
FONT_BOLD = _find_font("DejaVuSans-Bold.ttf")

# ====================== БАЗА ДАННЫХ ======================

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            booking_date TEXT NOT NULL,
            start_min INTEGER NOT NULL,
            end_min INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# ====================== ПОМОЩНИКИ С ВРЕМЕНЕМ / ДАТОЙ ======================

TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})")


def parse_time_to_minutes(text: str) -> int | None:
    m = TIME_RE.fullmatch(text.strip())
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if mm > 59 or h > 24 or (h == 24 and mm != 0):
        return None
    return h * 60 + mm


def minutes_to_time(m: int) -> str:
    if m == 1440:
        return "24:00"
    return f"{m // 60:02d}:{m % 60:02d}"


def today_date():
    return datetime.now(TIMEZONE).date()


def date_to_str(d) -> str:
    return d.strftime("%Y-%m-%d")


def date_to_human(d) -> str:
    return d.strftime("%d.%m")


def human_date_from_str(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return date_to_human(d)


# ====================== ДОСТУП / НИКНЕЙМЫ ======================

def get_nickname(conn, chat_id: int, user_id: int, fallback: str) -> str:
    row = conn.execute(
        "SELECT nickname FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    return row[0] if row else fallback


def set_nickname(conn, chat_id: int, user_id: int, nickname: str):
    conn.execute(
        """
        INSERT INTO users (chat_id, user_id, nickname) VALUES (?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET nickname=excluded.nickname
        """,
        (chat_id, user_id, nickname),
    )
    conn.commit()


# ====================== ЛОГИКА БРОНИРОВАНИЯ ======================

def get_schedule(conn, chat_id, booking_date):
    return conn.execute(
        """
        SELECT start_min, end_min, nickname FROM bookings
        WHERE chat_id=? AND booking_date=?
        ORDER BY start_min
        """,
        (chat_id, booking_date),
    ).fetchall()


def find_overlap(conn, chat_id: int, booking_date: str, start_min: int, end_min: int):
    for s, e, nick in get_schedule(conn, chat_id, booking_date):
        if start_min < e and end_min > s:
            return (s, e, nick)
    return None


def add_booking(conn, chat_id, booking_date, start_min, end_min, user_id, nickname):
    conn.execute(
        """
        INSERT INTO bookings (chat_id, booking_date, start_min, end_min, user_id, nickname, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (chat_id, booking_date, start_min, end_min, user_id, nickname, datetime.now(TIMEZONE).isoformat()),
    )
    conn.commit()


def cancel_booking(conn, chat_id, booking_date, start_min, end_min, user_id) -> bool:
    cur = conn.execute(
        """
        DELETE FROM bookings
        WHERE chat_id=? AND booking_date=? AND start_min=? AND end_min=? AND user_id=?
        """,
        (chat_id, booking_date, start_min, end_min, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def count_user_bookings_on_date(conn, chat_id, booking_date, user_id) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE chat_id=? AND booking_date=? AND user_id=?",
        (chat_id, booking_date, user_id),
    ).fetchone()
    return row[0] if row else 0


def cancel_all_bookings(conn, chat_id, booking_date, user_id) -> int:
    cur = conn.execute(
        "DELETE FROM bookings WHERE chat_id=? AND booking_date=? AND user_id=?",
        (chat_id, booking_date, user_id),
    )
    conn.commit()
    return cur.rowcount


def available_start_times(conn, chat_id, booking_date):
    bookings = get_schedule(conn, chat_id, booking_date)
    starts = []
    for t in range(0, 1440, TIME_STEP_MINUTES):
        min_dur = min_duration_for_start(t)
        if t + min_dur > 1440:
            continue
        conflict = any(t < e and (t + min_dur) > s for s, e, _ in bookings)
        if not conflict:
            starts.append(t)
    return starts


def available_end_times(conn, chat_id, booking_date, start_min):
    bookings = get_schedule(conn, chat_id, booking_date)
    cap = 1440
    for s, e, _ in bookings:
        if s > start_min and s < cap:
            cap = s

    max_dur = max_duration_for_start(start_min)
    if max_dur is not None:
        cap = min(cap, start_min + max_dur)

    min_dur = min_duration_for_start(start_min)
    # Первый допустимый конец, выровненный по шагу сетки, но не меньше минимальной длительности.
    first_end = start_min + min_dur
    remainder = (first_end - start_min) % TIME_STEP_MINUTES
    if remainder != 0:
        first_end += TIME_STEP_MINUTES - remainder

    ends = []
    t = first_end
    while t <= cap:
        ends.append(t)
        t += TIME_STEP_MINUTES
    return ends


# ====================== ГЕНЕРАЦИЯ КАРТИНКИ РАСПИСАНИЯ ======================

COLOR_DATE_BG = "#a9c9ec"
COLOR_HEADER_BG = "#f0bcd4"
COLOR_BORDER = "#000000"
COLOR_TEXT = "#000000"
COLOR_FREE = "#8a8a8a"

COL1_W = 200
COL2_W = 300
DATE_ROW_H = 64
HEADER_ROW_H = 50
ROW_H = 48


def _draw_centered(draw, box, text, font, fill):
    x0, y0, x1, y1 = box
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = x0 + (x1 - x0 - tw) / 2 - bbox[0]
    y = y0 + (y1 - y0 - th) / 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill)


def render_schedule_image(rows, human_date: str) -> io.BytesIO:
    entries = []
    cursor = 0
    for start, end, nick in rows:
        if start > cursor:
            entries.append((f"{minutes_to_time(cursor)} - {minutes_to_time(start)}", "свободно", True))
        entries.append((f"{minutes_to_time(start)} - {minutes_to_time(end)}", nick, False))
        cursor = max(cursor, end)
    if cursor < 1440:
        entries.append((f"{minutes_to_time(cursor)} - 24:00", "свободно", True))
    if not entries:
        entries.append(("00:00 - 24:00", "свободно", True))

    width = COL1_W + COL2_W
    height = DATE_ROW_H + HEADER_ROW_H + ROW_H * len(entries)

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    if FONT_BOLD and FONT_REGULAR:
        font_date = ImageFont.truetype(FONT_BOLD, 30)
        font_header = ImageFont.truetype(FONT_BOLD, 22)
        font_row = ImageFont.truetype(FONT_REGULAR, 20)
    else:
        font_date = ImageFont.load_default(size=30)
        font_header = ImageFont.load_default(size=22)
        font_row = ImageFont.load_default(size=20)

    draw.rectangle([0, 0, width, DATE_ROW_H], fill=COLOR_DATE_BG)
    _draw_centered(draw, (0, 0, width, DATE_ROW_H), human_date, font_date, COLOR_TEXT)

    y0, y1 = DATE_ROW_H, DATE_ROW_H + HEADER_ROW_H
    draw.rectangle([0, y0, width, y1], fill=COLOR_HEADER_BG)
    _draw_centered(draw, (0, y0, COL1_W, y1), "Время", font_header, COLOR_TEXT)
    _draw_centered(draw, (COL1_W, y0, width, y1), "Ник", font_header, COLOR_TEXT)

    for i, (time_str, nick, is_free) in enumerate(entries):
        ry0 = y1 + i * ROW_H
        ry1 = ry0 + ROW_H
        color = COLOR_FREE if is_free else COLOR_TEXT
        _draw_centered(draw, (0, ry0, COL1_W, ry1), time_str, font_row, COLOR_TEXT)
        _draw_centered(draw, (COL1_W, ry0, width, ry1), nick, font_row, color)

    total_h = height
    ys = [0, DATE_ROW_H, DATE_ROW_H + HEADER_ROW_H]
    for i in range(1, len(entries) + 1):
        ys.append(DATE_ROW_H + HEADER_ROW_H + i * ROW_H)
    for y in ys:
        draw.line([(0, y), (width, y)], fill=COLOR_BORDER, width=2)
    draw.line([(0, 0), (0, total_h)], fill=COLOR_BORDER, width=2)
    draw.line([(COL1_W, DATE_ROW_H), (COL1_W, total_h)], fill=COLOR_BORDER, width=2)
    draw.line([(width - 1, 0), (width - 1, total_h)], fill=COLOR_BORDER, width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "schedule.png"
    return buf


async def send_schedule_image(update: Update, chat_id: int, booking_date: str, human_date: str):
    conn = db_connect()
    try:
        rows = get_schedule(conn, chat_id, booking_date)
    finally:
        conn.close()
    img = render_schedule_image(rows, human_date)
    await update.effective_message.reply_photo(photo=img)


# ====================== КНОПКИ ДЛЯ БРОНИРОВАНИЯ ======================

def build_date_keyboard(user_id: int, prefix: str = "bd", cancel_data: str | None = None):
    today = today_date()
    tomorrow = today + timedelta(days=1)
    after_tomorrow = today + timedelta(days=2)
    if cancel_data is None:
        cancel_data = f"bcancel:{user_id}"
    kb = [
        [InlineKeyboardButton(f"📅 Сегодня, {date_to_human(today)}", callback_data=f"{prefix}:{user_id}:{date_to_str(today)}")],
        [InlineKeyboardButton(f"📅 Завтра, {date_to_human(tomorrow)}", callback_data=f"{prefix}:{user_id}:{date_to_str(tomorrow)}")],
        [InlineKeyboardButton(f"📅 Послезавтра, {date_to_human(after_tomorrow)}", callback_data=f"{prefix}:{user_id}:{date_to_str(after_tomorrow)}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=cancel_data)],
    ]
    return InlineKeyboardMarkup(kb)


def build_start_keyboard(conn, chat_id, date_str, user_id: int):
    starts = available_start_times(conn, chat_id, date_str)
    buttons = [
        InlineKeyboardButton(minutes_to_time(t), callback_data=f"bs:{user_id}:{date_str}:{t}")
        for t in starts
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data=f"bback_d:{user_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"bcancel:{user_id}"),
    ])
    return InlineKeyboardMarkup(rows), starts


def build_end_keyboard(conn, chat_id, date_str, start_min, user_id: int):
    ends = available_end_times(conn, chat_id, date_str, start_min)
    buttons = [
        InlineKeyboardButton(minutes_to_time(t), callback_data=f"be:{user_id}:{date_str}:{start_min}:{t}")
        for t in ends
    ]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data=f"bback_s:{user_id}:{date_str}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"bcancel:{user_id}"),
    ])
    return InlineKeyboardMarkup(rows), ends


def build_cancel_menu_keyboard(rows, user_id: int, booking_date: str):
    """Клавиатура со списком бронирований для отмены."""
    buttons = [
        [InlineKeyboardButton(
            f"❌ {minutes_to_time(s)} - {minutes_to_time(e)}",
            callback_data=f"co:{user_id}:{booking_date}:{s}:{e}",
        )]
        for s, e in rows
    ]
    buttons.append([InlineKeyboardButton("✅ Закрыть", callback_data=f"cc:{user_id}")])
    return InlineKeyboardMarkup(buttons)


# ====================== ПРОВЕРКА ЧАТА / ТЕМЫ ======================

def is_allowed(update: Update) -> bool:
    if ALLOWED_CHAT_ID is not None and update.effective_chat.id != ALLOWED_CHAT_ID:
        return False
    if ALLOWED_THREAD_ID is not None:
        msg_thread = update.effective_message.message_thread_id if update.effective_message else None
        if msg_thread != ALLOWED_THREAD_ID:
            return False
    return True


def is_allowed_chat_only(update: Update) -> bool:
    if ALLOWED_CHAT_ID is not None and update.effective_chat.id != ALLOWED_CHAT_ID:
        return False
    return True


# ====================== ХЕНДЛЕРЫ КОМАНД ======================

async def reply_chat_and_thread_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    msg = update.effective_message
    await msg.reply_text(
        f"chat_id: <code>{chat.id}</code>\n"
        f"message_thread_id: <code>{msg.message_thread_id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def set_nickname_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE, nickname: str):
    nickname = nickname.strip()
    if not nickname:
        await update.effective_message.reply_text(
            "Укажи ник после слова, например:\n<code>ник Лейлар</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    conn = db_connect()
    try:
        set_nickname(conn, update.effective_chat.id, update.effective_user.id, nickname)
    finally:
        conn.close()
    await update.effective_message.reply_text(f"✅ Готово! Твой ник теперь: <b>{nickname}</b>", parse_mode=ParseMode.HTML)


async def show_today_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = today_date()
    await send_schedule_image(update, update.effective_chat.id, date_to_str(today), date_to_human(today))


async def show_tomorrow_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tomorrow = today_date() + timedelta(days=1)
    await send_schedule_image(update, update.effective_chat.id, date_to_str(tomorrow), date_to_human(tomorrow))


async def show_my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = db_connect()
    try:
        rows = conn.execute(
            """
            SELECT start_min, end_min FROM bookings
            WHERE chat_id=? AND booking_date=? AND user_id=?
            ORDER BY start_min
            """,
            (update.effective_chat.id, date_to_str(today_date()), update.effective_user.id),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await update.effective_message.reply_text("У тебя нет бронирований на сегодня.")
        return

    lines = ["🕓 <b>Твои бронирования на сегодня:</b>"]
    for s, e in rows:
        lines.append(f"{minutes_to_time(s)} - {minutes_to_time(e)}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def show_cancel_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор даты, затем список бронирований пользователя с кнопками отмены."""
    user_id = update.effective_user.id
    kb = build_date_keyboard(user_id, prefix="cdsel", cancel_data=f"cc:{user_id}")
    await update.effective_message.reply_text(
        "За какой день показать твои бронирования для отмены?",
        reply_markup=kb,
    )


async def send_cancel_list_for_date(query, conn, chat_id: int, owner_id: int, booking_date: str):
    """Выводит (или редактирует существующее сообщение в) список бронирований owner_id на booking_date с кнопками отмены."""
    rows = conn.execute(
        "SELECT start_min, end_min FROM bookings "
        "WHERE chat_id=? AND booking_date=? AND user_id=? ORDER BY start_min",
        (chat_id, booking_date, owner_id),
    ).fetchall()

    human = human_date_from_str(booking_date)

    if not rows:
        await query.edit_message_text(f"У тебя нет бронирований на {human}.")
        return

    kb = build_cancel_menu_keyboard(rows, owner_id, booking_date)
    await query.edit_message_text(
        f"🗓 Твои бронирования на {human}.\nНажми на бронь, чтобы её отменить:",
        reply_markup=kb,
    )


async def cancel_specific_booking(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    """Отмена конкретной брони по времени: 'отмена HH:MM-HH:MM'."""
    times = TIME_RE.findall(raw_text)
    if len(times) < 2:
        await update.effective_message.reply_text(
            "Укажи время брони для отмены, например:\n<code>отмена 14:00-16:00</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    start_min = parse_time_to_minutes(f"{times[0][0]}:{times[0][1]}")
    end_min = parse_time_to_minutes(f"{times[1][0]}:{times[1][1]}")

    conn = db_connect()
    try:
        ok = cancel_booking(conn, update.effective_chat.id, date_to_str(today_date()), start_min, end_min, update.effective_user.id)
    finally:
        conn.close()

    if ok:
        await update.effective_message.reply_text(
            f"❎ Бронь {minutes_to_time(start_min)} - {minutes_to_time(end_min)} отменена."
        )
    else:
        await update.effective_message.reply_text(
            "Не нашёл такую бронь среди твоих на сегодня."
        )


# ====================== ПРЯМОЕ БРОНИРОВАНИЕ (КОМАНДОЙ С ВРЕМЕНЕМ) ======================

async def try_book_direct(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str):
    times = TIME_RE.findall(raw_text)

    if len(times) < 2:
        await update.effective_message.reply_text(
            "Не понял время. Укажи в формате <code>HH:MM-HH:MM</code>, например:\n"
            "<code>бронь 14:00-16:00</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    start_min = parse_time_to_minutes(f"{times[0][0]}:{times[0][1]}")
    end_min = parse_time_to_minutes(f"{times[1][0]}:{times[1][1]}")

    if start_min is None or end_min is None:
        await update.effective_message.reply_text("Некорректное время. Используй формат HH:MM, например 14:00.")
        return

    if end_min - start_min <= 0:
        await update.effective_message.reply_text("Время окончания должно быть позже времени начала.")
        return

    ok, err = validate_duration(start_min, end_min)
    if not ok:
        await update.effective_message.reply_text(err)
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    booking_date = date_to_str(today_date())

    conn = db_connect()
    try:
        existing_count = count_user_bookings_on_date(conn, chat_id, booking_date, user_id)
        if existing_count >= MAX_BOOKINGS_PER_DAY:
            await update.effective_message.reply_text(
                f"🚫 У тебя уже {MAX_BOOKINGS_PER_DAY} брони на этот день. Больше нельзя забронировать в этот день."
            )
            return

        overlap = find_overlap(conn, chat_id, booking_date, start_min, end_min)
        if overlap:
            o_start, o_end, o_nick = overlap
            await update.effective_message.reply_text(
                f"🚫 Время {minutes_to_time(start_min)} - {minutes_to_time(end_min)} занято.\n"
                f"Пересекается с бронью {minutes_to_time(o_start)} - {minutes_to_time(o_end)} ({o_nick})."
            )
            return

        fallback_nick = update.effective_user.first_name or update.effective_user.username or "Без ника"
        nickname = get_nickname(conn, chat_id, user_id, fallback_nick)
        add_booking(conn, chat_id, booking_date, start_min, end_min, user_id, nickname)
    finally:
        conn.close()

    await update.effective_message.reply_text(
        f"✅ Забронировано: {minutes_to_time(start_min)} - {minutes_to_time(end_min)} для {nickname}"
    )


# ====================== ОБРАБОТКА КНОПОК (CALLBACK QUERY) ======================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        return

    data = query.data
    chat_id = query.message.chat.id
    caller_id = query.from_user.id

    async def wrong_user():
        await query.answer("❗ Это не твоё меню", show_alert=True)

    conn = db_connect()
    try:

        # ── Отмена создания брони ──────────────────────────────────────────
        if data.startswith("bcancel:"):
            owner_id = int(data.split(":")[1])
            if caller_id != owner_id:
                await wrong_user()
                return
            await query.edit_message_text("❌ Создание брони отменено.")

        # ── Кнопка «Готово» после успешной брони ──────────────────────────
        elif data.startswith("bdone:"):
            owner_id = int(data.split(":")[1])
            if caller_id != owner_id:
                await wrong_user()
                return
            await query.edit_message_text(query.message.text)

        # ── Выбор даты ────────────────────────────────────────────────────
        elif data.startswith("bd:"):
            _, owner_s, date_str = data.split(":", 2)
            owner_id = int(owner_s)
            if caller_id != owner_id:
                await wrong_user()
                return
            kb, starts = build_start_keyboard(conn, chat_id, date_str, owner_id)
            human = human_date_from_str(date_str)
            if not starts:
                back_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад", callback_data=f"bback_d:{owner_id}"),
                    InlineKeyboardButton("❌ Отмена", callback_data=f"bcancel:{owner_id}"),
                ]])
                await query.edit_message_text(f"На {human} свободных слотов нет 😔", reply_markup=back_kb)
                return
            await query.edit_message_text(
                f"Дата: {human}\nВыбери время начала брони:\n"
                f"({DAY_MIN_DURATION}-{DAY_MAX_DURATION} мин с 8:00 до 24:00, "
                f"ночью — более 60 мин, без ограничения сверху)",
                reply_markup=kb,
            )

        # ── Выбор начала ──────────────────────────────────────────────────
        elif data.startswith("bs:"):
            _, owner_s, date_str, start_s = data.split(":")
            owner_id = int(owner_s)
            if caller_id != owner_id:
                await wrong_user()
                return
            start_min = int(start_s)
            kb, ends = build_end_keyboard(conn, chat_id, date_str, start_min, owner_id)
            human = human_date_from_str(date_str)
            await query.edit_message_text(
                f"Дата: {human}\nНачало: {minutes_to_time(start_min)}\nВыбери время окончания:",
                reply_markup=kb,
            )

        # ── Выбор конца → создание брони ─────────────────────────────────
        elif data.startswith("be:"):
            _, owner_s, date_str, start_s, end_s = data.split(":")
            owner_id = int(owner_s)
            if caller_id != owner_id:
                await wrong_user()
                return
            start_min, end_min = int(start_s), int(end_s)

            existing_count = count_user_bookings_on_date(conn, chat_id, date_str, owner_id)
            if existing_count >= MAX_BOOKINGS_PER_DAY:
                await query.edit_message_text(
                    f"🚫 У тебя уже {MAX_BOOKINGS_PER_DAY} брони на {human_date_from_str(date_str)}. "
                    "Больше нельзя забронировать в этот день."
                )
                return

            overlap = find_overlap(conn, chat_id, date_str, start_min, end_min)
            if overlap:
                await query.edit_message_text(
                    "🚫 Это время уже заняли, пока ты выбирал. Напиши «бронь» ещё раз."
                )
                return

            user = query.from_user
            fallback_nick = user.first_name or user.username or "Без ника"
            nickname = get_nickname(conn, chat_id, user.id, fallback_nick)
            add_booking(conn, chat_id, date_str, start_min, end_min, user.id, nickname)

            human = human_date_from_str(date_str)
            done_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Забронировать ещё", callback_data=f"bback_d:{owner_id}"),
                InlineKeyboardButton("✅ Готово", callback_data=f"bdone:{owner_id}"),
            ]])
            await query.edit_message_text(
                f"✅ Забронировано на {human}\n"
                f"{minutes_to_time(start_min)} - {minutes_to_time(end_min)} — {nickname}",
                reply_markup=done_kb,
            )

        # ── Назад к выбору даты ───────────────────────────────────────────
        elif data.startswith("bback_d:"):
            owner_id = int(data.split(":")[1])
            if caller_id != owner_id:
                await wrong_user()
                return
            await query.edit_message_text(
                "Выбери дату для бронирования:",
                reply_markup=build_date_keyboard(owner_id),
            )

        # ── Назад к выбору начала ─────────────────────────────────────────
        elif data.startswith("bback_s:"):
            _, owner_s, date_str = data.split(":")
            owner_id = int(owner_s)
            if caller_id != owner_id:
                await wrong_user()
                return
            kb, starts = build_start_keyboard(conn, chat_id, date_str, owner_id)
            human = human_date_from_str(date_str)
            await query.edit_message_text(
                f"Дата: {human}\nВыбери время начала брони:\n"
                f"({DAY_MIN_DURATION}-{DAY_MAX_DURATION} мин с 8:00 до 24:00, "
                f"ночью — более 60 мин, без ограничения сверху)",
                reply_markup=kb,
            )

        # ── Выбор даты для отмены бронирований ─────────────────────────────
        # callback_data формат: cdsel:{user_id}:{booking_date}
        elif data.startswith("cdsel:"):
            _, owner_s, booking_date = data.split(":", 2)
            owner_id = int(owner_s)
            if caller_id != owner_id:
                await wrong_user()
                return
            await send_cancel_list_for_date(query, conn, chat_id, owner_id, booking_date)

        # ── Отмена конкретной брони из меню отмены ────────────────────────
        # callback_data формат: co:{user_id}:{booking_date}:{start_min}:{end_min}
        elif data.startswith("co:"):
            parts = data.split(":")
            owner_id = int(parts[1])
            booking_date = parts[2]
            start_min = int(parts[3])
            end_min = int(parts[4])

            if caller_id != owner_id:
                await wrong_user()
                return

            ok = cancel_booking(conn, chat_id, booking_date, start_min, end_min, owner_id)
            if not ok:
                await query.answer("Бронь не найдена или уже отменена", show_alert=True)
                return

            # Обновляем список оставшихся бронирований
            remaining = conn.execute(
                "SELECT start_min, end_min FROM bookings "
                "WHERE chat_id=? AND booking_date=? AND user_id=? ORDER BY start_min",
                (chat_id, booking_date, owner_id),
            ).fetchall()

            human = human_date_from_str(booking_date)
            cancelled_text = f"❎ Бронь {minutes_to_time(start_min)} - {minutes_to_time(end_min)} ({human}) отменена."

            if remaining:
                kb = build_cancel_menu_keyboard(remaining, owner_id, booking_date)
                await query.edit_message_text(
                    f"{cancelled_text}\n\n"
                    f"🗓 Оставшиеся бронирования на {human}.\nНажми на бронь, чтобы её отменить:",
                    reply_markup=kb,
                )
            else:
                await query.edit_message_text(f"{cancelled_text}\nБольше бронирований на {human} нет.")

        # ── Закрыть меню отмены ───────────────────────────────────────────
        # callback_data формат: cc:{user_id}
        elif data.startswith("cc:"):
            owner_id = int(data.split(":")[1])
            if caller_id != owner_id:
                await wrong_user()
                return
            await query.edit_message_text("✅ Готово.")

    finally:
        conn.close()


# ====================== ОБРАБОТКА ОБЫЧНЫХ СООБЩЕНИЙ (ТРИГГЕРЫ) ======================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    stripped = text.strip()
    lower = stripped.lower()

    if lower in (TRIGGER_ID, "/" + TRIGGER_ID):
        if not is_allowed_chat_only(update):
            return
        await reply_chat_and_thread_id(update, context)
        return

    if not is_allowed(update):
        return

    if lower.startswith(TRIGGER_NICK_PREFIX):
        nickname = stripped[len(TRIGGER_NICK_PREFIX):]
        await set_nickname_trigger(update, context, nickname)
        return

    if lower == TRIGGER_BOOK or lower.startswith(TRIGGER_BOOK + " "):
        times = TIME_RE.findall(text)
        if len(times) >= 2:
            await try_book_direct(update, context, text)
        else:
            await update.effective_message.reply_text(
                "Выбери дату для бронирования:",
                reply_markup=build_date_keyboard(update.effective_user.id),
            )
        return

    if lower == TRIGGER_SCHEDULE_TOMORROW:
        await show_tomorrow_schedule(update, context)
        return

    if lower == TRIGGER_SCHEDULE:
        await show_today_schedule(update, context)
        return

    if lower == TRIGGER_MY_BOOKINGS:
        await show_my_bookings(update, context)
        return

    if lower in TRIGGER_CANCEL:
        await show_cancel_menu(update, context)
        return

    if lower.startswith("отмена "):
        await cancel_specific_booking(update, context, text)
        return


# ====================== ЗАПУСК ======================

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if BOT_TOKEN == "ВАШ_ТОКЕН_СЮДА":
        raise SystemExit("Укажи BOT_TOKEN через переменную окружения или прямо в bot.py")

    db_connect().close()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT, handle_text))

    logging.info("Бот запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()