# 配置与真实模式

默认 `python3 run.py --open` 使用 `.local/demo` 内的虚构数据。演示不需要 Codex、BOSS、邮件账号或第三方 Python 包；只在 `127.0.0.1:8766` 提供页面。已有演示状态会保留，程序不会自行轮询或恢复常驻执行器。

真实模式面向 macOS / Linux、Python 3.11 及以上。它依赖当前已登录的 Codex CLI，以及通过本机 CDP 暴露、已经登录 BOSS 官方页面的浏览器。程序不创建浏览器、不导出 Cookie、不修改代理。安装 `requirements.txt` 中的 `websocket-client` 仅为真实 CDP 连接所需。界面或协议变化可能导致连接被拒绝；这时工作台保留待办。

## 一个配置文件

把根目录的 `config.example.json` 复制到 `.local/config.json`，按自己的账号填写。设置环境变量指向这个 JSON 文件：

```sh
export JOB_WORKBENCH_CONFIG="$PWD/.local/config.json"
```

配置在启动时读取；修改后重启程序。它只包含账号核验值与本机连接参数，不放 API key、Cookie、OAuth token、浏览器会话或邮箱密码。代码使用 Codex 已有登录状态，不读取认证文件。

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `live_enabled` | `false` | 真实连接总开关；还必须显式传 `--live` |
| `self_uid` | 空字符串 | BOSS 本人账号 UID，使用字符串填写 |
| `self_name` | 空字符串 | BOSS 官网实际展示的本人姓名，用于账号核验 |
| `attachment_name` | 空字符串 | BOSS 已有附件简历名称，不含末尾 `.pdf`；不会自动上传或替换附件 |
| `notification_email` | 空字符串 | 可选 Gmail 提醒收件人，必须与已连接 Gmail 账号一致 |
| `notifications_enabled` | `false` | 可选邮件发送开关；关闭时待办仍在工作台显示 |
| `codex_executable` | `codex` | PATH 内的 Codex 命令，也可填写自己的可执行文件绝对路径 |
| `cdp_url` | `http://127.0.0.1:9229` | 已有浏览器的本机 CDP HTTP 地址；其他主机、用户信息和 URL 路径被拒绝 |

未知配置字段、非布尔开关和无效连接地址都会报错。身份字段没有默认候选人。`--live` 不会从演示账号推断真实身份。

## 独立的数据目录

真实数据默认放在 `.local/live`，也可以用 `--data-dir` 指定自己的目录。不得把演示目录切换为真实模式；演示主记录带有 `demo_mode: true`，启动器会拒绝它。

目录包含：

- `applications.json`：唯一主记录，保存授权、会话、实际原文、核验结果和消息去重依据。
- `facts.md`：本人确认过的经历、项目阶段、能力与表达边界。模型只能引用这些资料和当前授权。
- `resume.md`：可选的本地简历母版，只用于查看，不会自动上传。
- `learning.md`：可选的沟通复盘资料。
- `state/`：程序产生的本地执行锁、事件记录、偏好和必要的邮件核验材料。结果未知的发送依赖这些记录避免重复。

文件名固定；资料页面仅显示上述明确列出的文档，不公开任意目录文件。所有真实数据、配置和状态都应留在版本控制外。

空主记录可以从以下内容开始。城市、薪资和发送授权必须由本人明确填写，示例没有授予发送权限：

```json
{
  "applications": [],
  "ongoing_search_authorization": {
    "city_priority": [],
    "minimum_monthly_cny": null,
    "excluded_districts": [],
    "exclude_phone_sales": false,
    "reply_authorization": {"enabled": false},
    "resume_request_authorization": {
      "enabled": false,
      "attachment_name": "",
      "allow_text_resume_verification": false
    }
  },
  "reply_monitor": {
    "execution_owner": "workbench-app-server",
    "baseline": [],
    "notification_events": []
  }
}
```

`city_priority` 是本人可接受的城市列表；`minimum_monthly_cny` 是本人确认的月薪下限。没有明确这两项时，岗位转为待本人决定。区县排除和电话销售排除按当前主记录的选择执行，不预设某个人的偏好。

普通文字回复只有 `reply_authorization.enabled: true` 才能进入可跟进状态。附件需要 `resume_request_authorization.enabled: true`，且其中的 `attachment_name` 与配置一致。仅在当前已参与会话明确索要既有附件时处理。普通文字索要附件的首次 UI 核验另需 `allow_text_resume_verification: true`；官方明确索要卡片仍按原请求核验。

工作台当前只接管本人已参与的会话，不批量新建联系。具体时间、接受待遇或录用条件、交换联系方式、缺失事实和其他文件仍转为本人决定。

## 启动与停止

确认本机 Codex 登录、浏览器官方聊天页面、个人配置、真实主记录与 `facts.md` 都已准备好后：

```sh
python3 -m pip install -r requirements.txt
JOB_WORKBENCH_CONFIG="$PWD/.local/config.json" python3 run.py --live --open
```

`收集并分析` 读取当前会话和必要历史，生成计划并独立审查，不会给招聘方发送消息；若开启邮件提醒，本轮可能发送必要通知。`跟进待办` 在新的历史、权限、身份与事实核验通过后处理已授权事项；`停止本轮` 阻止后续操作，已经点击但没有可信回执的操作保留为未知。未知结果只回读核验，不能重发。

每轮使用共同执行锁。旧 `boss` 心跳配置文件不存在时不构成阻塞；如果它存在且没有暂停，或旧常驻服务仍在运行，则拒绝并行执行。本项目不会创建、启动或恢复这些旧执行器，旧 HTTP 常驻控制入口也不可用。

邮件提醒是实验功能，默认关闭，公开版尚未进行真实发送验收。邮件执行使用独立的 Codex CLI 进程；在桌面 App 连接 Gmail，不自动证明该进程可以使用它。启用前，需在同一 CLI 执行环境确认自己的 Gmail 已连接，且账号查询、Sent 查询和发送工具均可用，再显式启用邮件开关并配置同一邮箱。

邮件仅处理需要本人决定、连接故障和明确沟通进展。发送前核验 Gmail 账号与 Sent，发送后核验实际工具回执；工具缺失或无法证明结果时保持未知。不同 Codex 版本的连接器能力可能不同，首次接入应由本人检查。

## 离线验证与保留

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s workbench/tests
```

测试使用合成身份与依赖注入，不连接 BOSS、Codex 模型或邮件。可选 Node.js 只用于执行界面与浏览器表达式的测试，生产 Python 服务不依赖 Node。

建议长期保留真实 `applications.json`、`facts.md`、`resume.md`、`learning.md` 和 `state/`。删除执行状态会失去去重与未知发送核验依据。演示数据可在确认不再需要后单独删除；不会影响真实目录。程序不自动清理历史原文或故障材料。
