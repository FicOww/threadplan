# 首次设置

将完整的 `threadplan` 文件夹放入宿主 Skill 目录。需要 Python 3.11+、IANA 时区数据和原生 `lark-cli`。Skill 不自动安装、不登录、不申请授权；凭据由飞书 CLI 管理。

1. 运行 `python -X utf8 <实际Skill目录>/scripts/threadplan_tool.py setup-check`。找不到 CLI 就引导用户按飞书 CLI 的安装说明操作；未登录就引导用户以本人身份登录。若返回 `MISSING_SCOPES`，只按返回的缺失范围引导用户授权，完成后再诊断。已有绑定不做这些检查。
2. 设置就绪后，请用户确认：创建一个私有的「ThreadPlan」飞书日历，并允许后续在其中新增日程。由用户在终端运行下面的命令；Agent 不代填授权参数。只有具备独立可信批准机制的宿主才可代用户执行。同一聊天里对某条日程的同意不等于启用长期授权。

```text
python -X utf8 <实际Skill目录>/scripts/threadplan_tool.py initialize --timezone <IANA时区> --allow-event-create
```

`--timezone` 例如 `Asia/Shanghai`。工具核验并保存实际飞书账户、CLI 配置档和新建日历 ID，不按同名匹配。初始化不需要聊天作者 ID；处理消息时由来源宿主另行提供。新增授权不含改期或取消。

绑定保存在用户主目录下的 `.config/threadplan/skill.json`，与运行台账、初始化记录一起留在 Skill 包外。换聊天或升级 Skill 不需要重做。正式 Skill 不读取旧的外部配置，也不切换到主日历或其他日历。保留并妥善备份此目录，勿随包分享。

## 已有绑定或初始化未决

- 已绑定但登录过期、账号变更或日历不可访问：修复原账号与绑定，可按需运行 `doctor --check-connection` 排查；不要删除配置来重试。
- 初始化未决：保留 `skill-initializing.json`。若记录已有日历 ID，用户可用原参数重跑初始化，工具只核验和补绑；若无 ID，先从飞书确认实际创建的日历，再用 `--recover-calendar-id <已核实ID>` 补绑。只有与本次初始化唯一标记一致的日历才会被接纳；无法确认时停止，不重发创建。
- 事件写入结果未知：使用原 `command_id` 和 `status` / `recover`，不要重新提交写入。
