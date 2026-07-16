# Slack Task Reminder Bot

Receives tasks, stores in SQLite, posts a daily grouped reminder to ONE channel, stops reminding once a task is marked DONE.

## Setup

1. Create a Slack app at api.slack.com/apps
2. Enable Socket Mode, generate an app-level token (`xapp-...`) with `connections:write`
3. Bot token scopes: `chat:write`, `users:read`
4. Add slash commands: `/task` and `/done`
5. Invite the bot to your reminder channel, grab the channel ID, set it in `bot.py`:
   ```python
   REMINDER_CHANNEL_ID = "C0XXXXXXX"
   ```
6. Install deps:
   ```
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
7. Copy `.env.example` to `.env` and fill in your tokens, or export them directly:
   ```
   export SLACK_BOT_TOKEN=xoxb-...
   export SLACK_APP_TOKEN=xapp-...
   ```

## Run

```
.venv/bin/python bot.py
```

## Run under PM2

```
pm2 start .venv/bin/python --name task-bot --interpreter none -- bot.py
pm2 save
```

(Set `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` in your shell env or an `ecosystem.config.js` env block before starting — PM2 doesn't read `.env` automatically.)

## Usage

Create a task:
```
/task @person "Write the Q3 report" 2026-07-20 17:00
```

Mark done (works from anywhere, DM or channel):
```
/done 4
```

Daily reminder posts to `REMINDER_CHANNEL_ID` at 9:00 AM server time (change `REMINDER_HOUR` / `REMINDER_MINUTE` in `bot.py`). Assignees with no open tasks are skipped. Tasks are sorted by due date within each assignee's group.

## Out of scope for v1

Calendar sync, escalation/urgency logic, digest summaries, per-channel posting. Add later once this loop is solid.