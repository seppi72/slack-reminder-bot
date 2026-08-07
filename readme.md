# TaskBot — Slack Task Reminder Bot

A Slack bot for assigning tasks, nagging people about them until they're done, and
reporting on progress weekly. Each person gets their own private reminder channel
instead of one noisy shared channel.

## How it works, in short

- Assign a task to someone with `/addtask`.
- The bot reminds them in their private channel on a schedule based on the task's
  priority — more urgent priorities nag more often.
- They clear it with `/done`.
- Every Friday at 6pm ET, everyone gets a private weekly summary: what they
  finished, what's overdue, and what's still coming up.

## Setup

1. Install dependencies (Slack Bolt, `apscheduler`, `python-dotenv`).
2. Create a `.env` file next to `main.py` with:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```
3. In `main.py`, fill in the config constants near the top with real values:
   - `TEAM_LEADER_IDS` — Slack member IDs that get auto-invited to every
     registration channel.
   - `OWNER_ID` — Slack member ID always invited to every registration channel.

   These are still placeholders (`UTEAMLEADER1`, `UTEAMLEADER2`, `UOWNERID`) —
   leaving them unset causes confusing, hard-to-diagnose invite failures, so this
   is the first thing to check if `/register` misbehaves.
4. Run with `python main.py` (or under PM2 for production — `pm2 start main.py
   --interpreter python3 --name taskbot`). It connects over Socket Mode, so no
   public URL or inbound webhook is needed.

### Required Slack bot scopes

`chat:write`, `users:read`, `users:read.email`, `commands`, `channels:manage`,
`groups:write`. `/freechannel` additionally needs `channels:read` and
`groups:read`.

## Commands

### `/addtask @person "description" YYYY-MM-DD [HIGH|MEDIUM|LOW] [remind:YYYY-MM-DD]`

Creates a task assigned to `@person`, due on the given date (Eastern Time).

- Priority is optional and defaults to `MEDIUM`.
- `remind:YYYY-MM-DD` is optional and controls when reminders *start* — see
  [Deferred reminders](#deferred-reminders-remind) below. Without it, reminders
  start immediately.
- The person can be a real `@mention` (selected from Slack's dropdown, a linked
  mention, or a plain `@username`) — plain usernames are resolved against a
  cached member list that auto-refreshes on a cache miss.

Examples:
```
/addtask @giuseppe "Follow up with vendor" 2026-08-10
/addtask @giuseppe "Fix checkout bug" 2026-08-08 HIGH
/addtask @giuseppe "Call about renewal" 2026-08-14 MEDIUM remind:2026-08-12
```

### `/done <task_id>`

Marks a task done and confirms in the channel it was run in. DMs the assignee
and the task's creator (whichever of the two didn't run the command) so nobody
has to check the reminder channel to find out.

### `/register @person channel-name` or `/register email@company.com channel-name`

One-time setup per person. Creates a new **private** channel with that exact
name, and invites the assignee, whoever ran the command, `OWNER_ID`, and every
ID in `TEAM_LEADER_IDS` (deduplicated). That channel is where all of that
person's reminders and weekly reports go from then on.

- Channel names are typed manually — there's no auto-generation from a Slack
  profile. Must be lowercase letters/numbers/hyphens/underscores, max 80 chars.
- After inviting, the bot double-checks who actually landed in the channel and
  reports back anyone Slack silently dropped (see [Gotchas](#gotchas)).
- Refuses to register someone who's already registered — unregister them first.

### `/unregister @person` or `/unregister email@company.com`

Posts a notice in their channel, renames it to `{name}-archived-{timestamp}`,
archives it, and removes their registration. There's no hard-delete on Slack's
side, so this is the closest equivalent to removing someone.

### `/freechannel channel-name`

Admin utility. If a channel got archived without going through `/unregister`
(e.g. archived by hand, or archived before the auto-rename fix existed), its
name stays reserved and blocks reuse. This command finds that channel (active
or archived, exact name match), unarchives it if needed, renames it to free the
name up, and re-archives it.

## Reminders

### Hourly job

A scheduled job runs every hour on the hour, Eastern Time. It only actually
sends reminders during working hours (9am–9pm ET) — outside that window it's a
no-op.

### Priority-based cadence

Not every open task gets pinged every hour anymore. Each task is only included
in an hour's reminder batch if enough time has passed since it was last
reminded, based on its priority:

| Priority | Reminded every |
|---|---|
| HIGH | 1 hour |
| MEDIUM | 12 hours |
| LOW | 24 hours |
| Overdue (past due date, any original priority) | 1 hour |

Once a task's due date passes, it's treated as overdue ("BACKLOG") for display
and reminder purposes regardless of what priority it was created with, and goes
back to hourly nagging — the idea being that something overdue deserves more
attention, not less.

Internally this is tracked with a `last_reminded_at` timestamp on each task,
stamped right after a reminder for it is successfully posted. There's a small
5-minute buffer built into the interval check so a task doesn't get skipped by
a few seconds of scheduler jitter right at its boundary.

### Deferred reminders (`remind:`)

For tasks that are real but far out — e.g. something due in two weeks that
shouldn't start bugging anyone today — add `remind:YYYY-MM-DD` to `/addtask`.
The task is created and tracked immediately (it'll show up in the weekly
report), but it stays completely silent in the hourly reminders until that
date arrives. From that date on, it follows the normal priority-based cadence
above.

```
/addtask @person "Call vendor about renewal" 2026-08-14 MEDIUM remind:2026-08-12
```
Creates the task now; reminders start Aug 12 (two days before it's due) and
then repeat every 12 hours until it's marked done or goes overdue.

The `remind:` date must be on or before the due date — the bot rejects it
otherwise.

## Weekly report

Fires every Friday at 6pm ET. Each registered person gets one message in their
own channel with three sections:

- ✅ **Completed this week** — tasks marked done since Monday.
- ⏳ **Backlog / overdue** — still open, past their due date.
- 📌 **To do** — still open, not yet due (includes tasks whose deferred
  `remind:` date hasn't arrived yet).

No `@mentions` — it's posted straight into their own private channel, so
there's nothing to tag.

## Data

SQLite database at `tasks.db`, two tables:

- **`tasks`** — `task_id`, `description`, `assignee_id`, `due_date`, `status`
  (`open`/`done`), `priority`, `created_by`, `completed_at`, `remind_from`
  (the `remind:` date, nullable), `last_reminded_at` (nullable).
- **`registrations`** — `assignee_id` (primary key), `channel_id`,
  `channel_name`, `email`, `registered_by`, `registered_at`.

Schema migrations for new columns run automatically on startup (`init_db()`),
so upgrading an existing deployment is just a restart — no manual `ALTER
TABLE` needed.

## Gotchas

- **`conversations.open` can't set a custom channel name** — that's why
  registration channels use `conversations.create` + `is_private=True`
  instead of a DM/MPDM.
- **Private channels need `groups:write`.** `channels:manage` alone (public
  channels) returns `missing_scope`.
- **`conversations.invite` is atomic by default** — one bad user ID fails the
  whole invite, valid recipients included. `force=True` avoids that, but then
  Slack can return `ok: true` while silently skipping people it couldn't add —
  hence the membership check after every invite.
- **Archiving a channel does not free its name.** It has to be renamed first,
  then archived — and an archived channel can't be renamed directly, so
  freeing a name means unarchive → rename → re-archive (`/freechannel` does
  exactly this).
- **Guest / Slack Connect accounts can't be invited** to a normal channel by
  the bot or manually — this needs a workspace admin to check the account type
  and convert it if appropriate. Not something the bot's scopes can fix.
- **`TEAM_LEADER_IDS` / `OWNER_ID` left as placeholders** is a common cause of
  invite failures that otherwise look unrelated — check these first.

## Open items

- Fill in real values for `TEAM_LEADER_IDS` and `OWNER_ID`.
- Decide whether the weekly report should skip people with nothing in any of
  the three sections, instead of sending a mostly-empty report.
- `REMINDER_CHANNEL_ID` is a leftover from before per-person channels existed;
  currently unused, kept around in case an admin/log channel is wanted later.
