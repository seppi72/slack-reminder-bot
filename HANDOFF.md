# TaskBot — Change Handoff Log

One section per change, newest appended at the bottom. Each entry covers what changed, why it
changed, how to verify it before it goes near production, how to deploy it to the droplet, and
how to back it out if it misbehaves. Four changes are planned in this round; only Step 1 is
done so far.

## Step 1: Per-task notifications

**What changed**

The hourly reminder job used to build one message per person, bundling everything they had due
that hour into a single bulleted post in their private channel. It now posts one message per
task instead, so a person with four tasks due gets four separate reminders rather than one long
one. When several posts are headed to the same channel, the bot pauses about a second between
them so it stays under Slack's per-channel message rate limit.

The bookkeeping that tracks when each task was last reminded also moved. Previously it was
written once at the end, after the person's whole batch had been sent; now each task's timestamp
is written immediately after that task's own message posts successfully. No database columns
were added or changed — only the timing of the writes.

**Why**

Slack allows roughly one message per second per channel, so fanning out per-task posts without
spacing them would risk being throttled or dropped once someone has several tasks due at once.

The per-task timestamping is about partial failures. With the old batched write, a single
transient Slack error partway through a person's batch could leave every task in that batch
unstamped — including tasks whose reminders had already been delivered — so the next hourly run
would nag the assignee again about tasks they'd just been reminded of. A failure could also
abort the rest of the batch, leaving later tasks silently unreminded. Stamping each task the
moment its own post succeeds makes each reminder independent: a failed task is simply retried
next cycle, and its siblings are unaffected in either direction.

**How to test before deploying to the droplet**

- `tests/test_step1_reminders.py` — automated coverage for this step, exercising the
  partial-failure behavior (a task that fails to post does not stamp itself and does not prevent
  its siblings from posting or stamping) and the rate-limit spacing between consecutive posts to
  the same channel.
- `tests/STEP1_CHECKLIST.md` — the manual walkthrough. Run it against a staging copy of
  `tasks.db` (copy the file, point a local run at the copy — never the live database) with a few
  open tasks at different priorities assigned to one person, and confirm that each task arrives
  as its own message, that they arrive spaced out rather than in a burst, and that nothing gets
  re-sent on the following hourly cycle.

**Deploy steps**

1. Pull the change onto the droplet in the checkout the bot runs from.
2. `pm2 restart taskbot`
3. `pm2 logs taskbot` — leave it tailing through the first live hourly cycle inside working
   hours (9am–9pm ET; outside that window the job is a no-op and proves nothing). Confirm no
   exceptions, and sanity-check the pacing: messages to the same channel should land about a
   second apart, not all at once.

**Rollback**

```bash
git checkout -- main.py
pm2 restart taskbot
```

No database rollback is needed — this step adds no new column and changes no schema, so an
older `main.py` runs against the same `tasks.db` unchanged.

## Step 2: /edit command

**What changed**

A new slash command, `/edit <task_id> field:value [field:value ...]`, lets an existing task be
corrected in place instead of being marked done and re-created. Four fields are editable:
`description`, `due`, `priority`, and `remind_from`. As with `/done`, there's no permission
check — anyone can edit any task.

Values are validated the same way `/addtask` validates them: dates must be `YYYY-MM-DD`,
priority must be `HIGH`, `MEDIUM`, or `LOW`, and `remind_from` must fall on or before `due`.
That last check is made against whichever values are actually in effect after the edit — the
newly supplied one where given, the task's existing one otherwise — so editing just the due date
still catches a conflict with a `remind_from` that was set weeks ago.

The edit is atomic: every supplied field is validated before any field is written, so a single
bad value rejects the entire command and leaves the task exactly as it was. Supplying a value
identical to the task's current one is treated as no change — it isn't written and isn't
mentioned in the reply. The reply confirms what actually changed, one line per field, old value
to new value.

One side effect beyond the stored fields: when `priority` genuinely changes to a different
value, the task's `last_reminded_at` is reset to `NULL`. Restating the priority a task already
has does not do this.

No database columns were added or changed.

Because this is a brand-new slash command rather than a change to an existing one, it also has
to be registered in the Slack app's own configuration before it will work — see **Deploy
steps** below. No new bot token scopes are required.

**Why**

Until now the only way to fix a typo'd description, push out a due date, or re-prioritize
something was to close the task and add a replacement, which loses the task ID people have
already referenced and pollutes the weekly report's "completed this week" section with work
that was never actually completed.

Validating everything up front, before touching the row, is what makes the command safe to
retry: a rejected `/edit` leaves nothing half-applied, so the fix is simply to correct the bad
value and run it again. Applying fields as they were parsed would instead have left the task in
a state that depended on which field failed and in what order the fields were typed.

The `last_reminded_at` reset exists because the reminder cadence is driven by time elapsed since
the last reminder. Raising a task from LOW to HIGH is usually an urgent act, but without the
reset the task would keep its old timestamp and stay silent for up to the remainder of the
24-hour LOW interval before the 1-hour HIGH cadence took hold — the opposite of what the person
raising it expects. Clearing the timestamp makes the task eligible on the very next hourly run.
It's deliberately conditional on the value actually changing so that a no-op edit, or an edit
that only touches other fields, can't be used to sidestep the cadence.

**How to test before deploying to the droplet**

- `tests/test_step2_edit.py` — automated coverage for this step: parsing the `field:value`
  argument string (including multi-field edits and the known limitation around unquoted
  descriptions), validation of each field and the cross-field `remind_from <= due` rule against
  post-edit values, the all-or-nothing guarantee that a rejected edit writes nothing, the
  diffing that decides which fields actually changed and what the confirmation reports, and the
  `last_reminded_at` reset firing only on a real priority change.
- `tests/STEP2_CHECKLIST.md` — the manual walkthrough. Run it against a staging copy of
  `tasks.db` (copy the file, point a local run at the copy — never the live database). It
  includes the Slack command-registration step, since `/edit` cannot be exercised by hand at all
  until the command exists in the app config — a local run with correct code will still return
  "command not found" without it.

**Deploy steps**

1. **Register the command in Slack first.** Go to [api.slack.com/apps](https://api.slack.com/apps)
   → this app → **Slash Commands** → **Create New Command**, create `/edit` pointing at this
   same app, and save. Reinstall the app to the workspace if Slack prompts for it. This is a
   manual, one-time action; no restart is involved and no new scopes are needed, but until it's
   done Slack will not route `/edit` to the bot no matter what code is deployed. Doing it before
   the restart also means there's no window where the code is live but the command isn't
   recognized, which is the most likely thing to get reported as a bug.
2. Pull the change onto the droplet in the checkout the bot runs from.
3. `pm2 restart taskbot`
4. `pm2 logs taskbot` — with it tailing, run a real `/edit` against a throwaway task: one
   single-field edit, one multi-field edit, and one deliberately invalid value to confirm it's
   rejected cleanly with the task unchanged. Then bump a task's priority and confirm it's picked
   up on the next hourly cycle inside working hours (9am–9pm ET) rather than sitting out the old
   interval.

**Rollback**

```bash
git checkout -- main.py
pm2 restart taskbot
```

As with Step 1, there's no database rollback — no new column, no schema change, so an older
`main.py` runs against the same `tasks.db` unchanged. The Slack-side command registration needs
no undoing either: with the code rolled back, `/edit` simply has no handler and Slack reports it
as unrecognized again, which is harmless. Leaving it registered means a re-deploy doesn't need
the manual admin step a second time.
