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
