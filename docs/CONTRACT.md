# ThreadPlan 行为合同

工程实现、模型和真实日历验收分别记录。实际结果和未验证项见 [VALIDATION](../reports/VALIDATION.md)。原文消息载荷继续使用 schema_version=0.3；不为版本命名重造输入接口。

## 可信输入与语义范围

Skill 的 `record` 处理宿主提交的明确个人记事请求，不是历史聊天推理，不生成虚构的作者、时间或证据。已核验的单条回执可用于预览地点、备注或个人提醒的补充，须由本人在终端确认具体改动，不授予通用 UPDATE，也不认领手工记录。

Intake 接收 request_id、source、完整相关 messages。每条必须有真实稳定 id、sender_id、aware sent_at、非空 text；可有 reply_to、真实 updated_at 和 mentions。缺元数据整批拒收，不能用接收时间、正文日期或模型推测补造原始作者/时间。personal_note 的新记录可使用可信上游真实提交时刻；转发文本不适用。

UpstreamContext 由可信宿主注入 user_id、default_timezone、connection_ref、scope_ref、namespace、policy_ref。scope_mode 默认 self_related，可明确设为 team；语义归属由模型判断，正文不能改变身份、账户或权限。default_duration_minutes 未配置/null 解析为产品默认 30，显式正整数覆盖，解析后的配置进入请求指纹。相同 request_id 改内容/上下文拒绝；新会话片段失效旧未发计划。个人笔记按原记录隔离：有 conversation_id 时用它作稳定文档范围，否则使用首条原消息的真实稳定 ID；不同记录不能复用这个 ID，投递 request_id 变化不改变原记录身份。正文编辑保持原消息 ID 并提供真实 updated_at，才能失效同记录的旧未发计划；多段文档应显式提供稳定 conversation_id，不能用新的首段 ID 冒充同一记录。旧源已发命令仍可只读恢复。

## 唯一决策链与共享规范

行为规则的单一来源为 [decision_policy.md](../threadplan/decision_policy.md)。独立 ConversationPlanner 使用一个模型循环：完整原文和可信配置 → NEED_MORE 只读请求 → 真实读取事实 → 重新核对原文并最终决策。没有固定首轮完整抽取、工程语义首查、额外复核或第二 Agent。每轮事实只包括原文与真实工具结果；之前的模型输出仅在格式纠错时带入并标为待纠错内容，不冒充事实。

AgentSession 是独立版和宿主版共用的 open/read/propose/prepare/execute/status/recover 工具底座。独立版协调模型；Skill 由宿主模型协调，不启动新模型服务。读取为 async read，CLI 负责事件循环。宿主和独立版分别验收。

模型回调继续为 async llm(system_prompt, input_json, response_schema)。OpenRouter 固定 :free 模型和零输入/输出/请求价格上限，无付费覆盖和任何自动回退；缺本地 Key 阻断该路线。Codex 订阅 JSON 仅为显式开发路线，剥离 API Key、使用已有 ChatGPT 登录、隔离目录并拒绝模型自行用工具。实际模型未知时明确记未知，订阅用量不折算 API 价格。

## 读取与事实引用

find_events 接收有界 aware 起止、可选 hints 及提供方支持的 page_token；get_events 只接收已返回 event_ref；get_messages 只接收已知/回复引用的消息 ID。查询格式和大小先校验，不向模型暴露 native ID。实际连接/日历由可信配置和提供方适配器约束。

返回 read_ref、activity_ref、query、实际 searched.range/applied_filters、COMPLETE/PARTIAL/FAILED 或未接入能力的 UNAVAILABLE、下一页 token。COMPLETE 只指当前实际覆盖，不能把有筛选搜索当空日历。缺实际覆盖/过滤声明降为 PARTIAL；完整分页链才可支持 CREATE 去重。当前飞书 instance_view 读取不实现分页续取，若出现未遍历页则保持不完整，不能假装已查全。未加载字段保持未知；新快照不把旧详情冒充新事实，原生历次结果仍留 sources 审计。

同一请求内相同查询复用事实并标 cached，不计新提供方调用；重复无新信息停止。失败读取不认领部分合并的候选。事件引用跨进程保持绑定，读回提交采用短 SQLite 事务，远端等待不持有数据库写锁；会话关闭/失效后不提交迟到事实。工具超时、畸形返回及技术失败保留类别，不能变成“无匹配”。可选 history_reader 复用同一原消息准入与源顺序，不改写原文；飞书 IM 未接通。

## 按活动输出

每个最终活动有 activity_ref、真实 evidence_message_ids、reason：

- PROPOSE：CREATE fields / UPDATE event_ref+changes / CANCEL event_ref；不是授权或执行成功。
- NO_ACTION：有依据的不需要变化。
- WAIT：业务事实缺失，含 missing_facts、一个最小 clarification_question，以及相关 candidate_refs/attempted_read_refs。
- UNSUPPORTED：明确保留未支持的实质要求，不能悄悄删掉要求再执行。
- FAILED：工具/运行期或确定性校验失败，由工程产生，和业务 WAIT 分开。

共享编译器逐项处理。一个错误不抹掉独立合法项；声明同一真实目标的相互冲突操作都被阻断。相应活动的读取失败不能被 NO_ACTION/WAIT 掩盖；未标活动的失败按共享事实失败保守处理。COMPLETED 仅表示该批分析结束，可同时含成功提议、等待和失败；只有 operations 中经编译操作能进入执行。原模型输出在 MODEL trace，宿主提交在 DECISION trace，items 的原生声明不等于已核验字段。

## 时间、字段与必要工程约束

自然语言含义、约定状态、语义身份及相对日期指向由模型理解；不以关键词、标题相同、候选数或数字置信度代替业务判断。工程只计算和校验已明确的输入：

- CREATE 缺结束/时长时采用可信默认 30 或显式配置，记录 PRODUCT_DEFAULT/UPSTREAM_DEFAULT_DURATION；不补日期/开始，不确认提议。
- UPDATE 只改开始时默认保留真实旧时长；显式 end_at/duration_minutes 可以改变时长，preserve_duration=true 检查矛盾。shift_minutes 对真实旧开始做有界位移，不能同时指定新开始。
- 明确数字时长由工程推导 end_at；按真实经过时间计算，跨午夜/DST 不心算。显式 IANA timezone 须与当地时间/offset 一致，不存在时间或错误 offset 拒收；重复时间需有效明确 offset，不猜 naive 当地时间。
- UPDATE 只写变化字段，null 是未知，不是清空。实际支持 title/start_at/end_at/location/description；required_write_fields 须在实际保留/写入有效事件满足。备注修改为字段替换，保留未要求删除内容由模型核对原文。
- participants 只作信息，不邀请。已知身份引用保留 metadata；未知引用独立记录，不补造 ID。不把身份字符串当作无证据地点。
- 标题不同、普通时间重叠不独立阻断合法操作。重叠只报告，检查不足保持 PARTIAL，不自动重排其他日程。

## 授权、冻结与执行

prepare 只读核验并冻结，授权来自宿主预设策略或明确批准，和对话同意独立。独立 CLI 配置默认 read_only；可分享 Skill 在首次绑定时由用户明确授权新增。preauthorized.allowed_operations 只覆盖可信配置中的日历和动作。模型提交的 approved/allowed_operations 不授予权限。run 可在现有预授权覆盖时执行，analyze 只分析。

沿用 PlanningExecutor → journaled FeishuHost → FeishuCalendar。保留原文版本、指纹、期限、真实目标、写前新鲜基线、局部更新、稳定命令与提供方 CREATE 幂等键。CREATE 需要完整无过滤窗口；新候选/已知候选关键变化交回重新分析，重叠消失不阻断。同计划已核验写入纳入基线。批量 prepare 返回逐项 errors，执行逐命令保留回执。

receipt 是结果权威，v3_commands 只保存冻结载荷和派发进度。区分 PREPARED、FAILED_BEFORE_WRITE、IN_FLIGHT、OUTCOME_UNKNOWN、VERIFY_PENDING、SUCCEEDED；未知/待读回不换键重发。recover 只读，旧源已发命令可恢复。未知 UPDATE/CANCEL 的期望状态核验若未观察到原请求确认，provider_acknowledged=false；未知 CREATE 无真实 ID 不按标题认领。旧回执不能覆盖新来源绑定。历史台账不批量迁移/删除；不承诺服务端原子条件写入或跨系统 exactly-once。

## 初始预算与验收边界

PlanningLimits 共用：4 次模型尝试、8 次读、50 候选、128KB 模型输入、180 秒期限、模型 45 秒/读 20 秒、单查询 31 天；宿主交互会话默认 30 分钟。Codex 开发入口为容纳启动可显式使用 120 秒模型等待/420 秒期限。不是测得最优值或 SLA。纠错/重试共用预算，429 的 not_before、冻结时刻与计数持久保留；HTTP400 不自动重试，宿主显式 retry 后仍用原预算。

模型及读取 trace 记录真实尝试、原输出/错误、耗时和输入规模；提供方报告的用量未提供时保持未知。预算停止和读失败不会伪称已查全；已确定的独立项保留。工程测试不证明语义正确；历史 11 项与真实 Test 成绩见 VALIDATION，不自动归给重构版。

可分享 Skill 只操作首次创建并绑定的私有「ThreadPlan」日历。新绑定不含消息作者；历史请求由可信宿主提供来源身份并随请求冻结。日常从本机加载固定日历 ID，写前核对原 CLI 用户和服务端角色。未决初始化不重发，只有唯一标记匹配才补绑。独立 CLI 保留显式配置；两个入口都不接受 primary 别名或自动回退。旧测试标记仅用于历史编码解码，不授予所有权或执行权限。

本项目开发真实写入仍只限既有私有 shared ThreadPlan Test 日历及标记事件，保护移入未打包的 scripts/feishu_test_support.py；能力可配置不表示本轮获准访问其他日历。消息撤回和 IM 自动采集未接通。
