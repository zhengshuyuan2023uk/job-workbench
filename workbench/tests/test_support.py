"""Synthetic identity and explicit dependency injection for offline tests."""
from unittest import TestCase
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import Config

SELF_UID = '999000001'
SELF_NAME = '示例候选人'
RECIPIENT = 'candidate@example.com'
TEST_CONFIG = Config(live_enabled=True, self_uid=SELF_UID, self_name=SELF_NAME,
                     attachment_name='示例候选人简历', notification_email=RECIPIENT,
                     notifications_enabled=True)


class ConfiguredTestCase(TestCase):
    def run(self, result=None):
        with patch('config._override', TEST_CONFIG):
            return super().run(result)
