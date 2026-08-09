# Step 1 manual test checklist — one reminder message per task

Step 1 changed `send_hourly_reminders()` from posting **one Block Kit message per assignee**
(listing all their due tasks) to posting **one `chat_postMessage` per task**, paced by
`REMINDER_POST_DELAY_SECONDS` between consecutive posts to the same channel, with
`last_reminded_at` stamped per task immediately after each successful post.

The automated tests (`python -m unittest tests/test_step1_reminders.py -v` from the repo root)
cover the logic with a mocked Slack client. This checklist is the part they *can't* cover:
what the messages actually look like in Slack, and whether the pacing feels right against a
real copy of production data.

Work through this against a **staging copy** of `tasks.db` before touching the droplet.

---

## 1. Make a safe staging copy

The goal is a completely separate directory with its own DB, so nothing you do can touch the
live PM2-managed bot or its data.

```bash
# from your machine, NOT inside the live deploy directory
mkdir -p ~/taskbot-staging
cp -r /path/to/slackbot/. ~/taskbot-staging/
cd ~/taskbot-staging
```

If you're pulling the DB down from the droplet, copy it rather than moving it, and take the
copy from a quiesced file if you can:

```bash
# on the droplet — snapshot the DB without racing an in-flight write
sqlite3 tasks.db ".backup /tmp/tasks-staging.db"
# then, from your machine
scp user@droplet:/tmp/tasks-staging.db ~/taskbot-staging/tasks.db
```

- [ ] `~/taskbot-staging/tasks.db` exists and is a **copy** — the live `tasks.db` is untouched.
- [ ] `pm2 list` on the droplet still shows `taskbot` online and you have started nothing new there.
- [ ] The staging dir has its own `.env`. **Do not reuse the production bot token as-is** unless
      you accept that reminders will post into real people's registered channels.

**Pointing at a safe Slack target — best to worst:**

1. A separate Slack app in a test workspace (own `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN`), with
   your own test registrations.
2. The real app, but with the staging `registrations` table rewritten so *every* channel is a
   scratch channel only you are in:
   ```bash
   # in ~/taskbot-staging — rewrites the COPY, never the live DB
   sqlite3 tasks.db "UPDATE registrations SET channel_id = 'C_YOUR_TEST_CHANNEL';"
   sqlite3 tasks.db "SELECT assignee_id, channel_id FROM registrations;"   # verify
   ```
   The bot must be a member of that channel.
3. At minimum: delete every registration except your own, so the only person who can be
   pinged is you.
   ```bash
   sqlite3 tasks.db "DELETE FROM registrations WHERE assignee_id != 'U_YOUR_ID';"
   ```

- [ ] Verified with the `SELECT` above that no registration points at a real teammate's channel.

---

## 2. Trigger `send_hourly_reminders()` on demand

Don't wait for the APScheduler tick. Call the function directly — note this imports `main`,
which starts nothing (no scheduler, no socket connection) unless you run `main.main()`.

```bash
cd ~/taskbot-staging
python -c "import main; main.send_hourly_reminders()"
```

Seed test tasks either through Slack with `/addtask` (needs the bot running:
`python main.py` in another terminal), or directly, which is faster and doesn't need the bot up:

```bash
python - <<'PY'
import main
from datetime import timedelta
today = main.now_est().date()
me = "U_YOUR_ID"
main.add_task("staging: high due today",  me, today.isoformat(),                    "HIGH",   "U_YOUR_ID")
main.add_task("staging: medium tomorrow", me, (today + timedelta(days=1)).isoformat(), "MEDIUM", "U_YOUR_ID")
main.add_task("staging: overdue",         me, (today - timedelta(days=3)).isoformat(), "LOW",    "U_YOUR_ID")
main.add_task("staging: future remind",   me, (today + timedelta(days=14)).isoformat(), "HIGH",  "U_YOUR_ID",
              remind_from=(today + timedelta(days=7)).isoformat())
PY
```

- [ ] The one-liner runs and logs `Posted N reminder(s) across M channel(s); 0 failed.`

> **Watch the clock.** Reminders are silent outside 9am–9pm ET. If you get
> `Outside working hours (EST), skipping reminder.`, you are testing at the wrong hour —
> either come back later or temporarily widen `NON_WORKING_START_HOUR` /
> `NON_WORKING_END_HOUR` **in the staging copy only**.

---

## 3. Edge cases to click through by hand

Reset `last_reminded_at` between runs so tasks become due again:

```bash
sqlite3 tasks.db "UPDATE tasks SET last_reminded_at = NULL WHERE status='open';"
```

### 3a. One due task
- [ ] Exactly **one** message arrives.
- [ ] It names the task, its `#id`, its priority, and its due date.
- [ ] The mobile/desktop notification preview shows the task, not a generic "reminders" string.

### 3b. Three or more due tasks for the same person
- [ ] **Three separate messages**, not one bundled list — this is the whole point of Step 1.
- [ ] No message contains two tasks.
- [ ] Highest-priority / earliest-due task arrives first (posts are sorted before sending).

### 3c. Overdue (BACKLOG) mixed with non-overdue
- [ ] The overdue task is labelled `BACKLOG`, **not** its originally-set priority.
- [ ] It arrives even if its original priority was `LOW` (overdue tasks nag hourly).
- [ ] The non-overdue task in the same run keeps its own label (`HIGH`/`MEDIUM`/`LOW`).

### 3d. Future `remind_from`
- [ ] The task with `remind_from` set in the future produces **no message at all**.
- [ ] Confirm in the DB that its `last_reminded_at` is still `NULL` after the run — a silent
      task must not be stamped, or it'll be mis-scheduled once its date arrives.

### 3e. Pacing — messages genuinely spaced out
`REMINDER_POST_DELAY_SECONDS` is currently `1.1`, applied between consecutive posts to the
same channel only.

- [ ] With 3+ tasks for one person, the messages visibly trickle in about a second apart
      rather than landing as one instant burst.
- [ ] Timestamps confirm it:
      ```bash
      time python -c "import main; main.send_hourly_reminders()"
      ```
      For N tasks belonging to one person, wall-clock time should be roughly
      `(N - 1) x 1.1s` plus API latency. For 3 tasks, expect ~2.2s or more, not ~0s.
- [ ] With two different people each having one task, there is **no** delay between them —
      the pacing is per-channel, so a run shouldn't stall moving between assignees.
- [ ] No `ratelimited` errors in the log.

### 3f. Interval suppression — already-reminded tasks are skipped
- [ ] Run the trigger twice in a row **without** clearing `last_reminded_at`.
- [ ] The second run posts **nothing** for `MEDIUM` (12h) and `LOW` (24h) tasks.
- [ ] `HIGH` (1h) and overdue/`BACKLOG` (1h) tasks also stay silent on an immediate re-run —
      the interval hasn't elapsed. The log should say `No tasks due for a reminder this hour.`
- [ ] Nobody receives a duplicate of a message they just got.

### 3g. Partial failure (optional but valuable)
Simulate a channel the bot can't post to, to prove one bad task doesn't kill the rest:

```bash
sqlite3 tasks.db "UPDATE registrations SET channel_id='C_DOES_NOT_EXIST' WHERE assignee_id='U_SOME_OTHER_ID';"
python -c "import main; main.send_hourly_reminders()"
```

- [ ] The log shows `Failed to post reminder for task #… ` for the bad channel.
- [ ] Your own channel **still receives all of its messages**.
- [ ] The failed tasks kept `last_reminded_at = NULL` (see §4) so they'll retry next hour.

---

## 4. Verify `last_reminded_at` in the staging DB

After each run:

```bash
sqlite3 tasks.db "SELECT task_id, priority, due_date, last_reminded_at FROM tasks WHERE status='open';"
```

Nicer formatting:

```bash
sqlite3 -header -column tasks.db \
  "SELECT task_id, priority, due_date, remind_from, last_reminded_at FROM tasks WHERE status='open' ORDER BY task_id;"
```

- [ ] Every task that produced a message has a fresh `last_reminded_at` (ET ISO timestamp).
- [ ] Every task that produced **no** message (future `remind_from`, unregistered assignee,
      still inside its interval, failed post) has its **previous** value — `NULL` if it had
      never been reminded.

Count anything that looks wrong:

```bash
# tasks stamped but silenced by remind_from — should return 0 rows
sqlite3 tasks.db "SELECT task_id, remind_from, last_reminded_at FROM tasks
                  WHERE status='open' AND remind_from > date('now') AND last_reminded_at IS NOT NULL;"

# open tasks belonging to nobody registered — these should never be stamped by this run
sqlite3 tasks.db "SELECT t.task_id, t.assignee_id FROM tasks t
                  LEFT JOIN registrations r ON r.assignee_id = t.assignee_id
                  WHERE t.status='open' AND r.assignee_id IS NULL;"
```

---

## 5. Rollback before touching the droplet

Step 1 changed **code only** — no schema change, no new column, nothing added to
`_migrate_columns()`. So there is **no DB rollback to perform**; a staging DB that has been
reminded against is still schema-compatible with the old code.

Review what's actually changing:

```bash
cd /path/to/slackbot
git status
git diff main.py
```

Throw the change away if it looks wrong:

```bash
git checkout -- main.py          # discard uncommitted changes to main.py
git checkout -- .                # discard everything uncommitted
```

If it was already committed locally but not deployed:

```bash
git log --oneline -5
git revert <commit-sha>          # safest — keeps history
# or, if the commit is not pushed anywhere:
git reset --hard HEAD~1
```

Clean up staging afterwards:

```bash
rm -rf ~/taskbot-staging
```

- [ ] `git diff main.py` reviewed and understood.
- [ ] Staging test tasks were created in the **staging** DB only — confirm the live
      `tasks.db` has no rows described as `staging: …`.
- [ ] Staging directory deleted so nobody runs a second bot against production tokens by accident.

---

## Deploy gate

Only proceed to the droplet when every box above is checked, plus:

- [ ] `python -m unittest tests/test_step1_reminders.py -v` passes from the repo root.
- [ ] A person with 3 tasks received 3 distinct, correctly-labelled messages, spaced ~1s apart.
- [ ] A second immediate run sent nothing (interval suppression works — no double-nagging).

On the droplet, deploying is just a restart, since nothing about the DB changed:

```bash
git pull
pm2 restart taskbot
pm2 logs taskbot --lines 50
```

- [ ] After restart, `pm2 logs taskbot` shows the bot connecting cleanly, and the next hourly
      tick logs `Posted N reminder(s) across M channel(s); 0 failed.`
