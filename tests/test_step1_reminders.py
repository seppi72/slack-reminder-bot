"""Tests for Step 1 of the reminder rework: one Slack message per task.

Before Step 1, `send_hourly_reminders()` posted a single Block Kit message per
assignee listing all of their due tasks, and stamped `last_reminded_at` on the
whole batch at once. After Step 1 it posts one `chat_postMessage` per task, with
a short `time.sleep(REMINDER_POST_DELAY_SECONDS)` between posts to the same
channel, and stamps `last_reminded_at` for each task immediately after that
task's own post succeeds.

These tests deliberately assert on *observable behaviour* — the calls made to
`chat_postMessage` and the rows left behind in the database — rather than on the
name or signature of the block-building helper, so they stay valid regardless of
whether it ends up being called `build_task_block` or something else.

Run from the repo root:

    python -m unittest tests/test_step1_reminders.py -v
"""

import json
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
# tokens (load_dotenv() does not override values already in the environment) and
# stub out App so importing main never touches the network or the real .env
# credentials.
os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token-not-real"
os.environ["SLACK_APP_TOKEN"] = "xapp-test-token-not-real"

with mock.patch("slack_bolt.App"), mock.patch(
    "slack_bolt.adapter.socket_mode.SocketModeHandler"
):
    import main

EST_TZ = ZoneInfo("America/New_York")

# A fixed "now" inside working hours (9am–9pm ET) on a weekday, so the tests are
# deterministic no matter what time of day they actually run. This assumes the
# implementation gets the current time from main.now_est(), as CLAUDE.md
# requires — a run that returns zero posts is the symptom of a regression to a
# bare datetime.now().
FAKE_NOW = datetime(2026, 8, 10, 10, 0, 0, tzinfo=EST_TZ)

ALICE = "U_ALICE"
ALICE_CHANNEL = "C_ALICE"
BOB = "U_BOB"
BOB_CHANNEL = "C_BOB"
CREATOR = "U_CREATOR"


def sleep_patcher():
    """Patch whichever `sleep` the implementation actually calls.

    `import time; time.sleep(...)` and `from time import sleep` bind different
    names, and main.py does not import time at all before Step 1, so pick the
    one that exists rather than hard-coding a target.
    """
    if hasattr(main, "sleep"):  # from time import sleep
        return mock.patch.object(main, "sleep")
    if hasattr(main, "time"):  # import time -> time.sleep(...)
        return mock.patch.object(main.time, "sleep")
    return mock.patch("time.sleep")


class ReminderTestBase(unittest.TestCase):
    """Real temporary SQLite DB + mocked Slack client.

    Using a real DB (rather than mocking the DB layer) means these tests also
    exercise init_db/_migrate_columns/add_task/add_registration, and lets the
    assertions about last_reminded_at read the actual persisted rows.
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
        self.sleep = self._start_patch(sleep_patcher())

    def _remove_db(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _start_patch(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    # --- fixtures ---------------------------------------------------------

    def add_task(self, description, assignee_id, due_offset_days=0,
                 priority="HIGH", remind_from=None):
        """Add an open task due `due_offset_days` from the fixed now.

        Defaults to HIGH (a 1-hour reminder interval) so a task that has never
        been reminded is always due for a reminder in the current run.
        """
        due = (self.now.date() + timedelta(days=due_offset_days)).isoformat()
        return main.add_task(description, assignee_id, due, priority, CREATOR, remind_from)

    def register(self, assignee_id, channel_id):
        main.add_registration(
            assignee_id,
            channel_id,
            channel_id.lower(),
            f"{assignee_id.lower()}@example.com",
            CREATOR,
        )

    def set_last_reminded(self, task_id, when):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE tasks SET last_reminded_at = ? WHERE task_id = ?",
                (when.isoformat(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    # --- assertions helpers -----------------------------------------------

    def last_reminded(self, task_id):
        """Read last_reminded_at straight out of the temp DB."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT last_reminded_at FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0]

    def payload(self, call):
        """Everything a human would see in one posted message: the notification
        `text` plus the serialized blocks."""
        return json.dumps(
            {"text": call.kwargs.get("text"), "blocks": call.kwargs.get("blocks")},
            default=str,
        )

    def posted_channels(self):
        return [call.kwargs.get("channel") for call in self.post.call_args_list]

    def assert_posted_once_for(self, description):
        """Exactly one posted message mentions this task description."""
        matches = [c for c in self.post.call_args_list if description in self.payload(c)]
        self.assertEqual(
            len(matches),
            1,
            f"expected exactly one message mentioning {description!r}, "
            f"got {len(matches)}",
        )
        return matches[0]

    def assert_not_posted_for(self, description):
        matches = [c for c in self.post.call_args_list if description in self.payload(c)]
        self.assertEqual(
            matches, [], f"did not expect any message mentioning {description!r}"
        )


class PerTaskPostingTests(ReminderTestBase):
    """One assignee, several due tasks -> one message per task."""

    def test_three_due_tasks_produce_three_separate_posts_to_one_channel(self):
        self.register(ALICE, ALICE_CHANNEL)
        for description in ("alpha-task", "beta-task", "gamma-task"):
            self.add_task(description, ALICE)

        main.send_hourly_reminders()

        self.assertEqual(
            self.post.call_count,
            3,
            "expected one chat_postMessage per task (3), not a single batched "
            f"message; got {self.post.call_count}",
        )
        self.assertEqual(self.posted_channels(), [ALICE_CHANNEL] * 3)

    def test_each_post_covers_exactly_one_task(self):
        self.register(ALICE, ALICE_CHANNEL)
        descriptions = ["alpha-task", "beta-task", "gamma-task"]
        for description in descriptions:
            self.add_task(description, ALICE)

        main.send_hourly_reminders()

        for description in descriptions:
            self.assert_posted_once_for(description)

        # and no single message bundles two tasks together
        for call in self.post.call_args_list:
            payload = self.payload(call)
            mentioned = [d for d in descriptions if d in payload]
            self.assertEqual(
                len(mentioned),
                1,
                f"a message mentioned {len(mentioned)} tasks ({mentioned}); "
                "each post should cover exactly one task",
            )

    def test_every_post_carries_notification_text(self):
        """Slack's push/banner preview comes from the top-level `text`, so a
        per-task message still needs one — an empty text means silent-looking
        notifications on mobile."""
        self.register(ALICE, ALICE_CHANNEL)
        self.add_task("alpha-task", ALICE)

        main.send_hourly_reminders()

        for call in self.post.call_args_list:
            self.assertTrue(
                call.kwargs.get("text"),
                "chat_postMessage was called without a non-empty `text` fallback",
            )

    def test_all_successful_tasks_get_last_reminded_at_stamped(self):
        self.register(ALICE, ALICE_CHANNEL)
        task_ids = [self.add_task(d, ALICE) for d in ("alpha-task", "beta-task")]

        main.send_hourly_reminders()

        for task_id in task_ids:
            self.assertEqual(
                self.last_reminded(task_id),
                self.now.isoformat(),
                f"task #{task_id} posted successfully but was not stamped",
            )


class PartialFailureTests(ReminderTestBase):
    """A failure on one post must not lose the other posts or mis-stamp them."""

    def setUp(self):
        super().setUp()
        self.register(ALICE, ALICE_CHANNEL)
        self.alpha = self.add_task("alpha-task", ALICE)
        self.beta = self.add_task("beta-task", ALICE)
        self.gamma = self.add_task("gamma-task", ALICE)

        def fail_on_beta(**kwargs):
            payload = json.dumps(
                {"text": kwargs.get("text"), "blocks": kwargs.get("blocks")}, default=str
            )
            if "beta-task" in payload:
                raise RuntimeError("simulated Slack failure for beta-task")
            return {"ok": True}

        self.post.side_effect = fail_on_beta

    def test_remaining_tasks_still_get_posted(self):
        main.send_hourly_reminders()

        self.assertEqual(
            self.post.call_count,
            3,
            "a failure on one task's post must not abort the rest of the loop",
        )
        self.assert_posted_once_for("alpha-task")
        self.assert_posted_once_for("gamma-task")

    def test_only_successfully_posted_tasks_are_stamped(self):
        main.send_hourly_reminders()

        self.assertEqual(self.last_reminded(self.alpha), self.now.isoformat())
        self.assertEqual(self.last_reminded(self.gamma), self.now.isoformat())

    def test_failed_task_keeps_last_reminded_at_untouched_so_it_retries(self):
        main.send_hourly_reminders()

        self.assertIsNone(
            self.last_reminded(self.beta),
            "the task whose post failed must keep its previous last_reminded_at "
            "(NULL here) so the next hourly run retries it",
        )

    def test_failed_task_is_picked_up_again_on_the_next_run(self):
        main.send_hourly_reminders()

        self.post.reset_mock()
        self.post.side_effect = None
        self.post.return_value = {"ok": True}
        main.send_hourly_reminders()

        # alpha/gamma were just reminded and are HIGH (1h interval), so only the
        # previously-failed task should go out again.
        self.assert_posted_once_for("beta-task")
        self.assert_not_posted_for("alpha-task")
        self.assert_not_posted_for("gamma-task")
        self.assertEqual(self.last_reminded(self.beta), self.now.isoformat())


class PostDelayTests(ReminderTestBase):
    """Posts to the same channel are spaced out rather than fired back to back."""

    def test_delay_constant_is_defined(self):
        self.assertTrue(
            hasattr(main, "REMINDER_POST_DELAY_SECONDS"),
            "Step 1 adds a REMINDER_POST_DELAY_SECONDS config constant",
        )
        self.assertGreater(main.REMINDER_POST_DELAY_SECONDS, 0)

    def test_sleeps_between_posts_to_the_same_channel(self):
        self.register(ALICE, ALICE_CHANNEL)
        for description in ("alpha-task", "beta-task", "gamma-task"):
            self.add_task(description, ALICE)

        main.send_hourly_reminders()

        self.assertEqual(self.post.call_count, 3)
        # 2 == sleep strictly between posts, 3 == sleep after each post. Both are
        # reasonable implementations; anything else means the delay is missing or
        # is being applied more often than there are posts.
        self.assertIn(
            self.sleep.call_count,
            (2, 3),
            f"expected 2 or 3 sleeps for 3 same-channel posts, got "
            f"{self.sleep.call_count}",
        )

    def test_sleep_uses_the_configured_delay(self):
        self.register(ALICE, ALICE_CHANNEL)
        self.add_task("alpha-task", ALICE)
        self.add_task("beta-task", ALICE)

        main.send_hourly_reminders()

        delay = getattr(main, "REMINDER_POST_DELAY_SECONDS", None)
        self.assertIsNotNone(delay, "REMINDER_POST_DELAY_SECONDS is not defined")
        for call in self.sleep.call_args_list:
            self.assertEqual(call.args[0], delay)

    def test_single_task_does_not_need_more_than_one_sleep(self):
        self.register(ALICE, ALICE_CHANNEL)
        self.add_task("alpha-task", ALICE)

        main.send_hourly_reminders()

        self.assertEqual(self.post.call_count, 1)
        self.assertLessEqual(
            self.sleep.call_count,
            1,
            "a single post should not be padded with multiple delays",
        )


class UnregisteredAssigneeTests(ReminderTestBase):
    """Unregistered people have nowhere to post to and are skipped silently."""

    def test_unregistered_assignee_gets_no_posts(self):
        self.add_task("orphan-task", BOB)  # BOB is never registered

        main.send_hourly_reminders()

        self.post.assert_not_called()

    def test_unregistered_assignee_is_skipped_without_blocking_others(self):
        self.register(ALICE, ALICE_CHANNEL)
        self.add_task("alpha-task", ALICE)
        orphan = self.add_task("orphan-task", BOB)

        main.send_hourly_reminders()

        self.assert_posted_once_for("alpha-task")
        self.assert_not_posted_for("orphan-task")
        self.assertEqual(self.posted_channels(), [ALICE_CHANNEL])
        self.assertIsNone(
            self.last_reminded(orphan),
            "a task that was never posted must not be stamped as reminded",
        )


class MultipleAssigneeTests(ReminderTestBase):
    """Each assignee's posts go to their own channel and fail independently."""

    def setUp(self):
        super().setUp()
        self.register(ALICE, ALICE_CHANNEL)
        self.register(BOB, BOB_CHANNEL)
        self.alice_one = self.add_task("alice-one", ALICE)
        self.alice_two = self.add_task("alice-two", ALICE)
        self.bob_one = self.add_task("bob-one", BOB)
        self.bob_two = self.add_task("bob-two", BOB)

    def test_each_assignee_gets_posts_in_their_own_channel(self):
        main.send_hourly_reminders()

        self.assertEqual(self.post.call_count, 4)
        self.assertEqual(self.posted_channels().count(ALICE_CHANNEL), 2)
        self.assertEqual(self.posted_channels().count(BOB_CHANNEL), 2)

        for description, channel in (
            ("alice-one", ALICE_CHANNEL),
            ("alice-two", ALICE_CHANNEL),
            ("bob-one", BOB_CHANNEL),
            ("bob-two", BOB_CHANNEL),
        ):
            call = self.assert_posted_once_for(description)
            self.assertEqual(
                call.kwargs.get("channel"),
                channel,
                f"{description} was posted to the wrong channel",
            )

    def test_one_assignees_failure_does_not_affect_the_other(self):
        def fail_on_alice_one(**kwargs):
            payload = json.dumps(
                {"text": kwargs.get("text"), "blocks": kwargs.get("blocks")}, default=str
            )
            if "alice-one" in payload:
                raise RuntimeError("simulated Slack failure for alice-one")
            return {"ok": True}

        self.post.side_effect = fail_on_alice_one

        main.send_hourly_reminders()

        self.assertEqual(self.post.call_count, 4)
        self.assert_posted_once_for("bob-one")
        self.assert_posted_once_for("bob-two")

        self.assertIsNone(self.last_reminded(self.alice_one))
        self.assertEqual(self.last_reminded(self.alice_two), self.now.isoformat())
        self.assertEqual(self.last_reminded(self.bob_one), self.now.isoformat())
        self.assertEqual(self.last_reminded(self.bob_two), self.now.isoformat())


class ExistingCadenceStillHoldsTests(ReminderTestBase):
    """Behaviour that pre-dates Step 1 and must survive the restructuring."""

    def test_future_remind_from_task_stays_silent(self):
        self.register(ALICE, ALICE_CHANNEL)
        future = (self.now.date() + timedelta(days=3)).isoformat()
        self.add_task("alpha-task", ALICE)
        self.add_task("scheduled-task", ALICE, due_offset_days=10, remind_from=future)

        main.send_hourly_reminders()

        self.assert_posted_once_for("alpha-task")
        self.assert_not_posted_for("scheduled-task")

    def test_task_reminded_within_its_interval_is_skipped(self):
        self.register(ALICE, ALICE_CHANNEL)
        recent = self.add_task("recent-task", ALICE, priority="LOW")  # 24h interval
        self.set_last_reminded(recent, self.now - timedelta(hours=2))
        self.add_task("alpha-task", ALICE, priority="HIGH")

        main.send_hourly_reminders()

        self.assert_posted_once_for("alpha-task")
        self.assert_not_posted_for("recent-task")

    def test_overdue_task_is_reminded_alongside_a_current_one(self):
        self.register(ALICE, ALICE_CHANNEL)
        overdue = self.add_task("overdue-task", ALICE, due_offset_days=-3, priority="LOW")
        self.set_last_reminded(overdue, self.now - timedelta(hours=2))
        self.add_task("today-task", ALICE, due_offset_days=0, priority="HIGH")

        main.send_hourly_reminders()

        # LOW would normally wait 24h, but an overdue task is BACKLOG and nags
        # hourly, so both go out — as two separate messages.
        self.assertEqual(self.post.call_count, 2)
        overdue_call = self.assert_posted_once_for("overdue-task")
        self.assertIn(
            "BACKLOG",
            self.payload(overdue_call),
            "an overdue task should be labelled BACKLOG, not by its original priority",
        )
        self.assert_posted_once_for("today-task")

    def test_no_posts_outside_working_hours(self):
        self.register(ALICE, ALICE_CHANNEL)
        self.add_task("alpha-task", ALICE)

        with mock.patch.object(
            main, "now_est", return_value=self.now.replace(hour=23)
        ):
            main.send_hourly_reminders()

        self.post.assert_not_called()

    def test_done_tasks_are_never_reminded(self):
        self.register(ALICE, ALICE_CHANNEL)
        finished = self.add_task("finished-task", ALICE)
        main.mark_done(finished)
        self.add_task("alpha-task", ALICE)

        main.send_hourly_reminders()

        self.assert_posted_once_for("alpha-task")
        self.assert_not_posted_for("finished-task")


if __name__ == "__main__":
    unittest.main()
