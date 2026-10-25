from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
from io import StringIO
from pathlib import Path
import json
import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from jjz.api import (ApiError, COUNT_CHECK_PATH, DATES_PATH, DRIVER_PATH, ROUTE_PATH,
                     SITE_CONFIG_PATH, VEHICLE_CHECK_PATH)
from jjz.cli import main
from jjz.config import Settings, load_config
from jjz.runtime import execute
from jjz.storage import BusyError, Journal, exclusive


NOW = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
PLATE = "冀A00000"


def record(**overrides):
    return {"hphm": PLATE, "vId": "synthetic-vehicle", "hpzl": "02",
            "jsrxm": "SYNTHETIC_PERSON", "jszh": "SYNTHETIC_IDENTITY",
            "applyId": "old-application", "jjzzl": "02", "jjzzlmc": "六环外",
            "blztmc": "审核通过(生效中)", "yxqs": "2026-10-19", "yxqz": "2026-10-25",
            "sqsj": "2026-10-18 09:00:00", **overrides}


def response(*records, **vehicle):
    return {"code": 200, "data": {"bzclxx": [{
        "hphm": PLATE, "vId": "synthetic-vehicle", "hpzl": "02", "cllx": "01",
        "ylzsfkb": True, "elzsfkb": True, "ylzmc": None, "elzmc": None,
        "ylzqyms": None, "elzqyms": None, "sycs": "8",
        "ecbzxx": list(records), "bzxx": None, **vehicle}]}}


def checks(**overrides):
    return {
        VEHICLE_CHECK_PATH: {"code": 200, "data": "200", "msg": "校验通过!"},
        DRIVER_PATH: {"code": 200, "data": {"jsrxm": "SYNTHETIC_PERSON",
                                            "jszh": "SYNTHETIC_IDENTITY", "dabh": ""}},
        SITE_CONFIG_PATH: {"code": 200, "data": {"txrkg": "0"}},
        COUNT_CHECK_PATH: {"code": 200, "data": "校验通过!"},
        DATES_PATH: {"code": 200, "data": {"jjrqs": ["2026-10-26", "2026-10-27", "2026-10-28"]}},
        ROUTE_PATH: {"code": 200, "data": "0"},
        **overrides,
    }


class FakeClient:
    def __init__(self, states, error=None, server=None):
        self.states = list(states)
        self.error = error
        self.server = server or checks()
        self.checked = []
        self.submissions = []

    def state(self):
        item = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(item, Exception):
            raise item
        return deepcopy(item)

    def check(self, path, payload):
        self.checked.append((path, deepcopy(payload)))
        item = self.server[path]
        if isinstance(item, Exception):
            raise item
        return deepcopy(item)

    def submit(self, payload):
        self.submissions.append(deepcopy(payload))
        if self.error:
            raise self.error
        return {"code": 200}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        auth = self.root / "authorization"
        auth.write_text("SYNTHETIC_AUTHORIZATION")
        auth.chmod(0o600)
        application = {
            "jjmd": "06", "jjmdmc": "SYNTHETIC_PURPOSE", "area": "顺义区", "jjdq": "010",
            "xxdz": "SYNTHETIC_ADDRESS", "jjdzgdjd": "116.6", "jjdzgdwd": "40.1",
            "sfzj": "0", "jjlk": "00306", "jjlkmc": "SYNTHETIC_ROUTE",
            "jjlkgdjd": "116.9", "jjlkgdwd": "39.6", "sqdzgdjd": "116", "sqdzgdwd": "40",
            "txrxx": [],
        }
        self.settings = Settings(config_path=self.root / "config.toml", base_url="https://example.invalid",
                                 auth_file=auth, plate=PLATE, data_dir=self.root / "data",
                                 submission_enabled=True, application=application)

    def test_preview_has_no_submission_or_journal_attempt(self):
        client = FakeClient([response(record())])
        result = execute(self.settings, "outside", NOW, client=client)
        self.assertEqual(result["action"], "preview")
        self.assertEqual(result["server_checks"], "passed")
        self.assertEqual(client.submissions, [])
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(PLATE), [])
        self.assertNotIn(PLATE, json.dumps(result))

    def test_payload_mirrors_official_apply_pages(self):
        client = FakeClient([response(record())])
        execute(self.settings, "outside", NOW, True, client)
        self.assertEqual([path for path, _ in client.checked],
                         [VEHICLE_CHECK_PATH, DRIVER_PATH, SITE_CONFIG_PATH, COUNT_CHECK_PATH,
                          DATES_PATH, ROUTE_PATH])
        self.assertEqual(dict(client.checked)[COUNT_CHECK_PATH],
                         {"jsrxm": "SYNTHETIC_PERSON", "jszh": "SYNTHETIC_IDENTITY", "dabh": "",
                          "txrxx": [], "txrkg": "0", "wtxr": ""})
        self.assertEqual(dict(client.checked)[DATES_PATH],
                         {"vId": "synthetic-vehicle", "jjzzl": "02", "hphm": PLATE})
        payload = client.submissions[0]
        self.assertNotIn("applyIdOld", payload)
        self.assertNotIn("sqdzbdjd", payload)
        self.assertEqual({key: payload[key] for key in ("vId", "hphm", "hpzl", "cllx", "elzsfkb",
                                                         "jjzzl", "jjrq", "jingState", "dabh")},
                         {"vId": "synthetic-vehicle", "hphm": PLATE, "hpzl": "02", "cllx": "01",
                          "elzsfkb": True, "jjzzl": "02", "jjrq": "2026-10-26",
                          "jingState": "", "dabh": ""})
        self.assertNotIn("sycs", payload)
        self.assertEqual((payload["jjlk"], payload["zjxxdz"], payload["zjxxdzgdjd"]), ("00306", "", ""))

    def test_in_beijing_template_clears_entry_road(self):
        self.settings.application.update({
            "sfzj": "1", "jjlk": "", "jjlkmc": "", "jjlkgdjd": "", "jjlkgdwd": "",
            "zjxxdz": "SYNTHETIC_PARKING", "zjxxdzgdjd": "116.6", "zjxxdzgdwd": "40.3"})
        client = FakeClient([response(record())])
        execute(self.settings, "outside", NOW, True, client)
        payload = client.submissions[0]
        self.assertEqual((payload["sfzj"], payload["zjxxdz"], payload["jjlk"], payload["jjlkgdwd"]),
                         ("1", "SYNTHETIC_PARKING", "", ""))

    def test_server_checks_block_before_any_attempt(self):
        cases = {
            "unavailable": ({"elzsfkb": False}, {}),
            "vehicle_check": ({}, {VEHICLE_CHECK_PATH: {"code": 200, "data": "500"}}),
            "driver_missing": ({}, {DRIVER_PATH: {"code": 200, "data": {"jsrxm": "", "jszh": "x"}}}),
            "companions_on": ({}, {SITE_CONFIG_PATH: {"code": 200, "data": {"txrkg": "1"}}}),
            "count_rejected": ({}, {COUNT_CHECK_PATH: ApiError("接口拒绝业务请求，请在官方 App 中核实。")}),
            "date_not_offered": ({}, {DATES_PATH: {"code": 200, "data": {"jjrqs": ["2026-10-27"]}}}),
            "route_required": ({}, {ROUTE_PATH: {"code": 200, "data": "1"}}),
        }
        for name, (vehicle, server) in cases.items():
            with self.subTest(name=name):
                client = FakeClient([response(record(), **vehicle)], server=checks(**server))
                result = execute(self.settings, "outside", NOW, True, client)
                self.assertEqual(result["action"], "blocked")
                self.assertTrue(result["reason"].startswith("提交前校验未通过"))
                self.assertFalse(client.submissions)
                with Journal(self.settings.data_dir) as journal:
                    self.assertEqual(journal.history(PLATE), [])

    def test_incomplete_template_blocks_preview_without_server_checks(self):
        self.settings.application["jjmd"] = ""
        client = FakeClient([response(record())])
        result = execute(self.settings, "outside", NOW, client=client)
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(client.checked, [])

    def test_submission_requires_switch(self):
        self.settings.submission_enabled = False
        client = FakeClient([response(record())])
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "blocked")
        self.assertFalse(client.submissions)

    def test_ack_does_not_claim_approval_and_does_not_resubmit(self):
        client = FakeClient([response(record())])
        result = execute(self.settings, "outside", NOW, True, client)
        self.assertEqual(result["action"], "accepted")
        self.assertEqual(client.submissions[0]["jjrq"], "2026-10-26")
        again = execute(self.settings, "outside", NOW, True, client)
        self.assertEqual(again["action"], "blocked")
        self.assertEqual(len(client.submissions), 1)

    def test_timeout_then_query_reconciles_without_resend(self):
        client = FakeClient([response(record())], ApiError("timeout", uncertain=True))
        result = execute(self.settings, "outside", NOW, True, client)
        self.assertEqual(result["action"], "uncertain")
        pending = record(applyId="new-application", blztmc="审核中", yxqs="2026-10-26",
                         yxqz=None, sqsj="2026-10-25 09:00:00")
        next_client = FakeClient([response(record(), pending)])
        result = execute(self.settings, "outside", NOW, True, next_client)
        self.assertEqual(result["action"], "wait")
        self.assertFalse(next_client.submissions)
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(PLATE)[0]["state"], "verified")

    def test_uncertain_inside_request_also_blocks_background_outside(self):
        with Journal(self.settings.data_dir) as journal:
            journal.begin(PLATE, "inside", "2026-10-26")
        client = FakeClient([response(record())])
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "blocked")
        self.assertFalse(client.submissions)

    def test_verified_pending_that_disappears_does_not_resubmit_on_a_new_day(self):
        pending = record(applyId="new-application", blztmc="审核中", yxqs="2026-10-26",
                         yxqz=None, sqsj="2026-10-25 09:00:00")
        initial = FakeClient([response(record()), response(record(), pending)])
        self.assertEqual(execute(self.settings, "outside", NOW, True, initial)["action"], "verified")
        later = datetime(2026, 10, 27, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        missing = FakeClient([response(record())])
        self.assertEqual(execute(self.settings, "outside", later, True, missing)["action"], "blocked")
        self.assertFalse(missing.submissions)
        approved = record(applyId="new-application", yxqs="2026-10-26", yxqz="2026-11-01",
                          sqsj="2026-10-25 09:00:00")
        stable = FakeClient([response(record(), approved)])
        self.assertEqual(execute(self.settings, "outside", later, True, stable)["action"], "wait")
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(PLATE)[0]["state"], "approved")

    def test_followup_query_failure_keeps_accepted_durable(self):
        client = FakeClient([response(record()), ApiError("query error")])
        result = execute(self.settings, "outside", NOW, True, client)
        self.assertEqual(result["action"], "accepted")
        with Journal(self.settings.data_dir) as journal:
            self.assertEqual(journal.history(PLATE)[0]["state"], "accepted")

    def test_post_query_approved_is_checked(self):
        approved = record(applyId="new-application", blztmc="审核通过(待生效)",
                          yxqs="2026-10-26", yxqz="2026-11-01", sqsj="2026-10-25 09:00:00")
        client = FakeClient([response(record()), response(record(), approved)])
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "approved")

    def test_approved_without_validity_is_only_verified(self):
        incomplete = record(applyId="new-application", blztmc="审核通过(待生效)",
                            yxqs="2026-10-26", yxqz=None, sqsj="2026-10-25 09:00:00")
        client = FakeClient([response(record()), response(record(), incomplete)])
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "verified")

    def test_query_crossing_noon_uses_time_after_response(self):
        self.settings.inside_application = dict(self.settings.application)
        clock_value = [datetime(2026, 10, 25, 11, 59, 59, tzinfo=NOW.tzinfo)]
        source = record(jjzzl="01", jjzzlmc="六环内", blztmc="已过期",
                        yxqs="2026-10-18", yxqz="2026-10-24")

        class SlowQuery(FakeClient):
            def state(self):
                clock_value[0] = datetime(2026, 10, 25, 12, 0, 1, tzinfo=NOW.tzinfo)
                return super().state()

        client = SlowQuery([response(source)])
        execute(self.settings, "inside", submit=True, client=client, clock=lambda: clock_value[0])
        self.assertEqual(client.submissions[0]["jjrq"], "2026-10-26")

    def test_business_rejection_keeps_same_day_protection(self):
        client = FakeClient([response(record())], ApiError("rejected"))
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "rejected")
        self.assertEqual(execute(self.settings, "outside", NOW, True, client)["action"], "blocked")
        self.assertEqual(len(client.submissions), 1)

    def test_explicit_recovery_only_for_current_vehicle_and_unconfirmed_attempt(self):
        with Journal(self.settings.data_dir) as journal:
            key = journal.begin(PLATE, "outside", "2026-10-26")
            with self.assertRaises(ValueError):
                journal.allow_retry(key, "OTHER")
            journal.update(key, "approved")
            with self.assertRaises(ValueError):
                journal.allow_retry(key, PLATE)
            journal.update(key, "uncertain")
            journal.allow_retry(key, PLATE)
            self.assertIsNone(journal.get(key))

    def test_journal_does_not_store_identity_and_has_private_mode(self):
        client = FakeClient([response(record())])
        execute(self.settings, "outside", NOW, True, client)
        path = self.settings.data_dir / "journal.sqlite3"
        contents = path.read_bytes()
        for value in (PLATE, "SYNTHETIC_IDENTITY", "SYNTHETIC_PERSON", "SYNTHETIC_AUTHORIZATION"):
            self.assertNotIn(value.encode(), contents)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mutual_exclusion(self):
        with exclusive(self.settings.data_dir):
            with self.assertRaises(BusyError):
                with exclusive(self.settings.data_dir):
                    self.fail("second process lock acquired")


class CliTests(unittest.TestCase):
    def test_init_offline_plan_and_pinned_outside_schedule(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / "config.toml"
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "init"]), 0)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            login = root / "secrets/login.toml"
            self.assertEqual(login.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_config(config).read_login(), ("", ""))
            # A later init only fills in missing files and never overwrites either one.
            login.unlink()
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--config", str(config), "init"]), 0)
            self.assertTrue(login.exists())
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "init"]), 0)
            self.assertEqual(json.loads(output.getvalue())["action"], "config_exists")
            output = StringIO()
            fixture = Path(__file__).resolve().parents[1] / "examples/state.json"
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "plan", "--fixture", str(fixture),
                                       "--at", NOW.isoformat()]), 0)
            self.assertEqual(json.loads(output.getvalue())["apply_date"], "2026-10-26")
            schedule = root / "schedule.plist"
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--config", str(config), "schedule", "--output", str(schedule)]), 0)
            data = plistlib.loads(schedule.read_bytes())
            self.assertEqual(data["StartInterval"], 1800)
            self.assertEqual(data["ProgramArguments"][-2:], ["--permit", "outside"])
            self.assertNotIn("--submit", data["ProgramArguments"])
            self.assertNotIn("SYNTHETIC_AUTHORIZATION", schedule.read_text())

    def test_init_creates_login_file_at_configured_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config = root / "config.toml"
            config.write_text('[api]\nlogin_file = "private/login.toml"\n', encoding="utf-8")
            config.chmod(0o600)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "init"]), 0)
            self.assertEqual(json.loads(output.getvalue())["created"], [str(root / "private/login.toml")])
            self.assertEqual((root / "private/login.toml").stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "secrets").exists())

    def test_status_reports_server_flags_and_auth_expiry_category(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            auth = root / "auth"
            auth.write_text("SYNTHETIC_AUTHORIZATION")
            auth.chmod(0o600)
            config = root / "config.toml"
            config.write_text(f'[api]\nbase_url="https://example.invalid"\nauth_file="{auth}"\n'
                              f'[vehicle]\nplate="{PLATE}"\n', encoding="utf-8")
            client = FakeClient([response(record(), elzsfkb=False, sycs="8")])
            output = StringIO()
            with patch("jjz.cli.ApiClient", return_value=client), redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "status"]), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["can_apply"], {"inside": True, "outside": False})
            self.assertEqual(data["inside_remaining"], 8)
            self.assertNotIn(PLATE, output.getvalue())
            expired = FakeClient([ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)])
            output = StringIO()
            with patch("jjz.cli.ApiClient", return_value=expired), redirect_stdout(output):
                self.assertEqual(main(["--config", str(config), "status"]), 2)
            self.assertEqual(json.loads(output.getvalue())["category"], "auth_expired")
            self.assertNotIn("SYNTHETIC_AUTHORIZATION", output.getvalue())

    def test_simulated_time_cannot_be_used_for_live_plan(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.toml"
            with redirect_stdout(StringIO()):
                main(["--config", str(config), "init"])
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(config), "plan", "--at", NOW.isoformat()])
            self.assertEqual(code, 2)
            self.assertIn("--fixture", output.getvalue())


if __name__ == "__main__":
    unittest.main()
