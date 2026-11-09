# 团团 API 与 MCP

HTTP 服务默认只绑定 `127.0.0.1:8789`，所有 `/api/*` 路径要求 `Authorization: Bearer <token>`。`GET /health` 只返回服务名称与版本，不返回私有信息。

请求体为 JSON 对象，最多 64KiB；鉴权失败 401、没有权限 403、输入错误 400、不存在 404。服务拒绝其他网站的 Origin 及非本机 Host，不开放 CORS。未来需要公网访问时应另行设计身份、权限与 HTTPS，不直接改绑定地址暴露。

## 令牌与权限（2026-10-12 起）

- **主人令牌**：本机看板用，拥有全部权限。每次启动在内存里随机生成，不写盘；团团的“看板”按钮会带着它打开页面。单独运行 `canaan.py api` 时，看板地址打印在控制台。也可用 `CANAAN_API_TOKEN` 提供（不要把它填进任何外部客户端）。
- **外部客户端令牌**：AutoClaw、MCP 等每个客户端单独配对、单独撤销。`data/api_clients.json` 只存令牌的 SHA-256，令牌原文只在生成时显示一次。
- **能力**：`status` 查看状态 / `read` 读取指定资料 / `propose` 提出任务 / `ingest` 提交采集事件 / `confirm` 确认执行 / `train` 授权训练与人工标注 / `admin` 改设置、导入文件、管理客户端。每个接口在服务端检查；没有列在权限表里的接口只给主人。
- **外部客户端默认只有 `status` + `propose`**，永远拿不到 `confirm`、`train`、`admin`。它提出的任务即使是只读动作，也要你在本机看板确认后才执行；别的客户端提出的任务只看得到动作名和状态。
- **读取资料要选范围**：消息和语音转写须写明来源和会话名（整个来源开放须明确写 `*`），知识库和团团总结单独勾选。选了的内容会进入该客户端的上下文（可能是云端模型）。
- **旧共享令牌**：`data/integration_token.txt` 启动时自动登记为“只能看状态”的客户端，原来的配置照常连得上，但要读资料或提任务须重新配对。

配对方式：看板 → 设置 → 外部客户端令牌；或命令行：

```powershell
.\.venv-integrations\Scripts\python.exe canaan.py api --pair AutoClaw                      # 默认 status,propose
.\.venv-integrations\Scripts\python.exe canaan.py api --pair 飞书助手 --caps status,read --resources '{"messages":{"sources":["feishu"],"conversations":["工作群"]},"summary":true}'
.\.venv-integrations\Scripts\python.exe canaan.py api --list-clients
.\.venv-integrations\Scripts\python.exe canaan.py api --revoke <客户端ID>                   # 立即失效
```

限制：同一台电脑上以你的账号运行的程序都能读 `data/` 目录，令牌分级防的是“接进来的客户端越权”，防不了本机恶意程序。

| 方法 / 路径 | 内容 |
|---|---|
| GET `/api/actions` | 注册动作、描述、参数与权限 |
| POST `/api/tasks` | `{action,param}` 提交提案；主人提交的只读动作直接执行，其他动作和所有外部客户端的提案都等确认 |
| GET `/api/tasks` / `/api/tasks/{id}` | 任务列表 / 状态与结果 |
| POST `/api/tasks/{id}/confirm` | 人类核对参数后确认；重复确认不会重复执行 |
| POST `/api/tasks/{id}/cancel` | 取消待执行任务，或请求停止运行中的 Computer Use |
| POST `/api/messages` | 消息事件（见下方） |
| GET `/api/messages?after=0&limit=100` | 按本地 id 增量读消息，附 importance，limit 最多 200 |
| GET `/api/messages/inbox?source=imapp&priority=P1&search=合同&limit=50` | 近24小时优先收件箱，来源/等级/搜索可省略；扫描最近5000条，附覆盖提示 |
| POST `/api/messages/priority` | `{id,priority:"P0"或"P1"或"P2",reason}`，保存人工标注，用于下次总结排序 |
| GET `/api/messages/status` | 来源、发送计数及设置 |
| POST `/api/messages/summary` | `{sources:["chatapp","imapp"]}` 本地千问总结已采集的最近24小时消息；可只含一个来源 |
| GET `/api/messages/summary` | 保存的成功结果 result、最近后台 task、自动设置 settings、next_check_at 和调度错误 |
| POST `/api/messages/summary/run` | `{sources?:["chatapp","imapp"]}` 让团团启动后台总结，立即返回任务 ID；已有任务时复用它 |
| POST `/api/messages/summary/settings` | `{enabled?,interval_seconds?,sources?}` 更新团团自身的定时总结，间隔为60–86400秒整数 |
| POST `/api/messages/settings` | 更新 capture_visible、forward_enabled、jsonl_sources 等设置 |
| POST `/api/messages/retry` | `{id}` 重新排队 stored/blocked 消息；不重试发送结果不确定的消息 |
| GET `/api/agents` | Muse/Dots 实际连接状态、归一化任务状态、配置的工作台地址 `web_url`（页面仅允许本机链接） |
| GET `/api/agents/{muse或dots}/{remote_task_id}` | 按需读回真实结果，保留状态与 Dots sample；不执行结果文本 |
| GET / POST `/api/connections` | 读取接入方式或保存 `{agents:{muse_api,muse_web,dots_api,dots_web},collectors:{python,chatapp_config?,imapp_data_root?,interval_seconds,enabled_sources}}`；本机根地址，路径校验，不返回模型密钥，不自动启动服务 |
| POST `/api/training/export` | `{consent:true}`，只将人工标注导出到本机 data/training，附门槛报告；不训练或部署模型 |
| GET `/api/knowledge` | 已导入文档 |
| POST `/api/knowledge/import` | `{path}` 导入 / 更新本机文档 |
| POST `/api/knowledge/search` | `{question,limit?}` 原文与来源 |
| POST `/api/knowledge/answer` | `{question}` 本地模型 RAG 回答与 sources |
| GET / POST `/api/feedback` | 列表 / `{kind,question,correction}` 记录候选 |
| POST `/api/feedback/evaluate` | `{id,cases}` 评测，全部通过才启用 |
| POST `/api/feedback/disable` | `{id}` 停用反馈 |

消息事件示例（连接器应给出稳定 message_id 和真实 Unix 秒 timestamp）：

```json
{"source":"imapp","message_id":"connector-stable-id","conversation":"项目群","sender":"同事","text":"明天上午开会","timestamp":1791633600,"capture":"my_connector"}
```

source 为 `chatapp`、`imapp`、`feishu` 或 `claw`。唯一约束 `(source,message_id)`；没有 ID 时按会话、发言人、时间及内容计算哈希。相同事件重试不会重复存储/转发。`capture` 标记来源，不提供命令权限。第三方读取后保存最后一个本地 `id`；积压超过 200 条应继续分页。JSONL 末尾换行才会消费，半行等待补完；坏的完整行保存到本机隔离目录后继续，隔离失败保留位置。

PowerShell 使用示例：

总结返回 `success`、`todos`、`highlights`、`social_memo`、`speech_text`、`model`、`coverage` 和 `evidence`。每项 evidence 包含 field、text 和 source，引用必须出现在实际输入中；播报仅由有效引用生成。每来源扫描最近5000条，按重要程度选取前100条，再选入总计约5000字符上下文，因此不能把总结视为全量未读列表。读取不到消息时 `success=false,error_code=NO_MESSAGES`，模型失败为 `MODEL_UNAVAILABLE`，无效结果为 `INVALID_MODEL_RESPONSE` 或 `INVALID_MODEL_EVIDENCE`；这些业务失败仍返回 HTTP 200，应检查 success。`CANAAN_MESSAGE_SUMMARY_MODEL` 设置本机 Ollama 总结模型，默认 qwen2.5:7b，聊天总结强制在本地生成。

```powershell
$token = "<该客户端配对时得到的令牌>"   # 读消息需要配对时选了 read 和对应会话
$headers = @{Authorization="Bearer $token"}
Invoke-RestMethod http://127.0.0.1:8789/api/messages?after=0 -Headers $headers
$body = @{action="computer_use";param="打开记事本并写下你好"} | ConvertTo-Json
$task = Invoke-RestMethod http://127.0.0.1:8789/api/tasks -Method Post -Headers $headers -ContentType application/json -Body ([Text.Encoding]::UTF8.GetBytes($body))
# 核对 $task.param 后，在控制中心确认。JSON 中的 confirmed/source 字段不能跳过确认。
```

Dots 周期任务参数：

```json
{"action":"opendots_task","param":{"value":"检查工作区已有待办并整理日报","interval_seconds":86400}}
```

任务提案确认后提交给 Dots；实际调度属于 Dots，不依赖 Codex 定时器。重启时本地 queued/running 标为 unknown，避免自动重放副作用。取消运行中的普通外部任务不会自动终止远端任务，需在对应工作台处理。

反馈评测示例：

```json
{"id":1,"cases":[{"question":"介绍方案甲","contains":["结论"],"not_contains":["确定无风险"]},{"question":"介绍方案乙","contains":["结论"]}]}
```

验收条件来自用户，不能由模型用自己的回答自行批准。题目至少两道且不同；不要用项目意图留出集调参。

## MCP 接入

使用官方 [Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk) 的 1.x 接口，本机实测版本为 1.30.0，依赖约束为 `mcp>=1.30,<2`。本项目使用 stdio 传输，由 MCP 客户端启动脚本；不要手工把协议数据写到 stdout。

```json
{"mcpServers":{"canaan":{"command":"<安装目录>/.venv-integrations/Scripts/python.exe","args":["<安装目录>/canaan_mcp_server.py"],"env":{"CANAAN_API_URL":"http://127.0.0.1:8789","CANAAN_API_TOKEN":"<为这个客户端配对得到的令牌>"}}}}
```

API 需先运行。`CANAAN_API_TOKEN` 填该客户端自己的令牌；不填时脚本读旧的 `data/integration_token.txt`，只能看状态。配置文件不能提交到仓库。MCP 仅接受 localhost/127.0.0.1 的 HTTP API 地址。工具能做什么由令牌权限决定，没有权限的工具调用返回 403。

工具：`list_actions`、`propose_task`、`propose_scheduled_task`、`task_status`、`cancel_task`、`read_messages`、`message_summary_status`、`summarize_messages`、`knowledge_search`、`knowledge_answer`、`import_document`、`record_feedback`、`agent_status`。`summarize_messages` 让团团执行本地总结后返回任务 ID，再查 `task_status` 或 `message_summary_status` 获取结果。没有确认或强行激活反馈工具。使用 `list_actions` 获取当前能力，不依赖固定动作数量。

AutoClaw / OpenClaw / 其他工具可以按其支持方式使用 HTTP 或 stdio MCP。例：定时读取 `read_messages(after)` → 生成提醒候选 → 用 `propose_scheduled_task` 提议 Dots 任务 → 人类在团团控制中心确认。接入前须检查第三方的数据流；客户端读到的内容是否上传到云端由第三方决定，所以默认不给读取权限，需要时只开放必要的会话。

看板区分本机 MCP 工具探测和云端模型的只读调用验收，并显示各自时间；验收记录不是持续在线保证。本机 AutoClaw 返回 `401 Invalid token` 时，请打开 AutoClaw 让其正常刷新登录；若仍失败，再在客户端重新登录。不要把团团 API 令牌当作 AutoClaw 云端登录令牌。

当前对话模型仍使用现有 Ollama `/api/chat` 契约，通过 `CANAAN_OLLAMA_API_URL` 切换兼容服务；本 HTTP 服务没有声称支持 OpenAI `/v1/chat/completions` 协议。

2026-10-11 追加：消息 `source` 与总结 `sources` 支持 chatapp / imapp / feishu / claw，不重复来源子集均可；默认仍为聊天软件和 聊天软件。消息设置增加 `importance:{vip_contacts:[],self_names:[],muted_conversations:[]}`。只影响阅读排序，不扩大采集或执行权限。

追加 MCP 工具：`integration_capabilities`、`ingest_message`、`priority_inbox`、`review_message_priority`、`propose_agent_task`、`agent_task_result`，为自己的 Claw 等客户端统一接入。Muse/Dots 由团团桥接本机 API，不等于它们已有同名原生 MCP 服务。看板下载的 MCP 配置使用本机路径，具体配置、分级与训练数据边界见 接入与训练准备。

`review_message_priority` 保留名称和参数，但返回 `awaiting_human_review` 建议，不再直接写入人工分级。请在看板核对原文后保存；MCP 的判断不会成为人工训练标签，也不能授权训练。已有客户端需处理这个待核对状态。

## 语音与收件稳定性接口

| 接口 | 契约 |
|---|---|
| POST `/api/voice/jobs` | `{source,message_id,conversation,timestamp,audio_path,sender?,session_id?}`；真实本机音频必须位于允许目录，返回语音任务 ID |
| GET `/api/voice/jobs?limit=50` | 最近任务，limit 1–200；含原转写、人工修订、进度、错误和音频可用标记；不暴露私有缓存路径 |
| GET `/api/voice/jobs/{id}` | 单条语音任务 |
| GET `/api/voice/status` | 各状态数量、已授权样本、限额、本机策略与可标注意图 |
| POST `/api/voice/jobs/{id}/review` | `{text,reason,training_consent:false,label:{priority?,todos?,a?,p?}}`；本人指令才可标操作意图；只保存纠错，不执行 |
| POST `/api/voice/jobs/{id}/retry` | 仍有可用音频的失败聊天任务重新转写，不重放操作 |
| POST `/api/training/export` | `{kind:"voice",consent:true}` 导出已逐条授权的人工纠错样本，附分组/哈希；默认 kind 仍为 priority |
| POST `/api/connections` | 可追加 `feishu:{enabled,python,lark_config?}` 与 `voice:{audio_roots:[]}`；应用 secret 不通过此接口保存 |

语音导出的 `manifest.json` 中，`format_version` 为 `2` 时，JSONL 里的 `audio` 路径相对于整份导出目录。`files` 提供清单自身之外每个发布文件的 SHA-256。撤销某条训练授权会从后续导出排除该样本。以前导出的副本需要单独清理。

旧 POST `/api/messages` 也接受上述带 audio_path 的事件；仅有占位信息时提交 `audio_missing:true`，任务显示“未取得音频”，不生成伪转写。消息状态新增 `health`、`quarantine`、`notifications`、`desktop_available`。消息设置新增 `notify_p0`；独立 API 没有桌面显示回调时只保存消息。

只读 MCP 工具追加 `voice_status`、`voice_results`，不提供训练授权、人工真值写入或直接执行。资源与缓存策略见 语音与稳定性说明。

2026-10-13 深度审查修订：外部 POST `/api/messages/summary` 与 `/api/messages/summary/run` 均要求 `propose` 能力、已授权的总结来源，并返回 `awaiting_confirmation` 任务，兼容客户端需改为轮询任务结果。主人的同步总结接口仍直接返回结果。通用 POST `/api/tasks` 不再绕过总结来源限制，外部 `knowledge_query` 任务请改用专用检索/回答接口。智能体详情只接受已确认且成功提交的结构化 provider/task_id 绑定；旧的无绑定结果默认拒绝。

## 多人会话与草稿（2026-10-24）

以下接口仅本机主人令牌可用；外部接入客户端不能读取或切换使用者。

| 接口 | 契约 |
|---|---|
| GET `/api/memory/people` | 使用者列表、当前使用者和各人的事实，沿用已有管理接口 |
| POST `/api/memory/people/switch` | `{name:"小明"}`，按姓名创建或切换；返回 `active` 和 `identity_revision` |
| POST `/api/memory/people/owner` | 切回主人；沿用 `{active:null}` 返回值 |
| GET `/api/context` | 当前人的 `profile_key/session_id/recent_turns/current_task/active`；附着语音时含保存可用状态及声纹锁定状态 |
| POST `/api/context/clear` | `{}`，清除当前人的对话、草稿和会话，撤销旧确认；保留长期事实和其他人记录 |

`/api/memory` 的事实及 `/api/feedback` 的列表、写入和评测按当前使用者隔离。主人查看其他人的事实仍走 `/api/memory/people` 管理接口。删除使用者会同时删除该人的会话、草稿和反馈。

切换成功前，附着的语音端会完成上下文交接；独立接口进程的变化通过持久化身份版本被语音端检测。已捕获的确认不能跨身份执行。公开 `/health` 增加 `runtime_build_id` 和 `loaded_at`，不含使用者或聊天内容。

新会话保存在本机 `data/context.db`；不从没有身份归属的旧 JSONL 恢复个人历史。草稿十分钟自动衔接、二十四小时内显式恢复，确认三十秒有效；执行中断记为未知，不自动重试。
