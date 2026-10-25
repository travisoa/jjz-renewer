from pathlib import Path
import json
import tempfile
import unittest

from jjz.api import ApiError, STATE_PATH
from jjz.config import ConfigError, Settings
from jjz.credentials import extract_authorization, verify_and_save
from jjz.domain import DomainError


ORIGIN = "https://service.example.invalid"
TOKEN = "SYNTHETIC_BUSINESS_AUTH"
PLATE = "冀ATEST01"


def entry(origin=ORIGIN, path=STATE_PATH, token=TOKEN):
    return {"request": {"method": "POST", "url": origin + path,
                        "headers": [{"name": "Authorization", "value": token}]}}


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.har = self.root / "capture.har"
        self.settings = Settings(config_path=self.root / "config.toml", base_url=ORIGIN,
                                 auth_file=self.root / "secrets/authorization.txt", plate=PLATE)

    def capture(self, entries):
        self.har.write_text(json.dumps({"log": {"entries": entries}}))

    def test_only_selected_state_request_not_login_token_or_foreign_host(self):
        self.capture([entry(path="/login", token="LOGIN_TOKEN"),
                      entry(origin="https://foreign.example.invalid", token="FOREIGN_TOKEN"),
                      entry(), entry()])
        self.assertEqual(extract_authorization(self.har, ORIGIN), TOKEN)

    def test_official_h5_double_slash_path_matches(self):
        self.capture([entry(path="/pro//applyRecordController/stateList")])
        self.assertEqual(extract_authorization(self.har, ORIGIN), TOKEN)

    def test_conflicting_tokens_or_missing_header_do_not_pick_one(self):
        for entries in ([entry(), entry(token="OTHER")], [entry(path="/login")], []):
            self.capture(entries)
            with self.assertRaises(ConfigError):
                extract_authorization(self.har, ORIGIN)

    def test_no_response_or_cookies_are_extracted(self):
        sample = entry()
        sample["request"]["headers"] = [{"name": "Cookie", "value": TOKEN}]
        sample["response"] = {"content": {"text": TOKEN}}
        self.capture([sample])
        with self.assertRaises(ConfigError):
            extract_authorization(self.har, ORIGIN)

    def test_header_injection_and_non_https_are_rejected(self):
        self.capture([entry(token="SECRET\r\nOther: value")])
        with self.assertRaises(ConfigError):
            extract_authorization(self.har, ORIGIN)
        with self.assertRaises(ConfigError):
            extract_authorization(self.har, "http://service.example.invalid")

    def test_bad_har_errors_are_sanitized(self):
        self.har.write_text(TOKEN)
        with self.assertRaises(ConfigError) as result:
            extract_authorization(self.har, ORIGIN)
        self.assertNotIn(TOKEN, str(result.exception))

    def test_verify_then_atomic_save_only_for_target_vehicle(self):
        seen = []

        class Client:
            def __init__(self, settings):
                seen.append(settings.read_authorization())

            def state(self):
                return {"code": 200, "data": {"bzclxx": [{"hphm": PLATE, "ecbzxx": None}]}}

        result = verify_and_save(self.settings, ORIGIN, TOKEN, Client)
        self.assertEqual(result["action"], "business_authorization_verified")
        self.assertEqual(self.settings.read_authorization(), TOKEN)
        self.assertEqual(seen, [TOKEN])
        self.assertEqual(self.settings.auth_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_failed_query_keeps_previous_credential_and_removes_tempfile(self):
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("PREVIOUS_AUTH")
        self.settings.auth_file.chmod(0o600)

        class Client:
            def __init__(self, settings):
                pass

            def state(self):
                raise ApiError("query failed")

        with self.assertRaises(ApiError):
            verify_and_save(self.settings, ORIGIN, TOKEN, Client)
        self.assertEqual(self.settings.read_authorization(), "PREVIOUS_AUTH")
        self.assertEqual(list(self.settings.auth_file.parent.iterdir()), [self.settings.auth_file])

    def test_foreign_vehicle_and_service_change_never_save(self):
        class Client:
            def __init__(self, settings):
                pass

            def state(self):
                return {"code": 200, "data": {"bzclxx": [{"hphm": "OTHER"}]}}

        with self.assertRaises(DomainError):
            verify_and_save(self.settings, ORIGIN, TOKEN, Client)
        with self.assertRaises(ConfigError):
            verify_and_save(self.settings, "https://other.example.invalid", TOKEN, Client)
        self.assertFalse(self.settings.auth_file.exists())


if __name__ == "__main__":
    unittest.main()
