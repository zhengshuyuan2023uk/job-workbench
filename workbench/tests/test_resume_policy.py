from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Pure local recognizer boundary tests; never uses a browser or network."""
import unittest
from resume_policy import (request_mode, delivered_receipt, resume_key,
                           resume_send_attempt, superseding_request)


class ResumePolicyTests(ConfiguredTestCase):
    def test_only_standalone_text_requests(self):
        for text in ('请发一份简历','方便发下简历吗？','您好，麻烦您发一下简历，谢谢','简历发给我看看'):
            with self.subTest(text=text):
                self.assertEqual(request_mode({'body_type':1,'text':text}),'text')
        for text in ('简历呢','发简历，周二能面试吗？','请发简历到test@example.com','请发一份英文简历','请发简历和作品集'):
            with self.subTest(text=text):
                self.assertIsNone(request_mode({'body_type':1,'text':text}))

    def test_official_receipt_structure_and_order_required(self):
        request={'message_id':'200','timestamp':20}
        receipt={'message_id':'300','timestamp':30,'from_uid':'7','body_type':12,
                 'hyperLink':{'text':'您的附件简历 示例候选人简历 已发送给Boss点击查看附件'}}
        self.assertEqual(delivered_receipt([receipt],request),receipt)
        for altered in ({**receipt,'body_type':1},{**receipt,'message_id':'150'},
                        {**receipt,'timestamp':10}, {**receipt,'message_id':''},
                        {**receipt,'hyperLink':{'text':'示例候选人简历已发送'}}):
            self.assertIsNone(delivered_receipt([altered],request))

    def test_request_key_contains_identity_not_incoming_batch(self):
        row={'uid':'7','job_id':'job7','last_msg':'请发简历'}
        self.assertEqual(resume_key(row,'200'),resume_key({**row,'last_msg':'方便吗'},'200'))
        self.assertNotEqual(resume_key(row,'200'),resume_key({**row,'job_id':'other'},'200'))
        self.assertNotEqual(resume_key(row,'200'),resume_key(row,'201'))

    def test_send_attempt_requires_own_action_38_after_request(self):
        request={'message_id':'200','timestamp':20,'from_uid':'7'}
        attempt={'message_id':'300','timestamp':30,'from_uid':'self','body_type':4,'action':{'aid':38}}
        self.assertEqual(resume_send_attempt([attempt],request,'self'),attempt)
        string_aid={**attempt,'action':{'aid':'38'}}
        self.assertEqual(resume_send_attempt([string_aid],request,'self'),string_aid)
        for altered in ({**attempt,'from_uid':'7'},{**attempt,'body_type':1},
                        {**attempt,'action':{'aid':37}},{**attempt,'message_id':'150'},
                        {**attempt,'timestamp':10},{**attempt,'action':None},
                        {**attempt,'action':'38'}):
            with self.subTest(message=altered):
                self.assertIsNone(resume_send_attempt([altered],request,'self'))

    def test_request_changes_preserve_unrelated_questions(self):
        request={'message_id':'200','timestamp':20,'from_uid':'7'}
        message={'message_id':'300','timestamp':30,'from_uid':'7','body_type':1}
        for text in ('先不用发了','请发英文版简历','请把简历发到邮箱 test@example.com','简历和作品集一起发'):
            with self.subTest(text=text):
                changed={**message,'text':text}
                self.assertEqual(superseding_request([changed],request),changed)
        for text in ('你的英文能力如何？','你做过什么项目？','请发一份简历','简历里面有项目介绍吗？'):
            with self.subTest(text=text):
                self.assertIsNone(superseding_request([{**message,'text':text}],request))
        for altered in ({**message,'from_uid':'self','text':'先不用发了'},
                        {**message,'message_id':'150','text':'先不用发了'}):
            self.assertIsNone(superseding_request([altered],request))


if __name__=='__main__':
    unittest.main()
