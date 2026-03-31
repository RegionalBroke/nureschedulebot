#!/usr/bin/env python3
"""
NURE Schedule Telegram Bot
Fetches schedule via CSV export from cist.nure.ua (no auth required).
Group list via P_API_GROUP_JSON (also no auth required).
Selection flow: Faculty → Year → Group
"""

import asyncio
import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("nure_bot")

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TIMEZONE     = ZoneInfo("Europe/Kyiv")
DAILY_HOUR   = int(os.getenv("DAILY_HOUR",   "7"))
DAILY_MINUTE = int(os.getenv("DAILY_MINUTE", "0"))
DATA_FILE    = "chat_settings.json"

LESSON_TYPES = {
    "Лк": "Лекція",
    "Пз": "Практика",
    "Лб": "Лабораторна",
    "Зл": "Залік",
    "Ек": "Екзамен",
    "Кп": "Курсовий проєкт",
    "Зч": "Залік",
    "Конс": "Консультація",
}

WEEKDAYS_UK = {
    0: "Понеділок", 1: "Вівторок", 2: "Середа",
    3: "Четвер",    4: "П'ятниця", 5: "Субота", 6: "Неділя",
}

LESSON_NUMBER = {
    "07:45:00": "1",
    "09:30:00": "2",
    "11:15:00": "3",
    "13:10:00": "4",
    "14:55:00": "5",
    "16:40:00": "6",
}
# Semester bounds — used when fetching the full CSV
# Wide range so we always get the current semester
SEMESTER_START = "01.02.2026"
SEMESTER_END   = "30.06.2026"


# ── Persistence ───────────────────────────────────────────────────────────────
def load_settings() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_settings(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── CIST helpers ──────────────────────────────────────────────────────────────
_http = requests.Session()
_http.headers.update({"User-Agent": "Mozilla/5.0 (compatible; NUREScheduleBot/2.0)"})


def fetch_groups_tree() -> dict:
    """
    Fetch group list from P_API_GROUP_JSON (no auth needed).
    Returns { faculty_short_name: { "25": [{id, name}, ...], ... } }
    """
    try:
        r = _http.get("https://cist.nure.ua/ias/app/tt/P_API_GROUP_JSON", timeout=15)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            data = json.loads(r.content.decode("windows-1251"))
    except Exception as exc:
        logger.error("Failed to fetch groups: %s", exc)
        return {}

    tree: dict[str, dict[str, list]] = {}
    for faculty in data.get("university", {}).get("faculties", []):
        fname = faculty.get("short_name") or faculty.get("full_name", "—")
        groups_flat: list[dict] = []

        for direction in faculty.get("directions", []):
            for g in direction.get("groups", []):
                groups_flat.append({"id": g["id"], "name": g["name"]})
            for spec in direction.get("specialities", []):
                for g in spec.get("groups", []):
                    groups_flat.append({"id": g["id"], "name": g["name"]})

        if not groups_flat:
            continue

        buckets: dict[str, list] = {}
        for g in groups_flat:
            m = re.search(r"-(\d{2})-", g["name"])
            year = m.group(1) if m else "??"
            buckets.setdefault(year, []).append(g)

        tree[fname] = buckets

    return tree


def fetch_csv(group_id: int) -> list[dict]:
    """
    Download CSV schedule for the whole semester, return list of lesson dicts:
    { date, time_start, time_end, subject, lesson_type }
    """
    url = "https://cist.nure.ua/ias/app/tt/WEB_IAS_TT_GNR_RASP.GEN_GROUP_POTOK_RASP"
    params = {
        "ATypeDoc":        3,
        "Aid_group":       group_id,
        "Aid_potok":       0,
        "ADateStart":      SEMESTER_START,
        "ADateEnd":        SEMESTER_END,
        "AMultiWorkSheet": 0,
    }
    try:
        r = _http.get(url, params=params, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        logger.error("CSV fetch failed: %s", exc)
        return []

    # Decode windows-1251, strip BOM if present
    try:
        text = r.content.decode("windows-1251")
    except Exception:
        text = r.content.decode("utf-8", errors="replace")

    lessons = []
    reader = csv.reader(io.StringIO(text, newline=''), delimiter=",", quotechar='"')
    for row in reader:
        if len(row) < 10:
            continue
        try:
            # row[0]: "Subject Type Teacher Group1;Group2;..."
            # row[1]: date start  DD.MM.YYYY
            # row[2]: time start  HH:MM:SS
            # row[3]: date end    DD.MM.YYYY
            # row[4]: time end    HH:MM:SS

            first_field = row[0].strip()

            # Parse subject, type from first field
            # Format: "ВМ Лк DL ТРІМІ-25-1;УК-25-1"
            # or:     "ВМ Лк Іванов DL ТРІМІ-25-1"  (teacher before DL)
            parts = first_field.split()
            subject = parts[0] if len(parts) > 0 else "?"
            lesson_type_raw = parts[1] if len(parts) > 1 else ""
            lesson_type = LESSON_TYPES.get(lesson_type_raw, lesson_type_raw)

            date_str  = row[1].strip()   # DD.MM.YYYY
            t_start   = row[2].strip()[:5]  # HH:MM
            t_end     = row[4].strip()[:5]  # HH:MM
            pair_num = LESSON_NUMBER.get(row[2].strip())

            # Parse date
            date = datetime.strptime(date_str, "%d.%m.%Y").date()

            lessons.append({
                "date":         date,
                "time_start":   t_start,
                "time_end":     t_end,
                "subject":      subject,
                "lesson_type":  lesson_type,
                "pair_num":     pair_num,
            })
        except Exception as exc:
            logger.debug("Skipping CSV row: %s | error: %s", row, exc)
            continue

    logger.info("Fetched %d lessons from CSV for group %d", len(lessons), group_id)
    return lessons


# ── Schedule formatting ───────────────────────────────────────────────────────
def today_midnight(tz=TIMEZONE) -> datetime:
    now = datetime.now(tz)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def format_day(group_name: str, target: datetime, lessons: list[dict]) -> str:
    target_date = target.date()
    day_lessons = [l for l in lessons if l["date"] == target_date]
    day_lessons.sort(key=lambda l: l["time_start"])

    weekday  = WEEKDAYS_UK[target.weekday()]
    date_str = f"{weekday}, {target.strftime('%d.%m.%Y')}"
    header   = f"📅 *{group_name}* — {date_str}\n{'─' * 30}\n"

    if not day_lessons:
        return header + "_Пар немає_ 🎉"

    lines = [header]
    for l in day_lessons:
        lines.append(
            f"*{l['pair_num']}.* `{l['time_start']}–{l['time_end']}` — *{l['subject']}*\n"
            f"   🏷 {l['lesson_type']}"
        )
    return "\n".join(lines)


# ── /start ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *NURE Schedule Bot*\n\n"
        "Команди:\n"
        "• /setgroup — Обрати групу\n"
        "• /schedule — Розклад на сьогодні\n"
        "• /tomorrow — Розклад на завтра\n"
        "• /week — Розклад на тиждень\n"
        "• /autopost — Увімк/вимк щоденну публікацію\n"
        "• /status — Поточні налаштування",
        parse_mode="Markdown",
    )


# ── /setgroup flow ────────────────────────────────────────────────────────────
async def cmd_setgroup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Завантаження списку груп…")
    tree = fetch_groups_tree()
    if not tree:
        await msg.edit_text("❌ Не вдалося отримати список груп. Спробуйте пізніше.")
        return

    ctx.bot_data["groups_tree"] = tree
    ctx.bot_data["fac_index"]   = sorted(tree.keys())
    await msg.delete()

    fac_index = ctx.bot_data["fac_index"]
    buttons, row = [], []
    for i, fname in enumerate(fac_index):
        row.append(InlineKeyboardButton(fname, callback_data=f"fac:{i}"))
        if len(row) == 3 or i == len(fac_index) - 1:
            buttons.append(row)
            row = []

    await update.message.reply_text(
        "🏫 *Оберіть факультет:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_faculty(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    fac_idx   = int(query.data.split(":")[1])
    fac_index = ctx.bot_data.get("fac_index", [])
    if fac_idx >= len(fac_index):
        await query.edit_message_text("❌ Застарілі дані. Використайте /setgroup знову.")
        return

    faculty = fac_index[fac_idx]
    years   = sorted(ctx.bot_data["groups_tree"].get(faculty, {}).keys(), reverse=True)
    if not years:
        await query.edit_message_text("❌ Немає груп для цього факультету.")
        return

    ctx.user_data["sel_fac_idx"] = fac_idx
    ctx.user_data["year_index"]  = years

    buttons = [[InlineKeyboardButton(y, callback_data=f"yr:{i}") for i, y in enumerate(years)]]
    await query.edit_message_text(
        f"🏫 *{faculty}*\n\n📆 *Оберіть рік вступу:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_year(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    yr_idx     = int(query.data.split(":")[1])
    year_index = ctx.user_data.get("year_index", [])
    fac_idx    = ctx.user_data.get("sel_fac_idx", 0)
    fac_index  = ctx.bot_data.get("fac_index", [])

    if yr_idx >= len(year_index) or fac_idx >= len(fac_index):
        await query.edit_message_text("❌ Застарілі дані. Використайте /setgroup знову.")
        return

    year    = year_index[yr_idx]
    faculty = fac_index[fac_idx]
    groups  = sorted(
        ctx.bot_data["groups_tree"].get(faculty, {}).get(year, []),
        key=lambda g: g["name"],
    )
    if not groups:
        await query.edit_message_text("❌ Немає груп для цього року.")
        return

    ctx.user_data["grp_name_map"] = {str(g["id"]): g["name"] for g in groups}
    buttons = [[InlineKeyboardButton(g["name"], callback_data=f"grp:{g['id']}")] for g in groups]
    buttons.append([InlineKeyboardButton("← Назад", callback_data=f"fac:{fac_idx}")])

    await query.edit_message_text(
        f"🏫 *{faculty}* — {year}\n\n👥 *Оберіть групу:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_group_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    group_id   = int(query.data.split(":")[1])
    group_name = ctx.user_data.get("grp_name_map", {}).get(str(group_id), f"Group {group_id}")

    settings = load_settings()
    chat_id  = str(query.message.chat_id)
    settings.setdefault(chat_id, {})
    settings[chat_id]["group_id"]   = group_id
    settings[chat_id]["group_name"] = group_name
    save_settings(settings)

    await query.edit_message_text(
        f"✅ Групу встановлено: *{group_name}*\n\n"
        "/schedule — розклад на сьогодні\n"
        "/autopost — щоденна публікація",
        parse_mode="Markdown",
    )


# ── Schedule helpers ──────────────────────────────────────────────────────────
def _cfg(update: Update) -> Optional[dict]:
    cfg = load_settings().get(str(update.effective_chat.id), {})
    return cfg if cfg.get("group_id") else None


async def _get_lessons(group_id: int) -> list[dict]:
    """Always fetch fresh CSV from CIST."""
    return fetch_csv(group_id)


async def _send(chat_id: int, group_id: int, group_name: str, target: datetime, ctx):
    lessons = await _get_lessons(group_id)
    if not lessons:
        await ctx.bot.send_message(chat_id, "❌ Не вдалося отримати розклад. Спробуйте пізніше.")
        return
    text = format_day(group_name, target, lessons)
    await ctx.bot.send_message(chat_id, text, parse_mode="Markdown")


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = _cfg(update)
    if not cfg:
        await update.message.reply_text("⚠️ Групу не встановлено. Використайте /setgroup.")
        return
    await _send(update.message.chat_id, cfg["group_id"], cfg["group_name"], today_midnight(), ctx)


async def cmd_tomorrow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = _cfg(update)
    if not cfg:
        await update.message.reply_text("⚠️ Групу не встановлено. Використайте /setgroup.")
        return
    await _send(update.message.chat_id, cfg["group_id"], cfg["group_name"],
                today_midnight() + timedelta(days=1), ctx)


async def cmd_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg = _cfg(update)
    if not cfg:
        await update.message.reply_text("⚠️ Групу не встановлено. Використайте /setgroup.")
        return

    today  = today_midnight()
    monday = today - timedelta(days=today.weekday())
    lessons = await _get_lessons(cfg["group_id"])
    if not lessons:
        await update.message.reply_text("❌ Не вдалося отримати розклад.")
        return

    for i in range(7):
        day  = monday + timedelta(days=i)
        text = format_day(cfg["group_name"], day, lessons)
        await update.message.chat.send_message(text, parse_mode="Markdown")
        await asyncio.sleep(0.3)



async def cmd_autopost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = str(update.effective_chat.id)
    settings = load_settings()
    settings.setdefault(chat_id, {})
    cfg = settings[chat_id]

    if not cfg.get("group_id"):
        await update.message.reply_text("⚠️ Спочатку оберіть групу через /setgroup.")
        return

    cfg["autopost"] = not cfg.get("autopost", False)
    save_settings(settings)

    status = "✅ увімкнено" if cfg["autopost"] else "❌ вимкнено"
    await update.message.reply_text(
        f"Щоденна публікація: *{status}*\n"
        f"Розклад о *{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}* (Київ) щодня.",
        parse_mode="Markdown",
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg    = load_settings().get(str(update.effective_chat.id), {})
    group  = cfg.get("group_name", "не встановлено")
    status = "✅ увімк" if cfg.get("autopost") else "❌ вимк"
    await update.message.reply_text(
        f"📊 *Поточні налаштування*\n\n"
        f"Група: *{group}*\n"
        f"Авто-публікація: *{status}* о `{DAILY_HOUR:02d}:{DAILY_MINUTE:02d}`",
        parse_mode="Markdown",
    )


# ── Daily job ─────────────────────────────────────────────────────────────────
async def daily_post_job(app: Application):
    today    = today_midnight()
    settings = load_settings()
    for chat_id_str, cfg in settings.items():
        if cfg.get("autopost") and cfg.get("group_id"):
            try:
                await _send(int(chat_id_str), cfg["group_id"],
                            cfg["group_name"], today, app)
                logger.info("Posted to %s", chat_id_str)
            except Exception as exc:
                logger.error("Failed for %s: %s", chat_id_str, exc)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не встановлено.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_start))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("week",     cmd_week))
    app.add_handler(CommandHandler("autopost", cmd_autopost))
    app.add_handler(CommandHandler("status",   cmd_status))

    app.add_handler(CallbackQueryHandler(callback_faculty,      pattern=r"^fac:\d+$"))
    app.add_handler(CallbackQueryHandler(callback_year,         pattern=r"^yr:\d+$"))
    app.add_handler(CallbackQueryHandler(callback_group_select, pattern=r"^grp:\d+$"))

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(daily_post_job, "cron",
                      hour=DAILY_HOUR, minute=DAILY_MINUTE, args=[app])

    async def post_init(application: Application):
        scheduler.start()
        logger.info("Scheduler ready — daily at %02d:%02d", DAILY_HOUR, DAILY_MINUTE)

    app.post_init = post_init
    logger.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
