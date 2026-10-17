from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import json
import queue
import threading
import unittest

from app_server_client import AppServerCancelled, AppServerError, AppServerPlanner, AppServerVerifier


FACTS = '我主要做跨境业务中的AI应用实践。'
CONTEXT = {'facts': FACTS, 'policy': {}, 'incoming': ['请介绍一下你的经验'], 'history': []}
PLAN = {'kind': 'reply', 'text': FACTS, 'reason': '回答实际经验', 'evidence': [FACTS],
        'notification': 'none', 'question_for_user': ''}


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()
        self.closed = False

    def readline(self, _limit):
        return self.lines.get()

    def close(self):
        self.closed = True
        self.lines.put('')


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, text):
        message = json.loads(text)
        self.process.sent.append(message)
        self.process.respond(message)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self.process.returncode = 0
        self.process.stdout.lines.put('')


class FakeServer:
    def __init__(self, value=None, scenario='success', stop_event=None):
        self.value = dict(PLAN) if value is None else value
        self.scenario = scenario
        self.stop_event = stop_event
        self.sent = []
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.returncode = None
        self.timer = None

    def popen(self, command, **kwargs):
        self.command = command
        self.kwargs = kwargs
        return self

    def emit(self, message):
        self.stdout.lines.put(json.dumps(message) + '\n')

    def respond(self, message):
        method = message.get('method')
        if method is None or method == 'initialized':
            return
        request_id = message['id']
        if self.scenario == 'malformed_protocol' and method == 'initialize':
            self.stdout.lines.put('{broken\n')
            return
        if self.scenario == 'rpc_error' and method == 'initialize':
            self.emit({'id': request_id, 'error': {'code': -1, 'message': 'SECRET-DO-NOT-LOG'}})
            return
        results = {
            'initialize': {'userAgent': 'test'},
            'account/read': {'account': None if self.scenario == 'signed_out' else {'type': 'chatgpt'}},
            'config/read': {'config': {'mcp_servers': {'boss': {}, 'odd.name': {}}}},
            'thread/start': {'thread': {'id': 'thread-1'}, 'sandbox': {'type': 'readOnly'}, 'approvalPolicy': 'never'},
            'turn/start': {'turn': {'id': 'turn-1', 'status': 'inProgress'}},
            'turn/interrupt': {},
        }
        if self.scenario == 'unsafe_sandbox':
            results['thread/start']['sandbox']['type'] = 'dangerFullAccess'
        self.emit({'id': request_id, 'result': results[method]})
        if method != 'turn/start':
            return
        if self.scenario in ('timeout', 'cancel', 'close'):
            if self.scenario in ('cancel', 'close'):
                callback = self.stop_event.set if self.scenario == 'cancel' else self.stop_callback
                self.timer = threading.Timer(0.03, callback)
                self.timer.start()
            return
        if self.scenario == 'eof':
            self.stdout.lines.put('')
            return
        params = {'threadId': 'thread-1', 'turnId': 'turn-1'}
        if self.scenario == 'approval':
            self.emit({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval', 'params': params})
            return
        if self.scenario == 'unknown_request':
            self.emit({'id': 'tool-1', 'method': 'item/tool/call', 'params': params})
            return
        if self.scenario == 'tool_event':
            self.emit({'method': 'item/started', 'params': dict(params, item={'id': 'tool-1', 'type': 'mcpToolCall'})})
            return
        if self.scenario != 'missing_answer':
            self.emit({'method': 'item/completed', 'params': dict(params, item={
                'id': 'commentary', 'type': 'agentMessage', 'phase': 'commentary', 'text': 'working',
            })})
            self.emit({'method': 'item/completed', 'params': dict(params, item={
                'id': 'answer', 'type': 'agentMessage', 'phase': 'final_answer',
                'text': '{broken' if self.scenario == 'malformed_answer' else json.dumps(self.value),
            })})
        self.emit({'method': 'turn/completed', 'params': {
            'threadId': 'thread-1', 'turn': {
                'id': 'turn-1', 'status': 'failed' if self.scenario == 'failed' else 'completed',
            },
        }})

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        return self.returncode

    def terminate(self):
        self.stdin.close()

    def kill(self):
        self.stdin.close()

    def request(self, method):
        return next(message for message in self.sent if message.get('method') == method)


class AppServerTests(ConfiguredTestCase):
    def planner(self, scenario='success', value=None, stop_event=None, cls=AppServerPlanner, **kwargs):
        server = FakeServer(value, scenario, stop_event)
        planner = cls('/not-used-for-model-cwd', stop_event=stop_event, popen=server.popen, **kwargs)
        self.addCleanup(planner.close)
        return planner, server

    def test_plan_uses_schema_isolated_thread_and_validates_evidence(self):
        planner, server = self.planner()
        result = planner.plan(CONTEXT)
        self.assertEqual(result['kind'], 'reply')
        self.assertGreaterEqual(result['planning_seconds'], 0)
        thread = server.request('thread/start')['params']
        self.assertTrue(thread['ephemeral'])
        self.assertEqual(thread['cwd'], '/tmp')
        self.assertEqual(thread['environments'], [])
        self.assertEqual(thread['dynamicTools'], [])
        self.assertFalse(thread['config']['mcp_servers']['boss']['enabled'])
        self.assertFalse(thread['config']['mcp_servers']['odd.name']['enabled'])
        self.assertEqual(thread['config']['notify'], [])
        self.assertFalse(thread['config']['features.plugins'])
        turn = server.request('turn/start')['params']
        self.assertIn('outputSchema', turn)
        self.assertEqual(turn['sandboxPolicy'], {'type': 'readOnly', 'networkAccess': False})
        self.assertIn('app-server', server.command)
        self.assertNotIn('--ignore-user-config', server.command)
        self.assertTrue(server.stdin.closed)

    def test_bad_fact_evidence_is_not_accepted(self):
        planner, _ = self.planner(value=dict(PLAN, evidence=['资料中不存在的经历信息']))
        with self.assertRaises(ValueError):
            planner.plan(CONTEXT)

    def test_bad_protocol_output_failure_or_incomplete_turn_never_returns_plan(self):
        for scenario in ('malformed_protocol', 'malformed_answer', 'failed', 'missing_answer', 'eof'):
            with self.subTest(scenario=scenario):
                planner, server = self.planner(scenario)
                with self.assertRaises(AppServerError):
                    planner.plan(CONTEXT)
                self.assertTrue(server.stdin.closed)

    def test_rpc_errors_do_not_leak_server_payload(self):
        planner, _ = self.planner('rpc_error')
        with self.assertRaises(AppServerError) as error:
            planner.plan(CONTEXT)
        self.assertNotIn('SECRET', str(error.exception))

    def test_signed_out_or_unsafe_sandbox_never_starts_turn(self):
        for scenario in ('signed_out', 'unsafe_sandbox'):
            with self.subTest(scenario=scenario):
                planner, server = self.planner(scenario)
                with self.assertRaises(AppServerError):
                    planner.plan(CONTEXT)
                self.assertFalse(any(message.get('method') == 'turn/start' for message in server.sent))

    def test_approval_is_cancelled_and_active_turn_interrupted(self):
        planner, server = self.planner('approval')
        with self.assertRaises(AppServerError):
            planner.plan(CONTEXT)
        response = next(message for message in server.sent if message.get('id') == 'approval-1')
        self.assertEqual(response['result'], {'decision': 'cancel'})
        self.assertEqual(server.request('turn/interrupt')['params'], {'threadId': 'thread-1', 'turnId': 'turn-1'})

    def test_unknown_server_request_is_rejected_and_tool_event_aborts(self):
        for scenario in ('unknown_request', 'tool_event'):
            with self.subTest(scenario=scenario):
                planner, server = self.planner(scenario)
                with self.assertRaises(AppServerError):
                    planner.plan(CONTEXT)
                server.request('turn/interrupt')
                if scenario == 'unknown_request':
                    response = next(message for message in server.sent if message.get('id') == 'tool-1')
                    self.assertEqual(response['error']['code'], -32601)

    def test_pre_cancel_starts_no_process(self):
        event = threading.Event()
        event.set()
        planner, server = self.planner(stop_event=event)
        with self.assertRaises(AppServerCancelled):
            planner.plan(CONTEXT)
        self.assertEqual(server.sent, [])

    def test_cancel_during_turn_interrupts_and_preserves_no_result(self):
        event = threading.Event()
        planner, server = self.planner('cancel', stop_event=event)
        with self.assertRaises(AppServerCancelled):
            planner.plan(CONTEXT)
        server.request('turn/interrupt')
        server.timer.join()
        self.assertTrue(server.stdin.closed)

    def test_timeout_interrupts_and_closes_process(self):
        planner, server = self.planner('timeout', timeout=0.05)
        with self.assertRaisesRegex(AppServerError, '超时'):
            planner.plan(CONTEXT)
        server.request('turn/interrupt')
        self.assertTrue(server.stdin.closed)

    def test_close_from_another_thread_cancels_inflight_work_and_is_idempotent(self):
        planner, server = self.planner('close')
        server.stop_callback = planner.close
        with self.assertRaises(AppServerCancelled):
            planner.plan(CONTEXT)
        server.timer.join()
        server.request('turn/interrupt')
        planner.close()
        with self.assertRaises(AppServerCancelled):
            planner.plan(CONTEXT)

    def test_each_call_gets_fresh_process_and_thread(self):
        servers = []
        def popen(command, **kwargs):
            server = FakeServer()
            servers.append(server)
            return server.popen(command, **kwargs)
        planner = AppServerPlanner('.', popen=popen)
        self.addCleanup(planner.close)
        planner.plan(CONTEXT)
        planner.plan(CONTEXT)
        self.assertEqual(len(servers), 2)
        for server in servers:
            server.request('thread/start')
            self.assertTrue(server.stdin.closed)

    def test_verifier_checks_commitment_before_starting_model(self):
        verifier, server = self.planner(cls=AppServerVerifier)
        result = verifier.verify(dict(CONTEXT, incoming=['明天可以面试吗？']), PLAN)
        self.assertFalse(result['approved'])
        self.assertEqual(server.sent, [])

    def test_verifier_receives_only_reply_not_planner_claimed_evidence(self):
        verifier, server = self.planner(cls=AppServerVerifier, value={
            'approved': True, 'reason': '事实相符', 'unsupported_claims': [],
        })
        result = verifier.verify(CONTEXT, dict(PLAN, reason='FAKE_PROOF_MARKER'))
        self.assertTrue(result['approved'])
        prompt = server.request('turn/start')['params']['input'][0]['text']
        self.assertNotIn('FAKE_PROOF_MARKER', prompt)
        self.assertIn('trusted_facts', prompt)

    def test_verifier_failure_or_unsupported_claims_never_approve(self):
        for scenario, value in [('failed', None), ('success', {
            'approved': True, 'reason': '问题', 'unsupported_claims': ['无依据'],
        }), ('success', {'approved': 'true'})]:
            with self.subTest(scenario=scenario, value=value):
                verifier, _ = self.planner(scenario, value=value, cls=AppServerVerifier)
                self.assertFalse(verifier.verify(CONTEXT, PLAN)['approved'])

    def test_verifier_cancel_is_propagated_instead_of_escalated(self):
        event = threading.Event()
        verifier, server = self.planner('cancel', stop_event=event, cls=AppServerVerifier)
        with self.assertRaises(AppServerCancelled):
            verifier.verify(CONTEXT, PLAN)
        server.timer.join()


if __name__ == '__main__':
    unittest.main()
