from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from test_data import fixture, application, write_sources
from server import create_server
from automation_state import AutomationState


class HTTPTests(ConfiguredTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        data = fixture()
        data['applications']['applications'] = [application()]
        write_sources(cls.root, data)
        (cls.root / 'resume.md').write_text('测试简历 <script>alert(1)</script>')
        (cls.root / 'secret.txt').write_text('PRIVATE')
        cls.automation = AutomationState(cls.root)
        cls.server = create_server(cls.root, port=0)
        cls.port = cls.server.server_address[1]
        cls.origin = f'http://127.0.0.1:{cls.port}'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        result = (r.status, data, dict(r.getheaders()))
        c.close()
        return result

    def test_task_endpoint_is_local_token_protected_and_validated(self):
        self.assertEqual(self.call('GET','/api/tasks')[0],200)
        self.assertEqual(self.call('POST','/api/tasks',json.dumps({'action':'collect'}))[0],403)
        headers=self.automation_headers()
        self.assertEqual(self.call('POST','/api/tasks',json.dumps({'action':'collect','text':'injection'}),headers)[0],400)
        self.assertEqual(self.call('POST','/api/tasks',json.dumps({'action':'send'}),headers)[0],400)
        # Stop may only affect this task instance; never starts an external call.
        status,body,_=self.call('POST','/api/tasks',json.dumps({'action':'stop'}),headers)
        self.assertEqual(status,202)
        self.assertNotIn('row',body.decode())

    def test_wrong_host_rejected(self):
        self.assertEqual(self.call('GET', '/api/view', headers={'Host': 'evil.example'})[0], 403)

    def test_cross_origin_write_rejected(self):
        token = json.loads(self.call('GET', '/api/session')[1])['token']
        self.assertEqual(self.call('PUT', '/api/preferences/a', '{}', {
            'Origin': 'https://evil.example', 'Content-Type': 'application/json', 'X-Workbench-Token': token})[0], 403)

    def test_missing_token_rejected(self):
        self.assertEqual(self.call('PUT', '/api/preferences/a', '{}', {
            'Origin': self.origin, 'Content-Type': 'application/json'})[0], 403)

    def test_path_traversal_is_not_served(self):
        for path in ['/../secret.txt', '/static/../../secret.txt', '/api/documents/%2e%2e%2fsecret.txt']:
            self.assertEqual(self.call('GET', path)[0], 404)

    def test_backup_has_no_document_route(self):
        self.assertEqual(self.call('GET', '/api/documents/network-backup')[0], 404)

    def test_document_returned_as_json_text(self):
        status, body, headers = self.call('GET', '/api/documents/resume')
        self.assertEqual(status, 200)
        self.assertIn('application/json', headers['Content-Type'])
        self.assertIn('<script>', json.loads(body)['text'])

    def test_save_and_conflict_via_http(self):
        token = json.loads(self.call('GET', '/api/session')[1])['token']
        prefs = json.loads(self.call('GET', '/api/preferences')[1])
        headers = {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Workbench-Token': token}
        payload = json.dumps({'expected_revision': prefs['revision'], 'values': {'note': '已核对'}})
        self.assertEqual(self.call('PUT', '/api/preferences/a', payload, headers)[0], 200)
        self.assertEqual(self.call('PUT', '/api/preferences/a', payload, headers)[0], 409)

    def automation_headers(self):
        token = json.loads(self.call('GET', '/api/session')[1])['token']
        return {'Origin': self.origin, 'Content-Type': 'application/json', 'X-Workbench-Token': token}

    def test_automation_status_is_local_and_read_only(self):
        status, body, headers = self.call('GET', '/api/automation')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['mode'], 'shadow')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.call('GET', '/api/automation', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.call('GET', '/api/automation', headers={'Origin': 'https://evil.example'})[0], 403)

    def test_automation_control_rejects_cross_site_missing_token_and_wrong_type(self):
        headers = self.automation_headers()
        payload = json.dumps({'action': 'pause', 'expected_revision': 0})
        for changes in ({'Origin': 'https://evil.example'}, {'X-Workbench-Token': ''},
                        {'Content-Type': 'text/plain'}, {'Origin': ''}):
            self.assertEqual(self.call('PUT', '/api/automation/control', payload, {**headers, **changes})[0], 403)

    def test_all_legacy_automation_controls_are_disabled(self):
        headers = self.automation_headers()
        before = self.automation.status()
        for action in ('pause', 'resume', 'recheck'):
            status, body, _ = self.call('PUT', '/api/automation/control', json.dumps(
                {'action': action, 'expected_revision': before['revision']}), headers)
            self.assertEqual(status, 400)
        self.assertEqual(self.automation.status()['revision'], before['revision'])
        for invalid in ({'action': 'send', 'expected_revision': before['revision']},
                        {'action': 'pause', 'expected_revision': before['revision'], 'message': 'must not send'},
                        {'action': 'pause', 'expected_revision': True}):
            self.assertEqual(self.call('PUT', '/api/automation/control', json.dumps(invalid), headers)[0], 400)

    def test_automation_fault_cannot_be_cleared_by_legacy_controls(self):
        current = self.automation.update(status='blocked', issue={'code': '37', 'message': '请完成官方验证'})
        for action in ('resume', 'recheck'):
            self.assertEqual(self.call('PUT', '/api/automation/control', json.dumps(
                {'action': action, 'expected_revision': current['revision']}), self.automation_headers())[0], 400)
        self.assertEqual(self.automation.status()['issue']['code'], '37')

    def test_heartbeat_owner_reports_handoff_and_blocks_resident_controls(self):
        master=self.root/'applications.json'
        original=master.read_bytes()
        data=json.loads(original)
        data['reply_monitor'].update(execution_owner='heartbeat-boss',
            last_checked_at='2026-10-16T15:30:00+08:00',
            last_connection_issue={'status':'unresolved','message':'连接待复查'})
        master.write_text(json.dumps(data))
        try:
            current=self.automation.update(status='stopped',paused=True,issue=None,
                                           last_heartbeat_at='2020-01-01T00:00:00+00:00',recheck_requested_at=None)
            status,body,_=self.call('GET','/api/automation')
            self.assertEqual(status,200)
            result=json.loads(body)
            self.assertEqual(result['execution_owner'],'heartbeat-boss')
            self.assertFalse(result['controls_enabled'])
            self.assertEqual(result['heartbeat_last_check_at'],'2026-10-16T15:30:00+08:00')
            self.assertEqual(result['heartbeat_issue'],'连接待复查')
            for action in ('resume','recheck','pause'):
                with self.subTest(action=action):
                    status,body,_=self.call('PUT','/api/automation/control',json.dumps(
                        {'action':action,'expected_revision':current['revision']}),self.automation_headers())
                    self.assertEqual(status,400)
                    self.assertIn('常驻已停用',json.loads(body)['error'])
            self.assertEqual(self.automation.status()['revision'],current['revision'])
            self.assertTrue(self.automation.status()['paused'])
            self.assertIsNone(self.automation.status()['recheck_requested_at'])
        finally:
            master.write_bytes(original)

    def test_legacy_owner_never_reenables_resident_controls(self):
        master=self.root/'applications.json'
        original=master.read_bytes()
        data=json.loads(original)
        data['reply_monitor']['execution_owner']='resident-v1'
        master.write_text(json.dumps(data))
        try:
            current=self.automation.update(status='paused',paused=True,issue=None,last_heartbeat_at=None)
            result=json.loads(self.call('GET','/api/automation')[1])
            self.assertEqual(result['execution_owner'],'resident-v1')
            self.assertFalse(result['controls_enabled'])
            status,body,_=self.call('PUT','/api/automation/control',json.dumps(
                {'action':'resume','expected_revision':current['revision']}),self.automation_headers())
            self.assertEqual(status,400)
            self.assertTrue(self.automation.status()['paused'])
        finally:
            master.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
