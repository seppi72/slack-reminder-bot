# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TaskBot: a Slack bot (Slack Bolt, Socket Mode) for assigning tasks, reminding assignees on a
priority-based cadence until they're done, and posting a weekly summary. Each assignee gets
their own private Slack channel instead of one shared noisy channel. The entire implementation
is a single file, `main.py`.

## Commands

```bash
pip install -r requirements.txt   # slack_bolt, apscheduler, dotenv (python-dotenv)
python main.py                    # run the bot (Socket Mode — no public URL needed)
```

Production is run under PM2: `pm2 start main.py --interpreter python3 --name taskbot`.

There is no test suite, linter, or build step in this repo.

Required `.env` (next to `main.py`):
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

Required Slack bot scopes: `chat:write`, `users:read`, `users:read.email`, `commands`,
`channels:manage`, `groups:write`. `/freechannel` additionally needs `channels:read` and
`groups:read`.

Before this bot is functional, the config constants near the top of `main.py` must be filled
in with real Slack member IDs: `TEAM_LEADER_IDS` (auto-invited to every registration channel)
and `OWNER_ID` (always invited to every registration channel). Left as placeholders, these
cause confusing invite failures that look unrelated to config — check them first when
`/register` misbehaves.

## Architecture

Everything lives in `main.py`, organized into clearly marked sections (search for the `# ---`
banner comments): config, priority, non-working hours, command parsing, DB, slash commands,
hourly reminders, weekly report, entry point.

**Storage**: SQLite at `tasks.db` (created next to `main.py`), two tables — `tasks` and
`registrations`. `init_db()` creates both and then runs `_migrate_columns()`, which
`ALTER TABLE`s in any columns added since the table was first created. This means upgrading a
deployment is just a restart, never a manual migration — new columns must be added to
`_migrate_columns()`, not just the `CREATE TABLE` statement, or existing production DBs won't
pick them up.

**Registration model**: a person must be `/register`ed (one real private Slack channel per
assignee, tracked in `registrations` keyed by `assignee_id`) before their tasks generate
reminders or appear in weekly reports. Unregistering renames-then-archives the channel (Slack
has no hard delete) and deletes the mapping; open tasks for an unregistered assignee are
silently skipped by reminders/reports rather than erroring.

**Command parsing**: slash commands are parsed with hand-written regexes rather than Slack's
structured input — `MENTION_RE`/`REST_RE` for `/addtask`, `REGISTER_RE`/`PERSON_TOKEN_RE` for
`/register`, etc. A person reference can be a real Slack mention, a plain `@username`, or an
email; plain usernames are resolved via `resolve_username()` against `_user_cache`, an
in-memory cache of the workspace's member list that lazily refreshes on first use and on cache
misses (new members, renamed handles).

**Reminder cadence** (`send_hourly_reminders`, scheduled hourly via APScheduler): not every
open task is reminded every run. A task is only included if enough time has passed since its
`last_reminded_at`, per `REMINDER_INTERVAL_HOURS` (keyed by priority: HIGH/MEDIUM/LOW, plus
BACKLOG for overdue tasks). Once a task's due date passes it becomes "BACKLOG" for display and
reminder purposes regardless of its original priority, and reverts to hourly nagging —
`effective_priority()` is the single source of truth for this and must be used anywhere a
task's current (as opposed to originally-set) priority matters. Reminders are also silent
entirely outside 9am–9pm ET (`is_working_hours`), and a task with a future `remind_from`
(`remind:` flag on `/addtask`) stays silent until that date regardless of priority. Each due
task gets its own `chat_postMessage` (one per-task block builder, no bundling of an assignee's
tasks into a single message), with `REMINDER_POST_DELAY_SECONDS` (~1.1s) slept between
consecutive posts to the same channel to stay under Slack's ~1 msg/sec/channel limit — so
posting is deliberately not the place to add more per-task Slack API calls. `last_reminded_at`
is stamped per task immediately after that task's own post succeeds, never batched for the
whole assignee at the end; keep it that way, since a mid-loop failure would otherwise either
double-remind the tasks that already posted or block the ones after it.

**Weekly report** (`send_weekly_reports`, scheduled Fridays 6pm ET): one message per registered
person in their own channel, split into completed-this-week / backlog-overdue / still-to-do.
Deliberately has no `@mentions` since it's posted into the person's own private channel.

**Timezone**: all scheduling and date logic is Eastern Time (`EST_TZ` / `now_est()`), including
the APScheduler instance itself (`BackgroundScheduler(timezone=EST_TZ)`) — don't introduce
naive `datetime.now()` calls for anything user-facing or reminder-related.

## Slack API gotchas (from lived experience in this codebase)

- `conversations.open` can't set a custom channel name — registration channels use
  `conversations.create(is_private=True)` instead of a DM/MPDM.
- Private channels need the `groups:write` scope; `channels:manage` alone only covers public
  channels and returns `missing_scope`.
- `conversations.invite` is atomic by default (one bad user ID fails the whole call), so invites
  use `force=True` — but that means Slack can return `ok: true` while silently skipping members
  it couldn't add (wrong workspace, restricted account). Always follow an invite with a
  `conversations.members` check against who was actually added, as `/register` does.
- Archiving a channel does not free its name for reuse — it has to be renamed first (and an
  archived channel can't be renamed directly, so freeing a name means unarchive → rename →
  re-archive). `/unregister` and `/freechannel` both do this dance.
- Guest / Slack Connect accounts can't be invited to a normal channel by the bot (or manually) —
  that needs a workspace admin, not a bot scope change.
