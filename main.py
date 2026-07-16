import os
import re
import sqlite3
import logging
from dotenv import load_dotenv
from datetime import datetime
from collections import defaultdict

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("task-bot")

# CONFIGURATION -----------------------------------------------------------------------

REMINDER_CHANNEL_ID = "C0BGU7Z0YHJ"  # hardcoded for now, could be made configurable later
DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")
 
# --- Reminder schedule -----------------------------------------------------
# Testing: fire every N minutes. Swap to the cron block below for production
# (once a day at a fixed time).
REMINDER_INTERVAL_MINUTES = 1

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
 
app = App(token=SLACK_BOT_TOKEN)
 
MENTION_RE = re.compile(
    r'^\s*(?:<@(?P<user_id>\w+)(?:\|[^>]*)?>|@(?P<username>[A-Za-z0-9_.\-]+))\s+'
)
REST_RE = re.compile(
    r'^["\u201c](?P<description>[^"\u201d]+)["\u201d]\s+'
    r'(?P<due_date>\d{4}-\d{2}-\d{2})\s+(?P<due_time>\d{2}:\d{2})\s*$'
)
 
_user_cache = {"by_username": {}, "fetched_at": None}
 
 
def _refresh_user_cache():
    by_username = {}
    cursor = None
    while True:
        resp = app.client.users_list(cursor=cursor, limit=200)
        for member in resp.get("members", []):
            profile = member.get("profile", {})
            candidates = {
                member.get("name"),
                profile.get("display_name"),
                profile.get("display_name_normalized"),
                profile.get("real_name"),
                profile.get("real_name_normalized"),
            }
            for name in candidates:
                if name:
                    by_username[name.strip().lower()] = member["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    _user_cache["by_username"] = by_username
    _user_cache["fetched_at"] = datetime.now()
 
 
def resolve_username(username, force_refresh=False):
    """Look up a Slack user ID from a plain @username. Refreshes cache on miss."""
    key = username.strip().lstrip("@").lower()
 
    if not _user_cache["by_username"] or force_refresh:
        _refresh_user_cache()
 
    user_id = _user_cache["by_username"].get(key)
    if user_id is None:
        # Cache may be stale (new member, renamed handle) — refresh once and retry.
        _refresh_user_cache()
        user_id = _user_cache["by_username"].get(key)
 
    return user_id
 
# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
 
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
 
 
def init_db():
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            assignee_id TEXT NOT NULL,
            due_date TEXT NOT NULL,      -- ISO 'YYYY-MM-DD HH:MM'
            status TEXT NOT NULL DEFAULT 'open',  -- open | done
            created_by TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
 
 
def add_task(description, assignee_id, due_date, created_by):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO tasks (description, assignee_id, due_date, status, created_by) "
        "VALUES (?, ?, ?, 'open', ?)",
        (description, assignee_id, due_date, created_by),
    )
    conn.commit()
    task_id = cur.lastrowid
    conn.close()
    return task_id
 
 
def get_open_tasks():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'open' ORDER BY assignee_id, due_date ASC"
    ).fetchall()
    conn.close()
    return rows
 
 
def get_task(task_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    return row
 
 
def mark_done(task_id):
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tasks SET status = 'done' WHERE task_id = ? AND status = 'open'",
        (task_id,),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return updated > 0
 
 
# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
 
@app.command("/addtask")
def handle_task(ack, respond, command):
    ack()
    text = command.get("text", "")
 
    mention_match = MENTION_RE.match(text)
    if not mention_match:
        respond(
            'Couldn\'t find a person at the start. Format: '
            '`/addtask @person "description" YYYY-MM-DD HH:MM`\n'
            f"Here's exactly what I received:\n```{text!r}```"
        )
        return
 
    rest = text[mention_match.end():]
    rest_match = REST_RE.match(rest)
    if not rest_match:
        respond(
            'Got the person, but couldn\'t parse the rest. Format: '
            '`/addtask @person "description" YYYY-MM-DD HH:MM`\n'
            f"Here's exactly what I received:\n```{text!r}```"
        )
        return
 
    if mention_match.group("user_id"):
        assignee_id = mention_match.group("user_id")
    else:
        username = mention_match.group("username")
        assignee_id = resolve_username(username)
        if assignee_id is None:
            respond(
                f"Couldn't find a Slack member matching `@{username}`. "
                "Double check the spelling, or try selecting them from the "
                "mention dropdown again."
            )
            return
 
    description = rest_match.group("description").strip()
    due_date = f'{rest_match.group("due_date")} {rest_match.group("due_time")}'
 
    try:
        datetime.strptime(due_date, "%Y-%m-%d %H:%M")
    except ValueError:
        respond("That date/time doesn't look valid. Use YYYY-MM-DD HH:MM.")
        return
 
    created_by = command["user_id"]
    task_id = add_task(description, assignee_id, due_date, created_by)
 
    respond(
        f"Task #{task_id} created for <@{assignee_id}>: \"{description}\" — due {due_date}"
    )
 
 
@app.command("/done")
def handle_done(ack, respond, command):
    ack()
    text = command.get("text", "").strip()
 
    if not text.isdigit():
        respond("Usage: `/done <task_id>`")
        return
 
    task_id = int(text)
    task = get_task(task_id)
 
    if task is None:
        respond(f"No task #{task_id} found.")
        return
 
    if task["status"] == "done":
        respond(f"Task #{task_id} is already marked done.")
        return
 
    mark_done(task_id)
 
    respond(f'Task #{task_id} ("{task["description"]}") marked done.')
 
    # Confirm to the assignee (if the caller wasn't the assignee) and the creator.
    notify_ids = {task["assignee_id"], task["created_by"]}
    notify_ids.discard(command["user_id"])
    for user_id in notify_ids:
        try:
            app.client.chat_postMessage(
                channel=user_id,
                text=f'Task #{task_id} ("{task["description"]}") was marked done.',
            )
        except Exception:
            logger.exception("Failed to DM %s about task #%s", user_id, task_id)
 
 
# ---------------------------------------------------------------------------
# Daily reminder job
# ---------------------------------------------------------------------------
 
def build_reminder_blocks(tasks_by_assignee):
    blocks = []
    for assignee_id, tasks in tasks_by_assignee.items():
        lines = [
            f'• #{t["task_id"]} {t["description"]} — due {t["due_date"]}' for t in tasks
        ]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<@{assignee_id}>\n" + "\n".join(lines),
                },
            }
        )
    return blocks
 
 
def send_daily_reminders():
    tasks = get_open_tasks()
 
    if not tasks:
        logger.info("No open tasks, skipping reminder post.")
        return
 
    grouped = defaultdict(list)
    for t in tasks:
        grouped[t["assignee_id"]].append(t)
 
    blocks = build_reminder_blocks(grouped)
 
    try:
        app.client.chat_postMessage(
            channel=REMINDER_CHANNEL_ID,
            text="Daily task reminders",  # fallback text for notifications
            blocks=blocks,
        )
        logger.info("Posted reminders for %d assignee(s).", len(grouped))
    except Exception:
        logger.exception("Failed to post daily reminders.")
 
 
# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
 
def main():
    init_db()
 
    scheduler = BackgroundScheduler()
 
    # TESTING (current): fire every REMINDER_INTERVAL_MINUTES minutes.
    scheduler.add_job(
        send_daily_reminders,
        trigger="interval",
        minutes=REMINDER_INTERVAL_MINUTES,
        id="daily_reminder",
    )
 
    # PRODUCTION: once a day at a fixed time. Comment out the interval block
    # above and uncomment this instead:
    #
    # scheduler.add_job(
    #     send_daily_reminders,
    #     trigger="cron",
    #     hour=9,      # 24h, local server time
    #     minute=0,
    #     id="daily_reminder",
    # )
 
    scheduler.start()
 
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Bot starting (Socket Mode)...")
    handler.start()
 
 
if __name__ == "__main__":
    main()