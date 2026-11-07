import sqlite3
import logging
from datetime import datetime, timedelta, time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

# ===== НАСТРОЙКИ =====
TOKEN = "8984218525:AAEEzzwU3OHnX-bPsUByINnj1jU5Krqiek0"
CHANNEL_ID = "@rideschool_spb"   # или числовой id вида -100123456789
ADMIN_ID = 1680544342                 # ваш числовой id
OPEN_HOUR = 10                       # первый слот начинается в 10:00
CLOSE_HOUR = 20                      # последний слот заканчивается в 20:00
DAYS_AHEAD = 7                       # на сколько дней вперёд можно записаться
DB_FILE = "bookings.db"

# Посты по расписанию: (часы, минуты, текст). Публикуются каждый день.
SCHEDULED_POSTS = [
    (9, 0, "Доброе утро! Запись на сегодняшние занятия открыта, пишите боту."),
    (18, 0, "Вечерние слоты ещё есть. Записывайтесь через бота!"),
]

# Автоответы на ключевые слова
AUTO_REPLIES = {
    "цена": "Стоимость занятия уточняйте у администратора.",
    "адрес": "Мы находимся по адресу: УКАЖИТЕ АДРЕС.",
    "расписание": "Занятия каждый день с 10:00 до 20:00. Нажмите /book, чтобы записаться.",
}

logging.basicConfig(level=logging.INFO)


# ===== БАЗА ДАННЫХ =====
def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                day TEXT,
                hour INTEGER
            )
        """)


def taken_hours(day):
    with db() as con:
        rows = con.execute("SELECT hour FROM bookings WHERE day=?", (day,)).fetchall()
    return {r[0] for r in rows}


def free_hours(day):
    taken = taken_hours(day)
    now = datetime.now()
    result = []
    for h in range(OPEN_HOUR, CLOSE_HOUR):
        if h in taken:
            continue
        if day == now.strftime("%Y-%m-%d") and h <= now.hour:
            continue  # прошедшие часы сегодня скрываем
        result.append(h)
    return result


# ===== КОМАНДЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я помогу записаться на занятие.\n"
        "Команды: /book — записаться, /my — мои записи, /cancel — отменить запись."
    )


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["chosen"] = set()
    today = datetime.now().date()
    buttons = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        buttons.append([InlineKeyboardButton(
            d.strftime("%d.%m (%a)"), callback_data=f"day:{d.isoformat()}")])
    await update.message.reply_text(
        "Выберите дату:", reply_markup=InlineKeyboardMarkup(buttons))


def hours_keyboard(day, chosen):
    buttons, row = [], []
    for h in free_hours(day):
        mark = "✅ " if h in chosen else ""
        row.append(InlineKeyboardButton(
            f"{mark}{h}:00", callback_data=f"hour:{day}:{h}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Подтвердить запись", callback_data=f"ok:{day}")])
    return InlineKeyboardMarkup(buttons)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, _, rest = q.data.partition(":")
    chosen = context.user_data.setdefault("chosen", set())

    if kind == "day":
        chosen.clear()
        if not free_hours(rest):
            await q.edit_message_text("На эту дату свободных мест нет. Нажмите /book и выберите другую.")
            return
        await q.edit_message_text(
            f"Дата: {rest}\nВыберите один или несколько часов (каждый час — отдельное занятие), затем нажмите «Подтвердить».",
            reply_markup=hours_keyboard(rest, chosen))

    elif kind == "hour":
        day, h = rest.split(":")
        h = int(h)
        chosen.symmetric_difference_update({h})  # выбрать / снять выбор
        await q.edit_message_reply_markup(reply_markup=hours_keyboard(day, chosen))

    elif kind == "ok":
        day = rest
        if not chosen:
            await q.answer("Сначала выберите хотя бы один час", show_alert=True)
            return
        free = set(free_hours(day))
        hours = sorted(chosen)
        if not set(hours) <= free:
            await q.edit_message_text("Кто-то успел занять часть слотов. Нажмите /book и попробуйте снова.")
            return
        user = q.from_user
        name = user.full_name + (f" (@{user.username})" if user.username else "")
        with db() as con:
            for h in hours:
                con.execute(
                    "INSERT INTO bookings (user_id, user_name, day, hour) VALUES (?,?,?,?)",
                    (user.id, name, day, h))
        times = ", ".join(f"{h}:00–{h+1}:00" for h in hours)
        await q.edit_message_text(f"Вы записаны на {day}: {times}. Ждём вас!")
        await context.bot.send_message(
            ADMIN_ID, f"Новая запись: {name}\n{day}: {times}")
        chosen.clear()

    elif kind == "del":
        with db() as con:
            con.execute("DELETE FROM bookings WHERE id=? AND user_id=?", (int(rest), q.from_user.id))
        await q.edit_message_text("Запись отменена.")
        await context.bot.send_message(ADMIN_ID, f"Отмена записи #{rest} от {q.from_user.full_name}")


async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute(
            "SELECT day, hour FROM bookings WHERE user_id=? AND day>=? ORDER BY day, hour",
            (update.effective_user.id, today)).fetchall()
    if not rows:
        await update.message.reply_text("У вас нет предстоящих записей.")
        return
    text = "\n".join(f"{d}, {h}:00–{h+1}:00" for d, h in rows)
    await update.message.reply_text("Ваши записи:\n" + text)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as con:
        rows = con.execute(
            "SELECT id, day, hour FROM bookings WHERE user_id=? AND day>=? ORDER BY day, hour",
            (update.effective_user.id, today)).fetchall()
    if not rows:
        await update.message.reply_text("Отменять нечего: предстоящих записей нет.")
        return
    buttons = [[InlineKeyboardButton(f"{d}, {h}:00", callback_data=f"del:{i}")] for i, d, h in rows]
    await update.message.reply_text("Какую запись отменить?", reply_markup=InlineKeyboardMarkup(buttons))


# ===== ОТВЕТЫ ПОДПИСЧИКАМ =====
async def auto_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").lower()
    for key, answer in AUTO_REPLIES.items():
        if key in text:
            await update.message.reply_text(answer)
            return
    # Если ключевых слов нет, пересылаем вопрос админу
    await update.message.reply_text("Спасибо за сообщение! Администратор скоро ответит. Для записи нажмите /book.")
    await context.bot.forward_message(ADMIN_ID, update.effective_chat.id, update.message.id)


# ===== ПОСТЫ ПО РАСПИСАНИЮ =====
async def publish_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🏂 Записаться на занятие",
            url="https://t.me/Rideschoolspbbot"
        )]
    ])

    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=(
            "🏂 RIDE SCHOOL SPB — ШКОЛА СНОУБОРДА\n\n"
            "❄️ Обучение с нуля и для продолжающих\n"
            "📍 Санкт-Петербург и Ленинградская область\n\n"
            "👇 Нажимай кнопку и записывайся на занятие!"
        ),
        reply_markup=keyboard
    )
    await update.message.reply_text("Пост с кнопкой опубликован в канале!")
def make_post_job(text):
    async def job(context: ContextTypes.DEFAULT_TYPE):
        await context.bot.send_message(CHANNEL_ID, text)
    return job


# ===== ЗАПУСК =====
async def testpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=CHANNEL_ID,
        text="✅ Тестовая публикация Ride School SPB. Бот работает!"
    )
    await update.message.reply_text("Тестовая публикация отправлена в канал.")
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("testpost", testpost))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("book", book))
    app.add_handler(CommandHandler("my", my))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, auto_reply))
    app.add_handler(CommandHandler("publish", publish_channel_post))

    for i, (h, m, text) in enumerate(SCHEDULED_POSTS):
        app.job_queue.run_daily(make_post_job(text), time(hour=h, minute=m), name=f"post{i}")

    app.run_polling()


if __name__ == "__main__":
    main()