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
2. Create a `.env` file next to `main.py` (copy `.env.example` as a starting point)
   with:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   TEAM_LEADER_IDS=U0123ABCD,U0456EFGH
   OWNER_ID=U0789IJKL
   ```
   - `TEAM_LEADER_IDS` — comma-separated Slack member IDs that get auto-invited to
     every registration channel.
   - `OWNER_ID` — Slack member ID always invited to every registration channel.

   Every workspace-identifying value (tokens, channel/user IDs) lives in `.env`,
   never hardcoded in `main.py` — `.env` is git-ignored, so nothing workspace-specific
   is ever committed. Leaving `OWNER_ID`/`TEAM_LEADER_IDS` unset causes confusing,
   hard-to-diagnose invite failures (the bot logs a startup warning if either is
   missing), so this is the first thing to check if `/register` misbehaves.
4. Run with `python main.py` (or under PM2 for production — `pm2 start main.py
   --interpreter python3 --name taskbot`). It connects over Socket Mode, so no
   public URL or inbound webhook is needed.

### Required Slack bot scopes

`chat:write`, `users:read`, `users:read.email`, `commands`, `channels:manage`,
`groups:write`. `/freechannel` additionally needs `channels:read` and
`groups:read`. `/edit` needs no additional scopes — but it does need the
one-time registration step below.

### Registering the `/edit` slash command

**This is a manual, one-time Slack-admin action, and `git pull` + `pm2 restart`
does not do it for you.** A brand-new slash command has to exist in the Slack
app's own configuration before Slack will route it to the bot — until then
`/edit` just isn't a command as far as Slack is concerned, no matter what the
deployed code does. This is completely separate from bot token scopes; `/edit`
requires no new ones.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and open this app.
2. **Slash Commands** → **Create New Command**.
3. Command: `/edit`. Point it at this same app (Socket Mode means there's no
   request URL to fill in — if the field is required, any placeholder URL works,
   since Socket Mode delivers the payload over the existing connection).
4. Give it a short description and usage hint, e.g.
   `/edit <task_id> field:value [field:value ...]`, then save.
5. If Slack prompts to reinstall the app to the workspace, do so.

Existing commands (`/addtask`, `/done`, `/register`, `/unregister`,
`/freechannel`) are already registered and don't need this.

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

### `/edit <task_id> field:value [field:value ...]`

Changes one or more fields on an existing task without deleting and re-creating
it. Like `/done`, there's no permission check — anyone can edit any task.

Editable fields:

| Field | Value |
|---|---|
| `description` | New task text. |
| `due` | New due date, `YYYY-MM-DD` (Eastern Time). |
| `priority` | `HIGH`, `MEDIUM`, or `LOW`. |
| `remind_from` | New deferred-reminder start date, `YYYY-MM-DD` — see [Deferred reminders](#deferred-reminders-remind). |

Notes:

- **Same validation as `/addtask`.** Dates must be `YYYY-MM-DD`, priority must be
  one of the three values, and `remind_from` must still be on or before `due` —
  checked against whichever of the two ends up in effect after the edit (the new
  value if you supplied one, otherwise the task's current value).
- **All-or-nothing.** Every supplied field is validated before any of them is
  applied, so one bad value rejects the whole command and the task is left
  completely untouched. There's no partial edit to clean up.
- **Changing the priority restarts the reminder clock.** If `priority` actually
  changes to a different value, the task's `last_reminded_at` is cleared, so the
  new priority's cadence takes effect on the next hourly run instead of waiting
  out the remainder of the old interval. Restating the priority it already has
  is a no-op and does *not* reset the clock.
- **Setting a field to the value it already has counts as no change** — it isn't
  applied and isn't listed in the confirmation.
- The reply confirms exactly what changed, one line per field, as `old -> new`.
- There's no quoting for `/edit` values (unlike `/addtask`'s required quoted
  description), so a description containing something that looks like another
  field — text with `due:` or `priority:` in it — will be misread as the start
  of a new field. Reword it, or set the description via a separate `/edit`.

Examples:
```
/edit 42 due:2026-08-20
/edit 42 description:Follow up with vendor about the renewal due:2026-08-20 priority:HIGH
/edit 42 priority:HIGH
```
The first pushes the due date out and leaves everything else alone. The second
changes three fields at once — and if any one of them were invalid, none would
be applied. The third bumps the priority to `HIGH` and clears the reminder
timestamp, so the task starts nagging hourly right away rather than at the end
of the old MEDIUM/LOW interval.

**`/edit` needs no new bot scopes, but it does need to be registered as a slash
command in the Slack app config before Slack will route it to the bot at all** —
see [Registering the `/edit` slash command](#registering-the-edit-slash-command).

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

Each task that's due for a reminder gets its own separate message in the
assignee's channel, rather than one combined message listing everything at
once. That makes each reminder easier to read and act on, and means a task
can be marked done without hunting for it inside a longer list. When someone
has several tasks due in the same hour, the posts to their channel are spaced
out to stay under Slack's per-channel rate limit, so they arrive a second or
so apart instead of all at once.

Because each task is posted on its own, one task failing to send (a transient
Slack error, say) no longer holds up the rest of that person's reminders —
the others still go out, and only the failed one is retried on the next hourly
run.

### Priority-based cadence

Not every open task gets pinged every hour anymore. Each task is only reminded
on a given run if enough time has passed since it was last reminded, based on
its priority:

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
stamped individually the moment that task's own reminder posts successfully —
so a task that did get reminded never gets nagged twice just because another
of that person's reminders failed. There's a small 5-minute buffer built into
the interval check so a task doesn't get skipped by a few seconds of scheduler
jitter right at its boundary.

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
- **`TEAM_LEADER_IDS` / `OWNER_ID` unset in `.env`** is a common cause of
  invite failures that otherwise look unrelated — check these first (the bot
  logs a startup warning if either is missing).
- **Deploying a new slash command isn't enough to make it exist.** The command
  also has to be created in the Slack app config, or Slack never routes it to
  the bot and users just see "command not found" against perfectly working
  deployed code. Nothing to do with scopes — see
  [Registering the `/edit` slash command](#registering-the-edit-slash-command).

## Open items

- Fill in real values for `TEAM_LEADER_IDS` and `OWNER_ID` in `.env`.
- Decide whether the weekly report should skip people with nothing in any of
  the three sections, instead of sending a mostly-empty report.
- `REMINDER_CHANNEL_ID` is a leftover from before per-person channels existed;
  currently unused, kept around in case an admin/log channel is wanted later.
