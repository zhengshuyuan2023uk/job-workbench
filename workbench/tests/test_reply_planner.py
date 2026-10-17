from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import unittest
from reply_planner import validate_plan

class PlannerTests(ConfiguredTestCase):
    def plan(self, **kw):
        p=dict(kind='reply',text='我主要做跨境业务中的AI应用实践。',reason='回答经验',evidence=['主要做跨境业务中的AI应用实践'],notification='none',question_for_user='')
        p.update(kw); return p
    def test_grounded_reply(self):
        self.assertEqual(validate_plan(self.plan(),'主要做跨境业务中的AI应用实践',{})['kind'],'reply')
    def test_fabricated_evidence_blocked(self):
        with self.assertRaises(ValueError): validate_plan(self.plan(),'资料没有此事实',{})
    def test_missing_evidence_blocked(self):
        with self.assertRaises(ValueError): validate_plan(self.plan(evidence=[]),'主要做跨境业务中的AI应用实践',{})
    def test_commitment_blocked(self):
        with self.assertRaises(ValueError): validate_plan(self.plan(text='明天可以到岗。'),'主要做跨境业务中的AI应用实践',{})
    def test_escalation_always_notifies(self):
        self.assertEqual(validate_plan(self.plan(kind='escalate'),' ',{})['notification'],'needs_user_input')
