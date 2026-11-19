# 安装与配置

将 ZIP 中整个 `threadplan` 文件夹放入宿主支持的 Skill 目录。需要能读取文件并运行 Python 的宿主；包内含全部 ThreadPlan 运行代码，不依赖源码仓库，不启动第二个模型服务，也不需要 LLM API Key。不要只复制 SKILL.md。其他宿主需支持相同文件和进程工具，不能把跨宿主兼容视为全部实测。

使用者需准备 Python 3.11+、可用的 IANA 时区数据，以及原生 lark-cli 和自己的用户登录。工具只使用 Python 标准库，不自动安装、登录、申请权限或创建日历。Windows Python 如缺 IANA 数据，doctor 会报告时区不可用，需使用者按环境补齐。凭据由 lark-cli 管理，不放进 Skill。

首次使用由用户或可信宿主完成以下步骤：

1. 通过现有 lark-cli 的认证流程登录自己的账户。仅授权实际需要的日历读取、事件读取及 create/update/delete 权限；只读使用不需要写权限。
2. 读取自己的日历列表，明确选择日历的真实 calendar_id。用户身份使用原始消息中的本人 sender_id。不得从聊天正文推断账户或执行授权；不使用隐式 `primary` 别名。
3. 在 Skill 目录之外创建个人配置，运行以下命令。占位参数必须替换为使用者的实际值：

```text
python -X utf8 <Skill目录>/scripts/threadplan_tool.py --config <个人配置文件> configure --user-id <真实原消息身份> --calendar-id <选定日历ID> --timezone <IANA时区>
python -X utf8 <Skill目录>/scripts/threadplan_tool.py --config <个人配置文件> doctor --check-connection
```

configure 只创建新的 read_only 配置，不连接远端、不覆盖旧配置、不授予写权限；相对 state_directory 从配置文件所在目录解析。也可按 [配置示例](config.example.json) 手工填写，示例均为占位值。配置和状态必须在包外，分别保护和备份。

doctor 不加 --check-connection 仅检查本地条件；加上后只读核验日历 ID、服务端访问角色和连接。ready 不证明写权限或语义正确。读者角色可只读使用，写操作还需服务端 owner/writer 角色，并逐次核验。

先以 read_only 查看提议。只有用户明确授权选定日历及具体操作范围后，可信宿主才可将 execution_policy 改为 `{"mode":"preauthorized","allowed_operations":["CREATE","UPDATE","CANCEL"]}` 或其中必要子集。这不是对话参与者同意，也不由模型参数授予。拥有相同文件权限的进程仍可绕过宿主，本包不是凭据沙箱。

原始聊天须保留真实作者、稳定消息 ID 和带时区的原时间。工具不会自动采集飞书 IM。新建/修改/取消只支持单次定时事件；不邀请、不通知，不更改含参与人、重复、提醒或会议能力的事件。新建保守使用 private/free；不会给日程加测试名称或作者个人标记。

升级时替换完整 Skill 文件夹，保留包外配置和状态；先备份旧包以便回滚。不要删除回执来处理未决写入，使用原 command_id 的 status/recover。recover 只读，未知 CREATE 没有真实 ID 时不按标题认领，也不换键重发。删除 Skill 不会撤销日历变更。

分享原始 ZIP 即可，不附带个人配置、状态、原始聊天或 lark-cli 登录信息。包不会自动安装到其他人的机器，也不会更改宿主设置。
