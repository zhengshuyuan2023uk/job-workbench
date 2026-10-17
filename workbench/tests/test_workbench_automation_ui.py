from test_support import ConfiguredTestCase, SELF_UID, SELF_NAME, RECIPIENT
"""Render the actual status card in a minimal DOM; no browser or network access."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


APP = Path(__file__).resolve().parents[1] / 'static' / 'app.js'


@unittest.skipUnless(shutil.which('node'), 'Node.js is optional and needed only for UI tests')
class AutomationCardTests(ConfiguredTestCase):
    def render(self, state):
        script = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Node {
  constructor(tag='', text='') { this.tag=tag; this.text=text; this.children=[]; this.attrs={}; }
  append(...children) { this.children.push(...children); }
  setAttribute(key,value) { this.attrs[key]=value; }
  addEventListener() {}
  get textContent() { return this.text + this.children.map(c=>c.textContent).join(''); }
  set textContent(value) { this.text=value; this.children=[]; }
}
const roots=new Map();
const context=vm.createContext({Node, Intl, URL, Date, Map, Promise,
  document:{createElement:tag=>new Node(tag),createTextNode:text=>new Node('',text),
    querySelectorAll:()=>[],getElementById:id=>{if(!roots.has(id))roots.set(id,new Node());return roots.get(id);}},
  window:{addEventListener(){}},location:{hash:'#today'},
  fetch:()=>new Promise(()=>{}),setInterval:()=>{},input});
vm.runInContext(input.source,context,{timeout:1000});
const output=vm.runInContext(`
  state.automation=input.state;
  const card=automationCard();
  const buttons=[];
  function walk(node) { if(node.tag==='button')buttons.push({text:node.textContent,id:node.attrs.id,disabled:'disabled' in node.attrs}); node.children.forEach(walk); }
  walk(card);
  ({text:card.textContent,buttons});
`,context,{timeout:1000});
process.stdout.write(JSON.stringify(output));
"""
        result=subprocess.run([shutil.which('node'),'-e',script],
            input=json.dumps({'source':APP.read_text(),'state':state}),
            text=True,capture_output=True,timeout=5,check=True)
        return json.loads(result.stdout)

    def test_heartbeat_handoff_does_not_look_like_offline_resident_or_offer_controls(self):
        result=self.render({'revision':10,'execution_owner':'heartbeat-boss','controls_enabled':False,
            'status':'offline','mode':'live','paused':True,'heartbeat_last_check_at':None,
            'issue':{'message':'常驻旧故障'},'heartbeat_issue':'当前连接待复查'})
        self.assertIn('每30分钟检查 · 常驻已停用',result['text'])
        self.assertIn('当前连接待复查',result['text'])
        self.assertNotIn('后台离线',result['text'])
        self.assertNotIn('常驻旧故障',result['text'])
        self.assertNotIn('活跃对话优先处理',result['text'])
        self.assertEqual(result['buttons'],[])

    def test_resident_paused_still_offers_resume(self):
        result=self.render({'revision':1,'execution_owner':'resident-v1','controls_enabled':True,
                            'status':'paused','mode':'live','paused':True})
        self.assertEqual(result['buttons'],[{'text':'继续自动回复','id':'automation-resume','disabled':False}])

    def test_resident_fault_still_offers_read_only_recheck(self):
        result=self.render({'revision':1,'execution_owner':'resident-v1','controls_enabled':True,
                            'status':'blocked','mode':'live','paused':True,'issue':{'message':'请完成验证'}})
        self.assertEqual(result['buttons'],[{'text':'已处理验证，重新检查','id':'automation-recheck','disabled':False}])


if __name__=='__main__':
    unittest.main()
