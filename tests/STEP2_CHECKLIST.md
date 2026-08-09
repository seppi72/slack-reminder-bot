# Step 2 manual test checklist — the `/edit` command

Step 2 adds a new slash command:

```
/edit <task_id> field:value [field:value ...]
```

Editable fields are exactly `description`, `due` (the `due_date` column), `priority` and
`remind_from`. Anyone can edit any task — there is no permission check. Every supplied field
is validated **before** anything is written, so a command with one bad field changes nothing.
Fields whose new value equals the current one are silently skipped, and a priority that
*actually* changes also clears `last_reminded_at` so the new cadence takes effect on the next
hourly run.

The automated tests (`python -m unittest tests/test_step2_edit.py -v` from the repo root) cover
the parsing, validation and DB behaviour with a mocked Slack client. This checklist is the part
they can't cover: whether Slack actually routes `/edit` to the bot, what the confirmations read
like, and how an edit behaves against a real copy of production data.

---

## 1. Staging copy

Same setup as Step 1 — see [`STEP1_CHECKLIST.md` §1](./STEP1_CHECKLIST.md) and follow it
verbatim: a separate `~/taskbot-staging` directory, a **copy** of `tasks.db` (`sqlite3 tasks.db
".backup /tmp/tasks-staging.db"`), and a Slack target that can't ping real teammates.

- [ ] `~/taskbot-staging/tasks.db` is a copy; the live `tasks.db` is untouched.
- [ ] `sqlite3 tasks.db "SELECT assignee_id, channel_id FROM registrations;"` shows no real
      teammate's channel.
- [ ] `pm2 list` on the droplet still shows only the original `taskbot` running.

Step 2 changes no schema, so a staging DB from Step 1 can be reused as-is.

---

## 2. Register `/edit` in the Slack app config — **do this first**

> **This is the one step that is not code.** `/edit` is a brand-new slash command. Slack will
> not deliver it to the bot at all until the command itself is created in the app
> configuration — no amount of code or OAuth scopes changes that. Symptom if you skip it: Slack
> replies *"/edit is not a valid command"* (or just doesn't autocomplete it) while every
> automated test passes locally and the bot logs show nothing arriving.

At [api.slack.com/apps](https://api.slack.com/apps) → your app → **Slash Commands** →
**Create New Command**:

| Field | Value |
| --- | --- |
| Command | `/edit` |
| Short Description | `Edit an existing task` |
| Usage Hint | `<task_id> field:value  (description, due, priority, remind_from)` |
| Request URL | leave blank / unused — this app runs in **Socket Mode** |
| Escape channels, users, links | leave **off**, same as the other commands |

- [ ] `/edit` created in the app config and saved.
- [ ] **No new OAuth scopes are required.** `/edit` only reads and writes the local SQLite DB
      and replies through `respond()`, which the existing `commands` scope already covers. If
      you find yourself adding a scope, something else is wrong.
- [ ] Reinstalled the app to the workspace **only if** Slack prompts you to (creating a command
      usually doesn't require it; adding a scope would).
- [ ] Do this in the **test/staging** Slack app first if you're using a separate one — and
      remember it has to be repeated in the production app before deploying, since the command
      registration lives in Slack, not in git.
- [ ] Typing `/edit` in Slack now shows the command with its usage hint in the autocomplete.

Then start the staging bot so it can receive it:

```bash
cd ~/taskbot-staging
python main.py            # Socket Mode — no public URL needed
```

- [ ] The log shows `Bot starting (Socket Mode)...` and stays connected.

---

## 3. Seed a task to edit

```bash
cd ~/taskbot-staging
python - <<'PY'
import main
from datetime import timedelta
today = main.now_est().date()
me = "U_YOUR_ID"
task_id = main.add_task(
    "staging: editable task", me,
    (today + timedelta(days=14)).isoformat(), "LOW", "U_SOMEONE_ELSE",
    remind_from=(today + timedelta(days=1)).isoformat(),
)
print("task_id:", task_id)
PY
```

Note the printed id — every case below uses it as `N`. Verify the starting state:

```bash
sqlite3 -header -column tasks.db "SELECT * FROM tasks WHERE task_id = N;"
```

- [ ] Row exists, `status = open`, `priority = LOW`, `due_date` two weeks out.

---

## 4. Cases to click through by hand

After **every** case, re-read the row:

```bash
sqlite3 -header -column tasks.db "SELECT * FROM tasks WHERE task_id = N;"
```

### 4a. Each field on its own

| Command | Expected |
| --- | --- |
| `/edit N description:call the client back` | `description` changes; confirmation shows old -> new |
| `/edit N due:2026-12-01` | `due_date` changes |
| `/edit N priority:HIGH` | `priority` changes (see 4c) |
| `/edit N remind_from:2026-11-25` | `remind_from` changes |

- [ ] Each confirmation names the task id and shows the **old and new** value.
- [ ] A description containing spaces survives intact (`description:call the client back`,
      not truncated at the first space).
- [ ] `sqlite3` confirms only the edited column moved; everything else is byte-identical.
- [ ] Lowercase works too: `/edit N priority:high` stores `HIGH`.

```bash
sqlite3 tasks.db "SELECT task_id, description, due_date, priority, remind_from FROM tasks WHERE task_id = N;"
```

### 4b. Several fields at once

```
/edit N description:rewrite the proposal due:2026-12-15
```

- [ ] Both change in one go, and the confirmation lists **both** old -> new pairs.
- [ ] `priority` and `remind_from` are untouched.

### 4c. Priority change resets the reminder cadence

This is the interesting one — it ties back to Step 1's per-task reminders.

```bash
# make the task remindable now and pretend it was reminded 2 hours ago
sqlite3 tasks.db "UPDATE tasks SET priority='LOW', remind_from=NULL,
                  last_reminded_at=datetime('now','-2 hours') WHERE task_id = N;"
python -c "import main; main.send_hourly_reminders()"
```

- [ ] **No** message arrives — `LOW` has a 24h interval and it was reminded 2h ago.

Now bump it in Slack:

```
/edit N priority:HIGH
```

- [ ] The confirmation mentions that the reminder cadence/clock was reset.
- [ ] `sqlite3 tasks.db "SELECT priority, last_reminded_at FROM tasks WHERE task_id = N;"`
      shows `HIGH` and `last_reminded_at` is **empty (NULL)**.

```bash
python -c "import main; main.send_hourly_reminders()"
```

- [ ] The reminder arrives **immediately** on this run, instead of waiting out the old 24h
      `LOW` interval.
- [ ] `last_reminded_at` is now a fresh ET timestamp.

> Reminders are silent outside 9am–9pm ET — if you get
> `Outside working hours (EST), skipping reminder.`, you're testing at the wrong hour
> (see STEP1_CHECKLIST.md §2).

### 4d. Setting a field to the value it already has

```
/edit N priority:HIGH          (when it is already HIGH)
```

- [ ] Reply says nothing changed, rather than reporting a `HIGH -> HIGH` edit.
- [ ] `last_reminded_at` is **not** cleared — check it still holds the timestamp from 4c.
      (This is the whole point of the no-op rule: re-typing the current priority must not
      restart the nagging clock.)

### 4e. Invalid date

```
/edit N due:2026-13-45
/edit N due:next-tuesday
/edit N remind_from:2026-02-31
```

- [ ] Each gets a clear error naming the expected `YYYY-MM-DD` format.
- [ ] The row is **completely unchanged**.

### 4f. Invalid priority

```
/edit N priority:URGENT
/edit N priority:BACKLOG
```

- [ ] Both rejected — `BACKLOG` is a *derived* state for overdue tasks, never something you
      can set.
- [ ] The row is unchanged.

### 4g. Atomicity — one bad field kills the whole edit

```
/edit N description:this should not stick priority:URGENT
```

- [ ] Error response.
- [ ] `description` did **not** change, even though it was valid on its own.

### 4h. `remind_from` past the due date

```bash
sqlite3 tasks.db "UPDATE tasks SET due_date='2026-12-01', remind_from='2026-11-20' WHERE task_id = N;"
```

```
/edit N remind_from:2026-12-20     -> rejected (later than the existing due date)
/edit N due:2026-11-10             -> rejected (earlier than the existing remind_from)
/edit N due:2027-01-15 remind_from:2027-02-01   -> rejected (both supplied, still out of order)
/edit N due:2027-01-15 remind_from:2027-01-10   -> accepted
```

- [ ] The first three are rejected and leave the row untouched — the check uses the task's
      **current** DB value when only one of the two dates is supplied.
- [ ] The fourth is accepted and both columns move together.
- [ ] `remind_from` exactly equal to `due` is accepted (`/edit N remind_from:2027-01-15`).

### 4i. Task that doesn't exist

```
/edit 999999 due:2026-12-01
```

- [ ] Reply is `No task #999999 found.` (same shape as `/done`).
- [ ] No crash in the bot log, and no new row appears:
      ```bash
      sqlite3 tasks.db "SELECT COUNT(*) FROM tasks WHERE task_id = 999999;"   # 0
      ```

### 4j. Malformed commands

```
/edit
/edit banana
/edit N
/edit N hello there
/edit N status:done
/edit due:2026-12-01
```

- [ ] Every one gets a usage message showing the `/edit <task_id> field:value` format.
- [ ] Nothing raises — `pm2 logs` / the terminal shows no traceback.
- [ ] `status` is a real column but deliberately **not** editable (use `/done`), so it is
      treated as an unrecognised field.

### 4k. No permission restriction

Have a **second Slack user** (or a second account) run:

```
/edit N due:2026-12-20
```

on a task they neither created nor are assigned to.

- [ ] It succeeds. `/edit` is intentionally open to everyone.
- [ ] Both people can see the change reflected by re-running `/edit N` … or by the SQL below.

---

## 5. Verifying DB state

The one command to reach for after any case:

```bash
sqlite3 tasks.db "SELECT * FROM tasks WHERE task_id = N;"
```

Readable version, plus the two columns most likely to be wrong:

```bash
sqlite3 -header -column tasks.db \
  "SELECT task_id, description, due_date, priority, remind_from, last_reminded_at, status
   FROM tasks WHERE task_id = N;"
```

Sanity sweeps over the whole staging DB after you're done poking:

```bash
# nothing should have an out-of-order date pair
sqlite3 tasks.db "SELECT task_id, remind_from, due_date FROM tasks
                  WHERE remind_from IS NOT NULL AND remind_from > due_date;"

# nothing should carry a priority outside the three valid values
sqlite3 tasks.db "SELECT task_id, priority FROM tasks
                  WHERE priority NOT IN ('HIGH','MEDIUM','LOW');"

# no empty descriptions
sqlite3 tasks.db "SELECT task_id FROM tasks WHERE TRIM(description) = '';"
```

- [ ] All three return **zero rows**.

---

## 6. Rollback

Step 2 is **code only** — no new column, nothing added to `_migrate_columns()` — so there is no
DB rollback to perform, exactly as in Step 1:

```bash
cd /path/to/slackbot
git status
git diff main.py
git checkout -- main.py          # discard uncommitted changes
```

If it was already committed locally but not deployed:

```bash
git log --oneline -5
git revert <commit-sha>          # safest — keeps history
```

- [ ] `git diff main.py` reviewed and understood.
- [ ] Staging test rows (`staging: …`) exist only in the staging DB.
- [ ] `rm -rf ~/taskbot-staging` once finished.

> Rolling the code back does **not** remove the `/edit` command from the Slack app config.
> A registered command with no handler will just time out with
> *"failed with the error 'dispatch_failed'"*. If you're abandoning Step 2 for good, delete the
> command at api.slack.com/apps → Slash Commands too.

---

## Deploy gate

- [ ] `python -m unittest tests/test_step2_edit.py -v` passes from the repo root.
- [ ] `python -m unittest tests/test_step1_reminders.py -v` still passes (the reminder loop
      reads the columns `/edit` writes).
- [ ] Every box above is checked, especially 4c (cadence reset) and 4g (atomicity).
- [ ] **`/edit` has been created in the production Slack app config**, not just the staging one.

```bash
git pull
pm2 restart taskbot
pm2 logs taskbot --lines 50
```

- [ ] After the restart, run one real `/edit` in production against a task of your own and
      confirm the confirmation message and the DB row agree.
