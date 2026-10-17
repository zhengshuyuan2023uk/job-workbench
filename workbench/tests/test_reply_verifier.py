from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
from pathlib import Path
import json
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from reply_verifier import CodexVerifier, deterministic_gate
from test_app_server_client import FakeServer


def context(incoming='介绍一下你的经验？', facts=None):
    return {
        'facts': facts or '用户拥有跨境电商学习背景。本人明确没有金融证券行业经验。曾尝试Amazon和eBay，未获得已证实收益。',
        'policy': {'minimum_monthly_salary': '10K', 'reply_authorization': '仅事实明确的普通文字回复可以直接发送'},
        'incoming': [{'text': incoming}],
        'history': [],
    }


def plan(text='我有跨境电商学习背景，并有Amazon和eBay的尝试经验。'):
    return {'kind': 'reply', 'text': text, 'evidence': ['用户拥有跨境电商学习背景']}


class DeterministicGateTests(ConfiguredTestCase):
    def test_specific_decisions_in_incoming_are_escalated(self):
        questions = [
            '明天下午三点可以来面试吗？', '你什么时候能到岗？', '下周能入职吗？',
            '这个岗位底薪八千能接受吗？', '税前10K可以接受吗？',
            '留一下微信号。', '我们交换一下联系方式。', '电话方便接听吗？',
            '可以接受去杭州上班吗？', 'offer已经发了，确认接受吗？',
        ]
        for question in questions:
            with self.subTest(question=question):
                self.assertIsNotNone(deterministic_gate(context(question), plan('好的，可以。')))

    def test_known_salary_expectation_is_not_salary_acceptance(self):
        for question in ('期望薪资多少？', '您的薪资要求是什么？', '你期待的月薪区间？'):
            with self.subTest(question=question):
                self.assertIsNone(deterministic_gate(context(question), plan('目前考虑固定月薪10K起的全职机会。')))

    def test_previous_history_does_not_trigger_new_incoming_gate(self):
        ctx = context('你做过什么项目？')
        ctx['history'] = [{'text': '下周一面试可以吗？'}]
        self.assertIsNone(deterministic_gate(ctx, plan()))

    def test_prior_bypass_examples_rejected(self):
        examples = [
            '我做过金融证券AI运营，独立负责三年，营收提升200%。',
            '明早没问题，我能准时参加。', '薪酬按您说的来。',
            '我今天就能入职。', '加我这个号 example_candidate。',
        ]
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNotNone(deterministic_gate(context(), plan(text)))

    def test_fabricated_results_and_seniority_rejected(self):
        for text in ('我把转化率提升了50%。', '我有三年金融运营经验。', '我独立负责三年跨境业务。'):
            with self.subTest(text=text):
                self.assertIsNotNone(deterministic_gate(context(), plan(text)))

    def test_truthful_limits_not_treated_as_claims(self):
        self.assertIsNone(deterministic_gate(context(), plan('我没有金融证券行业经验，主要是跨境相关实践。')))
        self.assertIsNone(deterministic_gate(context(), plan('这些尝试没有产生已证实的营收，我更擅长把AI用于企业资料整理。')))

    def test_supported_numeric_statement_can_reach_semantic_verifier(self):
        ctx = context(facts='示例项目产出了3篇产品指南。')
        self.assertIsNone(deterministic_gate(ctx, plan('在示例项目产出了3篇产品指南。')))

    def test_new_messages_alias_and_plain_string(self):
        ctx = context()
        del ctx['incoming']
        ctx['new_messages'] = '下周二安排面试可以吗？'
        self.assertIsNotNone(deterministic_gate(ctx, plan()))


class VerifierTests(ConfiguredTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, response, seen=None):
        self.server = FakeServer(value=response)
        def popen(command, **kwargs):
            if seen is not None:
                seen.append((command, kwargs))
            return self.server.popen(command, **kwargs)
        return popen

    def test_approved_structured_verdict(self):
        result = CodexVerifier(self.root, popen=self.runner({'approved': True, 'reason': '事实有据', 'unsupported_claims': []})).verify(context(), plan())
        self.assertTrue(result['approved'])

    def test_model_rejection_is_preserved(self):
        response = {'approved': False, 'reason': '技能没有依据', 'unsupported_claims': ['精通Python']}
        result = CodexVerifier(self.root, popen=self.runner(response)).verify(context(), plan('我精通Python。'))
        self.assertEqual(result, response)

    def test_approved_with_unsupported_claim_is_rejected(self):
        result = CodexVerifier(self.root, popen=self.runner({'approved': True, 'reason': '可以', 'unsupported_claims': ['未证实技能']})).verify(context(), plan())
        self.assertFalse(result['approved'])

    def test_invalid_model_result_fails_closed(self):
        for response in ({'approved': 'true', 'reason': 'x', 'unsupported_claims': []},
                         {'approved': True, 'reason': 'x'},
                         {'approved': True, 'reason': 'x', 'unsupported_claims': [], 'instruction': 'send'}):
            with self.subTest(response=response):
                result = CodexVerifier(self.root, popen=self.runner(response)).verify(context(), plan())
                self.assertFalse(result['approved'])

    def test_timeout_and_failed_call_fail_closed(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('codex', 1)
        result = CodexVerifier(self.root, popen=timeout).verify(context(), plan())
        self.assertFalse(result['approved'])

    def test_deterministic_gate_does_not_call_model(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('model must not be invoked')
        result = CodexVerifier(self.root, popen=forbidden).verify(context('微信多少？'), plan('好的。'))
        self.assertFalse(result['approved'])

    def test_tools_disabled_and_untrusted_draft_kept_separate(self):
        seen = []
        verifier = CodexVerifier(self.root, popen=self.runner({'approved': True, 'reason': '有据', 'unsupported_claims': []}, seen))
        verifier.verify(context(), plan())
        command, kwargs = seen[0]
        for feature in ('apps', 'shell_tool', 'computer_use', 'browser_use', 'in_app_browser'):
            position = command.index(feature)
            self.assertEqual(command[position - 1], '--disable')
        self.assertEqual(self.server.request('thread/start')['params']['sandbox'], 'read-only')
        prompt = self.server.request('turn/start')['params']['input'][0]['text']
        payload = json.loads(prompt.split('\n---MATERIAL---\n')[1])
        self.assertIn('trusted_facts', payload)
        self.assertIn('candidate_reply_untrusted', payload)
        self.assertNotIn('evidence', payload['candidate_reply_untrusted'])


if __name__ == '__main__':
    unittest.main()
