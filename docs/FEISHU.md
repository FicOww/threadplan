# 飞书接入与开发测试隔离

本文件维护接入机制与开发测试隔离。1.1.0包已在原私有ThreadPlan Test日历完成一条标记事件的新增、同ID修改、取消、读回与重复回执核验，测试记录已清理；这是零模型调用的执行验收，与50条模拟日历模型回归分开。使用者需自己的登录及明确配置，其他真实日历/账户/操作系统未实测；版本和未验证项见 [VALIDATION](../reports/VALIDATION.md)。

## 执行范围

v0.3 核心产出决策与平台无关操作，由 `PlanningExecutor` 转为 `Proposal` 执行。上游策略或人工授权后，调用 `threadplan.adapters.feishu.FeishuCalendar`；所有对外方法默认拒绝执行，必须显式传 `approved=True`。对话中的约定状态不等于执行授权。adapter 不登录、不读写密钥、不切换 bot、不安装 CLI 或依赖。

可分享版由可信配置选择真实 calendar_id；不绑定开发者日历名称、类型、账户、路径或网络。拒绝隐式 primary 别名，每次写入重新核对真实日历 ID 和 owner/writer；reader 可只读使用。默认 read_only，仅可信宿主的明确预授权允许相应操作。新建使用原样标题/备注，private/free、无会议、提醒、邀请或通知。含参与人、重复、提醒、全天或会议的已有事件不改动。

项目开发真实写入继续只允许既有私有 shared ThreadPlan Test 和标记事件，约束位于未打包的 scripts/feishu_test_support.py。开发专用创建辅助 create_private_test_calendar 也已移出产品；本轮不创建新日历。历史完整测试格式的前缀/尾注仅用于老事件和回执解码，不是产品所有权门禁，普通用户记录不会自动加标记。

## 接口

```python
FeishuCalendar(calendar_id, *, cli=None, managed_event_ids=None)
calendar.get_calendar(*, approved=False, writable=False)                        # -> calendar dict
calendar.get_event(event_id, *, approved=False, writable=False)                  # -> event dict
calendar.find_events(range, *, approved=False)                  # -> bounded raw view
calendar.create_event(proposal, approval_key, *, approved=False) # -> event dict
calendar.update_event(proposal, baseline_event, approval_key, *, approved=False)
calendar.cancel_event(proposal, approval_key, *, approved=False) # -> raw delete data dict
```

`proposal` 是核心的 `Proposal` 对象；`approval_key` 是上游保存的执行审批键。返回的 calendar/event ID 均来自服务端响应。删除接口正常可能返回空对象，不补造“已删除事件”响应。上游应保留请求对应的真实 event ID，并在删除后读取核验其状态。

创建事件将审批键与日历 ID 转为固定 UUID5，传入服务端 `idempotency_key`。同一已批准创建请求的重试必须沿用同一键；不同创建请求必须使用不同键。上游负责持久保存审批键、请求内容及结果映射，不得把一个键复用于改过内容的请求。adapter 不提供跨进程审批存储。

创建成功后真实 event ID 加入实例和持久台账。对外部已建事件，Host在可信执行批准时核对精确基线与支持能力，登记 authorized_events；不冒称本系统创建。单纯读取、同名或相似不构成授权，标题和个人标记不作为许可依据。

执行前基线使用 `FeishuHost.snapshot(range=...)` 的完整有界视图或 `snapshot(event_ids=...)` 的精确目标读取；无参snapshot仅为本地台账清单，不能用于断言日历为空。时间视图包含非本系统创建的记录，发现不完整返回时停止该次基线核对。

Host使用`calendar.prepare_write(proposal, baseline, approval_key, approved=True, authorize_target=...)`完成一次新鲜范围/目标读取、基线比较、授权登记与编码，然后立即调用返回的发送函数。回调仅供可信Host登记已核验精确目标，不向模型暴露，也不是额外授权来源。Host先持久化receipt再准备写入；准备失败记failed_before_write，发送后未收到确认记outcome_unknown，收到确认后读回失败记verify_pending。独立create/update/cancel入口保留；写后读取单独重新核验范围。

修改前应调用 `get_event` 获取新读取的 `baseline_event`，供上游策略或人工授权。adapter 再读取目标事件，拒绝标题、时间、地点、描述、状态、参与人、忙闲或隔离条件已经变化的陈旧审批。支持非空文本description新增/修改；修改备注原样写入，只有完整历史测试编码保留原尾注，核验实际业务文本。仅标题、地点或备注改变时，不发送时间字段；只改开始或结束时，使用原事件另一端组成有效区间，验证结束晚于开始，不猜时长。由于飞书 PATCH 仅在开始与结束同时出现时更新时间，两端都会随该次时间改动提交。当前未实现服务端条件写入；最终读取与 PATCH 间仍可能发生并发编辑。

日期必须是带明确 offset 的 ISO 8601 时间，精确到整秒；adapter 复用核心时间校验并转换成 Unix 秒，明确发送 `timezone: UTC`。无时区、非法 offset、缺少起止时间及无效区间会拒绝执行。此版本不处理全天事件。

## CLI 与失败处理

adapter 直接运行已安装的原生 `lark-cli` 可执行文件，Windows 会检查现有 npm 安装下的原生 exe，避开可能自动安装的 Node 包装器。参数通过 argv 传递，JSON body 用 `--data -` 从 stdin 发送；不使用 `cmd /c` 或 shell 字符串拼接。所有请求显式 `--as user --format json`。

成功必须同时满足退出码 0、顶层 `ok: true`、身份 `user` 和对象类型的 `data`，不判断旧格式 `code: 0`。认证、权限等失败以 `FeishuError.details` 保留结构化错误。退出码 10 抛出 `FeishuApprovalRequired`，保留 `action`、`risk`、`hint` 供上游审查；adapter 不自动追加 `--yes` 或重试。超时、响应损坏或执行失败可能导致写入结果未知，应通过原审批键和真实读取排查。

`FeishuHost`须由调用者显式传入台账路径，不含开发目录默认值；历史开发台账保留原地，不迁移或删除。`threadplan_host.feishu.FeishuHost`将冻结命令存入`receipt_commands`，区分 `failed_before_write`、`in_progress`/`outcome_unknown`、`write_acknowledged`/`verify_pending` 和 `succeeded`。`receipt(key)` 只读本地记录；`recover(key, approved=True)` 不发送创建/修改/删除。已确认写入时重做读取验证；未知 UPDATE/CANCEL 若有可信目标 ID，可读取并核对实际期望状态，成功回执标注 `provider_acknowledged=False` 和读取核对依据，表示接收端未观察到原写入确认，不断言原请求一定成功。未知 CREATE 无 ID、旧版命令信息不足或状态仍不匹配时保持未决，不按标题认领或盲重发。已确认写入后再 `execute` 同一冻结审批也只恢复验证。

完整Skill使用外部用户配置指定独立state_directory，保存agent.sqlite/provider.sqlite，不复用或打包个人台账；执行策略默认read_only，启用范围内预授权后才写入。安装与工具协议见[Skill](../skills/threadplan/SKILL.md)。`python -X utf8 scripts/verify_skill.py`仅预览安装包CLI验收，显式`--run`才对一条标记Test记录连续新增/改期/取消并核验；这是零模型调用的构造工程操作，不计语义任务成功率。

历史 v0.2 真实创建/改期/取消及三阶段只读恢复回执保留，旧业务验收脚本已退役。真实运行必须显式指定`--provider`；当前业务验收入口为 `scripts/verify_planning_v3.py`，恢复工具为 `scripts/verify_calendar_recovery.py`；显式 `--scenario lifecycle` 验证新增/改期/取消，`--scenario cancel-only --cases 1` 单独验证取消，`--scenario existing --cases 2 --max-model-calls 6` 验证外部已建标记TEST事件的改期与取消。`--scenario mixed --provider codex --cases 1 --max-model-calls 3`预览混聊组合验收，显式加`--run`才执行：两条已有测试记录保持，只新增下午安排与备注，读回与重复回执核验后清理。真实写入验收不附加影子复核；旧实验分支源码留在本地归档。默认不因此重复远端操作。实际范围和失败见 [VALIDATION.md](../reports/VALIDATION.md)。

当前续期仅请求既有Test日历所需的五项：`calendar:calendar:read`、`calendar:calendar.event:create`、`calendar:calendar.event:read`、`calendar:calendar.event:update`、`calendar:calendar.event:delete`。创建新日历另需`calendar:calendar:create`，本轮不请求、不创建新日历。历史集成曾在六项权限下验证，不等同当前令牌仍有效；身份保持user。

运行契约测试：

```text
python -X utf8 -m unittest discover -s tests -p test_feishu_adapter.py -v
```

创建日历响应字段不足时按返回 ID 补读；补读失败在异常 details 保留已创建 ID，不能盲目重建。verify_cancelled 仅检查可信受管 ID，接受明确 cancelled 或已观察到的 api/193003/event is deleted；认证与权限失败不能当作删除成功。
