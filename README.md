# 🎓 NURE Schedule Telegram Bot

A Telegram bot that fetches schedules from [cist.nure.ua](https://cist.nure.ua) and posts them to any Telegram group or private chat.

---

## ✨ Features

| Feature | Description |
|---|---|
| `/setgroup` | Search & select any NURE group interactively |
| `/schedule` | Today's class schedule |
| `/tomorrow` | Tomorrow's schedule |
| `/week` | Full week (Mon–Sun) |
| `/autopost` | Toggle daily auto-post at 07:00 (Kyiv time) |
| `/status` | Show current group & settings |
| Multi-chat | Each group chat stores its own group & settings |

---

## 🚀 Quick Setup

### 1. Create a Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram
2. Send `/newbot` and follow the prompts
3. Copy the **API token** you receive

### 2. Add bot to your group

1. Add the bot as a member of your Telegram group
2. Give it permission to **send messages**
3. (Optional) Make it an admin so it can pin messages

### 3. Install & run

```bash
# Clone / copy the bot files
cd nure_schedule_bot

# Install dependencies
pip install -r requirements.txt

# Set your bot token
export TELEGRAM_BOT_TOKEN="your_token_here"

# (Optional) Change the daily post time (default: 07:00)
export DAILY_HOUR=7
export DAILY_MINUTE=0

# Run the bot
python bot.py
```

---

## ⚙️ Configuration via environment variables

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(required)* | Token from @BotFather |
| `DAILY_HOUR` | `7` | Hour for daily auto-post (Kyiv time) |
| `DAILY_MINUTE` | `0` | Minute for daily auto-post |

---

## 🐳 Docker (optional)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt bot.py ./
RUN pip install --no-cache-dir -r requirements.txt
ENV TELEGRAM_BOT_TOKEN=""
ENV DAILY_HOUR=7
ENV DAILY_MINUTE=0
CMD ["python", "bot.py"]
```

Build & run:
```bash
docker build -t nure-bot .
docker run -d \
  -e TELEGRAM_BOT_TOKEN="your_token" \
  -v $(pwd)/chat_settings.json:/app/chat_settings.json \
  --name nure-bot \
  nure-bot
```

---

## 📱 Usage in a group chat

1. Add the bot to your group
2. `/setgroup` → type part of your group name (e.g. `ПЗПІ-24`) → tap your group
3. `/schedule` — see today's classes instantly
4. `/autopost` — enable daily 07:00 post

Each chat independently stores its group selection, so one bot can serve multiple different groups at the same time.

---

## 🗺️ How it works

The bot uses the official **CIST JSON API**:

- `GET /P_API_GROUP_JSON` — all groups with IDs
- `GET /P_API_EVEN_JSON?type_id=1&timetable_id={id}&time_from={ts}&time_to={ts}` — events for a group

It automatically falls back to `cist2.nure.ua` if the primary server is unavailable.

Settings (group ID, autopost flag) are saved locally in `chat_settings.json`.

---

## 📋 Bot commands to register with BotFather

Send this to @BotFather via `/setcommands`:

```
start - Show help
setgroup - Choose your study group
schedule - Today's schedule
tomorrow - Tomorrow's schedule
week - This week's full schedule
autopost - Toggle daily 7am auto-post
status - Show current settings
```

---

## 🛠 Troubleshooting

| Problem | Fix |
|---|---|
| "No groups found" | CIST may be down. Try again in a few minutes |
| Bot doesn't reply | Check token; make sure bot is in the group |
| Wrong timezone | Set `TZ=Europe/Kyiv` or edit `TIMEZONE` in `bot.py` |
| Scheduled post not firing | Make sure the process stays running (use screen/tmux/systemd/Docker) |

---

## 📄 License

MIT — free to use and modify.
