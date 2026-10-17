"""Entirely fictional, redistributable fixtures for the offline demo."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NOTICE = '演示模式：所有候选人、公司、岗位与消息均为虚构。操作只在本地模拟，不连接招聘平台、模型服务或邮箱。'
CANDIDATE = '林一（虚构）'
CASES = [
    dict(key='qinglan-product', company='青岚实验室（虚构）', title='AI 应用助理', city='广州', salary='12-16K',
         direction='AI 应用', kind='reply', status='ready', incoming='可以介绍一下你做过的知识库项目吗？',
         reason='虚构资料包含项目范围，可直接回答；不虚构上线效果。',
         text='您好，我在课程项目中做过一个小型知识库问答原型，负责整理示例文档、设计回答格式并人工检查引用。项目尚未上线，我可以继续介绍实现过程和验证方法。',
         review='观察：对方追问项目职责。演示要点：讲清做过什么，同时保留原型阶段的边界。'),
    dict(key='weiguang-content', company='微光协作社（虚构）', title='产品内容运营', city='深圳', salary='11-15K',
         direction='内容运营', kind='reply', status='ready', incoming='你有写产品帮助文档的经历吗？',
         reason='虚构事实卡已有帮助文档练习，可以说明具体产物。',
         text='您好，我在课程练习中为一个虚构工具写过 4 篇中文帮助文档，覆盖首次使用、常见问题和操作示例。文档由同学试读后修改过，尚没有真实客户使用数据。',
         review='观察：对方关注文档经历。演示要点：提供具体产物，不把练习写成商业成绩。'),
    dict(key='zhizhou-assistant', company='纸舟工坊（虚构）', title='产品助理', city='杭州', salary='10-14K',
         direction='产品', kind='resume', status='ready', incoming='方便发一份简历吗？',
         reason='明确索要已有演示简历；按钮只模拟一次附件操作。', text='',
         review='观察：对方索要简历。演示要点：已有附件请求与结果分别记录，不重复操作。'),
    dict(key='qinglan-interview', company='青岚实验室（虚构）', title='用户研究助理', city='广州', salary='10-13K',
         direction='产品', kind='escalate', status='awaiting_user', incoming='下周二下午三点方便参加线上面试吗？',
         reason='资料里没有个人日程；面试时间需要本人决定。', text='',
         question_for_user='下周二 15:00 是否方便？如不方便，请补充可用时间。',
         review='观察：对方提出具体面试时间。演示要点：日程与承诺由本人确认。'),
    dict(key='yunxiu-operations', company='云岫商贸（虚构）', title='运营助理', city='广州', salary='8-9K',
         direction='内容运营', kind='ignore', status='ignored', incoming='这个岗位固定月薪 8–9K，可以接受吗？',
         reason='示例固定月薪低于 10K 底线，本轮跳过，不模拟发送。', text='',
         review='观察：固定月薪 8–9K。演示要点：明确的硬约束直接用于筛选。'),
    dict(key='mandao-design', company='慢岛设计社（虚构）', title='设计研究助理', city='深圳', salary='12-18K',
         direction='产品', kind='uncertain', status='uncertain', incoming='附件记录不完整，暂时看不到回执。',
         reason='演示历史中有附件操作记录，但没有回执。保留未知，不重复模拟发送。', text='',
         review='观察：只有操作记录，没有结果凭证。演示要点：未知就是未知，不能当作成功或重试依据。'),
]


def build_demo_record():
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    applications = []
    for index, case in enumerate(CASES):
        uid, jid = f'demo-person-{index+1}', f'demo-job-{index+1}'
        when = (now-timedelta(minutes=30-index*3)).isoformat()
        applications.append(dict(job_id=jid, conversation_uid=uid, company=case['company'], title=case['title'],
            city=case['city'], district='演示城区', salary=case['salary'], direction=case['direction'],
            address=f"{case['city']} · 虚构地址（不可导航）", description=f"{NOTICE}\n\n岗位：{case['title']}\n工作内容：协助整理需求、完成小型实践、记录反馈。\n固定月薪：{case['salary']}。\n材料不代表任何真实招聘机会。",
            status='user_contact_verified', identity_verified=True, monitoring_enabled=True,
            origin='demo', attempted_at=when, first_contact_at=when, server_verified_at=when,
            server_read_status='已读' if index<4 else '未读', counts_as_new_outbound_contact=False,
            opening_version='demo-v1', conversation_observations=[], followup_actions=[],
            conversation_messages=[dict(message_id=f'demo-in-{index+1}', conversation_uid=uid,
                actor='recruiter', speaker='虚构招聘方', kind='incoming_text', body_type=1,
                timestamp_iso=when, text=case['incoming'], response_required=False)],
            case_review=dict(facts=case['review'], interpretation='这是预设演示场景，不构成招聘效果证据。',
                next_trial='只根据已给出的虚构事实继续沟通；缺失信息保留给本人。')))
    return dict(demo_mode=True, demo_schema=1, demo_notice=NOTICE, candidate_name=CANDIDATE,
        applications=applications, daily_contact_audits={}, demo_completed_actions=[],
        ongoing_search_authorization=dict(city_priority=['广州','深圳','杭州'], excluded_districts=[],
            minimum_monthly_cny=10000, maximum_monthly_cny=None, daily_new_contact_cap=0),
        reply_monitor=dict(execution_owner='workbench-app-server', last_checked_at=now.isoformat(),
            interval_minutes=30, notification_events=[], workbench_tasks=dict(demo=True, status='idle', mode=None,
                run_id=None, phase='idle', message='从 6 段虚构会话开始，点击“收集并分析”。本演示不连接任何外部服务。',
                counts={}, items=[])))


def demo_items(record):
    completed = set(record.get('demo_completed_actions', []))
    result = []
    for case, app in zip(CASES, record['applications']):
        action_id = 'demo-action:'+case['key']
        item = dict(action_id=action_id, uid=app['conversation_uid'], company=case['company'], title=case['title'],
            kind=case['kind'], status='simulated_sent' if action_id in completed else case['status'],
            reason='已完成本地模拟；未向任何招聘方发送。' if action_id in completed else case['reason'],
            text=case.get('text',''), question_for_user=case.get('question_for_user',''),
            incoming_ids=[app['conversation_messages'][0]['message_id']],
            row=dict(job_id=app['job_id'], uid=app['conversation_uid']))
        result.append(item)
    return deepcopy(result)


DOCUMENT_TEXT = {
    'resume': '# 林一的演示简历\n\n'+NOTICE+'\n\n## 虚构经历\n- 做过课程知识库问答原型：整理示例文档、设计回答格式、人工检查引用。尚未上线。\n- 为虚构工具写过 4 篇中文帮助文档，由同学试读修改。没有客户效果数据。\n- 求职示例方向：AI 应用、产品助理、内容运营。',
    'facts': '# 演示事实卡\n\n'+NOTICE+'\n\n可用事实：课程知识库原型；4 篇帮助文档。\n不可推断：真实商业经历、收入、业务提升、可到岗日或可面试时间。\n面试日程必须让本人决定。演示附件名为“演示简历.pdf”，只是一条本地模拟记录。',
    'learning': '# 沟通复盘示例\n\n'+NOTICE+'\n\n1. 事实明确的问题可以按资料回复。\n2. 面试时间属于个人承诺，需本人决定。\n3. 缺少附件回执时保留未知，不重复操作。\n4. 8–9K 低于示例 10K 底线，跳过。\n\n以上是预设场景，不代表真实求职成果。',
    'manual': '# 演示使用说明\n\n'+NOTICE+'\n\n收集并分析：读取本地 6 段虚构会话，得到 3 项可跟进、1 项需本人决定、1 项低薪跳过、1 项未知。\n一键跟进：只生成 2 条模拟回复和 1 条模拟简历记录。\n停止本轮：停止剩余本地步骤。再次收集不会重复模拟已完成事项。\n岗位收藏和备注保存在演示目录，可重新加载查看。',
}
