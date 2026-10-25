"""Submission recovery and clock boundaries, using only synthetic local state."""

from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import test_runtime as fixtures
from jjz.api import DATES_PATH, ROUTE_PATH
from jjz.cli import main
from jjz.domain import EXPIRED
from jjz.runtime import execute, reconcile
from jjz.storage import Journal, attempt_key


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.RuntimeTests.setUp(self)
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)

    def test_immediate_failure_or_cancellation_returns_rejected_without_resend(self):
        for index, status in enumerate(("失败(审核不通过)", "已取消")):
            with self.subTest(status=status):
                settings = replace(self.settings, data_dir=self.root / f"rejection-{index}")
                refused = fixtures.record(
                    applyId="synthetic-new", blztmc=status, yxqs="2026-10-26", yxqz=None,
                    sqsj="2026-10-25 09:00:00",
                )
                client = fixtures.FakeClient([
                    fixtures.response(fixtures.record()),
                    fixtures.response(fixtures.record(), refused),
                ])
                result = execute(settings, "outside", fixtures.NOW, True, client)
                self.assertEqual(result["action"], "rejected")
                self.assertEqual(len(client.submissions), 1)
                with Journal(settings.data_dir) as journal:
                    self.assertEqual(journal.get(result["attempt"])["state"], "rejected")
                for now in (fixtures.NOW, datetime(2026, 10, 27, 9, tzinfo=fixtures.NOW.tzinfo)):
                    again = execute(settings, "outside", now, True, client)
                    self.assertEqual(again["action"], "blocked")
                self.assertEqual(len(client.submissions), 1)

    def test_immediate_rejection_cli_emits_one_json_line_and_exit_two(self):
        refused = fixtures.record(
            applyId="synthetic-new", blztmc="失败(审核不通过)", yxqs="2026-10-26",
            yxqz=None, sqsj="2026-10-25 09:00:00",
        )
        client = fixtures.FakeClient([
            fixtures.response(fixtures.record()), fixtures.response(fixtures.record(), refused),
        ])

        def run(settings, permit, submit=False):
            return execute(settings, permit, fixtures.NOW, submit, client)

        output = StringIO()
        with patch("jjz.cli.load_config", return_value=self.settings), \
                patch("jjz.cli.execute", side_effect=run), redirect_stdout(output):
            code = main(["--config", str(self.settings.config_path), "run", "--submit"])
        self.assertEqual(code, 2)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(json.loads(output.getvalue())["action"], "rejected")
        self.assertNotIn("SYNTHETIC_AUTHORIZATION", output.getvalue())
        self.assertNotIn(fixtures.PLATE, output.getvalue())

    def test_expired_server_evidence_resolves_old_attempt_without_deleting_it(self):
        resumed = datetime(2026, 11, 3, 9, tzinfo=fixtures.NOW.tzinfo)
        cases = ((state, status) for state in ("accepted", "verified", "uncertain", "submitting")
                 for status in sorted(EXPIRED))
        for index, (state, status) in enumerate(cases):
            with self.subTest(state=state, status=status):
                settings = replace(self.settings, data_dir=self.root / f"recovery-{index}")
                with Journal(settings.data_dir) as journal:
                    key = journal.begin(fixtures.PLATE, "outside", "2026-10-26")
                    journal.update(key, state)
                expired = fixtures.record(
                    applyId="synthetic-confirmed", blztmc=status,
                    yxqs="2026-10-26", yxqz="2026-11-01", sqsj="2026-10-25 09:00:00",
                )
                server = fixtures.checks(**{
                    DATES_PATH: {"code": 200, "data": {"jjrqs": ["2026-11-03", "2026-11-04"]}},
                })
                client = fixtures.FakeClient([fixtures.response(expired)], server=server)
                result = execute(settings, "outside", resumed, client=client)
                self.assertEqual(result["action"], "preview")
                self.assertEqual(result["apply_date"], "2026-11-03")
                self.assertEqual(result["server_checks"], "passed")
                self.assertEqual(len(client.checked), 6)
                self.assertEqual(client.submissions, [])
                with Journal(settings.data_dir) as journal:
                    self.assertEqual(journal.get(key)["state"], "expired")
                    self.assertEqual(journal.unresolved(fixtures.PLATE), [])
                    self.assertEqual(len(journal.history(fixtures.PLATE)), 1)
                    with self.assertRaises(ValueError):
                        journal.allow_retry(key, fixtures.PLATE)

    def test_incomplete_future_or_nonmatching_expiry_keeps_submission_protection(self):
        resumed = datetime(2026, 11, 3, 9, tzinfo=fixtures.NOW.tzinfo)
        changes = (
            {"blztmc": "未知状态"},
            {"yxqz": None},
            {"yxqz": "not-a-date"},
            {"yxqz": "2026-10-25"},
            {"yxqz": "2026-11-03"},
            {"yxqz": "2026-11-04"},
            {"yxqs": "2026-10-27"},
            {"jjzzl": "01", "jjzzlmc": "六环内"},
        )
        for index, changed in enumerate(changes):
            with self.subTest(changed=changed):
                settings = replace(self.settings, data_dir=self.root / f"unconfirmed-{index}")
                with Journal(settings.data_dir) as journal:
                    key = journal.begin(fixtures.PLATE, "outside", "2026-10-26")
                    journal.update(key, "accepted")
                values = {
                    "applyId": "synthetic-candidate", "blztmc": "已过期", "yxqs": "2026-10-26",
                    "yxqz": "2026-11-01", "sqsj": "2026-10-25 09:00:00", **changed,
                }
                candidate = fixtures.record(**values)
                with Journal(settings.data_dir) as journal:
                    reconcile(journal, fixtures.PLATE, [candidate], resumed)
                    self.assertEqual(journal.get(key)["state"], "accepted")
                    self.assertEqual(len(journal.unresolved(fixtures.PLATE)), 1)
                client = fixtures.FakeClient([fixtures.response(candidate)])
                result = execute(settings, "outside", resumed, True, client)
                self.assertIn(result["action"], ("blocked", "wait"))
                self.assertEqual(client.submissions, [])
                with Journal(settings.data_dir) as journal:
                    self.assertEqual(journal.get(key)["state"], "accepted")

    def test_expiry_uses_beijing_day_not_callers_utc_day(self):
        # 16:00 UTC is already the day after the permit's final day in Beijing.
        from datetime import timezone

        observed = datetime(2026, 11, 1, 16, 0, tzinfo=timezone.utc)
        expired = fixtures.record(
            blztmc="已过期", yxqs="2026-10-26", yxqz="2026-11-01",
        )
        with Journal(self.settings.data_dir) as journal:
            key = journal.begin(fixtures.PLATE, "outside", "2026-10-26")
            reconcile(journal, fixtures.PLATE, [expired], observed)
            self.assertEqual(journal.get(key)["state"], "expired")

    def _run_with_clock_change(self, before, after, permit, source, allowed, submit=True):
        settings = self.settings
        settings.inside_application = deepcopy(settings.application)
        settings.earliest_time = "00:00" if before.date() != after.date() else "08:00"
        clock = [before]

        class SlowChecks(fixtures.FakeClient):
            def check(self, path, payload):
                if path == ROUTE_PATH:
                    clock[0] = after
                return super().check(path, payload)

        server = fixtures.checks(**{DATES_PATH: {"code": 200, "data": {"jjrqs": allowed}}})
        client = SlowChecks([fixtures.response(source)], server=server)
        result = execute(settings, permit, submit=submit, client=client, clock=lambda: clock[0])
        return result, client

    def test_checks_crossing_noon_block_stale_inside_date_without_attempt(self):
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 26, 11, 59, 59, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 26, 12, 0, 1, tzinfo=fixtures.NOW.tzinfo),
            "inside", fixtures.record(jjzzl="01", jjzzlmc="六环内", blztmc="已过期"),
            ["2026-10-26", "2026-10-27"],
        )
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(len(client.checked), 6)
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(fixtures.PLATE), [])

    def test_checks_crossing_midnight_do_not_submit_previous_day(self):
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 25, 23, 59, 59, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 26, 0, 0, 1, tzinfo=fixtures.NOW.tzinfo),
            "outside", fixtures.record(blztmc="已过期", yxqs="2026-10-18", yxqz="2026-10-24"),
            ["2026-10-25", "2026-10-26"],
        )
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(fixtures.PLATE), [])

    def test_preview_does_not_report_passed_checks_for_a_stale_date(self):
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 26, 11, 59, 59, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 26, 12, 0, 1, tzinfo=fixtures.NOW.tzinfo),
            "inside", fixtures.record(jjzzl="01", jjzzlmc="六环内", blztmc="已过期"),
            ["2026-10-26", "2026-10-27"], submit=False,
        )
        self.assertEqual(result["action"], "blocked")
        self.assertNotEqual(result.get("server_checks"), "passed")
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(fixtures.PLATE), [])

    def test_clock_moving_before_earliest_time_blocks_without_attempt(self):
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 25, 8, 0, 1, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 25, 7, 59, 59, tzinfo=fixtures.NOW.tzinfo),
            "outside", fixtures.record(), ["2026-10-26"],
        )
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(fixtures.PLATE), [])

    def test_clock_change_without_decision_change_still_submits_once(self):
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 25, 9, 0, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 25, 9, 1, tzinfo=fixtures.NOW.tzinfo),
            "outside", fixtures.record(), ["2026-10-26"],
        )
        self.assertEqual(result["action"], "accepted")
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(client.submissions[0]["jjrq"], "2026-10-26")

    def test_clock_change_cannot_skip_an_existing_new_date_key(self):
        with Journal(self.settings.data_dir) as journal:
            existing = journal.begin(fixtures.PLATE, "outside", "2026-10-26")
            journal.update(existing, "rejected")
        result, client = self._run_with_clock_change(
            datetime(2026, 10, 25, 23, 59, 59, tzinfo=fixtures.NOW.tzinfo),
            datetime(2026, 10, 26, 0, 0, 1, tzinfo=fixtures.NOW.tzinfo),
            "outside", fixtures.record(blztmc="已过期", yxqs="2026-10-18", yxqz="2026-10-24"),
            ["2026-10-25", "2026-10-26"],
        )
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(len(journal.history(fixtures.PLATE)), 1)
            self.assertIsNotNone(journal.get(existing))
            self.assertIsNone(journal.get(attempt_key(fixtures.PLATE, "outside", "2026-10-25")))


if __name__ == "__main__":
    unittest.main()
