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

Required `.env` (next to `main.py`; copy `.env.example` as a starting point). Every
workspace-identifying value — tokens, channel/user IDs — lives here, never hardcoded in
`main.py`, since `.env` is git-ignored and nothing workspace-specific should end up committed:
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
TEAM_LEADER_IDS=U0123ABCD,U0456EFGH   # comma-separated
OWNER_ID=U0789IJKL
REMINDER_CHANNEL_ID=                  # optional, currently unused
```

Required Slack bot scopes: `chat:write`, `users:read`, `users:read.email`, `commands`,
`channels:manage`, `groups:write`. `/freechannel` additionally needs `channels:read` and
`groups:read`. The `/addtask` and `/edit` modals and the Done/Edit reminder buttons need no
additional scopes, but **do** need "Interactivity & Shortcuts" turned on for the app at
api.slack.com/apps (no Request URL required under Socket Mode) — a one-time manual config step,
the same category as registering a new slash command.

Before this bot is functional, `.env` must have real Slack member IDs for `TEAM_LEADER_IDS`
(auto-invited to every registration channel) and `OWNER_ID` (always invited to every
registration channel). Left unset, `main.py` logs a startup warning, but invites will still be
incomplete — check `.env` first when `/register` misbehaves.

## Architecture

Everything lives in `main.py`, organized into clearly marked sections (search for the `# ---`
banner comments): config, priority, non-working hours, command parsing, DB, interactive
components (modals & buttons), slash commands, hourly reminders, weekly report, entry point.

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

**Task editing** (`/edit <task_id> field:value [field:value ...]`): editable fields are
`description`, `due`, `priority`, `remind_from`; no permission check (anyone can edit any task,
matching `/done`), and no new DB columns. Parsing is field-name-based — the argument string is
split on the known `field:` tokens, so unlike `/addtask` (whose description must be quoted)
there is no quoting mechanism, and a `description` value containing literal `due:`/`priority:`/
`remind_from:` text will be misparsed as the start of a new field. Validation and applying the
edit are `validate_edit_fields()`/`apply_edit()` — the single source of truth used by both the
typed command and the edit modal (see below), so a rule only has to be written once. Validation
mirrors `/addtask`'s (date format, priority in HIGH/MEDIUM/LOW, `remind_from` on or before
`due`) and is atomic: every supplied field is validated first, and only if all pass is anything
written, so a bad value never leaves a half-applied edit. The `remind_from <= due` check uses
the post-edit values — the newly supplied one where given, the task's stored one otherwise — so
it must not be evaluated against the old row. A field set to the value it already holds is a
no-op: not written, not reported in the confirmation. Only an *actual* priority change (old !=
new) clears `last_reminded_at` to `NULL`, so the new cadence starts on the next hourly run
rather than serving out the remainder of the old interval; restating the current priority must
not reset it. Note that `/edit` is a slash command and must be registered in the Slack app
config (api.slack.com/apps → Slash Commands) before Slack routes it to the bot — a deploy alone
is not enough, though no new scopes are needed.

**Modals & reminder buttons** (mobile-friendly alternative to typing the commands above):
`/addtask` with no arguments, and `/edit <task_id>` with a bare task id and no `field:value`
pairs, open a Block Kit modal (`build_addtask_modal()`/`build_edit_modal()`) instead of showing
a usage error — a real user-picker, date-pickers, and a priority select, pre-filled from the
task's current row for edits. Typed usage of both commands is completely unchanged; the modals
are additive. Modal submissions are handled by `@app.view("addtask_modal")` and
`@app.view("edit_task_modal")`; the edit one calls the same `validate_edit_fields()`/
`apply_edit()` functions the typed `/edit` uses, so there's exactly one rulebook for what's a
valid edit — a modal field error surfaces inline via `ack(response_action="errors", ...)`
rather than a chat reply. Both modals carry the invoking channel (and, for edits, the task id)
through `private_metadata` as JSON, since a view submission payload has no `channel_id` of its
own; the confirmation posts back as a `chat_postEphemeral` into that channel. Every reminder
message (`build_task_block()`) also gets a "✅ Done" / "✏️ Edit" actions row — `mark_done_btn`
reuses `mark_done()` and a shared `notify_done()` helper (also used by `/done`) and replaces the
message via `chat_update` so the buttons can't be double-clicked; `edit_task_btn` opens the same
edit modal `/edit <task_id>` would. None of this needs new OAuth scopes, but it does need
"Interactivity & Shortcuts" turned on for the app (see Commands section above) — without it,
Slack silently fails to deliver the modal-open request and view submissions.

**Reminder cadence** (`send_hourly_reminders`, scheduled hourly via APScheduler): not every
open task is reminded every run. A task is only included if enough time has passed since its
`last_reminded_at`, per `REMINDER_INTERVAL_HOURS` (keyed by priority: HIGH/MEDIUM/LOW, plus
BACKLOG for overdue tasks). Once a task's due date passes it becomes "BACKLOG" for display and
reminder purposes regardless of its original priority, and reverts to hourly nagging —
`effective_priority()` is the single source of truth for this and must be used anywhere a
task's current (as opposed to originally-set) priority matters. Reminders are also silent
entirely outside 9am–9pm ET (`is_working_hours`), and a task with a future `remind_from`
(`remind:` flag on `/addtask`) stays silent until that date regardless of priority. Each due
task gets its own `chat_postMessage` (one per-task block builder returning a section + a
Done/Edit actions row, no bundling of an assignee's tasks into a single message), with
`REMINDER_POST_DELAY_SECONDS` (~1.1s) slept between
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
