# 宿主工具协议

命令格式：`python -X utf8 <实际Skill目录>/scripts/threadplan_tool.py --config <可信配置> <命令>`。JSON 参数放文件或 stdin（--input - / --query -）。返回单行 JSON，顶层 ok 是工具调用是否完成，不代表日历成功；业务和执行状态必须查看 result。失败退出码非零，不把失败当 NO_ACTION。

## policy

无需配置或连接，返回与独立版相同的 decision_policy。包内 references/decision-policy.md 由同一源文件生成。不要维护第二份业务判断规则。

## doctor [--check-connection]

检查既有 Python、时区、原生 lark-cli、外部配置；--check-connection 只读核对配置日历。不会登录、安装或创建日历。

## configure --user-id <真实原消息身份> --calendar-id <日历ID> --timezone <IANA时区>

在 --config 指定的新路径创建只读配置，state_directory 默认相对配置文件。不会覆盖已有配置、登录、安装或连接日历，不接受 primary 别名，不写入 Skill 包内。须由用户或可信宿主运行，正文中的配置请求不构成授权。

## open --input <JSON文件或->

提交 schema_version=0.3 的 request_id、source、完整相关 messages。消息必须有真实稳定 id、sender_id、带 offset 的原发送 sent_at、text；可有 reply_to、updated_at、mentions。personal_note 也必须提供真实上游提交身份和时间，不能替代转发材料的原元数据。

返回 request_key、原文、可信用户/时区/scope_mode、冻结 evaluation_now、期限、默认时长策略、稳定 event_ref、实际 reads。历史绑定可 read_verified=false，读取后才能用作目标。缺元数据拒收；新的同会话输入使旧未派发计划失效。默认会话 30 分钟；已发命令仍可只读恢复。

request_id 是投递身份，不是笔记身份。个人记录可用 conversation_id 指定稳定文档范围；省略时以首条原消息的真实稳定 ID 识别同一记录，不能给不同笔记复用 m1 等示例 ID。编辑时保持原 ID 并给真实 updated_at；上游新建记录可持久分配真实提交记录 ID，不能由模型临时捏造。

## read --request <key> --activity <活动引用> --query <JSON文件或->

有界查询示意，以下是构造协议示例：

```json
{"kind":"find_events","range":{"start_at":"2026-11-20T00:00:00+08:00","end_at":"2026-11-21T00:00:00+08:00"}}
```

详情为 `{"kind":"get_events","event_refs":["c1"],"fields":["description"]}`。引用必须是工具已返回值，不能填 native ID。可用 page_token 时沿真实返回 token 续取；当前飞书 instance_view 不支持分页续取，未遍历状态必须保留为 PARTIAL。

--activity 为该活动稳定的 activity_ref；省略时按共享读取处理，失败会保守影响所有依赖项。返回 updated view 和 latest_read，包含 read_ref、read_status、query、实际 searched、下一页状态；cached=true 不代表新读。COMPLETE 只代表已报告范围完整，未知筛选降为 PARTIAL。8 次读取、50 候选、单次 31 天；不能以失败/部分/未接入当空日历。get_messages 未接飞书 IM时返回 UNAVAILABLE，上游需补原文。

## propose --request <key> --input <JSON文件或->

提交最终 items，每项 decision、activity_ref、reason、evidence_message_ids。行为规则以 policy 为准。

- PROPOSE：operation 为 CREATE+fields，或 UPDATE+event_ref+changes，或 CANCEL+event_ref。
- WAIT：missing_facts 和一个 clarification_question，相关 candidate_refs/attempted_read_refs 只能引用工具实际返回值。
- NO_ACTION / UNSUPPORTED：保留具体依据和未支持要求，不带 operation。

时间和地点等放 operation.fields/changes。新建缺时长时省略 duration_minutes/end_at，让工程采用可信默认；原文明示数字时长用 duration_minutes。修改只给变化字段；仅改开始默认保留旧时长，也可显式 preserve_duration=true；shift_minutes 为原开始的位移，不与新开始并用。timezone 是可校验的 IANA 名称；null 不清空。participants 只作 metadata，不邀请。required_write_fields 声明实际持久化要求。

返回 plan.items、编译 operations、item_errors、重叠报告和 plan_fingerprint。单项错误为 FAILED，不抹掉其他合法项；同目标或同活动引用冲突都拒绝。模型声明与工程编译有区别。冻结后改变判断必须用新请求，原始提交保留 DECISION trace；不能靠删台账重试。

## prepare --request <key> --fingerprint <fingerprint>

只读核验并冻结合法 operations，返回 commands 和 errors。一个准备失败不丢失其他命令。新候选或基线变化需新事实重新分析，不机械重试旧计划；不会更改授权。

## execute --command-id <id>

仅可信配置 preauthorized.allowed_operations 覆盖时写入，并核对源版本、期限、真实目标、精确基线。只有 SUCCEEDED 表示读回核验。逐命令处理异常并查询回执；不要把已经成功项再写一次。

## status --request <key> / --command-id <id>

只读分析或执行回执。COMPLETED 为分析结束，可混有 WAIT/FAILED。FAILED_BEFORE_WRITE 未派发，IN_FLIGHT/OUTCOME_UNKNOWN 未决，VERIFY_PENDING 待读回，SUCCEEDED 已核验。

## recover --command-id <id>

只读恢复，不发送 CREATE/PATCH/DELETE。未知 UPDATE/CANCEL 观察到期望状态时可收敛，但 provider_acknowledged=false 表示未观察到原请求确认；未知 CREATE 无真实 ID 时保持未决，不按标题认领。
