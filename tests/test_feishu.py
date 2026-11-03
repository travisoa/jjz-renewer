from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_vehicles as fixtures
from jjz.api import ApiError
from jjz.cli import main
from jjz.config import ConfigError, load_config


OPEN_ID = "ou_synthetic0001"
NOTIFY = f'\n[notify]\nfeishu_open_id = "{OPEN_ID}"\n'
LARK = "/opt/synthetic/bin/lark-cli"


def outcome(action, **extra):
    return {"action": action, "reason": "合成结果", "vehicle": "冀A***01", "permit": "outside", **extra}


class NotifyCase(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        auth = self.root / "secrets/authorization.txt"
        auth.parent.mkdir()
        auth.write_text("SYNTHETIC_AUTHORIZATION", encoding="utf-8")
        auth.chmod(0o600)
        self.config = self.root / "config.toml"
        self.write(fixtures.BASE + NOTIFY)
        self.calls = []
        self.replies = []  # lark-cli stdout per call; None is a failed call, missing ones succeed
        self.client = Mock()
        self.client.state.return_value = fixtures.state(fixtures.PLATE)

    def write(self, text):
        self.config.write_text(text, encoding="utf-8")
        self.config.chmod(0o600)

    def lark(self, argv, input=None, **kwargs):
        self.calls.append((argv, input))
        reply = self.replies.pop(0) if self.replies else '{\n  "ok": true\n}'
        return subprocess.CompletedProcess(argv, 1 if reply is None else 0, stdout=reply or "", stderr="")

    def run_cli(self, *args, result=None, error=None, lark=LARK):
        execute = Mock(return_value=result, side_effect=error)
        output = StringIO()
        with patch("jjz.cli.execute", execute), patch("jjz.cli.ApiClient", return_value=self.client), \
                patch("jjz.feishu.shutil.which", return_value=lark), \
                patch("jjz.feishu.subprocess.run", side_effect=self.lark), \
                redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["--config", str(self.config), "run", "--scheduled", "--permit", "outside", *args])
        # Neither the receiver nor the full plate reaches the output or the job log.
        self.assertNotIn(OPEN_ID, output.getvalue())
        self.assertNotIn(fixtures.PLATE, output.getvalue())
        return code, json.loads(output.getvalue())


class RunNotifyTests(NotifyCase):
    def test_a_submission_is_sent_as_a_card_from_the_bot(self):
        code, result = self.run_cli("--submit", "--notify",
                                    result=outcome("accepted", apply_date="2026-11-09", attempt="a" * 64))
        self.assertEqual((code, result["notify"]), (0, "sent"))
        self.assertNotIn("notify_reason", result)
        [(argv, content)] = self.calls
        self.assertEqual(argv[:7], [LARK, "im", "+messages-send", "--as", "bot", "--user-id", OPEN_ID])
        self.assertEqual(argv[7:11], ["--msg-type", "interactive", "--content", "-"])
        key = argv[argv.index("--idempotency-key") + 1]
        self.assertTrue(key.startswith("jjz-") and key.endswith("-config") and len(key) <= 45)
        card = json.loads(content)
        self.assertEqual(card["header"]["title"]["content"], "进京证办理")
        self.assertIn("六环外证申请，等待审核", card["config"]["summary"]["content"])
        self.assertIn("已提交 · 已受理", content)
        self.assertNotIn(OPEN_ID, content)
        # The card adds a fresh status query for the permit columns.
        self.client.state.assert_called_once()

    def test_different_results_in_one_minute_get_different_keys(self):
        # lark-cli sends a key once, so a reused key would swallow the second result.
        self.run_cli("--submit", "--notify", error=ApiError("接口网络请求失败或超时"))
        self.run_cli("--submit", "--notify", result=outcome("accepted", apply_date="2026-11-09"))
        keys = [argv[argv.index("--idempotency-key") + 1] for argv, _ in self.calls]
        self.assertEqual(len(set(keys)), 2)

    def test_a_wait_sends_nothing(self):
        code, result = self.run_cli("--submit", "--notify", result=outcome("wait"))
        self.assertEqual(code, 0)
        self.assertNotIn("notify", result)
        self.assertEqual(self.calls, [])
        self.client.state.assert_not_called()

    def test_runs_without_the_flag_never_notify(self):
        # Agents run the same commands without --notify and send their own cards.
        code, result = self.run_cli("--submit", result=outcome("accepted", apply_date="2026-11-09"))
        self.assertEqual(code, 0)
        self.assertNotIn("notify", result)
        self.assertEqual(self.calls, [])

    def test_a_block_keeps_its_exit_code_and_is_reported(self):
        code, result = self.run_cli("--submit", "--notify", result=outcome("blocked"))
        self.assertEqual((code, result["notify"]), (2, "sent"))
        self.assertIn("办理被拦下", self.calls[0][1])

    def test_an_error_is_reported_too(self):
        code, result = self.run_cli("--submit", "--notify", error=ApiError("接口网络请求失败或超时"))
        self.assertEqual((code, result["action"], result["notify"]), (2, "error", "sent"))
        self.assertIn("接口网络请求失败或超时", self.calls[0][1])

    def test_an_expired_credential_skips_the_status_query(self):
        self.write(fixtures.BASE.replace("auto_relogin = true", "auto_relogin = false") + NOTIFY)
        code, result = self.run_cli("--submit", "--notify", error=ApiError("凭证失效", auth_expired=True))
        self.assertEqual((code, result["category"], result["notify"]), (2, "auth_expired", "sent"))
        self.client.state.assert_not_called()
        self.assertIn("凭证已失效", self.calls[0][1])

    def test_a_failed_card_falls_back_to_text(self):
        self.replies = [None]
        code, result = self.run_cli("--submit", "--notify", result=outcome("accepted", apply_date="2026-11-09"))
        self.assertEqual((code, result["notify"]), (0, "sent_text"))
        argv, content = self.calls[1]
        self.assertEqual(argv[7:9], ["--text", "-"])
        self.assertTrue(argv[argv.index("--idempotency-key") + 1].endswith("-config-text"))
        self.assertTrue(content.startswith("【进京证办理"))

    def test_a_failed_send_never_changes_the_result(self):
        self.replies = [None, '{"ok": false}']
        code, result = self.run_cli("--submit", "--notify", result=outcome("accepted", apply_date="2026-11-09"))
        self.assertEqual((code, result["action"]), (0, "accepted"))
        self.assertEqual((result["notify"], result["notify_reason"]), ("failed", "lark-cli 发送失败"))

    def test_missing_lark_cli_or_receiver_is_reported(self):
        code, result = self.run_cli("--submit", "--notify", result=outcome("blocked"), lark=None)
        self.assertEqual((code, result["notify"], result["notify_reason"]), (2, "failed", "未找到 lark-cli"))
        self.write(fixtures.BASE)
        code, result = self.run_cli("--submit", "--notify", result=outcome("blocked"))
        self.assertEqual((result["notify"], result["notify_reason"]), ("failed", "未配置 notify.feishu_open_id"))
        self.assertEqual(self.calls, [])


class NotifySettingTests(NotifyCase):
    def test_receiver_must_be_an_open_id(self):
        self.assertEqual(load_config(self.config).feishu_open_id, OPEN_ID)
        self.write(fixtures.BASE + '\n[notify]\nfeishu_open_id = "someone@example.com"\n')
        with self.assertRaises(ConfigError):
            load_config(self.config)

    def schedule(self, which):
        output = StringIO()
        with patch("jjz.cli.shutil.which", side_effect=which.get), redirect_stdout(output):
            code = main(["--config", str(self.config), "schedule", "--submit", "--notify"])
        return code, json.loads(output.getvalue())

    def test_schedule_passes_the_flag_and_a_path_that_finds_lark_cli(self):
        code, result = self.schedule({"lark-cli": LARK, "node": "/opt/node/bin/node"})
        self.assertEqual((code, result["notify"]), (0, True))
        data = plistlib.loads(Path(result["path"]).read_bytes())
        self.assertEqual(data["ProgramArguments"][-2:], ["--submit", "--notify"])
        # launchd's bare PATH would not find lark-cli or the node it runs on.
        self.assertEqual(data["EnvironmentVariables"]["PATH"],
                         "/opt/synthetic/bin:/opt/node/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertNotIn(OPEN_ID, Path(result["path"]).read_text(encoding="utf-8"))

    def test_schedule_refuses_without_lark_cli_or_receiver(self):
        code, result = self.schedule({})
        self.assertEqual((code, result["action"]), (2, "error"))
        self.write(fixtures.BASE)
        code, result = self.schedule({"lark-cli": LARK})
        self.assertEqual((code, result["action"]), (2, "error"))
        self.assertFalse((self.root / "data").exists())


class CardScriptTests(unittest.TestCase):
    def test_the_skill_script_builds_the_same_card_through_its_link(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            status.write_text(json.dumps({"action": "status", "vehicle": "冀A***01", "records": [],
                                          "can_apply": {"outside": True, "inside": True},
                                          "inside_remaining": 8}), encoding="utf-8")
            done = subprocess.run([sys.executable, str(project / ".claude/skills/jjz-feishu-card/build_card.py"),
                                   "--mode", "check", "--status", str(status)],
                                  capture_output=True, text=True, cwd=folder, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        card = json.loads(done.stdout)
        self.assertEqual((card["schema"], card["header"]["title"]["content"]), ("2.0", "进京证办理检查"))


if __name__ == "__main__":
    unittest.main()
