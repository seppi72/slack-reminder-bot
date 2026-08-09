"""Tests for Step 2: the `/edit` slash command.

`/edit <task_id> field:value [field:value ...]` changes an existing task in
place. Editable fields are exactly `description`, `due` (DB column `due_date`),
`priority` and `remind_from`. Anyone may edit any task — there is deliberately
no permission check.

The behaviour these tests pin down:

* parsing is a module-level `parse_edit_command(text)` returning
  `(task_id, fields)` with raw string values, or `(None, None)`;
* every supplied field is validated *before* anything is written, so a command
  with one bad field changes nothing at all (all-or-nothing);
* the `remind_from <= due` rule is checked against the *result* of the edit —
  a new value if supplied, otherwise the value already in the DB;
* fields whose new value equals the current one are no-ops: not written, not
  reported;
* a priority that actually changes also resets `last_reminded_at` to NULL, so
  the new cadence takes effect on the very next hourly run.

As in `test_step1_reminders.py`, the assertions are about *observable*
behaviour — the rows left in a real temporary SQLite DB and the text sent back
to Slack — rather than exact response wording, which is free to differ.

Run from the repo root:

    python -m unittest tests/test_step2_edit.py -v
"""

import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# main.py reads these at import time and immediately constructs a slack_bolt App,
# which by default calls Slack's auth.test to validate the token. Set dummy
# tokens (load_dotenv() does not override values already in the environment) so
# importing main never touches the network or the real .env credentials.
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-token-not-real"

_COMMAND_HANDLERS = {}


class _RecordingApp:
    """Stand-in for `slack_bolt.App` that keeps the real handler functions.

    Patching `App` with a plain MagicMock (as test_step1_reminders.py does) is
    enough to import main.py, but it also means `@app.command("/edit")` returns
    a mock, so `main.handle_edit` would be a mock rather than the function under
    test. This fake records each decorated function under its command name and
    hands the function straight back.
    """

    def __init__(self, *args, **kwargs):
        self.client = mock.MagicMock()

    def command(self, command_name):
        def decorator(func):
            _COMMAND_HANDLERS[command_name] = func
            return func

        return decorator

    def __getattr__(self, name):
        # any other Bolt decorator/attribute main.py might grow (event, action, …)
        return mock.MagicMock()


with mock.patch("slack_bolt.App", _RecordingApp), mock.patch(
    "slack_bolt.adapter.socket_mode.SocketModeHandler"
):
    import main


def get_command_handler(command_name):
    """The function registered for a slash command, or None if there isn't one."""
    if command_name in _COMMAND_HANDLERS:
        return _COMMAND_HANDLERS[command_name]

    # main may already have been imported by another test module (running the
    # whole tests/ directory in one process) with a plain MagicMock App, in
    # which case the decorators were recorded on that mock instead. Recover the
    # function from its call history: every `app.command(name)` returns the same
    # child mock, which is then called with the handler, so the two call lists
    # line up in registration order.
    register = getattr(main.app, "command", None)
    if not isinstance(register, mock.Mock):
        return None
    names = [c.args[0] for c in register.call_args_list if c.args]
    funcs = [c.args[0] for c in register.return_value.call_args_list if c.args]
    for name, func in zip(names, funcs):
        if name == command_name and callable(func):
            return func
    return None


def sleep_patcher():
    """Patch whichever `sleep` the reminder loop actually calls."""
    if hasattr(main, "sleep"):  # from time import sleep
        return mock.patch.object(main, "sleep")
    if hasattr(main, "time"):  # import time -> time.sleep(...)
        return mock.patch.object(main.time, "sleep")
    return mock.patch("time.sleep")


EST_TZ = ZoneInfo("America/New_York")

# A fixed "now" inside working hours (9am–9pm ET) on a weekday, so tests that
# also exercise the reminder loop are deterministic whatever time they run at.
FAKE_NOW = datetime(2026, 8, 10, 10, 0, 0, tzinfo=EST_TZ)

ALICE = "U_ALICE"
ALICE_CHANNEL = "C_ALICE"
CREATOR = "U_CREATOR"
# a third party: neither the assignee nor the creator of the seeded task
OUTSIDER = "U_OUTSIDER"


class HandlerResult:
    """What one invocation of a slash-command handler did."""

    def __init__(self, ack, respond):
        self.ack = ack
        self.respond = respond

    @property
    def texts(self):
        """Every message the handler sent back, as strings.

        `respond()` is normally called with a single string, but a Block Kit
        dict is also legal, so anything non-string is stringified rather than
        dropped — the assertions here only ever look for substrings.
        """
        out = []
        for call in self.respond.call_args_list:
            if call.args:
                value = call.args[0]
            elif "text" in call.kwargs:
                value = call.kwargs["text"]
            else:
                value = call.kwargs
            out.append(value if isinstance(value, str) else str(value))
        return out

    @property
    def text(self):
        return "\n".join(self.texts)


def call_handler(handler, command):
    """Invoke a Bolt handler with whichever arguments its signature asks for.

    Bolt injects handler arguments by parameter name, so the exact set a
    handler takes (`ack, respond, command` vs. also `client`/`logger`/…) is up
    to the implementer. Build the call from the signature rather than assuming
    one shape.
    """
    ack = mock.Mock()
    respond = mock.Mock()
    available = {
        "ack": ack,
        "respond": respond,
        "command": command,
        "body": command,
        "payload": command,
        "client": main.app.client,
        "logger": main.logger,
        "say": mock.Mock(),
        "context": {},
        "next": mock.Mock(),
    }

    params = inspect.signature(handler).parameters
    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        kwargs = dict(available)
    else:
        kwargs = {}
        for name, param in params.items():
            if name in available:
                kwargs[name] = available[name]
            elif param.default is param.empty:
                raise AssertionError(
                    f"the handler asks for an argument this test doesn't know how "
                    f"to supply: {name!r}"
                )

    handler(**kwargs)
    return HandlerResult(ack, respond)


class DBTestBase(unittest.TestCase):
    """Real temporary SQLite DB + mocked Slack client.

    Using a real DB (rather than mocking the DB layer) is what makes the
    "the task is completely unchanged" assertions meaningful: they re-read the
    persisted row instead of trusting a mock.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="taskbot-test-", suffix=".db")
        os.close(fd)
        self.addCleanup(self._remove_db)

        self._start_patch(mock.patch.object(main, "DB_PATH", self.db_path))
        main.init_db()

        self.now = FAKE_NOW
        self._start_patch(mock.patch.object(main, "now_est", return_value=self.now))
        self.post = self._start_patch(
            mock.patch.object(main.app.client, "chat_postMessage")
        )

    def _remove_db(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _start_patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    # --- fixtures ---------------------------------------------------------

    def seed_task(
        self,
        description="original description",
        assignee_id=ALICE,
        due_date="2026-08-20",
        priority="MEDIUM",
        created_by=CREATOR,
        remind_from=None,
        last_reminded_at=None,
    ):
        task_id = main.add_task(
            description, assignee_id, due_date, priority, created_by, remind_from
        )
        if last_reminded_at is not None:
            self.set_column(task_id, "last_reminded_at", last_reminded_at)
        return task_id

    def register(self, assignee_id, channel_id):
        main.add_registration(
            assignee_id,
            channel_id,
            channel_id.lower(),
            f"{assignee_id.lower()}@example.com",
            CREATOR,
        )

    def set_column(self, task_id, column, value):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"UPDATE tasks SET {column} = ? WHERE task_id = ?", (value, task_id)
            )
            conn.commit()
        finally:
            conn.close()

    # --- assertion helpers ------------------------------------------------

    def row(self, task_id):
        """The whole task row as a plain dict, straight out of the temp DB."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def assert_unchanged(self, task_id, before):
        self.assertEqual(
            self.row(task_id),
            before,
            "a rejected /edit must leave the task exactly as it was — validation "
            "runs for every supplied field before anything is written",
        )


class EditCommandTestBase(DBTestBase):
    """Adds the `/edit` handler itself."""

    def setUp(self):
        super().setUp()
        self.handler = get_command_handler("/edit")
        self.assertIsNotNone(
            self.handler,
            "no handler is registered for the `/edit` slash command in main.py "
            "(expected an `@app.command(\"/edit\")` handler)",
        )

    def edit(self, text, user_id=OUTSIDER):
        command = {
            "text": text,
            "user_id": user_id,
            "user_name": "tester",
            "channel_id": "C_TEST",
            "command": "/edit",
        }
        return call_handler(self.handler, command)


# ---------------------------------------------------------------------------
# parse_edit_command
# ---------------------------------------------------------------------------


class ParseEditCommandTests(unittest.TestCase):
    """Pure parsing: no DB, no validation — just text in, (task_id, fields) out."""

    def setUp(self):
        self.assertTrue(
            hasattr(main, "parse_edit_command"),
            "main.parse_edit_command(text) is missing — Step 2 adds it as the "
            "module-level parser for `/edit`",
        )
        self.parse = main.parse_edit_command

    def test_single_field(self):
        task_id, fields = self.parse("42 due:2026-08-20")

        self.assertEqual(task_id, 42)
        self.assertIsInstance(task_id, int)
        self.assertEqual(fields, {"due": "2026-08-20"})

    def test_multiple_fields(self):
        task_id, fields = self.parse("42 due:2026-08-20 priority:HIGH")

        self.assertEqual(task_id, 42)
        self.assertEqual(set(fields), {"due", "priority"})
        self.assertEqual(fields["due"], "2026-08-20")
        # the parser returns raw, unvalidated values; casing is the handler's job,
        # so accept either as long as the value round-trips.
        self.assertEqual(fields["priority"].upper(), "HIGH")

    def test_all_four_editable_fields_at_once(self):
        task_id, fields = self.parse(
            "7 description:ship the deck due:2026-09-01 priority:LOW "
            "remind_from:2026-08-25"
        )

        self.assertEqual(task_id, 7)
        self.assertEqual(
            set(fields), {"description", "due", "priority", "remind_from"}
        )
        self.assertEqual(fields["description"], "ship the deck")
        self.assertEqual(fields["due"], "2026-09-01")
        self.assertEqual(fields["remind_from"], "2026-08-25")

    def test_description_value_may_contain_spaces(self):
        task_id, fields = self.parse("42 description:call the client back")

        self.assertEqual(task_id, 42)
        self.assertEqual(fields, {"description": "call the client back"})

    def test_description_with_spaces_stops_at_the_next_field(self):
        _, fields = self.parse("42 description:call the client back due:2026-09-01")

        self.assertEqual(fields["description"], "call the client back")
        self.assertEqual(fields["due"], "2026-09-01")

    def test_a_field_name_mid_word_is_not_a_new_field(self):
        """Field names only start a new field at the start or after whitespace,
        so a colon-y word inside a description doesn't split it."""
        _, fields = self.parse("42 description:see notpriority:HIGH for context")

        self.assertEqual(set(fields), {"description"})
        self.assertEqual(fields["description"], "see notpriority:HIGH for context")

    def test_lowercase_priority_is_preserved_for_the_handler_to_normalise(self):
        _, fields = self.parse("42 priority:high")

        self.assertEqual(fields["priority"].upper(), "HIGH")

    def test_surrounding_whitespace_is_tolerated(self):
        task_id, fields = self.parse("  42   due:2026-08-20  ")

        self.assertEqual(task_id, 42)
        self.assertEqual(fields["due"].strip(), "2026-08-20")

    def test_rejects_missing_task_id(self):
        self.assertEqual(self.parse("due:2026-08-20"), (None, None))

    def test_rejects_non_numeric_task_id(self):
        self.assertEqual(self.parse("banana due:2026-08-20"), (None, None))

    def test_rejects_task_id_with_no_fields(self):
        self.assertEqual(self.parse("42"), (None, None))
        self.assertEqual(self.parse("42   "), (None, None))

    def test_rejects_empty_text(self):
        self.assertEqual(self.parse(""), (None, None))
        self.assertEqual(self.parse("   "), (None, None))

    def test_rejects_text_with_no_recognised_field(self):
        self.assertEqual(self.parse("42 hello there"), (None, None))

    def test_rejects_an_unknown_field_name(self):
        # `status` and `assignee` are real columns but deliberately not editable
        self.assertEqual(self.parse("42 status:done"), (None, None))
        self.assertEqual(self.parse("42 assignee:U_BOB"), (None, None))

    def test_rejects_garbage_before_the_first_field(self):
        self.assertEqual(self.parse("42 blah due:2026-08-20"), (None, None))

    def test_a_hash_prefixed_task_id_does_not_raise(self):
        """People type `#42` because that's how task ids are displayed. Whether
        the parser strips the `#` or rejects the whole command is up to the
        implementation — it just must not blow up."""
        self.assertIn(
            self.parse("#42 due:2026-08-20"),
            [(None, None), (42, {"due": "2026-08-20"})],
        )


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


class UpdateTaskHelperTests(DBTestBase):
    """The DB helper the `/edit` handler writes through."""

    def setUp(self):
        super().setUp()
        self.assertTrue(
            hasattr(main, "update_task"),
            "main.update_task(task_id, updates) is missing — Step 2 adds it as "
            "the DB helper behind `/edit`",
        )

    def test_applies_only_the_given_columns(self):
        task_id = self.seed_task(
            description="old description",
            due_date="2026-08-20",
            priority="MEDIUM",
            remind_from="2026-08-15",
        )

        main.update_task(
            task_id, {"description": "new description", "due_date": "2026-09-01"}
        )

        row = self.row(task_id)
        self.assertEqual(row["description"], "new description")
        self.assertEqual(row["due_date"], "2026-09-01")
        self.assertEqual(row["priority"], "MEDIUM")
        self.assertEqual(row["remind_from"], "2026-08-15")
        self.assertEqual(row["status"], "open")

    def test_can_write_a_null(self):
        stamp = (self.now - timedelta(hours=3)).isoformat()
        task_id = self.seed_task(priority="LOW", last_reminded_at=stamp)

        main.update_task(task_id, {"priority": "HIGH", "last_reminded_at": None})

        row = self.row(task_id)
        self.assertEqual(row["priority"], "HIGH")
        self.assertIsNone(row["last_reminded_at"])

    def test_leaves_other_tasks_alone(self):
        target = self.seed_task(description="target", due_date="2026-08-20")
        bystander = self.seed_task(description="bystander", due_date="2026-08-20")
        before = self.row(bystander)

        main.update_task(target, {"due_date": "2026-09-01"})

        self.assertEqual(self.row(bystander), before)


# ---------------------------------------------------------------------------
# /edit — successful edits
# ---------------------------------------------------------------------------


class SuccessfulEditTests(EditCommandTestBase):
    def test_editing_due_alone_updates_due_date(self):
        task_id = self.seed_task(due_date="2026-08-20")

        result = self.edit(f"{task_id} due:2026-09-01")

        self.assertEqual(self.row(task_id)["due_date"], "2026-09-01")
        self.assertIn("2026-09-01", result.text, "the new value should be reported")
        self.assertIn("2026-08-20", result.text, "the old value should be reported")
        self.assertIn(str(task_id), result.text)

    def test_editing_description_alone(self):
        task_id = self.seed_task(description="old description")

        result = self.edit(f"{task_id} description:call the client back")

        self.assertEqual(self.row(task_id)["description"], "call the client back")
        self.assertIn("call the client back", result.text)
        self.assertIn("old description", result.text)

    def test_description_is_stripped(self):
        task_id = self.seed_task(description="old description")

        self.edit(f"{task_id} description:   spaced out   ")

        self.assertEqual(self.row(task_id)["description"], "spaced out")

    def test_editing_remind_from_alone(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from=None)

        result = self.edit(f"{task_id} remind_from:2026-08-15")

        self.assertEqual(self.row(task_id)["remind_from"], "2026-08-15")
        self.assertIn("2026-08-15", result.text)

    def test_editing_description_and_due_together(self):
        task_id = self.seed_task(
            description="old description",
            due_date="2026-08-20",
            priority="MEDIUM",
            remind_from="2026-08-12",
        )

        result = self.edit(
            f"{task_id} description:call the client back due:2026-09-01"
        )

        row = self.row(task_id)
        self.assertEqual(row["description"], "call the client back")
        self.assertEqual(row["due_date"], "2026-09-01")
        # untouched fields stay exactly as they were
        self.assertEqual(row["priority"], "MEDIUM")
        self.assertEqual(row["remind_from"], "2026-08-12")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["assignee_id"], ALICE)
        self.assertEqual(row["created_by"], CREATOR)

        self.assertIn("call the client back", result.text)
        self.assertIn("2026-09-01", result.text)

    def test_editing_priority_updates_it(self):
        task_id = self.seed_task(priority="LOW")

        result = self.edit(f"{task_id} priority:HIGH")

        self.assertEqual(self.row(task_id)["priority"], "HIGH")
        self.assertIn("HIGH", result.text)
        self.assertIn("LOW", result.text)

    def test_lowercase_priority_is_uppercased(self):
        task_id = self.seed_task(priority="LOW")

        self.edit(f"{task_id} priority:high")

        self.assertEqual(
            self.row(task_id)["priority"],
            "HIGH",
            "priority is stored uppercased, the same way /addtask does it",
        )

    def test_due_can_move_freely_when_the_task_has_no_remind_from(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from=None)

        self.edit(f"{task_id} due:2026-08-11")

        self.assertEqual(self.row(task_id)["due_date"], "2026-08-11")

    def test_editing_acknowledges_the_command(self):
        """Slack times a slash command out after 3s, so every handler acks."""
        task_id = self.seed_task()

        result = self.edit(f"{task_id} due:2026-09-01")

        result.ack.assert_called()

    def test_anyone_can_edit_anyone_elses_task(self):
        """No permission check: the editor is neither assignee nor creator."""
        task_id = self.seed_task(
            assignee_id=ALICE, created_by=CREATOR, due_date="2026-08-20"
        )

        result = self.edit(f"{task_id} due:2026-09-01", user_id=OUTSIDER)

        self.assertEqual(
            self.row(task_id)["due_date"],
            "2026-09-01",
            "/edit has no permission restriction — any user may edit any task",
        )
        self.assertIn("2026-09-01", result.text)


# ---------------------------------------------------------------------------
# /edit — the priority/cadence interaction
# ---------------------------------------------------------------------------


class PriorityResetsCadenceTests(EditCommandTestBase):
    """Changing priority changes the reminder interval, so the clock restarts."""

    def setUp(self):
        super().setUp()
        self.stamp = (self.now - timedelta(hours=3)).isoformat()

    def test_changed_priority_resets_last_reminded_at(self):
        task_id = self.seed_task(priority="LOW", last_reminded_at=self.stamp)
        self.assertEqual(self.row(task_id)["last_reminded_at"], self.stamp)

        self.edit(f"{task_id} priority:HIGH")

        row = self.row(task_id)
        self.assertEqual(row["priority"], "HIGH")
        self.assertIsNone(
            row["last_reminded_at"],
            "a real priority change must clear last_reminded_at so the new "
            "cadence applies on the next hourly run",
        )

    def test_the_reset_is_mentioned_in_the_confirmation(self):
        task_id = self.seed_task(priority="LOW", last_reminded_at=self.stamp)

        result = self.edit(f"{task_id} priority:HIGH")

        self.assertRegex(
            result.text,
            r"(?i)reset|cadence|remind",
            "the confirmation should say the reminder cadence was reset",
        )

    def test_editing_other_fields_does_not_reset_last_reminded_at(self):
        task_id = self.seed_task(due_date="2026-08-20", last_reminded_at=self.stamp)

        self.edit(f"{task_id} due:2026-09-01")

        self.assertEqual(
            self.row(task_id)["last_reminded_at"],
            self.stamp,
            "only a priority change restarts the reminder clock",
        )

    def test_same_priority_is_not_a_change_and_leaves_the_clock_alone(self):
        task_id = self.seed_task(priority="HIGH", last_reminded_at=self.stamp)

        result = self.edit(f"{task_id} priority:HIGH")

        row = self.row(task_id)
        self.assertEqual(row["priority"], "HIGH")
        self.assertEqual(
            row["last_reminded_at"],
            self.stamp,
            "re-setting priority to its current value is a no-op, so it must "
            "not reset the reminder clock",
        )
        self.assertRegex(result.text, r"(?i)no change|nothing|unchanged|already|same")

    def test_same_priority_in_different_case_is_still_a_no_op(self):
        task_id = self.seed_task(priority="HIGH", last_reminded_at=self.stamp)

        self.edit(f"{task_id} priority:high")

        self.assertEqual(self.row(task_id)["last_reminded_at"], self.stamp)

    def test_a_no_op_field_alongside_a_real_change_is_not_reported(self):
        task_id = self.seed_task(
            description="original description",
            priority="HIGH",
            due_date="2026-08-20",
            last_reminded_at=self.stamp,
        )

        result = self.edit(f"{task_id} priority:HIGH due:2026-09-01")

        row = self.row(task_id)
        self.assertEqual(row["due_date"], "2026-09-01")
        self.assertEqual(
            row["last_reminded_at"],
            self.stamp,
            "priority did not actually change, so the clock must not be reset",
        )
        self.assertIn("2026-09-01", result.text)

    def test_a_priority_bump_makes_the_next_run_remind_immediately(self):
        """End-to-end with Step 1's per-task reminders: a LOW task reminded two
        hours ago is inside its 24h interval and stays silent, but after being
        bumped to HIGH (1h) the cleared clock means it goes out right away."""
        self._start_patch(sleep_patcher())
        self.register(ALICE, ALICE_CHANNEL)
        task_id = self.seed_task(
            description="cadence-task",
            assignee_id=ALICE,
            priority="LOW",
            due_date="2026-08-20",
            last_reminded_at=(self.now - timedelta(hours=2)).isoformat(),
        )

        main.send_hourly_reminders()
        self.post.assert_not_called()

        self.edit(f"{task_id} priority:HIGH")
        main.send_hourly_reminders()

        self.assertEqual(
            self.post.call_count,
            1,
            "after the priority bump the task should be reminded on the very "
            "next run",
        )
        self.assertEqual(self.row(task_id)["last_reminded_at"], self.now.isoformat())


# ---------------------------------------------------------------------------
# /edit — validation failures leave the task untouched
# ---------------------------------------------------------------------------


class ValidationRejectsTheWholeEditTests(EditCommandTestBase):
    def test_invalid_due_date_leaves_the_task_untouched(self):
        for bad in ("2026-13-45", "next-tuesday", "08/20/2026", "2026-8-1x"):
            with self.subTest(due=bad):
                task_id = self.seed_task(due_date="2026-08-20")
                before = self.row(task_id)

                result = self.edit(f"{task_id} due:{bad}")

                self.assert_unchanged(task_id, before)
                self.assertTrue(
                    result.text.strip(), "a rejected edit still needs a reply"
                )

    def test_invalid_remind_from_date_leaves_the_task_untouched(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from="2026-08-15")
        before = self.row(task_id)

        result = self.edit(f"{task_id} remind_from:2026-02-31")

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_invalid_priority_leaves_the_task_untouched(self):
        # BACKLOG is a derived state for overdue tasks, never a settable priority
        for bad in ("URGENT", "BACKLOG", "P1", "highish"):
            with self.subTest(priority=bad):
                task_id = self.seed_task(priority="MEDIUM")
                before = self.row(task_id)

                result = self.edit(f"{task_id} priority:{bad}")

                self.assert_unchanged(task_id, before)
                self.assertTrue(result.text.strip())

    def test_valid_priorities_are_exactly_high_medium_low(self):
        self.assertEqual(main.VALID_PRIORITIES, {"HIGH", "MEDIUM", "LOW"})

    def test_empty_description_leaves_the_task_untouched(self):
        task_id = self.seed_task(description="original description")
        before = self.row(task_id)

        result = self.edit(f"{task_id} description:    ")

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_one_bad_field_rejects_the_whole_edit(self):
        """Validation runs for every field before any write, so a good field
        travelling with a bad one is not applied either."""
        task_id = self.seed_task(description="original description", priority="MEDIUM")
        before = self.row(task_id)

        result = self.edit(
            f"{task_id} description:brand new description priority:URGENT"
        )

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_a_bad_field_after_a_good_one_still_rejects_everything(self):
        task_id = self.seed_task(due_date="2026-08-20", priority="MEDIUM")
        before = self.row(task_id)

        self.edit(f"{task_id} priority:LOW due:not-a-date")

        self.assert_unchanged(task_id, before)


class CrossFieldDateRuleTests(EditCommandTestBase):
    """`remind_from <= due`, checked against the *result* of the edit."""

    def test_remind_from_after_the_existing_due_date_is_rejected(self):
        """Only remind_from is supplied — the comparison has to come from the
        task's current due_date in the DB."""
        task_id = self.seed_task(due_date="2026-08-20", remind_from="2026-08-12")
        before = self.row(task_id)

        result = self.edit(f"{task_id} remind_from:2026-09-15")

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_due_moved_before_the_existing_remind_from_is_rejected(self):
        """The same rule from the other direction: only due is supplied, and it
        is compared against the remind_from already on the task."""
        task_id = self.seed_task(due_date="2026-08-20", remind_from="2026-08-18")
        before = self.row(task_id)

        result = self.edit(f"{task_id} due:2026-08-15")

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_both_supplied_at_once_are_checked_against_each_other(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from=None)
        before = self.row(task_id)

        result = self.edit(f"{task_id} due:2026-09-01 remind_from:2026-09-10")

        self.assert_unchanged(task_id, before)
        self.assertTrue(result.text.strip())

    def test_remind_from_equal_to_due_is_accepted(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from=None)

        self.edit(f"{task_id} remind_from:2026-08-20")

        self.assertEqual(self.row(task_id)["remind_from"], "2026-08-20")

    def test_moving_both_dates_together_is_accepted(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from="2026-08-18")

        self.edit(f"{task_id} due:2026-10-01 remind_from:2026-09-25")

        row = self.row(task_id)
        self.assertEqual(row["due_date"], "2026-10-01")
        self.assertEqual(row["remind_from"], "2026-09-25")

    def test_pushing_due_out_past_an_existing_remind_from_is_fine(self):
        task_id = self.seed_task(due_date="2026-08-20", remind_from="2026-08-18")

        self.edit(f"{task_id} due:2026-12-01")

        self.assertEqual(self.row(task_id)["due_date"], "2026-12-01")


# ---------------------------------------------------------------------------
# /edit — bad input that never reaches validation
# ---------------------------------------------------------------------------


class BadCommandInputTests(EditCommandTestBase):
    def test_nonexistent_task_id_reports_not_found(self):
        result = self.edit("9999 due:2026-09-01")

        self.assertIn("9999", result.text)
        self.assertRegex(
            result.text,
            r"(?i)no task",
            "an unknown task id should get the same `No task #N found.` reply "
            "as /done",
        )

    def test_nonexistent_task_id_creates_nothing(self):
        self.edit("9999 due:2026-09-01")

        self.assertIsNone(self.row(9999))

    def test_malformed_commands_get_a_usage_error_instead_of_an_exception(self):
        task_id = self.seed_task()
        before = self.row(task_id)

        malformed = [
            "banana",
            "banana due:2026-09-01",
            "",
            "   ",
            f"{task_id}",
            f"{task_id} ",
            f"{task_id} hello there",
            f"{task_id} status:done",
            f"{task_id} blah due:2026-09-01",
            "due:2026-09-01",
        ]

        for text in malformed:
            with self.subTest(text=text):
                result = self.edit(text)

                self.assertTrue(
                    result.text.strip(),
                    "a command that doesn't parse still needs a usage reply",
                )
                self.assertIn(
                    "edit",
                    result.text.lower(),
                    "the usage reply should show the /edit format",
                )
                self.assert_unchanged(task_id, before)

    def test_a_missing_text_key_does_not_crash(self):
        """Slack sends an empty string for a bare `/edit`, but be defensive the
        way the other handlers are (`command.get("text", "")`)."""
        command = {"user_id": OUTSIDER, "channel_id": "C_TEST", "command": "/edit"}

        result = call_handler(self.handler, command)

        self.assertTrue(result.text.strip())


if __name__ == "__main__":
    unittest.main()
