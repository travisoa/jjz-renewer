from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import plistlib
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

from jjz.api import ApiError, BUSINESS_LOGIN_ORIGIN, UNIFIED_AUTH_CALLBACK_URL
from jjz.cli import main, unattended_login
from jjz.config import ConfigError, config_for_vehicle, load_config


PLATE, OTHER, THIRD, FOURTH = "冀ATEST01", "冀BTEST02", "冀CTEST03", "冀DTEST04"
BASE = f"""# 申请模板按本人实际情况填写
[api]
base_url = "{BUSINESS_LOGIN_ORIGIN}"
auth_file = "secrets/authorization.txt"
auto_relogin = true

[vehicle]
plate = "{PLATE}" # 本人车牌
default_permit = "outside"

[renewal]
earliest_time = "09:30"
submission_enabled = true

[storage]
data_dir = "data"

[application]
xxdz = "测试地址" # 目的地
txrxx = []
"""


def state(*plates):
    """A stateList response carrying every vehicle field the official pages use."""
    return {"code": 200, "data": {"bzclxx": [
        {"vId": f"SYNTHETIC_VID_{index}", "hphm": plate, "hpzl": "02", "cllx": "K33",
         "ylzsfkb": True, "elzsfkb": index != 1, "ylzqyms": "", "elzqyms": "",
         "ylzmc": "六环内", "elzmc": "六环外", "sycs": 8, "ecbzxx": None, "bzxx": None}
        for index, plate in enumerate(plates)]}}


class VehicleConfigTextTests(unittest.TestCase):
    def test_copy_changes_only_plate_data_dir_and_both_switches(self):
        text = config_for_vehicle(BASE, OTHER, "data/car2")
        self.assertEqual(tomllib.loads(text), {
            "api": {"base_url": BUSINESS_LOGIN_ORIGIN, "auth_file": "secrets/authorization.txt",
                    "auto_relogin": True},
            "vehicle": {"plate": OTHER, "default_permit": "outside"},
            "renewal": {"earliest_time": "09:30", "submission_enabled": False, "auto_renew": False},
            "storage": {"data_dir": "data/car2"},
            "application": {"xxdz": "测试地址", "txrxx": []},
        })
        # Explanations and code tables in comments stay with the copied template.
        self.assertIn('xxdz = "测试地址" # 目的地', text)

    def test_missing_keys_and_tables_are_added(self):
        base = f'[api]\nbase_url = "{BUSINESS_LOGIN_ORIGIN}"\n[renewal]\nearliest_time = "08:00"\n'
        self.assertEqual(tomllib.loads(config_for_vehicle(base, OTHER, "data/car2")), {
            "api": {"base_url": BUSINESS_LOGIN_ORIGIN},
            "renewal": {"earliest_time": "08:00", "submission_enabled": False, "auto_renew": False},
            "vehicle": {"plate": OTHER},
            "storage": {"data_dir": "data/car2"},
        })

    def test_layout_the_line_edits_cannot_reproduce_is_refused(self):
        for base in (f'vehicle.plate = "{PLATE}"\n', f'[vehicle]\nplate = """{PLATE}\n"""\n'):
            with self.subTest(base=base), self.assertRaises(ConfigError):
                config_for_vehicle(base, OTHER, "data/car2")


class AutoRenewSettingTests(unittest.TestCase):
    def test_switch_defaults_on_and_must_be_boolean(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text("[renewal]\n", encoding="utf-8")
            self.assertTrue(load_config(path).auto_renew)
            path.write_text("[renewal]\nauto_renew = false\n", encoding="utf-8")
            self.assertFalse(load_config(path).auto_renew)
            path.write_text('[renewal]\nauto_renew = "no"\n', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class ConfigFileTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def run_cli(self, config, *args):
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["--config", str(config), *args])
        return code, json.loads(output.getvalue())

    def test_init_and_schedule_accept_only_names_that_git_ignores(self):
        for name in ("car2.toml", "config_car2.toml", "config-车2.toml"):
            with self.subTest(name=name):
                code, result = self.run_cli(self.root / name, "init")
                self.assertEqual((code, result["action"]), (2, "error"))
                self.assertFalse((self.root / name).exists())
        config = self.root / "config_car2.toml"
        config.write_text('[storage]\ndata_dir = "data/car2"\n', encoding="utf-8")
        code, result = self.run_cli(config, "schedule")
        self.assertEqual((code, result["action"]), (2, "error"))
        self.assertFalse((self.root / "data").exists())

    def test_init_starts_an_extra_vehicle_on_demand(self):
        # Like vehicles --create: a new vehicle is not renewed by schedules until both switches are on.
        for name, automatic in (("config.toml", True), ("config-car2.toml", False)):
            with self.subTest(name=name):
                code, _ = self.run_cli(self.root / name, "init")
                self.assertEqual(code, 0)
                settings = load_config(self.root / name)
                self.assertIs(settings.auto_renew, automatic)
                self.assertFalse(settings.submission_enabled)

    def test_init_gives_an_extra_vehicle_its_own_data_directory(self):
        for name, data_dir in (("config.toml", "data"), ("config-car2.toml", "data/car2")):
            with self.subTest(name=name):
                code, _ = self.run_cli(self.root / name, "init")
                self.assertEqual(code, 0)
                self.assertEqual(load_config(self.root / name).data_dir, self.root / data_dir)


class VehicleCliCase(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.auth = self.root / "secrets/authorization.txt"
        self.auth.parent.mkdir()
        self.auth.write_text("SYNTHETIC_AUTHORIZATION", encoding="utf-8")
        self.auth.chmod(0o600)
        self.config = self.write("config.toml", BASE)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def cli(self, *args, states=()):
        client = Mock()
        client.state.side_effect = list(states)
        output = StringIO()
        with patch("jjz.cli.ApiClient", return_value=client), redirect_stdout(output), \
                redirect_stderr(StringIO()):
            code = main(["--config", str(self.config), *args])
        for plate in (PLATE, OTHER, THIRD, FOURTH):
            self.assertNotIn(plate, output.getvalue())
        return code, json.loads(output.getvalue())

    def names(self):
        return sorted(path.name for path in self.root.iterdir())


class VehiclesCommandTests(VehicleCliCase):
    def test_lists_account_vehicles_and_their_configs_without_writing(self):
        code, result = self.cli("vehicles", states=[state(PLATE, OTHER)])
        self.assertEqual(code, 0)
        self.assertEqual(result["vehicles"], [
            {"vehicle": "冀A***01", "can_apply": {"inside": True, "outside": True},
             "configs": [{"file": "config.toml", "auto_renew": True, "submission_enabled": True}]},
            {"vehicle": "冀B***02", "can_apply": {"inside": True, "outside": False}, "configs": []},
        ])
        self.assertEqual(result["unmatched_configs"], [])
        self.assertEqual(self.names(), ["config.toml", "secrets"])

    def test_create_adds_private_config_for_each_vehicle_without_one(self):
        before = self.config.read_bytes()
        code, result = self.cli("vehicles", "--create", states=[state(PLATE, OTHER, THIRD)])
        self.assertEqual(code, 0)
        self.assertEqual([item["configs"] for item in result["vehicles"]], [
            [{"file": "config.toml", "auto_renew": True, "submission_enabled": True}],
            [{"file": "config-car2.toml", "auto_renew": False, "submission_enabled": False, "created": True}],
            [{"file": "config-car3.toml", "auto_renew": False, "submission_enabled": False, "created": True}],
        ])
        self.assertEqual(self.config.read_bytes(), before)
        for name, plate, data_dir in (("config-car2.toml", OTHER, "data/car2"),
                                      ("config-car3.toml", THIRD, "data/car3")):
            with self.subTest(name=name):
                path = self.root / name
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                settings = load_config(path)
                self.assertEqual(settings.plate, plate)
                self.assertEqual(settings.data_dir, self.root / data_dir)
                self.assertEqual(settings.auth_file, self.auth)
                self.assertFalse(settings.submission_enabled)
                self.assertFalse(settings.auto_renew)
                self.assertEqual(settings.earliest_time, "09:30")
                self.assertEqual(settings.application, {"xxdz": "测试地址", "txrxx": []})

    def test_create_twice_leaves_existing_configs_alone(self):
        self.cli("vehicles", "--create", states=[state(PLATE, OTHER)])
        created = (self.root / "config-car2.toml").read_bytes()
        code, result = self.cli("vehicles", "--create", states=[state(PLATE, OTHER)])
        self.assertEqual(code, 0)
        self.assertEqual(result["vehicles"][1]["configs"],
                         [{"file": "config-car2.toml", "auto_renew": False, "submission_enabled": False}])
        self.assertEqual((self.root / "config-car2.toml").read_bytes(), created)
        self.assertEqual(self.names(), ["config-car2.toml", "config.toml", "secrets"])

    def test_new_config_skips_names_and_data_directories_already_taken(self):
        self.write("config-car2.toml", BASE.replace(PLATE, THIRD).replace('"data"', '"data/car2"'))
        (self.root / "data/car3").mkdir(parents=True)
        code, result = self.cli("vehicles", "--create", states=[state(PLATE, OTHER, THIRD)])
        self.assertEqual(code, 0)
        self.assertEqual(result["vehicles"][1]["configs"][0]["file"], "config-car4.toml")
        self.assertEqual(load_config(self.root / "config-car4.toml").data_dir, self.root / "data/car4")

    def test_two_configs_for_one_vehicle_are_reported(self):
        self.write("config-car2.toml", BASE.replace('"data"', '"data/car2"'))
        code, result = self.cli("vehicles", states=[state(PLATE)])
        self.assertEqual(code, 2)
        self.assertEqual([entry["file"] for entry in result["vehicles"][0]["configs"]],
                         ["config.toml", "config-car2.toml"])

    def test_two_configs_for_a_vehicle_no_longer_bound_are_reported(self):
        for name, data_dir in (("config-car2.toml", "data/car2"), ("config-car3.toml", "data/car3")):
            self.write(name, BASE.replace(PLATE, THIRD).replace('"data"', f'"{data_dir}"'))
        code, result = self.cli("vehicles", states=[state(PLATE)])
        self.assertEqual(code, 2)
        self.assertEqual(result["duplicates"], [["config-car2.toml", "config-car3.toml"]])
        self.assertEqual([entry["file"] for entry in result["unmatched_configs"]],
                         ["config-car2.toml", "config-car3.toml"])

    def test_configs_whose_vehicle_left_the_account_are_listed(self):
        self.write("config-car2.toml", BASE.replace(PLATE, THIRD).replace('"data"', '"data/car2"'))
        other_account = BASE.replace(PLATE, FOURTH).replace("secrets/authorization.txt", "secrets/other.txt")
        self.write("config-car3.toml", other_account.replace('"data"', '"data/car3"'))
        code, result = self.cli("vehicles", states=[state(PLATE)])
        self.assertEqual(code, 0)
        self.assertEqual(result["unmatched_configs"], [{"file": "config-car2.toml", "vehicle": "冀C***03"}])

    def test_unreadable_config_beside_stops_before_creating(self):
        self.write("config-car2.toml", "[vehicle\n")
        code, result = self.cli("vehicles", "--create", states=[state(PLATE, OTHER)])
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "error")
        self.assertIn("config-car2.toml", result["reason"])
        self.assertEqual(self.names(), ["config-car2.toml", "config.toml", "secrets"])

    def test_vehicle_without_a_distinct_plate_stops_before_creating(self):
        for response in (state(PLATE, ""), state(PLATE, OTHER, OTHER)):
            with self.subTest(response=response):
                code, result = self.cli("vehicles", "--create", states=[response])
                self.assertEqual(code, 2)
                self.assertEqual(result["action"], "error")
                self.assertEqual(self.names(), ["config.toml", "secrets"])

    def test_expired_credential_is_renewed_before_listing(self):
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        with patch("jjz.cli.unattended_login"):
            code, result = self.cli("vehicles", states=[expired, state(PLATE)])
        self.assertEqual(code, 0)
        self.assertEqual(result["relogin"], "succeeded")
        self.assertEqual(result["vehicles"][0]["vehicle"], "冀A***01")


class SameVehicleTests(VehicleCliCase):
    def test_run_is_blocked_while_another_config_has_the_same_vehicle(self):
        self.write("config-car2.toml", BASE.replace('"data"', '"data/car2"'))
        self.auth.unlink()  # a run that went ahead would stop at the missing credential instead
        code, result = self.cli("run", "--submit")
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "blocked")
        self.assertIn("config-car2.toml", result["reason"])

    def test_unreadable_config_beside_does_not_block_run(self):
        # A config that cannot load cannot submit either.
        self.write("config-car2.toml", "[vehicle\n")
        self.auth.unlink()
        code, result = self.cli("run")
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "error")
        self.assertIn("凭证", result["reason"])


class OnDemandVehicleTests(VehicleCliCase):
    def on_demand(self):
        self.config.write_text(BASE.replace("submission_enabled = true",
                                            "submission_enabled = true\nauto_renew = false"), encoding="utf-8")

    def test_schedule_is_not_generated_for_an_on_demand_vehicle(self):
        self.on_demand()
        code, result = self.cli("schedule")
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "error")
        self.assertEqual(self.names(), ["config.toml", "secrets"])

    def test_scheduled_job_marks_its_runs_as_scheduled(self):
        code, result = self.cli("schedule")
        self.assertEqual(code, 0)
        arguments = plistlib.loads(Path(result["path"]).read_bytes())["ProgramArguments"]
        self.assertEqual(arguments[-4:], ["run", "--scheduled", "--permit", "outside"])

    def test_scheduled_run_skips_an_on_demand_vehicle_without_any_query(self):
        self.on_demand()
        self.auth.unlink()  # a run that went ahead would stop at the missing credential
        code, result = self.cli("run", "--scheduled")
        self.assertEqual(code, 0)
        self.assertEqual(result["action"], "wait")
        self.assertEqual(self.names(), ["config.toml", "secrets"])

    def test_on_demand_vehicle_still_runs_when_asked(self):
        self.on_demand()
        self.auth.unlink()
        code, result = self.cli("run")
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "error")
        self.assertIn("凭证", result["reason"])

    def test_doctor_reports_whether_the_vehicle_renews_automatically(self):
        for configure, expected in ((lambda: None, True), (self.on_demand, False)):
            with self.subTest(expected=expected):
                configure()
                output = StringIO()
                with redirect_stdout(output):
                    main(["--config", str(self.config), "doctor"])
                self.assertIs(json.loads(output.getvalue())["auto_renew"], expected)


class LoginAddsConfigsTests(VehicleCliCase):
    def login_doubles(self):
        gateway = Mock()
        gateway.captcha.return_value = b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE"
        gateway.login_sms.return_value = {"redirect_url": UNIFIED_AUTH_CALLBACK_URL + "?code=SECRET_CODE"}
        gateway.login_password.return_value = gateway.login_sms.return_value
        gateway.take_callback.return_value = (
            BUSINESS_LOGIN_ORIGIN + "/login_h5_byUserCenter.html?token=SECRET_TICKET&state=300001", b"")
        business = Mock()
        business.exchange.return_value = "SYNTHETIC_BUSINESS_AUTH"
        verifier = Mock()
        verifier.state.return_value = state(PLATE, OTHER)
        return gateway, business, verifier

    def test_interactive_login_adds_configs_for_the_other_vehicles(self):
        gateway, business, verifier = self.login_doubles()
        output = StringIO()
        with patch("jjz.cli.LoginGateway", return_value=gateway), \
                patch("sys.stdin.isatty", return_value=True), \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=verifier), \
                patch("jjz.cli.webbrowser.open", return_value=True), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["--config", str(self.config), "auth-set", "--login", "--method", "sms"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["credential_saved"])
        self.assertEqual(result["configs_created"], ["config-car2.toml"])
        created = load_config(self.root / "config-car2.toml")
        self.assertEqual(created.plate, OTHER)
        self.assertFalse(created.auto_renew)
        self.assertFalse(created.submission_enabled)
        self.assertNotIn(OTHER, output.getvalue())

    def test_unattended_login_never_adds_configs(self):
        gateway, business, verifier = self.login_doubles()
        self.write("secrets/login.toml", '[login]\nphone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with patch("jjz.cli.recognize_captcha", return_value="ab12"), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=verifier), \
                redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = unattended_login(load_config(self.config), gateway)
        self.assertTrue(result["credential_saved"])
        self.assertNotIn("config-car2.toml", self.names())


if __name__ == "__main__":
    unittest.main()
