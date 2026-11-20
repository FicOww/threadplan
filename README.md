# ThreadPlan

把聊天里定下来的安排，接到日历里。

ThreadPlan 根据相关聊天或个人记录，结合已有日程，提出新建、改期或取消计划。它可以嵌入 Python 应用，也提供命令行和 Codex Skill。目前已接入飞书日历。

## 一个改期的例子

假设日历里已有一条「方案评审」，时间是周五 14:00–14:30。随后聊到：

> 甲：周五的评审往后挪一下，三点半可以吗？
>
> 乙：行，还是半小时，地点不变。

这时需要找到原来的评审，把它改到 15:30–16:00，保留地点和其他内容。ThreadPlan 会按需查询日历，再结合完整对话判断该改哪一条。线索不足、无法区分候选时，会给出需要确认的问题。

同一段聊天可以包含多件事：已确定的分别形成计划，没说清的留待确认。新建事件没有写时长时默认 30 分钟，也可以在配置中修改；改期默认保留原时长。

## 开始使用

需要 Python 3.11+ 和可用的 IANA 时区数据。ThreadPlan 的 Python 代码只使用标准库，当前主要在 Windows / Python 3.13 下测试。


### 打开工作台

在仓库目录运行 `python -B -X utf8 -m demo`，浏览器打开 `http://127.0.0.1:8765/`。进入「把方案评审改到 15:30」场景，确认后在日历切换「执行前／当前」。

### 构建可分享的单文件

```bash
python -B -X utf8 -m scripts.build_offline_demo --builtin-only
```

双击生成的 `dist/ThreadPlan-离线演示.html` 即可打开。公开源码可独立构建 12 个内置场景。浏览器按已经验收的结果回放模拟日历变更，无需联网、登录或安装 Python。详见[工作台说明](docs/WORKBENCH.md)。

### 使用命令行

```bash
git clone https://github.com/FicOww/threadplan.git
cd threadplan
python -X utf8 -m threadplan --help
```

想先看看输入长什么样，可以运行：

```bash
python -X utf8 examples/planning.py
```

这会打印一份构造聊天的输入示例，不连接模型或日历。实际接入时，消息需要保留真实作者、稳定 ID 和带时区的发送时间，由你的应用提供相关聊天；目前还没有自动采集飞书聊天的功能。

### 在 Codex 中使用

构建完整 Skill 包：

```bash
python -X utf8 scripts/build_skill.py
```

产物是 `dist/threadplan-1.3.0.zip`。解压后放入 Skill 目录，按[首次设置](skills/threadplan/references/setup.md)登录飞书，创建并绑定私有「ThreadPlan」日历。已有绑定可直接沿用。

明确交代一条日程可走[单条入口](skills/threadplan/references/direct.md)，不需要历史消息作者。分析历史聊天仍由来源宿主提供真实元数据。单条写入成功后可凭回执预览地点、备注或个人提醒的补充，本人在终端确认后才更新原条目。不写入其他日历，也不增加长期更新权。

### 从命令行调用

独立 CLI 不使用 Skill 的默认绑定。按[独立 CLI 配置示例](skills/threadplan/references/config.example.json)准备自己的配置，再提供消息 JSON 文件。以下路径是示例，需要换成实际文件：

```bash
# 只读检查飞书连接
python -X utf8 -m threadplan --config ../threadplan-config.json doctor --check-connection

# 分析聊天，返回计划
python -X utf8 -m threadplan --config ../threadplan-config.json analyze --input messages.json --provider openrouter
```

OpenRouter 路线需要 `OPENROUTER_API_KEY`，可放在环境变量或配置文件同目录的 `.env` 中。该路线仅使用固定的 `:free` 模型，并将输入、输出和请求价格上限设为零；缺少密钥或调用失败时不会自动切换其他路线。也可以显式选择 `--provider codex`，使用已有 Codex 登录进行开发推理，会消耗订阅用量。

`analyze` 只返回计划。需要执行时使用 `run`；写入范围由配置中的 `execution_policy` 决定，默认仍为只读。具体字段见[配置示例](skills/threadplan/references/config.example.json)。

### 嵌入自己的应用

`threadplan` 提供消息与操作的数据结构，`threadplan_host` 提供模型循环、日历读取和执行工具。模型回调与日历读取器都可以替换；接入方负责提供消息、用户身份、时区，以及允许操作的日历和动作。

独立调用使用 `ConversationPlanner`；如果已有 Agent 负责理解和决策，可以使用 `AgentSession` 的工具接口。两种方式共用时间计算、目标校验和执行记录，接口见[行为合同](docs/CONTRACT.md)。

## 日历写入

ThreadPlan 先形成计划，再按接入方的授权执行。聊天中的“好，就这么定了”用于判断约定是否成立，操作日历的权限需要单独配置。

修改前会重新读取目标，写入后会读回核对。执行记录用于避免重试时重复创建；遇到结果不明的请求，用原命令的 `status` / `recover` 查询和恢复，恢复过程只读。

目前支持单次定时事件。个人提醒只支持 Skill 的单条直达和回执补充。重复日程、全天事件、会议邀请及消息撤回暂不支持，也不会向他人发送通知。

## 开发

```bash
python -B -X utf8 -m unittest discover -s tests
```

## 更多文档

- [飞书接入](docs/FEISHU.md)
- [数据与评测方法](docs/EVALUATION.md)
- [验证记录](reports/VALIDATION.md)
- [公开仓库包含哪些内容](docs/PUBLICATION.md)
