import os
import re
import sqlite3
import logging
from dotenv import load_dotenv
from datetime import datetime, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("task-bot")

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

REMINDER_CHANNEL_ID = "C0BJTQ09GFL"  # hardcoded channel ID — no longer used for reminders, but kept just in case
DB_PATH = os.path.join(os.path.dirname(__file__), "tasks.db")

# fixed team leaders who get added to every per-person registration channel
TEAM_LEADER_IDS = [
    "U0B6L4YQ734",
    "U09453J1QBW",
]

# set timezone to Eastern Time (handles EST/EDT automatically)
EST_TZ = ZoneInfo("America/New_York")


def now_est():
    return datetime.now(EST_TZ)

load_dotenv()

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]

app = App(token=SLACK_BOT_TOKEN)

# priority ---------------------------------------------------------------------------

VALID_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
DEFAULT_PRIORITY = "MEDIUM"

PRIORITY_RANK = {"BACKLOG": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}


def effective_priority(task, today_date):
    """A task's priority for display/sort purposes: BACKLOG if its due date
    has passed, otherwise whatever priority was set on it."""
    due = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
    if due < today_date:
        return "BACKLOG"
    return task["priority"]

# non-working hours ----------------------------------------------------------------------

NON_WORKING_START_HOUR = 21  # 9pm EST — reminders stop at/after this hour
NON_WORKING_END_HOUR = 9     # 9am EST — reminders resume at/after this hour


def is_working_hours(now_dt):
    hour = now_dt.hour
    if NON_WORKING_START_HOUR > NON_WORKING_END_HOUR:
        # window wraps past midnight, e.g. 9pm -> 9am.
        return not (hour >= NON_WORKING_START_HOUR or hour < NON_WORKING_END_HOUR)
    return not (NON_WORKING_START_HOUR <= hour < NON_WORKING_END_HOUR)

# command parsing --------------------------------------------------------------------

MENTION_RE = re.compile(
    r'^\s*(?:<@(?P<user_id>\w+)(?:\|[^>]*)?>|@(?P<username>[A-Za-z0-9_.\-]+))\s+'
)
REST_RE = re.compile(
    r'^["\u201c](?P<description>[^"\u201d]+)["\u201d]\s+'
    r'(?P<due_date>\d{4}-\d{2}-\d{2})'
    r'(?:\s+(?P<priority>HIGH|MEDIUM|LOW))?\s*$',
    re.IGNORECASE,
)

REGISTER_RE = re.compile(r'^\s*(?P<identifier>\S+)\s+(?P<channel_name>\S+)\s*$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
# The first token of /register — either a mention (linked, piped, or plain @username)
# or an email. All three mention formats contain no whitespace, so this matches as a
# single token the same way REGISTER_RE's identifier group expects.
PERSON_TOKEN_RE = re.compile(
    r'^(?:<@(?P<user_id>\w+)(?:\|[^>]*)?>|@(?P<username>[A-Za-z0-9_.\-]+)'
    r'|(?P<email>[^@\s]+@[^@\s]+\.[^@\s]+))$'
)
# Slack channel name rules: lowercase, no spaces/periods, letters/numbers/hyphens/underscores, max 80 chars.
CHANNEL_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
# /unregister takes just a single person reference — either a mention or an email —
# with nothing else after it, so this is a standalone version of MENTION_RE.
UNREGISTER_MENTION_RE = re.compile(
    r'^\s*(?:<@(?P<user_id>\w+)(?:\|[^>]*)?>|@(?P<username>[A-Za-z0-9_.\-]+))\s*$'
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

# DB ---------------------------------------------------------------------------

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
            due_date TEXT NOT NULL,      -- 'YYYY-MM-DD', Eastern Time
            status TEXT NOT NULL DEFAULT 'open',      -- open | done
            priority TEXT NOT NULL DEFAULT 'MEDIUM',  -- HIGH | MEDIUM | LOW
            created_by TEXT NOT NULL,
            completed_at TEXT              -- ISO timestamp (Eastern Time), set when marked done
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS registrations (
            assignee_id TEXT PRIMARY KEY,   -- the team member this channel is for
            channel_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            email TEXT,
            registered_by TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    _migrate_columns()


def _migrate_columns():
    """Add columns to tasks that didn't exist when the table was first created,
    so a production DB from before this change doesn't break on the next deploy."""
    conn = get_conn()
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "completed_at" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
    conn.commit()
    conn.close()


def get_registration_by_assignee(assignee_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM registrations WHERE assignee_id = ?", (assignee_id,)
    ).fetchone()
    conn.close()
    return row


def get_all_registrations():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM registrations").fetchall()
    conn.close()
    return rows


def add_registration(assignee_id, channel_id, channel_name, email, registered_by):
    conn = get_conn()
    conn.execute(
        "INSERT INTO registrations (assignee_id, channel_id, channel_name, email, "
        "registered_by, registered_at) VALUES (?, ?, ?, ?, ?, ?)",
        (assignee_id, channel_id, channel_name, email, registered_by, now_est().isoformat()),
    )
    conn.commit()
    conn.close()


def delete_registration(assignee_id):
    conn = get_conn()
    cur = conn.execute("DELETE FROM registrations WHERE assignee_id = ?", (assignee_id,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted > 0


def add_task(description, assignee_id, due_date, priority, created_by):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO tasks (description, assignee_id, due_date, status, priority, created_by) "
        "VALUES (?, ?, ?, 'open', ?, ?)",
        (description, assignee_id, due_date, priority, created_by),
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
        "UPDATE tasks SET status = 'done', completed_at = ? WHERE task_id = ? AND status = 'open'",
        (now_est().isoformat(), task_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return updated > 0


def get_tasks_for_weekly_report(week_start_iso):
    """Every task relevant to this week's report: still-open tasks (regardless of when
    created) plus tasks completed since week_start_iso."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE status = 'open' "
        "OR (status = 'done' AND completed_at >= ?) "
        "ORDER BY assignee_id, due_date ASC",
        (week_start_iso,),
    ).fetchall()
    conn.close()
    return rows

# slash commands ---------------------------------------------------------------------------

@app.command("/addtask")
def handle_task(ack, respond, command):
    ack()
    text = command.get("text", "")

    mention_match = MENTION_RE.match(text)
    if not mention_match:
        respond(
            'Couldn\'t find a person at the start. Format: '
            '`/addtask @person "description" YYYY-MM-DD [HIGH|MEDIUM|LOW]`\n'
            f"Here's exactly what I received:\n```{text!r}```"
        )
        return

    rest = text[mention_match.end():]
    rest_match = REST_RE.match(rest)
    if not rest_match:
        respond(
            'Got the person, but couldn\'t parse the rest. Format: '
            '`/addtask @person "description" YYYY-MM-DD [HIGH|MEDIUM|LOW]`\n'
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
    due_date = rest_match.group("due_date")

    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        respond("That date doesn't look valid. Use YYYY-MM-DD (Eastern Time).")
        return

    priority_input = rest_match.group("priority")
    priority = priority_input.upper() if priority_input else DEFAULT_PRIORITY

    created_by = command["user_id"]
    task_id = add_task(description, assignee_id, due_date, priority, created_by)

    respond(
        f'Task #{task_id} created for <@{assignee_id}>: "{description}" — '
        f"due {due_date} (ET), priority {priority}"
    )


@app.command("/register")
def handle_register(ack, respond, command):
    ack()
    text = command.get("text", "").strip()

    match = REGISTER_RE.match(text)
    if not match:
        respond(
            "Usage: `/register @person channel-name` or `/register email@company.com channel-name`\n"
            "Example: `/register @giuseppe giuseppe-automations`\n"
            "Example: `/register nicolas@company.com nicolas-ecommerce`"
        )
        return

    identifier = match.group("identifier").strip()
    channel_name = match.group("channel_name").strip().lower()

    if not CHANNEL_NAME_RE.match(channel_name):
        respond(
            f"`{channel_name}` isn't a valid channel name. Use lowercase letters, "
            "numbers, hyphens, or underscores only (no spaces or periods), max 80 characters."
        )
        return

    person_match = PERSON_TOKEN_RE.match(identifier)
    if not person_match:
        respond(
            f"Couldn't parse `{identifier}` as a person. Use a mention (`@person`, selected "
            "from the dropdown) or a valid email address."
        )
        return

    email = None  # only set if registration was done via email lookup

    if person_match.group("user_id"):
        assignee_id = person_match.group("user_id")
    elif person_match.group("username"):
        username = person_match.group("username")
        assignee_id = resolve_username(username)
        if assignee_id is None:
            respond(
                f"Couldn't find a Slack member matching `@{username}`. "
                "Double check the spelling, or try selecting them from the "
                "mention dropdown again."
            )
            return
    else:
        email = person_match.group("email")
        try:
            lookup = app.client.users_lookupByEmail(email=email)
        except SlackApiError as e:
            error_code = e.response.get("error") if e.response else str(e)
            if error_code == "users_not_found":
                respond(f"No Slack member found with email `{email}`.")
            else:
                logger.exception("users_lookupByEmail failed for %s", email)
                respond(f"Slack API error looking up `{email}`: `{error_code}`")
            return
        assignee_id = lookup["user"]["id"]

    # already registered? don't create a duplicate channel.
    existing = get_registration_by_assignee(assignee_id)
    if existing is not None:
        respond(
            f"<@{assignee_id}> is already registered to <#{existing['channel_id']}>. "
            "Remove that mapping first if you need to re-register them."
        )
        return

    created_by = command["user_id"]

    # create the private channel
    try:
        create_resp = app.client.conversations_create(name=channel_name, is_private=True)
    except SlackApiError as e:
        error_code = e.response.get("error") if e.response else str(e)
        if error_code == "name_taken":
            respond(f"A channel named `{channel_name}` already exists. Pick a different name.")
        else:
            logger.exception("conversations_create failed for %s", channel_name)
            respond(f"Slack API error creating channel `{channel_name}`: `{error_code}`")
        return

    channel_id = create_resp["channel"]["id"]

    # invite assignee, the admin running the command, and both team leaders (deduped)
    invite_ids = {assignee_id, created_by, *TEAM_LEADER_IDS}
    invite_ids = list(invite_ids)

    try:
        app.client.conversations_invite(channel=channel_id, users=invite_ids)
    except SlackApiError as e:
        error_code = e.response.get("error") if e.response else str(e)
        # already_in_channel / cant_invite_self type errors are harmless here, but
        # anything else means the channel exists with the wrong membership — flag it.
        if error_code not in ("already_in_channel",):
            logger.exception("conversations_invite failed for channel %s", channel_id)
            respond(
                f"Channel <#{channel_id}> was created, but inviting members failed: "
                f"`{error_code}`. You may need to invite people manually."
            )

    add_registration(assignee_id, channel_id, channel_name, email, created_by)

    try:
        app.client.chat_postMessage(
            channel=channel_id,
            text=(
                f"This channel is set up for <@{assignee_id}>'s task reminders. "
                f"Members: <@{assignee_id}>, <@{created_by}>, "
                + ", ".join(f"<@{tl}>" for tl in TEAM_LEADER_IDS)
            ),
        )
    except Exception:
        logger.exception("Failed to post welcome message to channel %s", channel_id)

    respond(f"Registered <@{assignee_id}> — created <#{channel_id}> (`{channel_name}`).")


@app.command("/unregister")
def handle_unregister(ack, respond, command):
    ack()
    text = command.get("text", "").strip()

    if not text:
        respond("Usage: `/unregister @person` or `/unregister email@company.com`")
        return

    assignee_id = None

    mention_match = UNREGISTER_MENTION_RE.match(text)
    if mention_match:
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
    elif EMAIL_RE.match(text):
        try:
            lookup = app.client.users_lookupByEmail(email=text)
        except SlackApiError as e:
            error_code = e.response.get("error") if e.response else str(e)
            if error_code == "users_not_found":
                respond(f"No Slack member found with email `{text}`.")
            else:
                logger.exception("users_lookupByEmail failed for %s", text)
                respond(f"Slack API error looking up `{text}`: `{error_code}`")
            return
        assignee_id = lookup["user"]["id"]
    else:
        respond(
            "Couldn't parse that. Usage: `/unregister @person` or "
            "`/unregister email@company.com`"
        )
        return

    registration = get_registration_by_assignee(assignee_id)
    if registration is None:
        respond(f"<@{assignee_id}> isn't registered — nothing to do.")
        return

    channel_id = registration["channel_id"]
    channel_name = registration["channel_name"]

    # Slack's API has no true "delete channel" endpoint — conversations.archive is
    # the closest equivalent (hides it, blocks new messages, can be un-archived by
    # an admin later if needed). Let the member know why the channel is going away
    # before archiving, best-effort.
    try:
        app.client.chat_postMessage(
            channel=channel_id,
            text=f"This channel (`{channel_name}`) is being unregistered and archived.",
        )
    except Exception:
        logger.exception("Failed to post unregister notice to channel %s", channel_id)

    archive_status = "archived"
    try:
        app.client.conversations_archive(channel=channel_id)
    except SlackApiError as e:
        error_code = e.response.get("error") if e.response else str(e)
        if error_code == "already_archived":
            archive_status = "was already archived"
        elif error_code == "channel_not_found":
            archive_status = "channel no longer exists (already deleted/archived elsewhere)"
        else:
            logger.exception("conversations_archive failed for channel %s", channel_id)
            archive_status = f"FAILED to archive (`{error_code}`) — you may need to archive it manually"

    delete_registration(assignee_id)

    respond(
        f"Unregistered <@{assignee_id}>. Channel `{channel_name}` (<#{channel_id}>): {archive_status}. "
        "Their registration mapping has been removed either way — any of their open tasks "
        "will now be skipped by reminders/reports until they're registered again."
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

    # confirm to the assignee (if the caller wasn't the assignee) and the creator.
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

# hourly reminders ---------------------------------------------------------------------------

def build_assignee_block(assignee_id, tasks, today_date):
    tasks_sorted = sorted(
        tasks,
        key=lambda t: (
            PRIORITY_RANK[effective_priority(t, today_date)],
            t["due_date"],
        ),
    )
    lines = [
        f'• [{effective_priority(t, today_date)}] #{t["task_id"]} '
        f'{t["description"]} — due {t["due_date"]}'
        for t in tasks_sorted
    ]
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"<@{assignee_id}>\n" + "\n".join(lines),
        },
    }


def send_hourly_reminders():
    now = now_est()

    if not is_working_hours(now):
        logger.info("Outside working hours (EST), skipping reminder.")
        return

    tasks = get_open_tasks()

    if not tasks:
        logger.info("No open tasks, skipping reminder post.")
        return

    grouped = defaultdict(list)
    for t in tasks:
        grouped[t["assignee_id"]].append(t)

    posted, unregistered = 0, []

    for assignee_id, assignee_tasks in grouped.items():
        registration = get_registration_by_assignee(assignee_id)
        if registration is None:
            unregistered.append(assignee_id)
            continue

        block = build_assignee_block(assignee_id, assignee_tasks, now.date())

        # Slack notification previews (mobile push, desktop banner, sidebar unread)
        # are driven by this top-level "text", not by the blocks — so it needs to
        # actually name the tasks, not just say "reminders".
        count = len(assignee_tasks)
        overdue_count = sum(
            1 for t in assignee_tasks if effective_priority(t, now.date()) == "BACKLOG"
        )
        summary = f"You have {count} open task{'s' if count != 1 else ''}"
        if overdue_count:
            summary += f", {overdue_count} overdue"

        try:
            app.client.chat_postMessage(
                channel=registration["channel_id"],
                text=summary,
                blocks=[block],
            )
            posted += 1
        except Exception:
            logger.exception(
                "Failed to post reminders to channel %s for assignee %s",
                registration["channel_id"],
                assignee_id,
            )

    if unregistered:
        logger.warning(
            "Open tasks exist for unregistered assignee(s), skipped: %s",
            ", ".join(unregistered),
        )

    logger.info("Posted reminders to %d registered channel(s).", posted)

# weekly report ---------------------------------------------------------------------------

def build_weekly_report_text(assignee_tasks, week_start_date, today_date):
    """Plain-text report body. Deliberately no <@user> mentions — this posts into
    the person's own private channel, so there's nothing to tag."""
    done = [t for t in assignee_tasks if t["status"] == "done"]
    open_tasks = [t for t in assignee_tasks if t["status"] == "open"]
    backlog = [t for t in open_tasks if effective_priority(t, today_date) == "BACKLOG"]
    todo = [t for t in open_tasks if effective_priority(t, today_date) != "BACKLOG"]

    lines = [
        f"*Weekly Report — {week_start_date.strftime('%b %d')} to {today_date.strftime('%b %d')}*"
    ]

    lines.append(f"\n✅ *Completed this week ({len(done)})*")
    if done:
        for t in sorted(done, key=lambda t: t["completed_at"] or ""):
            done_date = t["completed_at"][:10] if t["completed_at"] else "unknown date"
            lines.append(f'• #{t["task_id"]} {t["description"]} (done {done_date})')
    else:
        lines.append("_none_")

    lines.append(f"\n⏳ *Backlog / overdue ({len(backlog)})*")
    if backlog:
        for t in sorted(backlog, key=lambda t: t["due_date"]):
            lines.append(f'• #{t["task_id"]} {t["description"]} — was due {t["due_date"]}')
    else:
        lines.append("_none_")

    lines.append(f"\n📌 *To do ({len(todo)})*")
    if todo:
        for t in sorted(todo, key=lambda t: (PRIORITY_RANK[t["priority"]], t["due_date"])):
            lines.append(
                f'• [{t["priority"]}] #{t["task_id"]} {t["description"]} — due {t["due_date"]}'
            )
    else:
        lines.append("_none_")

    return "\n".join(lines)


def send_weekly_reports():
    now = now_est()
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    week_start_iso = week_start.isoformat()

    tasks = get_tasks_for_weekly_report(week_start_iso)
    grouped = defaultdict(list)
    for t in tasks:
        grouped[t["assignee_id"]].append(t)

    registrations = get_all_registrations()
    if not registrations:
        logger.info("No registered members, skipping weekly report.")
        return

    sent, failed = 0, []

    for reg in registrations:
        assignee_id = reg["assignee_id"]
        assignee_tasks = grouped.get(assignee_id, [])
        report_text = build_weekly_report_text(assignee_tasks, week_start.date(), now.date())

        try:
            app.client.chat_postMessage(
                channel=reg["channel_id"],
                text=f"Weekly report — {now.strftime('%b %d')}",
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": report_text}}],
            )
            sent += 1
        except Exception:
            logger.exception("Failed to post weekly report to channel %s", reg["channel_id"])
            failed.append(assignee_id)

    logger.info("Weekly report sent to %d channel(s).", sent)
    if failed:
        logger.warning("Weekly report failed for assignee(s): %s", ", ".join(failed))

# entry point ---------------------------------------------------------------------------

def main():
    init_db()

    scheduler = BackgroundScheduler(timezone=EST_TZ)

    # fires every hour on the hour, in EST/EDT
    scheduler.add_job(
        send_hourly_reminders,
        trigger="cron",
        minute=0,
        id="hourly_reminder",
    )

    # fires on fridays at 6pm, in EST/EDT
    scheduler.add_job(
        send_weekly_reports,
        trigger="cron",
        day_of_week="fri",
        hour=18,
        minute=0,
        id="weekly_report",
    )

    scheduler.start()

    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    logger.info("Bot starting (Socket Mode)...")
    handler.start()


if __name__ == "__main__":
    main()