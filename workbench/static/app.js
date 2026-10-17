'use strict';

const state = {view:null, prefs:{revision:0,jobs:{}}, token:null, page:'today', scope:'contacted',
  filters:{query:'',city:'',salary:'',direction:'',status:'',favorite:false}, selected:null,
  detailTab:'conversation', forms:new Map(), lastTrigger:null, error:null, prefError:null, busy:false,
  automation:null, automationError:null, automationBusy:false, automationRequest:0,
  task:null, taskError:null, taskActionError:null, taskBusy:false, taskLoading:false, taskRequest:0,
  taskLastCheck:0, taskRefreshed:null};
const $ = (id) => document.getElementById(id);
const labels = {today:'今日待办',jobs:'岗位管理',conversations:'沟通记录',learning:'案例复盘',documents:'我的资料'};
const contactLabels = {verified_sent:'已核验发送',user_contact_verified:'本人已联系',not_contacted:'本地未记录联系',authorized_pending_send:'首次联系待执行',pending:'结果待核验',dispatching:'结果待核验',uncertain:'结果待核验'};
const draftLabels = {superseded_by_authorized_version:'历史版本 · 已替代',superseded:'历史版本 · 已替代',resolved:'来信已回复 · 历史草稿',version_conflict:'草稿版本待核对',verified_sent:'已核验发送',draft_pending_user_review:'首次介绍草稿 · 未发送',pending_review:'后续回复 · 待审阅',authorized_pending_send:'已授权 · 待核验',notification_only:'提醒记录 · 无需审阅'};
const sourceLabels = {applications:'实际投递与沟通记录',guangzhou:'广州岗位依据',second:'第二轮岗位依据',cities:'三城岗位依据',reply_reviews:'回复分析',notification_events:'提醒事件'};
function isDemo() {return state.view?.meta.demo_mode===true || state.task?.demo===true;}
function renderModeNotice() {
  const notice=$('demo-notice');if(notice)notice.hidden=!isDemo();
  const footer=$('workspace-footer');if(footer)footer.textContent=isDemo()?
    '演示模式：公司、岗位、候选人和消息全部虚构。所有操作只在本地模拟；不会发送真实消息、附件或邮件。':
    '沟通记录保存在本机。收集并分析只读取和整理；一键跟进按当前授权处理。网页刷新本身不发送消息。';
}

function el(tag, attrs={}, ...children) {
  const node = document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2),value);
    else if (key === 'class') node.className=value;
    else if (key === 'checked') node.checked=value;
    else if (key === 'value') node.value=value;
    else node.setAttribute(key,value === true ? '' : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function button(text, callback, cls='button', attrs={}) { return el('button',{class:cls,type:'button',onclick:callback,...attrs},text); }
function badge(text,tone='') { return el('span',{class:`badge ${tone}`},text); }
function time(value, full=false) {
  if (!value) return '待核验';
  const date=new Date(value);
  if (Number.isNaN(date.getTime())) return '待核验';
  return new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',...(full?{year:'numeric'}:{})}).format(date);
}
function toast(text) {
  $('toast').textContent=text; $('toast').hidden=false;
  clearTimeout(toast.timer); toast.timer=setTimeout(()=>$('toast').hidden=true,4000);
}
async function request(path,options={}) {
  const response=await fetch(path,{cache:'no-store',...options});
  let body; try {body=await response.json();} catch {throw new Error('服务返回异常，请重新启动工作台。');}
  if(!response.ok) {const e=new Error(body.error || '读取失败');e.status=response.status;throw e;}
  return body;
}
async function copyText(text) {
  try {await navigator.clipboard.writeText(text);toast('已复制');}
  catch {showText('复制内容',text);toast('无法直接复制，请在打开的内容中选择文字复制。');}
}
function showText(title,text) {
  $('document-title').textContent=title; $('document-body').textContent=text;
  if(!$('document-dialog').open) $('document-dialog').showModal();
}
function pref(jid) {return state.prefs.jobs[jid] || {favorite:false,priority:'普通',note:''};}
function jobById(jid) {return state.view?.jobs.find(j=>j.job_id===jid);}
function readBadge(job) {
  if(isDemo())return badge('演示会话');
  if(job.contact_status === 'user_contact_verified') return badge(job.identity_verified?'本人已联系':'本人联系待核验');
  if(job.contact_status !== 'verified_sent') return badge(contactLabels[job.contact_status] || '状态待核验','amber');
  return badge(({read:'已读',unread:'未读',unknown:'阅读状态待核验'})[job.read_status],job.read_status==='read'?'green':'');
}
function safeLink(label,url,cls='button') {
  if(isDemo())return null;
  try {const parsed=new URL(url);if(!['http:','https:'].includes(parsed.protocol))return null;}
  catch{return null;}
  return el('a',{href:url,target:'_blank',rel:'noopener noreferrer',class:cls},label);
}
function setPage(page) {
  if(!labels[page]) page='today';
  state.page=page;history.replaceState(null,'',`#${page}`);render();
}
function heading(title,subtitle) {
  const now=new Date();
  return el('div',{class:'page-heading'},el('div',{},el('span',{class:'small-caps'},'行进 / 求职工作台'),el('h1',{},title),el('p',{},subtitle)),
    el('div',{class:'date-stamp'},new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',weekday:'long'}).format(now),
       el('strong',{},new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(now))));
}
function panelHead(title,right) {return el('div',{class:'panel-head'},el('h2',{},title),right);}
function star(job) {
  return button(pref(job.job_id).favorite?'★':'☆',async e=>{
    e.stopPropagation();
    try {await savePreference(job.job_id,{favorite:!pref(job.job_id).favorite},state.prefs.revision);render();}
    catch(error){toast(error.message);}
  },`icon-button ${pref(job.job_id).favorite?'favorited':''}`,{'aria-label':`${pref(job.job_id).favorite?'取消收藏':'收藏'} ${job.company || job.title}`});
}
function jobTable(jobs) {
  if(!jobs.length)return el('div',{class:'empty'},'没有符合当前条件的岗位。可以清除筛选，或切换到其他记录范围。');
  return el('table',{class:'job-table'},el('thead',{},el('tr',{},...['岗位 / 公司','城市 · 薪资','沟通进展','收藏'].map(t=>el('th',{scope:'col'},t)))),
    el('tbody',{},...jobs.map(j=>el('tr',{},
      el('td',{},button(j.title || '职位名称待核验',e=>openJob(j.job_id,e.currentTarget),'job-link'),el('div',{class:'company-name'},j.company || '公司待核验'),pref(j.job_id).priority==='高'?el('span',{class:'tag'},'个人优先'):null),
      el('td',{},el('div',{},j.city || '城市待核验'),el('div',{class:'company-name'},j.salary_text || '薪资待核验')),
      el('td',{},readBadge(j),j.observed_at?el('div',{class:'company-name'},time(j.observed_at)):null),
      el('td',{},star(j))))));
}
function taskCard(t) {
  const j=jobById(t.job_id);
  return el('div',{class:'task-card'},badge(t.kind==='reply'?'待你审阅':t.kind==='user_input'?'需要你补充':t.kind==='incoming' || t.kind==='uncertain_send'?'代理跟进':'需要核对','amber'),el('h3',{},t.title),
    el('p',{},j?`${j.company} · ${j.title}`:'运行记录'),el('p',{},t.detail),
    t.draft_text?el('div',{class:'draft-card prose'},t.draft_text):null,
    j?button('查看沟通',e=>openJob(j.job_id,e.currentTarget)):null,
    t.kind==='reply'?button('复制审阅提示',()=>copyText(reviewText(j,t)),'button small'):null);
}
function reviewText(job,draft) {
  return `请审阅以下回复，尚未发送给招聘方。\n公司：${job?.company || '待核验'}\n岗位：${job?.title || '待核验'}\n岗位 ID：${job?.job_id || '待核验'}\n对应来信：${draft.incoming_message_ids?.join('、') || '待核验'}\n草稿版本：${draft.draft_version || draft.version || '待核验'}\n\n${draft.draft_text || draft.text || ''}\n\n请在原 Codex 对话中确认或修改；复制不代表批准发送。`;
}
function automationText(value) {
  if(typeof value==='string')return value;
  if(!value || typeof value!=='object')return '';
  return value.message || value.summary || value.reason || value.action || '';
}
function automationCard() {
  const a=state.automation;
  const statuses={starting:'正在启动',running:'运行中',paused:'已暂停',pausing:'正在暂停',blocked:'需要处理',offline:'后台离线',stopped:'已停止',unknown:'状态待核验',error:'运行异常'};
  const current=state.automationError?'unknown':a?.status || 'unknown';
  const tones={running:'green',blocked:'amber',offline:'amber',unknown:'amber',error:'amber'};
  const card=el('section',{class:'panel automation-panel',id:'automation-card','aria-labelledby':'automation-title'});
  if(a?.execution_owner==='workbench-app-server') {
    card.hidden=true;
    return card;
  }
  if(a?.execution_owner==='heartbeat-boss') {
    card.append(el('div',{class:'automation-heading'},el('div',{},el('h2',{id:'automation-title'},'回复跟进'),
      el('p',{},'每30分钟检查 · 常驻已停用')),badge('定时任务接管')),
      el('div',{class:'automation-details'},
        el('div',{},el('span',{},'最近成功检查'),el('strong',{},a.heartbeat_last_check_at?time(a.heartbeat_last_check_at):'尚未完成')),
        el('div',{},el('span',{},'检查频率'),el('strong',{},'每30分钟'))));
    if(state.automationError || a.heartbeat_issue)card.append(el('div',{class:'alert automation-issue',role:'status'},
      state.automationError || `连接记录待核验：${a.heartbeat_issue}`));
    card.append(el('p',{class:'muted'},'由原对话的定时任务继续跟进；需要调整或暂停任务，请回到原对话。此页面不会启动常驻程序。'));
    return card;
  }
  const modes={shadow:'观察模式 · 不发送',live:'自动回复',active:'活跃对话',idle:'等待新消息'};
  card.append(el('div',{class:'automation-heading'},el('div',{},el('h2',{id:'automation-title'},'常驻回复'),
    el('p',{},'事实明确的普通回复和现有简历请求自动处理；需要你决定的事项集中提醒。')),
    el('div',{class:'automation-badges'},badge(statuses[current] || '状态待核验',tones[current] || ''),a?.mode?badge(modes[a.mode] || '模式待核验'):null)));
  const phases={planning:'正在整理回复',dispatching:'正在发送并核验',reconciling:'正在核验上次结果',reading:'正在读取会话',notifying:'正在处理提醒'};
  const busy=typeof a?.in_flight==='object' && a.in_flight?
    [a.in_flight.company,phases[a.in_flight.phase] || '正在处理'].filter(Boolean).join(' · '):automationText(a?.in_flight);
  card.append(el('div',{class:'automation-details'},
    el('div',{},el('span',{},'最近检查'),el('strong',{},a?.last_check_at?time(a.last_check_at):'尚未完成')),
    el('div',{},el('span',{},'下次检查'),el('strong',{},current==='paused' || current==='blocked'?'等待继续':a?.next_check_at?time(a.next_check_at):'待安排')),
    el('div',{},el('span',{},'当前处理'),el('strong',{},busy || (a?.in_flight?'正在核对会话':current==='running'?'等待新消息':'—'))),
    el('div',{},el('span',{},'待处理事件'),el('strong',{},Number.isInteger(a?.pending_count)?String(a.pending_count):'—'))));
  if(a?.issue || state.automationError)card.append(el('div',{class:'alert automation-issue',role:'status'},
    state.automationError || automationText(a.issue) || '后台遇到异常，已停止发送，请检查连接状态。'));
  const enabled=a && a.controls_enabled!==false && Number.isInteger(a.revision) && !state.automationBusy && !state.automationError;
  const actions=el('div',{class:'automation-actions'},el('span',{class:'muted'},
    current==='pausing'?'正在结束当前核验，之后不再处理新消息。':current==='offline'?'后台未报告在线状态；页面打开不代表自动回复正在运行。':
      a?.mode==='shadow'?'正在验证消息变化与处理流程，暂不向招聘方发送。':'活跃对话优先处理，实际速度取决于连接与核验结果。'));
  if(current==='blocked')actions.append(button('已处理验证，重新检查',()=>controlAutomation('recheck'),'button primary',
    {id:'automation-recheck',disabled:!enabled}));
  else if(current==='pausing')actions.append(button('暂停中…',()=>{},'button',
    {id:'automation-pausing',disabled:true}));
  else if(a?.paused || current==='paused')actions.append(button('继续自动回复',()=>controlAutomation('resume'),'button primary',
    {id:'automation-resume',disabled:!enabled || current==='offline' || current==='stopped'}));
  else actions.append(button('暂停自动回复',()=>controlAutomation('pause'),'button',
    {id:'automation-pause',disabled:!enabled}));
  card.append(actions);
  return card;
}
function renderAutomation() {
  const old=$('automation-card');if(!old)return;
  const focus=old.contains(document.activeElement)?document.activeElement.id:null;
  old.replaceWith(automationCard());
  if(focus && $(focus) && !$(focus).disabled)$(focus).focus({preventScroll:true});
}
async function refreshAutomation() {
  if(state.automationBusy)return;
  const requestId=++state.automationRequest;
  try {
    const value=await request('/api/automation');
    if(requestId!==state.automationRequest)return;
    state.automation=value;state.automationError=null;
  } catch(error) {
    if(requestId!==state.automationRequest)return;
    state.automationError='无法读取后台状态，请检查本机工作台服务。';
  }
  renderAutomation();
}
async function controlAutomation(action) {
  if(state.automationBusy || !state.automation || state.automation.execution_owner==='workbench-app-server')return;
  state.automationBusy=true;++state.automationRequest;renderAutomation();
  try {
    if(!state.token)state.token=(await request('/api/session')).token;
    // The worker updates its heartbeat independently; use the latest revision
    // for the requested action and let the server reject any intervening change.
    state.automation=await request('/api/automation');
    state.automation=await request('/api/automation/control',{method:'PUT',
      headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},
      body:JSON.stringify({action,expected_revision:state.automation.revision})});
    state.automationError=null;
    toast(action==='pause'?'暂停请求已保存':action==='resume'?'继续请求已保存':'重新检查请求已保存，期间保持暂停');
  } catch(error) {toast(error.message);}
  finally {state.automationBusy=false;await refreshAutomation();}
}
function taskRunning(task=state.task) {return ['running','stopping'].includes(task?.status);}
function taskCount(key) {return Math.max(0,Number(state.task?.counts?.[key]) || 0);}
function taskItem(item) {
  const kinds={reply:isDemo()?'模拟回复':'回复',resume:isDemo()?'模拟简历附件':'发送现有简历',escalate:'需要你决定',ignore:'无需处理',uncertain:'结果待核验'};
  const statuses={ready:'可跟进',pending:'待处理',authorized_pending_send:'可跟进',running:'处理中',dispatching:'发送并核验中',
    sent:'已核验发送',verified_sent:'已核验发送',observed_sent:'已观察到发送',needs_user:'需要你决定',awaiting_user:'需要你决定',escalated:'需要你决定',skipped:'已跳过',
    uncertain:'结果待核验',failed:'处理未完成',stopped:'已停止',ignored:'无需处理',superseded:'已由新消息替代',simulated_sent:'已模拟跟进'};
  const needsUser=item.kind==='escalate' || ['needs_user','awaiting_user','escalated'].includes(item.status);
  const uncertain=item.kind==='uncertain' || item.status==='uncertain';
  const sent=['sent','verified_sent','simulated_sent'].includes(item.status);
  return el('article',{class:'run-item'},
    el('div',{class:'run-item-heading'},el('div',{},el('h3',{},item.company || '公司待核验'),
      item.title?el('p',{class:'muted'},item.title):null),
      badge(statuses[item.status] || kinds[item.kind] || '状态待核验',sent?'green':needsUser || uncertain?'amber':'')),
    el('p',{class:'run-item-kind'},kinds[item.kind] || '待核对事项'),
    item.reason?el('p',{class:'run-item-reason'},item.reason):null,
    item.question_for_user?el('div',{class:'run-question'},item.question_for_user):null,
    item.text?el('details',{class:'run-draft','data-action-id':item.action_id || item.uid || ''},el('summary',{},isDemo()?'查看模拟内容':sent?'查看已发原文':'查看回复内容'),el('div',{class:'prose'},item.text)):null);
}
function currentTaskCard() {
  const task=state.task,active=taskRunning(),stopping=task?.status==='stopping';
  const statuses={idle:'等待开始',running:task?.mode==='followup'?'正在跟进':'正在收集分析',stopping:'正在停止',completed:'本轮已完成',failed:'本轮未完成',stopped:'本轮已停止'};
  const phases={starting:'正在准备',connecting:'正在连接',reading:'正在读取会话',collecting:'正在收集会话',
    analyzing:'正在分析变化',planning:'正在整理可跟进事项',dispatching:'正在发送并核验',sending:'正在发送并核验',
    verifying:'正在核验结果',reconciling:'正在核验上次结果',stopping:'正在结束当前处理',completed:'本轮已完成'};
  const ready=taskCount('ready');
  const canStart=Boolean(task) && !active && !state.taskBusy && !state.taskError;
  const card=el('section',{class:'panel run-panel',id:'current-task-card','aria-labelledby':'current-task-title'},
    el('div',{class:'run-heading'},el('div',{},el('h2',{id:'current-task-title'},'本轮求职跟进'),
      el('p',{class:'muted'},isDemo()?'从虚构会话中整理下一步。一键跟进仅在本地模拟回复和简历操作，不发送任何真实内容。':'收集并分析只读取会话、整理变化；一键跟进发送当前已授权的回复和现有简历。')),
      badge(state.taskError?'状态待核验':statuses[task?.status] || '正在读取状态',state.taskError || ['failed','stopped'].includes(task?.status)?'amber':active || task?.status==='completed'?'green':'')),
    el('div',{class:'run-controls'},
      button(state.taskBusy==='collect'?'正在提交…':'收集并分析',()=>controlTask('collect'),'button primary',
        {id:'task-collect',disabled:!canStart,'aria-describedby':'task-action-hint'}),
      button(state.taskBusy==='followup'?'正在提交…':'一键跟进',()=>controlTask('followup'),'button',
        {id:'task-followup',disabled:!canStart || ready===0,'aria-describedby':'task-action-hint'}),
      button(stopping || state.taskBusy==='stop'?'正在停止…':'停止本轮',()=>controlTask('stop'),'button quiet',
        {id:'task-stop',disabled:!active || stopping || Boolean(state.taskBusy),'aria-describedby':'task-action-hint'})),
    el('p',{class:'run-hint',id:'task-action-hint'},stopping?(isDemo()?'正在停止剩余模拟步骤；已完成的步骤保留。':'停止后不再处理新事项；已发出的内容仍需核验，结果不明的事项会保留。'):
      active?'本轮正在处理，完成前不能重复启动。可停止后续处理。':ready>0?`当前有 ${ready} 项可跟进；需要你决定和结果不明的事项会保留。`:'先收集并分析，有可跟进事项后可一键处理。'));
  const progress=task?.message || phases[task?.phase] || (task?.status==='idle'?'尚未开始本轮。':'');
  if(progress)card.append(el('p',{class:'run-progress',role:'status','aria-live':'polite'},progress));
  if(task?.started_at)card.append(el('p',{class:'run-time'},`${task.mode==='followup'?'跟进':'收集分析'} · 开始于 ${time(task.started_at)}${task.finished_at?' · 结束于 '+time(task.finished_at):''}`));
  if(task?.counts)card.append(el('div',{class:'run-counts','aria-label':'本轮处理数量'},
    ...[['conversations','已检查'],['changed','已读历史'],['ready','可跟进'],['sent',isDemo()?'已模拟跟进':'已核验发送'],['needs_user','需你决定'],['uncertain','待核验']]
      .map(([key,label])=>el('div',{},el('strong',{},taskCount(key)),el('span',{},label)))));
  const error=state.taskError || state.taskActionError || task?.error;
  if(error)card.append(el('div',{class:'alert error run-error',role:'alert'},error,
    state.taskError?button('重新读取状态',()=>refreshTasks(),'button small'):null));
  const items=Array.isArray(task?.items)?task.items:[];
  if(items.length)card.append(el('div',{class:'run-items'},
    el('div',{class:'run-items-heading'},el('h3',{},'分析与处理结果'),el('span',{class:'muted'},`${items.length} 项${taskCount('skipped')?' · '+taskCount('skipped')+' 项已跳过':''}`)),
    el('div',{class:'run-item-list'},...items.map(taskItem))));
  return card;
}
function renderCurrentTask() {
  renderModeNotice();
  const old=$('current-task-card');if(!old)return;
  const focus=old.contains(document.activeElement)?document.activeElement.id:null;
  const scroll=old.querySelector('.run-item-list')?.scrollTop || 0;
  const expanded=new Set([...old.querySelectorAll('.run-draft[open]')].map(node=>node.dataset.actionId));
  const card=currentTaskCard();old.replaceWith(card);
  const list=card.querySelector('.run-item-list');if(list)list.scrollTop=scroll;
  card.querySelectorAll('.run-draft').forEach(node=>node.open=expanded.has(node.dataset.actionId));
  if(focus && $(focus) && !$(focus).disabled)$(focus).focus({preventScroll:true});
}
async function refreshTaskRecords() {
  const task=state.task;
  if(!task?.run_id || !['completed','failed','stopped'].includes(task.status))return;
  const key=`${task.run_id}:${task.status}:${task.finished_at || ''}`;
  if(state.taskRefreshed===key || state.busy)return;
  state.taskRefreshed=key;
  await refresh();
}
async function refreshTasks() {
  if(state.taskBusy || state.taskLoading)return;
  state.taskLoading=true;const requestId=++state.taskRequest;
  try {
    const task=await request('/api/tasks');
    if(requestId!==state.taskRequest)return;
    state.task=task;state.taskError=null;
  } catch(error) {
    if(requestId!==state.taskRequest)return;
    state.taskError='无法读取本轮状态。请检查本机工作台服务，恢复后再开始新任务。';
  } finally {
    state.taskLoading=false;state.taskLastCheck=Date.now();renderCurrentTask();
  }
  await refreshTaskRecords();
}
async function controlTask(action) {
  if(state.taskBusy || !state.task || !['collect','followup','stop'].includes(action))return;
  if(action==='stop'?(!taskRunning() || state.task.status==='stopping'):(taskRunning() || state.taskError || (action==='followup' && taskCount('ready')===0)))return;
  state.taskBusy=action;state.taskActionError=null;++state.taskRequest;renderCurrentTask();
  try {
    if(!state.token)state.token=(await request('/api/session')).token;
    state.task=await request('/api/tasks',{method:'POST',
      headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify({action})});
    state.taskError=null;
    toast(action==='stop'?'已请求停止后续处理':action==='collect'?'已开始收集并分析':'已开始跟进');
  } catch(error) {
    state.taskActionError=error.status?error.message:'请求结果尚未确认，正在重新读取任务状态；请勿重复点击。';
  } finally {
    state.taskBusy=false;renderCurrentTask();await refreshTasks();
  }
}
function todayPage() {
  const v=state.view,s=v.summary,c=v.constraints;
  const contacts=v.jobs.filter(j=>j.contact_status==='verified_sent' || (j.contact_status==='user_contact_verified' && j.identity_verified));
  const tasks=el('section',{class:'panel todo-panel'},panelHead('待办与跟进',badge(`${v.tasks.length} 项`,v.tasks.length?'amber':'')));
  if(v.tasks.length) tasks.append(el('div',{class:'task-list'},...v.tasks.map(taskCard)));
  else tasks.append(el('div',{class:'empty-todo'},el('div',{class:'empty-mark','aria-hidden':'true'},'✓'),el('div',{},el('h3',{},'当前没有需要你决定的事项'),
    el('p',{},`截至 ${time(v.meta.boss_checked_at)}，记录中没有待你处理的事项。需要补充事实或决定的内容会集中在这里。`))));
  tasks.append(el('div',{class:'panel-bottom'},el('span',{class:'dot'}),`${isDemo()?'本地演示记录':'最近 BOSS 核验'} · ${time(v.meta.boss_checked_at)}${v.meta.stale?' · 记录较旧':''}`));
  const conditions=el('section',{class:'panel conditions'},el('h2',{},'你的求职坐标'),el('strong',{class:'salary'},
    `${c.minimum_monthly_cny?c.minimum_monthly_cny/1000:'—'}K`,el('small',{},' / 月起 · 上限不限')),
    el('p',{class:'muted'},isDemo()?'示例方向：AI 应用、产品与内容运营':'以自己的事实资料和偏好为准'),el('div',{class:'city-order'},...(c.city_priority || []).map(x=>el('span',{},x))),
    el('div',{class:'condition-foot'},`排除 ${(c.excluded_districts || []).join('、') || '未设置'} · 届别不单独淘汰`));
  const statItems=isDemo()?[
    ['模拟跟进',taskCount('sent'),'项','只在本地生成演示记录'],['虚构岗位',v.jobs.length,'个岗位','5 家虚构公司 · 6 段示例会话'],
    ['真实外部操作',0,'次','不访问招聘平台、模型或邮箱'],['需本人决定',taskCount('needs_user'),'项','示例：面试时间需要确认']
  ]:[['已核验发送',s.verified_sent,'个岗位','首次介绍或回复已核验'],['回复监控中',s.monitored,'个岗位',`${s.read} 已读 · ${s.unread} 未读${s.read_unknown?' · '+s.read_unknown+' 待核验':''}`],
    ['当日账号联系',`≥${s.daily_contacts.confirmed}`,'条',`含此前账号联系 · 每日上限 ${s.daily_contacts.cap || '未设置'}`],['需你决定的回复',s.pending_replies,'条','缺失事实、时间或条件承诺']];
  const stats=el('section',{class:'stats','aria-label':'求职进度'},...statItems.map(([label,n,unit,note])=>el('div',{class:'stat'},el('span',{class:'stat-label'},label),el('div',{class:'stat-number'},n,el('small',{},unit)),el('span',{class:'stat-note'},note))));
  const trajectory=el('section',{class:'panel trajectory'},panelHead('沟通轨迹',el('div',{class:'legend'},el('span',{},el('i',{class:'green'}),'已读'),el('span',{},el('i',{}),'未读 / 待核验'))),
    el('div',{class:'trajectory-list'},...contacts.map(j=>button([el('div',{class:'trajectory-line'}),el('span',{},j.company || '公司待核验')],e=>openJob(j.job_id,e.currentTarget),`trajectory-item ${j.read_status}`,{title:`${j.company} · ${j.title}`}))));
  const recent=[...contacts].sort((a,b)=>(b.contacted_at || b.sent_at || '').localeCompare(a.contacted_at || a.sent_at || '')).slice(0,6);
  const latest=el('section',{class:'panel'},panelHead('最近联系',button('查看全部 →',()=>setPage('jobs'),'button small quiet')),jobTable(recent));
  return [heading('今天，从这里继续。','把岗位、沟通和下一步放在一起。'),currentTaskCard(),automationCard(),el('div',{class:'overview-grid'},tasks,conditions),stats,trajectory,latest];
}
function selectFilter(id,label,options,key) {
  const input=el('select',{id,'aria-label':label,onchange:e=>{state.filters[key]=e.target.value;render();}},
    el('option',{value:''},label),...options.map(([v,t])=>el('option',{value:v},t)));
  input.value=state.filters[key];return input;
}
function filters(jobs,includeStatus=true) {
  const f=state.filters;
  const cities=[...new Set(jobs.map(j=>j.city).filter(Boolean))].sort();
  const directions=[...new Set(jobs.map(j=>j.direction).filter(Boolean))].sort();
  return el('div',{class:'filters'},el('input',{id:'job-search',type:'search',placeholder:'搜索公司或岗位',value:f.query,'aria-label':'搜索公司或岗位',oninput:e=>{f.query=e.target.value;render();}}),
    selectFilter('filter-city','全部城市',cities.map(x=>[x,x]),'city'),selectFilter('filter-salary','全部展示薪资',[['10000','月薪下限 ≥ 10K'],['15000','月薪下限 ≥ 15K'],['20000','月薪下限 ≥ 20K'],['unknown','月薪待核验']],'salary'),
    selectFilter('filter-direction','全部方向',directions.map(x=>[x,x]).concat([['unknown','方向未标注']]),'direction'),
    includeStatus?selectFilter('filter-status','全部进展',[['read','已读'],['unread','未读'],['unknown','阅读状态待核验'],['not_contacted','本地未记录联系']],'status'):null,
    el('label',{},el('input',{id:'filter-favorite',type:'checkbox',checked:f.favorite,onchange:e=>{f.favorite=e.target.checked;render();}}),'仅收藏'),
    button('清除',()=>{state.filters={query:'',city:'',salary:'',direction:'',status:'',favorite:false};render();},'button small quiet'));
}
function filtered(jobs) {
  const f=state.filters,q=f.query.trim().toLocaleLowerCase();
  return jobs.filter(j=>(!q || `${j.company || ''} ${j.title || ''}`.toLocaleLowerCase().includes(q))&&
    (!f.city || j.city===f.city)&&(!f.direction || (f.direction==='unknown'?!j.direction:j.direction===f.direction))&&
    (!f.salary || (f.salary==='unknown'?j.salary_monthly_min===null:j.salary_monthly_min!==null && j.salary_monthly_min>=Number(f.salary)))&&
    (!f.status || (f.status==='not_contacted'?j.contact_status==='not_contacted':j.read_status===f.status && j.scope==='contacted'))&&
    (!f.favorite || pref(j.job_id).favorite));
}
function jobsPage(conversations=false) {
  const all=state.view.jobs;
  const scope=conversations?'contacted':state.scope;
  const scoped=all.filter(j=>j.scope===scope);
  const tabs=el('div',{class:'segmented'},...['contacted','candidate','research'].map(s=>button(
    `${({contacted:'已联系',candidate:'筛选记录',research:'历史搜索'})[s]} ${all.filter(j=>j.scope===s).length}`,()=>{state.scope=s;render();},s===scope?'active':'')));
  return [heading(conversations?'每一次沟通，都有来处。':'找到值得投入的机会。',conversations?'原话、核验记录与回复草稿，在同一条时间线上。':'筛选记录包含冲刺和条件待确认岗位，具体判断见详情。'),
    conversations?null:tabs,filters(scoped),el('div',{class:'list-summary'},`${filtered(scoped).length} 个岗位`,conversations?'点击岗位查看实际沟通':'薪资为岗位展示范围，固定部分以进一步核实为准'),
    el('section',{class:'panel'},jobTable(filtered(scoped)))];
}
function learningPage() {
  const contacts=state.view.jobs.filter(j=>j.scope==='contacted');
  return [heading('让下一次表达更有依据。','保留原话、真实反馈和迭代假设；已读不等于话术有效。'),
    el('div',{class:'alert'},isDemo()?'以下为虚构案例，展示事实、承诺和未知结果的处理边界，不代表真实求职成果。':'当前案例仍在积累。项目表达可以优化，效果判断要等真实的招聘方反馈。'),filters(contacts,false),
    el('div',{class:'case-grid'},...filtered(contacts).map(j=>{
      const review=j.reviews.find(r=>r.facts || r.analysis);
      return el('article',{class:'panel case-card'},badge(j.sent_version || j.opening_version || '话术版本待核验'),el('h3',{},j.company),el('p',{},j.title),
        el('p',{class:'case-text'},review?.facts || review?.analysis || '已保留实际发言与沟通记录，等待实质反馈后复盘。'),
        review?.next_trial?el('p',{},'下次尝试：',review.next_trial):null,
        button('查看原话与版本 →',e=>{openJob(j.job_id,e.currentTarget);state.detailTab='drafts';renderDetail();},'button small quiet'));
    }))];
}
async function openDocument(doc) {
  try {const body=await request('/api/documents/'+encodeURIComponent(doc.id));showText(doc.title,body.text);}
  catch(error){toast(error.message);}
}
function documentsPage() {
  const docs=state.view.documents;
  return [heading('资料有位置，事实有依据。','按用途查找资料。历史报告保留当时状态，最新进展以岗位记录为准。'),
    el('div',{class:'doc-grid'},...['个人资料','复盘与报告','数据','运维','归档'].map(category=>el('section',{class:'panel doc-section'},el('h2',{},category),
      ...docs.filter(d=>d.category===category).map(d=>el('div',{class:'doc-row'},
        d.readable?button(d.title,()=>openDocument(d),''):el('strong',{},d.title),el('p',{},d.purpose),el('code',{},d.path),
        !d.readable?el('p',{},d.retention_note):null)))))];
}
function render() {
  renderModeNotice();
  const active=document.activeElement,focus=active?.id;
  const selection=active instanceof HTMLInputElement && ['search','text'].includes(active.type)?[active.selectionStart,active.selectionEnd]:null;
  document.querySelectorAll('nav button').forEach(b=>{b.classList.toggle('active',b.dataset.page===state.page);b.setAttribute('aria-current',b.dataset.page===state.page?'page':'false');});
  $('crumb').textContent=labels[state.page];
  $('nav-task-count').textContent=state.view?.tasks.length || '';
  $('refresh-time').textContent=state.view?.meta.loaded_at?`本地读取 ${time(state.view.meta.loaded_at)}`:'等待记录';
  const main=$('main');main.replaceChildren();
  if(state.error) main.append(el('div',{class:'alert error'},state.error,'。保留最后一次读取结果，可点击刷新重试。'));
  if(state.prefError) main.append(el('div',{class:'alert error'},state.prefError,'。个人管理状态不可用，暂不保存修改。'));
  if(!state.view?.meta.available) {main.append(heading('暂时无法读取记录。','请检查原始记录是否完整，再点击右上角刷新。'));return;}
  if(state.view.meta.stale || state.view.meta.read_errors.length) main.append(el('div',{class:'alert'},
    state.view.meta.read_errors.join(' ') || `最近 BOSS 核验在 ${time(state.view.meta.boss_checked_at,true)}，记录可能已过期；页面刷新只读取本地记录。`));
  const content=state.page==='today'?todayPage():state.page==='jobs'?jobsPage():state.page==='conversations'?jobsPage(true):state.page==='learning'?learningPage():documentsPage();
  main.append(...content.filter(Boolean));
  if(focus && $(focus)) {$(focus).focus({preventScroll:true});if(selection)$(focus).setSelectionRange(...selection);}
}
function openJob(jid,trigger) {
  state.selected=jid;state.detailTab='conversation';state.lastTrigger=trigger;
  renderDetail();$('detail-dialog').showModal();
}
function closeJob() {
  $('detail-dialog').close();state.selected=null;
  if(state.lastTrigger?.isConnected)state.lastTrigger.focus({preventScroll:true});
}
function ensureForm(jid) {
  if(!state.forms.has(jid) || (!state.forms.get(jid).dirty && !state.forms.get(jid).saving)) state.forms.set(jid,{values:{...pref(jid)},revision:state.prefs.revision,dirty:false,saving:false,editVersion:0,status:''});
  return state.forms.get(jid);
}
async function savePreference(jid,values,revision) {
  if(state.prefError)throw new Error('个人管理数据读取异常，请先刷新恢复。');
  if(!state.token)state.token=(await request('/api/session')).token;
  const result=await request('/api/preferences/'+encodeURIComponent(jid),{method:'PUT',headers:{'Content-Type':'application/json','X-Workbench-Token':state.token},body:JSON.stringify({expected_revision:revision,values})});
  state.prefs=result;return result;
}
function management(job) {
  const form=ensureForm(job.job_id);
  const change=(key,value)=>{form.values[key]=value;form.editVersion++;form.dirty=true;form.status=form.saving?'正在保存上一版；新输入尚未保存':'尚未保存';const label=$('save-status');if(label)label.textContent=form.status;};
  const select=el('select',{id:'personal-priority',onchange:e=>change('priority',e.target.value)},...['高','普通','低'].map(t=>el('option',{value:t},t)));select.value=form.values.priority;
  const save=async()=>{
    if(form.saving)return;
    form.saving=true;form.status='正在保存…';
    const submittedVersion=form.editVersion;
    const label=$('save-status');if(label)label.textContent=form.status;
    const saveButton=$('save-note');if(saveButton)saveButton.disabled=true;
    try {
      const result=await savePreference(job.job_id,{favorite:form.values.favorite,priority:form.values.priority,note:form.values.note},form.revision);
      form.revision=result.revision;form.conflict=false;
      if(form.editVersion===submittedVersion){form.dirty=false;state.forms.delete(job.job_id);toast('个人管理信息已保存');}
      else {form.dirty=true;form.status='上一版已保存；当前新输入尚未保存';toast(form.status);}
    }
    catch(error){form.status=error.message;form.conflict=error.status===409;}
    finally {form.saving=false;if(state.selected===job.job_id)renderDetail();render();}
  };
  const box=el('div',{class:'management'},el('p',{class:'muted'},'这些标记仅供你管理机会，不会修改招聘进展或自动投递规则。'),
    el('label',{},el('input',{id:'personal-favorite',type:'checkbox',checked:form.values.favorite,onchange:e=>change('favorite',e.target.checked)}),' 收藏这个岗位'),
    el('label',{for:'personal-priority'},'个人优先级'),select,el('label',{for:'personal-note'},'我的备注'),
    el('textarea',{id:'personal-note',maxlength:10000,value:form.values.note,oninput:e=>change('note',e.target.value),'aria-label':'我的备注'}),
    el('div',{class:'save-row'},button('保存备注',save,'button primary',{id:'save-note',disabled:!!state.prefError || form.saving}),el('span',{class:'save-status',id:'save-status'},form.status || '已保存内容可随时修改')));
  if(form.conflict) box.append(button('读取最新内容并对照',async()=>{
    try{state.prefs=await request('/api/preferences');form.comparison={...pref(job.job_id)};form.revision=state.prefs.revision;form.status='已读取最新版本；请对照后修改当前输入，再点击保存。';renderDetail();}
    catch(error){toast(error.message);}
  },'button small'));
  if(form.comparison)box.append(el('div',{class:'compare-note'},`另一窗口已保存：\n收藏：${form.comparison.favorite?'是':'否'} · 优先级：${form.comparison.priority}\n${form.comparison.note || '无备注'}\n\n上方保留你的输入，确认合并后再保存。`));
  return box;
}
function renderDetail() {
  const j=jobById(state.selected);if(!j)return;
  const root=$('detail');root.replaceChildren();
  root.append(el('div',{class:'dialog-head'},el('div',{},readBadge(j),el('h2',{id:'detail-title'},j.title || '职位待核验'),el('div',{class:'company-name'},j.company || '公司待核验'),
    el('div',{class:'detail-meta'},j.city || '城市待核验',' · ',j.salary_text || '薪资待核验',j.district?' · '+j.district:null)),button('×',closeJob,'icon-button',{'aria-label':'关闭岗位详情'})),
    el('div',{class:'detail-actions'},isDemo()?badge('虚构岗位 · 无外部链接'):null,safeLink('打开 BOSS 职位 ↗',j.official_url),safeLink('打开 BOSS 聊天 ↗','https://www.zhipin.com/web/geek/chat'),
      j.sent_text?button('复制实际介绍',()=>copyText(j.sent_text),'button small'):null),
    el('div',{class:'segmented detail-tabs'},...[['conversation','沟通时间线'],['job','岗位依据'],['drafts','话术与复盘'],['manage','个人管理']].map(([id,text])=>button(text,()=>{state.detailTab=id;renderDetail();},state.detailTab===id?'active':''))));
  const content=el('div',{class:'detail-content'});
  if(state.detailTab==='manage') content.append(management(j));
  if(state.detailTab==='conversation') {
    content.append(el('p',{class:'muted'},`最后阅读状态核验：${time(j.observed_at,true)}。送达、已读与实质回复分别记录。`));
    const tasks=state.view.tasks.filter(t=>t.job_id===j.job_id);
    tasks.forEach(t=>content.append(taskCard(t)));
    if(!j.messages.length)content.append(el('div',{class:'empty'},'尚无可展示的实际沟通记录。'));
    for(const m of j.messages)content.append(el('article',{class:`message ${m.kind==='incoming_text'?'incoming':m.kind==='platform_event'?'platform_event':''}`},
      el('div',{class:'message-label'},badge(isDemo()?(m.kind==='incoming_text'?'虚构招聘方':m.actor==='agent'?'本地模拟':'演示记录'):m.actor==='platform'?'平台':m.kind==='incoming_text'?'招聘方':m.actor==='agent'?'代理代发':m.actor==='user'?'本人':m.kind==='outgoing_text'?'本人账号':m.role),m.message_at?time(m.message_at):`观察于 ${time(m.observed_at)}`),
      el('div',{class:'message-text'},m.text || '平台事件 · 无可读正文'),m.text?button('复制原话',()=>copyText(m.text),'button small quiet'):null,
      m.message_id?el('div',{class:'source-line'},`消息 ID · ${m.message_id}`):null));
  }
  if(state.detailTab==='job') {
    content.append(el('h3',{},'筛选判断'),...j.fit_notes.map(x=>el('p',{class:'prose'},x)));
    if(!j.fit_notes.length)content.append(el('p',{class:'muted'},'尚无明确筛选判断。'));
    if(j.gaps.length)content.append(el('h3',{},'需要核实'),...j.gaps.map(x=>el('div',{class:'alert'},x)));
    content.append(el('h3',{},'工作地点'),el('p',{class:'prose'},j.address || '完整地址待核验'),el('h3',{},'职位正文'),el('div',{class:'prose'},j.description || '尚未读取完整职位正文，不应仅凭标题判断适配。'),
      el('div',{class:'source-line'},`来源：${j.sources.map(x=>sourceLabels[x] || x).join(' / ')}\n岗位 ID：${j.job_id}`));
  }
  if(state.detailTab==='drafts') {
    if(j.sent_text)content.append(el('h3',{},j.sent_kind==='reply'?'实际发送的回复':'实际发送的定制介绍'),el('div',{class:'draft-card'},badge('已核验发送','green'),el('p',{class:'source-line'},j.sent_version || j.opening_version || '版本待核验'),el('div',{class:'prose'},j.sent_text)));
    if(j.drafts.length)content.append(el('h3',{},'草稿与版本历史'));
    j.drafts.forEach(d=>{
      const needsReview=d.kind==='reply'&&d.requires_user_review!==false&&!['verified_sent','resolved','superseded','superseded_by_authorized_version'].includes(d.status);
      content.append(el('div',{class:'draft-card'},badge(draftLabels[d.status] || '版本状态待核验',d.status==='verified_sent'?'green':''),el('p',{class:'source-line'},d.version || '版本待核验'),el('div',{class:'prose'},d.text),
        button(needsReview?'复制审阅提示':d.status==='verified_sent'?'复制已发原文':'复制草稿',()=>copyText(needsReview?reviewText(j,d):d.text),'button small')));
    });
    if(j.reviews.length)content.append(el('h3',{},'已记录的复盘'));
    j.reviews.forEach(r=>{for(const [key,title] of [['facts','观察事实'],['evidence','依据'],['interpretation','可能解释'],['analysis','分析'],['next_trial','下次尝试'],['recommendation','建议']])if(r[key])content.append(el('h3',{},title),el('p',{class:'prose'},r[key]));});
    if(!j.sent_text && !j.drafts.length && !j.reviews.length)content.append(el('div',{class:'empty'},'尚未形成话术或复盘记录。'));
  }
  root.append(content);
}
async function refresh() {
  if(state.busy)return;state.busy=true;$('refresh').disabled=true;
  const results=await Promise.allSettled([request('/api/view'),request('/api/preferences')]);
  if(results[0].status==='fulfilled'){state.view=results[0].value;state.error=null;}else state.error=results[0].reason.message;
  if(results[1].status==='fulfilled'){state.prefs=results[1].value;state.prefError=null;}else state.prefError=results[1].reason.message;
  state.busy=false;$('refresh').disabled=false;render();
  // Refresh open evidence while preserving management forms and their original revision.
  if($('detail-dialog').open && state.detailTab!=='manage') {
    const scroll=$('detail-dialog').scrollTop;
    renderDetail();$('detail-dialog').scrollTop=scroll;
  }
}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>setPage(b.dataset.page)));
$('refresh').addEventListener('click',()=>{refresh();refreshTasks();refreshAutomation();});
$('close-document').addEventListener('click',()=>$('document-dialog').close());
$('detail-dialog').addEventListener('cancel',()=>{state.selected=null;if(state.lastTrigger?.isConnected)state.lastTrigger.focus();});
window.addEventListener('hashchange',()=>setPage(location.hash.slice(1)));
state.page=labels[location.hash.slice(1)]?location.hash.slice(1):'today';
refresh();refreshAutomation();refreshTasks();setInterval(refresh,30000);setInterval(refreshAutomation,10000);
setInterval(()=>{if(taskRunning() || Date.now()-state.taskLastCheck>=10000)refreshTasks();},2000);
