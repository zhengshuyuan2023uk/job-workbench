from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Exercise the task card and request safeguards without calling BOSS."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


APP = Path(__file__).resolve().parents[1] / 'static' / 'app.js'


@unittest.skipUnless(shutil.which('node'), 'Node.js is optional and needed only for UI tests')
class TaskCardTests(ConfiguredTestCase):
    def run_ui(self, task=None, code='', error=None):
        script = r"""
const vm=require('node:vm'), fs=require('node:fs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Node {
  constructor(tag='',text='') {this.tag=tag;this.text=text;this.children=[];this.attrs={};}
  append(...children) {this.children.push(...children);}
  setAttribute(key,value) {this.attrs[key]=value;}
  addEventListener() {}
  get textContent() {return this.text+this.children.map(c=>c.textContent).join('');}
  set textContent(value) {this.text=value;this.children=[];}
}
const roots=new Map();
const context=vm.createContext({Node,Intl,URL,Date,Map,Set,Promise,input,
  document:{createElement:tag=>new Node(tag),createTextNode:text=>new Node('',text),
    querySelectorAll:()=>[],getElementById:id=>{if(!roots.has(id))roots.set(id,new Node());return roots.get(id);}},
  window:{addEventListener(){}},location:{hash:'#today'},fetch:()=>new Promise(()=>{}),setInterval:()=>{}});
vm.runInContext(input.source,context,{timeout:1000});
vm.runInContext(`state.task=input.task;state.taskError=input.error;state.busy=false;
  renderCurrentTask=()=>{};toast=()=>{};
  function describe(card) {
    const buttons=[],tags=[];
    function walk(node) {tags.push(node.tag);if(node.tag==='button')buttons.push({text:node.textContent,id:node.attrs.id,disabled:'disabled' in node.attrs});node.children.forEach(walk);}
    walk(card);return {text:card.textContent,buttons,tags,hidden:!!card.hidden};
  }`,context);
(async()=>{
  const output=await vm.runInContext(input.code || 'describe(currentTaskCard())',context,{timeout:1000});
  process.stdout.write(JSON.stringify(output));
})().catch(error=>{process.stderr.write(String(error.stack));process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-e', script],
            input=json.dumps({'source': APP.read_text(), 'task': task, 'error': error, 'code': code}),
            text=True, capture_output=True, timeout=5, check=True)
        return json.loads(result.stdout)

    def disabled(self, result):
        return {b['id']: b['disabled'] for b in result['buttons'] if b.get('id')}

    def test_idle_can_collect_but_cannot_followup_or_stop(self):
        result = self.run_ui({'status': 'idle', 'counts': {'ready': 0}})
        self.assertEqual(self.disabled(result), {'task-collect': False, 'task-followup': True, 'task-stop': True})
        self.assertIn('只读取会话', result['text'])

    def test_ready_actions_remain_available_after_stopping(self):
        result = self.run_ui({'status': 'stopped', 'counts': {'ready': 2}, 'items': [
            {'action_id': 'a', 'company': '待跟进公司', 'kind': 'reply', 'status': 'authorized_pending_send', 'text': '回复草稿'}]})
        self.assertFalse(self.disabled(result)['task-followup'])
        self.assertIn('待跟进公司', result['text'])
        self.assertIn('回复草稿', result['text'])

    def test_running_can_only_stop_and_stopping_disables_all(self):
        result = self.run_ui({'status': 'running', 'mode': 'followup', 'counts': {'ready': 2}})
        self.assertEqual(self.disabled(result), {'task-collect': True, 'task-followup': True, 'task-stop': False})
        result = self.run_ui({'status': 'stopping', 'counts': {'ready': 2}})
        self.assertTrue(all(self.disabled(result).values()))

    def test_read_error_blocks_new_work_and_keeps_previous_evidence(self):
        result = self.run_ui({'status': 'completed', 'counts': {'ready': 1}, 'items': [
            {'company': '结果未明公司', 'kind': 'uncertain', 'status': 'uncertain', 'reason': '尚未核验发送结果'}]}, error='无法读取本轮状态')
        self.assertTrue(self.disabled(result)['task-collect'])
        self.assertTrue(self.disabled(result)['task-followup'])
        self.assertIn('结果待核验', result['text'])
        self.assertIn('尚未核验发送结果', result['text'])

    def test_user_questions_and_untrusted_text_are_plain_text(self):
        result = self.run_ui({'status': 'failed', 'items': [
            {'company': '<script>alert(1)</script>', 'kind': 'escalate', 'status': 'awaiting_user',
             'question_for_user': '请确认可以面试的时间', 'text': '<img src=x onerror=alert(1)>'}]})
        self.assertIn('请确认可以面试的时间', result['text'])
        self.assertIn('<script>alert(1)</script>', result['text'])
        self.assertNotIn('script', result['tags'])
        self.assertNotIn('img', result['tags'])

    def test_new_owner_has_no_resident_controls(self):
        result = self.run_ui(code="state.automation={execution_owner:'workbench-app-server',status:'paused',revision:1};describe(automationCard())")
        self.assertTrue(result['hidden'])
        self.assertEqual(result['buttons'], [])

    def test_duplicate_start_is_blocked_and_post_uses_session_token(self):
        result = self.run_ui({'status': 'idle'}, code=r"""
          (async()=>{
            const calls=[];let resolvePost;
            fetch=async(path,options={})=>{
              calls.push({path,options});
              if(path==='/api/session')return {ok:true,json:async()=>({token:'test-token'})};
              if(options.method==='POST')return new Promise(resolve=>resolvePost=resolve);
              return {ok:true,json:async()=>({status:'running',run_id:'r',mode:'collect'})};
            };
            const first=controlTask('collect');
            await controlTask('collect');
            for(let i=0;i<6 && !resolvePost;i++)await Promise.resolve();
            resolvePost({ok:true,json:async()=>({status:'running',run_id:'r',mode:'collect'})});
            await first;
            return calls.filter(call=>call.options.method==='POST');
          })()
        """)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['path'], '/api/tasks')
        self.assertEqual(result[0]['options']['headers']['X-Workbench-Token'], 'test-token')
        self.assertEqual(json.loads(result[0]['options']['body']), {'action': 'collect'})

    def test_followup_is_not_sent_without_ready_items(self):
        result = self.run_ui({'status': 'completed', 'counts': {'ready': 0}}, code=r"""
          (async()=>{let requests=0;fetch=()=>{requests++;};await controlTask('followup');return requests;})()
        """)
        self.assertEqual(result, 0)

    def test_terminal_state_refreshes_local_records_once(self):
        result = self.run_ui({'status': 'completed', 'run_id': 'run-1', 'finished_at': '2026-10-16T11:00:00+08:00'}, code=r"""
          (async()=>{let refreshed=0;refresh=async()=>{refreshed++;};
            await refreshTaskRecords();await refreshTaskRecords();
            state.task.run_id='run-2';await refreshTaskRecords();return refreshed;})()
        """)
        self.assertEqual(result, 2)

    def test_failed_read_retains_last_known_task(self):
        result = self.run_ui({'status': 'running', 'run_id': 'run-1', 'items': [{'company': '保留记录'}]}, code=r"""
          (async()=>{state.taskLoading=false;fetch=async()=>{throw new Error('offline');};
            await refreshTasks();return {task:state.task,error:state.taskError};})()
        """)
        self.assertEqual(result['task']['status'], 'running')
        self.assertEqual(result['task']['items'][0]['company'], '保留记录')
        self.assertIn('无法读取', result['error'])


if __name__ == '__main__':
    unittest.main()
