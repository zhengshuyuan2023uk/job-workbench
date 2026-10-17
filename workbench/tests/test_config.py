"""Configuration and permission defaults, with no external connections."""
from test_support import ConfiguredTestCase, TEST_CONFIG
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Config, get_config, _load
from resume_policy import official_receipt
from workbench_tasks import default_runtime_check, WorkbenchTasks, eligibility
from mail_notifier import MailNotifier


class ConfigTests(unittest.TestCase):
    def test_defaults_contain_no_identity_or_live_permission(self):
        with patch('config._override', None), patch.dict(os.environ, {'JOB_WORKBENCH_CONFIG': ''}):
            config = get_config()
            self.assertFalse(config.live_enabled)
            self.assertFalse(config.notifications_enabled)
            self.assertEqual((config.self_uid, config.self_name, config.attachment_name, config.notification_email), ('', '', '', ''))
            with self.assertRaises(ValueError):
                config.require_live()

    def test_environment_selects_json_file_and_rejects_unknown_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            good = Path(folder) / 'good.json'
            good.write_text(json.dumps({'self_name': '另一个虚构候选人', 'codex_executable': 'custom-codex'}))
            with patch('config._override', None), patch.dict(os.environ, {'JOB_WORKBENCH_CONFIG': str(good)}):
                self.assertEqual(get_config().self_name, '另一个虚构候选人')
                self.assertEqual(get_config().codex_executable, 'custom-codex')
            bad = Path(folder) / 'bad.json'
            bad.write_text('{"live_enabled": false, "api_key": "never-accepted"}')
            with self.assertRaises(ValueError):
                _load(str(bad))

    def test_only_loopback_cdp_and_strict_booleans_are_accepted(self):
        for endpoint in ('http://example.com:9229', 'http://localhost:9229', 'https://127.0.0.1:9229',
                         'http://127.0.0.1:9229/private', 'http://user:pass@127.0.0.1:9229', 'http://127.0.0.1:99999'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Config(cdp_url=endpoint).validate()
        with self.assertRaises(ValueError):
            Config(live_enabled='true').validate()
        self.assertEqual(Config(cdp_url='http://127.0.0.1:9333').validate().cdp_url, 'http://127.0.0.1:9333')

    def test_enabling_live_without_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            Config(live_enabled=True).require_live()

    def test_unconfigured_task_cannot_start_even_with_injected_factories(self):
        with tempfile.TemporaryDirectory() as folder, patch('config._override', Config()):
            task = WorkbenchTasks(folder, runtime_check=lambda: True)
            with self.assertRaises(ValueError):
                task.start('collect')
            self.assertIsNone(task.thread)

    def test_old_heartbeat_is_optional_but_active_one_blocks(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'CODEX_HOME': folder}), patch('shutil.which', return_value=None):
            self.assertTrue(default_runtime_check())
            path = Path(folder) / 'automations/boss/automation.toml'
            path.parent.mkdir(parents=True)
            path.write_text('status = "ACTIVE"\n')
            self.assertFalse(default_runtime_check())
            path.write_text('status = "PAUSED"\n')
            self.assertTrue(default_runtime_check())

    def test_no_default_personal_salary_or_city_policy(self):
        result = eligibility({'salary': '20-30K', 'city': '北京'}, {})
        self.assertEqual(result[0], 'escalate')

    def test_disabled_notifications_never_call_runner_or_claim_dispatch(self):
        with tempfile.TemporaryDirectory() as folder, patch('config._override', Config()):
            root = Path(folder)
            (root / 'applications.json').write_text(json.dumps({'applications': [], 'reply_monitor': {'notification_events': []}}))
            notifier = MailNotifier(root, runner=lambda *a, **k: self.fail('must not start any external process'))
            event = notifier.enqueue('test-event', 'needs_user_input', '虚构公司', '请补充事实')
            result = notifier.deliver(event)
            self.assertEqual(result['status'], 'pending')
            saved = json.loads((root / 'applications.json').read_text())['reply_monitor']['notification_events'][0]
            self.assertEqual(saved['status'], 'pending')


class ConfiguredReceiptTests(ConfiguredTestCase):
    def test_attachment_name_is_configured_and_regex_escaped(self):
        config = replace(TEST_CONFIG, attachment_name='新名字(工作)+2026')
        with patch('config._override', config):
            self.assertTrue(official_receipt({'body_type': 12, 'text': '您的附件简历 新名字(工作)+2026.pdf 已发送给Boss'}))
            self.assertFalse(official_receipt({'body_type': 12, 'text': '您的附件简历 新名字工作2026.pdf 已发送给Boss'}))
            self.assertFalse(official_receipt({'body_type': 12, 'text': '您的附件简历 示例候选人简历.pdf 已发送给Boss'}))
