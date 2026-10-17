"""Render demo labels with the existing minimal DOM; never opens a browser."""
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_workbench_tasks_ui as task_ui


class DemoUITests(unittest.TestCase):
    def render(self, task, code=''):
        return task_ui.TaskCardTests().run_ui(task, code=code)

    def test_task_card_clearly_labels_local_simulation_and_history_count(self):
        result = self.render({'demo': True, 'status': 'completed', 'counts': {'ready': 3, 'changed': 6, 'sent': 0}})
        self.assertIn('仅在本地模拟', result['text'])
        self.assertIn('不发送任何真实内容', result['text'])
        self.assertIn('已读历史', result['text'])
        self.assertIn('已模拟跟进', result['text'])
        self.assertNotIn('已核验发送', result['text'])

    def test_completed_demo_item_never_claims_verified_delivery(self):
        result = self.render({'demo': True}, code="describe(taskItem({company:'虚构公司',kind:'reply',status:'simulated_sent',text:'示例回复'}))")
        self.assertIn('已模拟跟进', result['text'])
        self.assertIn('查看模拟内容', result['text'])
        self.assertNotIn('已发原文', result['text'])
        self.assertNotIn('已核验发送', result['text'])

    def test_demo_does_not_render_external_links(self):
        result = self.render({'demo': True}, code="safeLink('外部页面','https://example.com')")
        self.assertIsNone(result)

    def test_demo_notice_and_footer_remain_visible_outside_today(self):
        result = self.render({'demo': True}, code="renderModeNotice();({hidden:document.getElementById('demo-notice').hidden,footer:document.getElementById('workspace-footer').textContent})")
        self.assertFalse(result['hidden'])
        self.assertIn('全部虚构', result['footer'])
        self.assertIn('不会发送真实消息', result['footer'])


if __name__ == '__main__':
    unittest.main()
