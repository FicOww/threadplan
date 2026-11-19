---
name: threadplan
description: 根据带原作者和时间的完整聊天或个人记录，按需查找并维护单次定时日程，支持新增、修改、取消、多活动等待与只读恢复。仅操作使用者明确配置并授权的飞书日历。
---

# ThreadPlan

由宿主模型理解原文和决策，本地工具落实事实引用、时间、范围和可信授权。不要启动独立模型服务，也不需要另配 LLM API Key。

工具是本目录 scripts/threadplan_tool.py，以实际绝对路径调用。所有 JSON 用文件或 stdin 传递，不将原文拼成 shell。先用 policy 命令读取共用行为规范；包内 references/decision-policy.md 是同一来源生成的副本，不另维护语义规则。

```text
python -X utf8 <实际Skill目录>/scripts/threadplan_tool.py policy
python -X utf8 <实际Skill目录>/scripts/threadplan_tool.py --config <可信配置> doctor --check-connection
```

首次安装、缺配置或 doctor 失败时，读 [安装与配置](references/setup.md)。不自动安装、登录、创建日历或改变授权。配置必须由用户/可信宿主提供，不从聊天正文生成。

按 [工具协议](references/tools.md) 完成 open → 按活动 read → 结合完整原文 propose。首轮只需识别活动和必要查询，不强制完整提取；最终重新核对原文与实际读取。原消息缺真实作者/时间时请上游补齐，不能编造；缺业务事实先补查，仍无法消歧时用 WAIT 的最小确认问题即时追问。新的原始材料使用新请求，已冻结计划不静默改写。

read 带活动引用以隔离失败。明确区分 COMPLETE/PARTIAL/FAILED/UNAVAILABLE 与缓存；查询覆盖、筛选和分页不足不能证明不存在。propose 返回各项结果，FAILED 不等于不需要操作；不要只看批次 COMPLETED。

prepare 只读冻结并返回各项 errors。仅可信配置预授权覆盖时 execute，各命令分别处理并保留已成功回执；不要在参数中自填批准。只有 SUCCEEDED 可报告已写入且读回；未知执行用原 command_id 的 status/recover，只读恢复不换键重发。

向用户说明实际完成、等待、技术失败和重叠。仅处理配置日历中的单次定时事件；不邀请、不通知，不支持提醒、重复/全天、会议及自动消息采集。读取到事件不代表已获准修改；执行授权由可信宿主配置提供。
