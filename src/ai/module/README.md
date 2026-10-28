# 模块系统（src/ai/module）

> 改代码前先读本文件。这里只放"模块机制"和"业务模块"，不放对话编排（那属于 brain）。

## 1. 目录结构

```
src/ai/module/
├── module_hub/
│   ├── module_hub.py        # ModuleHub：发现/开关/钩子调度/配置与状态
│   └── module_context.py    # ModuleContext：模块钩子能用到的一切能力
├── lib/
│   ├── message_store.py     # 消息库纯工具（读写 messages.md / 打码 / 群名校验），无模块状态
│   ├── memory_store.py      # 长期记忆纯工具（key 解析 / memory.md 读写 / 遗忘曲线 / 本地匹配）
│   └── identity_store.py    # 身份库纯工具（QQ 号 ↔ 真名 / 固定档案），无模块状态
├── modules/
│   ├── persona.py           # 人格文本（system 片段）
│   ├── identity.py          # 当前发言人的固定档案（system 片段）
│   ├── chat_style.py        # 输出格式约束（纯文本、不写括号动作）
│   ├── current_time.py      # 当前时间（末尾 user 片段，每轮刷新）
│   ├── wake.py              # 这轮该不该我说话
│   ├── recent_memory.py     # 最近 N 轮：自己写 messages.md，自己读出来注入
│   ├── long_memory.py       # 长期记忆：读 messages.md、增量总结进自己的 memory.md，按遗忘曲线检索
│   ├── reply_split.py       # 回答分句
│   ├── ai_typing_delay.py   # 打字延迟
│   ├── proactive.py         # 主动消息条件中心
│   └── qq_channel.py        # QQ 群通道：OneBot 11 上报接收 + API 发送
└── README.md
```

- 模块文件路径固定：`src/ai/module/modules/<name>.py`，模块名必须等于文件名；
- 新增模块 = 建一个 `.py` + 改配置，`module_hub` 一行不用改。

## 2. 职责边界（先看这个，再看代码）

| 层 | 只负责 | 绝对不做 |
| --- | --- | --- |
| **Brain** | 流程：判唤醒 → 通知模块 → 取片段 → 组 prompt → 调模型 → 分句 → 入队 → 送达后回调 | 不写任何业务文本（人格/风格/记忆/延迟都来自模块） |
| **ModuleHub** | 加载、开关、顺序、异常隔离、状态回写、把 `ctx` 递过去 | 不解释 `ctx` 里的内容，不 import `openai` |
| **Module** | 所有业务知识与自己的文件 | 不 import 别的模块，不 import `brain` |
| **lib** | 无状态纯工具（可被任意模块 import） | 不保存模块运行态、不写 `configs/` |

五条铁律：

1. 模块之间禁止 `import`、禁止互相调用，只能通过「数据契约」文件见面；
2. `brain.py` 里不允许出现业务名词（persona / style / memory / delay / wake）；
3. `hub` 不解释内容，只做机制与调度；
4. 模块不自己拼 `configs/` 路径；`data/` 路径用 `ctx.message_path` + `lib` 拼；
5. 一个文件只能有一个写入方；读取方必须能容忍文件缺失（读不到 → 返回空，不抛错）。

验收标准：**所有模块开关全关时，`main.py` 仍能启动、仍能聊天**（只是没有人格、没有记忆、不分句、不延迟）。

## 3. 配置约定

| 文件 | 放什么 | 说明 |
| --- | --- | --- |
| `configs/modules.json` | **只有开关**：`{"persona": true, "memory": false}` | 没列出来的模块一律不加载（白名单）；键的先后顺序 = 执行顺序 |
| `configs/modules/<name>.json` | 该模块的**详细配置 + 运行态** | 结构见下，缺文件等价于空配置 |
| `configs/config.json` | **程序级配置**：`base_url` / `api_key` / `model` / `message_path` | 全局唯一；`message_path` 是消息库**根目录**（默认 `data/groups`），每个群一个 `<根>/<群>/messages.md`；相对路径按项目根解析，不进模块配置 |

```json
{
  "config": { "path": "prompts/persona.md" },
  "state": { "last_written": "2026-10-24" }
}
```

- `config`：模块自己的参数，模块内通过 `ctx.params` 读取（hub 不解释内容）；
- `state`：运行态，模块通过 `ctx.state` 读写，钩子结束后 hub 自动回写（只在内容变化时写盘）；
- 两个键都是可选的，缺失按 `{}` 处理；文件顶层必须是 JSON 对象；
- hub 回写时会重排整个 JSON（`indent=2`），手写的排版不会保留；**不要手改 `state`**。

开关也兼容 `{"persona": {"enabled": true}}` 的写法，但推荐直接用 `true` / `false`。

## 4. 模块契约

模块是一个普通 `.py` 文件，按需实现下面任意钩子，**全部只接收一个 `ctx`**：

| 钩子 | 签名 | 何时被调用 | 用途 |
| --- | --- | --- | --- |
| `should_wake` | `should_wake(ctx) -> bool` | 每轮对话开始 | 这轮要不要回；任一模块返回 `False` 即休眠（只记录不回复） |
| `build` | `build(ctx) -> Fragment` | 组装 prompt 时 | 拼接型内容（人格、规则、风格、记忆）；空串/空片段表示不贡献 |
| `split` | `split(ctx) -> list[str]` | 拿到回答后 | 把一次回答拆成多条消息；第一个返回非空列表的生效 |
| `plan_delays` | `plan_delays(ctx) -> list[float]` | 入队前 | 每段发送前等几秒，读 `ctx.segments` |
| `on_start` | `on_start(ctx) -> None` | 程序启动 | 初始化、自检 |
| `on_user` | `on_user(ctx) -> None` | 收到用户输入 | 落库、计数、打标签 |
| `on_reply` | `on_reply(ctx) -> None` | 回复**真正送出后** | 落库、触发总结（旁路请求写这里） |
| `tick` | `tick(ctx) -> None` | 主循环周期性调用 | 定时任务（总结、清理） |
| `proactive` | `proactive(ctx) -> dict \| None` | 每次调度 | **提名**一条主动消息（只判断、不生成、不发送）；需要 `rule_id` / `attempt_id` / `instruction` 或 `text` |
| `on_proactive` | `on_proactive(ctx) -> None` | 主动消息执行完 | 读 `ctx.result`：`sent` 提交冷却、`error` 记退避、`skipped` 不计冷却 |

`build` 返回的片段（`Fragment`）三种形态，role 不限制（`system` / `user` / `assistant`…都可以）：

```python
"文本"                                              # 等价 system 消息
{"role": "user", "content": "检索到的记忆：..."}      # 指定 role
[{"role": "system", "content": "A"}, {"role": "user", "content": "B"}]   # 一次给多条
```

组装规则（`src/ai/prompt/prompt_construct.py`）：所有 `system` 片段合并成**一条**放最前 → 其它 role 片段（按模块顺序）→ 本轮用户输入。空内容片段丢弃。

会话窗口与长期记忆都归模块：`recent_memory` 自己写、自己读 `messages.md`；`long_memory` 只读 `messages.md`、只写自己的 `memory.md`，不改 Brain / Hub / 其它模块。

## 5. `ctx` 提供什么

| 成员 | 说明 |
| --- | --- |
| `ctx.params` | 该模块的 `config`（dict） |
| `ctx.state` | 该模块的 `state`（dict，可直接改，hub 自动回写） |
| `ctx.ask(messages, tag="", **options)` | 调用模型（旁路，不进对话主线），走 hub 注入的 `model_caller`（通常是 `post`）；`**options` 透传，如 `response_format={"type": "json_object"}` |
| `ctx.transcript(n=20)` | 最近 n 条对话消息（由 hub 注入的 provider 提供） |
| `ctx.message_path` | 消息库根目录（`configs/config.json` 的 `message_path`，由 hub 注入）；用 `message_store.message_file(root, group)` 拼出本群文件 |
| `ctx.user_text` / `ctx.reply` / `ctx.now` | 本次触发信息 |
| `ctx.phase` | `chat`（普通回复）/ `proactive`（主动开口）；由 Brain 告知，**模块据此决定内容** |
| `ctx.segments` | 本轮待发送的分段（仅 `plan_delays` 钩子有值） |
| `ctx.speaker` / `ctx.group` / `ctx.mentioned` | 群聊场景的身份信息（单人为空 / 默认 `mentioned=True`）；`speaker` 已由 `qq_channel` 按 QQ 号解析成**真名** |
| `ctx.private` | 本轮是 1 对 1 私聊（默认 `False`）。注意 `group` 不是"是否群聊"的判据：私聊也有记忆目录（`private-<QQ>`） |
| `ctx.conversation` | 只读会话快照（`dormant` / `started_at` / `last_user_at` / `last_reply_at` / `last_proactive_at`），仅 `proactive` 钩子可用 |
| `ctx.result` | 主动消息执行结果（`status` / `rule_id` / `attempt_id` / `text` / `reason` / `sent_at`），仅 `on_proactive` 钩子可用；`sent_at` 是真实送达时刻，记冷却用它 |
| `ctx.log_info(msg)` / `ctx.log_error(msg)` | 自动带 `[模块名]` 前缀的日志 |
| `ctx.save_state()` | 立刻回写状态（一般不用，钩子结束会自动写） |

模块**禁止**：`import openai` 自建客户端、`import brain`、自己往 `configs/` 里拼路径、把系统日志写到别处。

**输出契约**：一次模型请求只允许一种输出形态。要人类可读文本的（persona / 风格）走 `build`；要机器可读结构的（摘要、标签）必须在自己的钩子里用 `ctx.ask` 发**独立请求**并自带解析，绝不与主对话共用同一次输出。

## 6. 数据契约（谁写谁读）

| 文件 | 唯一写入方 | 读取方 | 写入方缺席时 |
| --- | --- | --- | --- |
| `<message_path>/<会话>/messages.md` | `recent_memory` | `recent_memory`、`long_memory` | 读不到 → 返回空片段，不报错 |
| `<message_path>/<会话>/memory.md` | `long_memory` | `long_memory` | 读不到 → 视为没有任何长期记忆 |
| `<message_path>/_global/memory.md` | `long_memory` | `long_memory` | `global_memory=true` 时所有会话共用这份 |
| `configs/identity.json` | **人**（声明式名单，运行时只读） | `identity`、`qq_channel` | 读不到 → 发言人沿用群名片、没人能私聊、档案不注入 |

`<会话>` 是群号（`949207129`）或私聊标识（`private-1027713979`）：**群聊与私聊各存一套**，
互不串味。`speaker` 一律写**真名**（由 `qq_channel` 按 QQ 号解析），所以 `messages.md` 与
`memory.md` 的 key 前缀不会因为某人改群名片而分裂。

消息格式（一行一个 JSON，只追加不改写）：

```json
{"time": "2026-10-26 14:45:41", "role": "user", "speaker": "张天宇", "content": "在吗"}
{"time": "2026-10-26 14:45:43", "role": "assistant", "speaker": "我", "content": "在"}
```

## 7. 加一个新模块（4 步，不改 hub）

1. 建 `src/ai/module/modules/<name>.py`；
2. 写需要的钩子，例如：
   ```python
   def build(ctx):
       return "你说话要简洁。"
   ```
3. 建 `configs/modules/<name>.json`（可省）：
   ```json
   { "config": { "top_k": 3 }, "state": {} }
   ```
4. 在 `configs/modules.json` 加开关：
   ```json
   { "persona": true, "<name>": true }
   ```

启动后 `core.log` 会出现 `模块系统就绪, 已启用模块: persona, <name>`。

## 8. 加载与调度规则

- **白名单**：只加载 `modules.json` 里为 `true` 的模块，目录里存在但没登记的文件不会被执行；
- **顺序**：按 `modules.json` 中键的出现顺序执行 `build`，先出现的在 prompt 更前面；
- **缓存**：模块对象进程内只 import 一次，`hub.reload()` 只重读开关与配置文件，不重复执行模块代码；
- **隔离**：单个模块加载失败 / 钩子抛异常 → 记 `core_log.error` 并跳过，不影响其它模块和主流程；
- **状态**：钩子执行完对比快照，变了才写 `configs/modules/<name>.json`；
- **注入**：模型调用与对话记录由调用方（brain / 入口脚本）注入，hub 本身不 import `openai`：
  ```python
  hub = ModuleHub(model_caller=post, transcript_provider=brain.transcript)
  fragments = hub.collect_fragments(user_text, phase="chat")   # 各模块 build 的原始片段
  messages = prompt_construct(fragments, user_text)            # 拼接（Brain 不额外加文本）
  delays = hub.plan_delays(segments)                           # 打字延迟（ai_typing_delay）
  hub.on_user(text); hub.on_reply(reply); hub.tick()            # on_reply 在回复送达后调用
  requests = hub.collect_proactive(now, snapshot)              # 主动消息：只提名
  hub.notify_proactive(result, now)                            # 执行结果回抛给模块
  ```
  `hub.build_prompt()` 已删除：拼接片段请用 `collect_fragments()` + `prompt_construct()`，
  避免丢失 role 信息。

## 9. 现有模块

### persona（`src/ai/module/modules/persona.py`）
- 人格文件默认 `prompts/persona.md`（UTF-8，可带 BOM），可在 `configs/modules/persona.json` 用 `config.path` 覆盖，相对路径按项目根解析；
- `get_persona(path=None) -> str`：返回去掉首尾空白（含 BOM、末尾换行）的正文，内部换行与缩进保留；每次调用重新读文件；
- `build(ctx) -> str`：hub 钩子，等价于 `get_persona(ctx.params.get("path"))`；
- 空文件返回 `""`；文件不存在 / 读取失败 → `core_log.error` + `RuntimeError`（消息含路径与原因）。

### identity（`src/ai/module/modules/identity.py`）

把**当前发言人**的固定档案拼进 prompt（`[成员档案]` 片段）。真名、生日、出生地、忌口这类
"几乎不变、答错代价高"的事实由人写进 `configs/identity.json`，不交给模型从对话里总结；
变化快的内容仍归 `long_memory`。配置（`configs/modules/identity.json`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `people_path` | `configs/identity.json` | 身份表位置，相对路径按项目根解析 |
| `inject_profile` | `true` | 关掉后 prompt 里不再出现档案（`qq_channel` 的认人不受影响） |
| `max_chars` | `300` | 片段字符上限，超出截断 |

身份表格式（纯工具在 `lib/identity_store.py`，带 mtime 缓存，改文件后下一条消息即生效）：

```json
{"people": [
  {"name": "王俊宇", "qq": "1027713979", "enable": true,
   "profile": {"生日": "2005-03-14", "出生地": "湖南岳阳"}}
]}
```

- `name`：真名，`messages.md` / `memory.md` 里只出现它；
- `qq`：认人主键，**唯一**的身份依据——改名、换群名片都不影响；没有 QQ 的人认不出来，
  只能沿用群名片（通道会提示补登记）；
- `enable`：是否在白名单里，缺省 `true`。`false` = **这个人发来的消息一律不接，群聊私聊
  都适用**（临时闭麦某个人就把它改成 false，改完下一条消息即生效）；
- `profile`：几乎不变的固定事实，只有当前发言人会被注入（别人的生日不会灌给模型）；
- 查不到 / 文件缺失 / JSON 损坏 → 返回空，不抛异常（发言人沿用群名片）。

`enable: false` 与"没登记"不是一回事：没登记的人在群里仍按群名片收，只是认不出真名、
不能私聊；`enable: false` 的人连群消息都不接。`identity` 的 `on_start` 会打印
「N/M 人启用」，停用了谁一眼能看见。

身份库**不存别名**：别名映射本身是一种猜测（两个人都叫"网格"时谁是真的？），留着只会让人
被认错。历史数据里的旧名字用 `scripts/migrate_identity.py --map 旧名=真名` 显式改。

### chat_style（`src/ai/module/modules/chat_style.py`）
聊天软件的**输出格式**约束，只靠 prompt，不在程序里改写回复。配置：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled_phases` | `["chat", "proactive"]` | 只在列出的场景生效；空数组表示全场景 |

- `ctx.phase == "chat"` → 返回 `STYLE_RULES`（纯文本、不写括号动作、不堆语气词）；
- `ctx.phase == "proactive"` → `STYLE_RULES` + `PROACTIVE_RULES`（主动开口的额外约束）；
- 这正是"风格从 Brain 搬出来"的落点：关掉它，回复不再受格式约束，但对话照常。

### current_time（`src/ai/module/modules/current_time.py`）

每轮在 prompt **末尾**注入一条 `user`：`当前真实时间为：2026-10-27 21:33`，让模型答得出
"今天几号 / 现在几点"。配置（`configs/modules/current_time.json`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `format` | `%Y-%m-%d %H:%M` | `strftime` 格式；只到分钟（秒会让 prompt 每轮都不同） |
| `template` | `当前真实时间为：{time}` | 整句模板，`{time}` 会被替换；写错退回默认 |

- 时间取 `ctx.now`（Hub 每轮写入），没有才退回系统时钟；
- **为什么不是 system**：所有 system 片段会被合并成一条放最前面，和人格、档案挤在一起，
  prompt 一长模型对它的注意力就弱（实测问"现在几点"答不出来）；`user` 片段按
  `prompt_construct` 的规则排在历史之后、本轮输入之前，正好贴着用户这句话；
- 在 `modules.json` 里排**最后**，保证它是末尾上下文的最后一条（只在本轮输入之前）；
- 为什么不塞进 `persona`：那里是稳定设定，塞日期会让 system 前缀天天变（缓存全废），
  也会把"现在几点"写成长期设定。

### wake（`src/ai/module/modules/wake.py`）
"这轮该不该我说话"。纯计算、零 I/O。配置：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `require_mention` | `true` | 群聊里必须被 @ 或叫到名字才回 |
| `names` | `[]` | AI 的名字/别名；群聊没 @ 时按名字兜底判定 |
| `private_always` | `true` | 私聊（`ctx.private` 或没有 `group`）是否总是回复 |

私聊排在最前面判且**不看 `group` 是否为空**：私聊同样有记忆目录（`private-<QQ>`），只看
`group` 会把它当群聊——那样正文里出现一个 `@`（"我邮箱 a@b.com"）就会被判成"@了别人"，
整轮不回复。

- 判定顺序：单聊 → 被 @ → `require_mention=false` → 文本含名字；任一命中即回；
  「被 @」优先认正文里的 `@名字`，正文里没有 `@` 但平台已标记 `mentioned`（QQ 的 @ 是独立消息段）时信平台；
- 名字放本模块配置，**不读 `persona.md`**（否则两个模块互相耦合）；
- 删掉它：Hub 默认"有问必回"，也就是单聊行为，不会崩。

### reply_split（`src/ai/module/modules/reply_split.py`）
把一次回答拆成多条短消息，`split(ctx) -> list[str]`。配置（`configs/modules/reply_split.json`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `max_chars` | 25 | 每段最多字符数（超长句优先在 `，、：；` 处切，断开处标点丢弃；无标点才硬切） |
| `max_sentences` | 3 | 每段最多句数 |
| `terminators` | `。！？!?；;…` | 句末标点集合 |

换行视为分段边界（一个自然段至少单独成条）；可单独复用：`split_sentences()` → `group_segments()` → `split_reply(text, ...)`。只切分不改写。

### ai_typing_delay（`src/ai/module/modules/ai_typing_delay.py`）
计算每段回复该等多久（不参与 prompt）。配置：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `msg_delay_base_range` | `[5, 10]` | 每条消息的基础延迟（秒，随机） |
| `msg_delay_per_char_range` | `[0.4, 0.8]` | 每条消息里每个非标点字符的延迟（秒，随机） |
| `msg_delay_max` | `30` | **单条消息**的延迟上限（秒） |

- 钩子：`plan_delays(ctx)`，分段在 `ctx.segments`，参数取 `ctx.params`（换了配置目录也跟着换）；
- 纯函数：`plan_for(segments, params=None)`、`delay_for(text, params=None)`，不传 `params` 时读配置文件；
- 口径：三个值都作用在**分割后的一条消息**上 —— 每条 `min(base + per_char × 该条有效字数, max)`，各自独立随机；
- 一次回答拆成 N 条就等 N 次，**总等待是各条之和，不做整体封顶**；
- 标点与空白不计字数。

### recent_memory（`src/ai/module/modules/recent_memory.py`）
最近 N 轮对话的**完整闭环**：自己写、自己读。配置：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `history_rounds` | 20 | 注入最近 N 轮（成员 + AI，不含本轮），0 = 关闭 |
| `max_chars` | 1200 | 片段字符预算，超出丢旧留新 |
| `max_line_chars` | 200 | 单行截断长度，0 = 不截断 |
| `record` | `true` | 是否记录新消息；关掉就是只读窗口 |
| `privacy_block_patterns` | `[]` | 追加隐私规则（内置规则始终生效） |

- `on_user` 追加 `role=user` 行（休眠轮也记），`on_reply` 在**回复真正送出后**追加 `role=assistant` 行；
- `build` 读最近 N 轮，**逐条**还原成 `{"role": "user"|"assistant", "content": ...}` 消息列表：
  成员发言带 `名字：` 前缀（群里要分清谁说的），AI 自己的话不加前缀。本轮输入已被 `on_user`
  写入，`build` 按内容跳过，避免和 prompt 末尾重复；
- 不再拼 `[最近对话]` 大段文本：role 本身表达"谁说的、说过几轮"，模型不用从 `- 我：`
  这种文字前缀里去猜；
- 消息库为 `<ctx.message_path>/<群>/messages.md`；群取 `ctx.group` > `config.group` > `default`；
- 任何异常都返回空串，窗口坏了不影响回复。

### long_memory（`src/ai/module/modules/long_memory.py`）
长期记忆：只读 `messages.md`，增量总结后写自己的 `memory.md`，按艾宾浩斯曲线衰减 / 强化。
纯工具在 `src/ai/module/lib/memory_store.py`（可脱离 `ctx` 单测）；
语义检索在 `src/ai/module/lib/embedding.py`（本地 ONNX 小模型，缺模型/缺依赖自动回退）。

| 钩子 | 做什么 |
| --- | --- |
| `on_start` | 校验记忆库路径，打印「可用 / 共」条数 |
| `on_user` | 未总结轮数 +1（O(1)，不读文件） |
| `build` | **零模型请求**：本地匹配 → 注入 `[长期记忆]`，命中的就地强化 |
| `tick` | 遗忘扫描；满足「轮数或时间」时增量总结建库（唯一 `ctx.ask` 点） |

key 一律三段 `{用户名}.{属性}.{次级属性}`：用户名取消息行的 `speaker`（空/`对方` 落到
`default_user`），属性限定 `身份/喜好/厌恶/关系/经历/约定/习惯/计划/其他`（枚举外归"其他"），
次级属性是具体主题。配置（`configs/modules/long_memory.json`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `global_memory` | `false` | 所有私聊/群聊**共用一份**记忆库：`<message_path>/_global/memory.md` |
| `memory_whitelist` | `[]` | 允许**读且写**记忆的会话：群号 / `private-<QQ>`，也可只写 QQ；留空 = 不限制 |
| `summarize_rounds` | 10 | 未总结轮数达到该值即总结 |
| `summarize_interval_minutes` | 30 | 距上次总结超过该分钟数且有待总结消息即总结 |
| `max_increment_messages` | 200 | 单次最多读入的增量消息行数 |
| `prompt_path` | `prompts/memory_extract.md` | 提炼提示词，相对路径按项目根解析 |
| `inject_top_k` / `inject_max_chars` | 3 / 600 | 每轮最多注入几条 / 片段字符预算 |
| `min_score` | 0.8 | 字面匹配分门槛：`实体×1.0 + 属性×0.8 + 次级属性×1.0`（次级属性只被 summary 字面覆盖时按 0.6 计） |
| `enabled_phases` | `["chat"]` | 只在列出的场景注入（主动开口不注入） |
| `recall_growth` | 1.8 | 每次命中稳定度乘数（封顶 `stability_max_hours`） |
| `stability_base_hours` / `stability_max_hours` | 24 / 720 | 稳定度基准 / 上限（小时） |
| `importance_factor` | 3 | 重要度对初始稳定度的系数 |
| `forget_threshold` | 0.25 | 可提取度低于该值即遗忘（免疫记忆除外） |
| `immortal_importance` / `immortal_floor` | 0.8 / 0.4 | 免疫遗忘的重要度门槛 / 排序时 `R` 的下限 |
| `default_user` | `默认用户` | 没有发言人时的实体名（建库与检索两侧同口径） |
| `attribute_aliases` | `{}` | 追加属性别名，如 `{"喜好": ["上头"]}` |
| `embedding_enabled` | `true` | 语义检索总开关；关掉 = 只用字面匹配 |
| `embedding_model_path` | `embedding/bge-base-zh-v1.5.onnx` | 本地 ONNX 模型（相对项目根）；换模型要同时改分词路径 |
| `embedding_tokenizer_path` | `embedding/bge-base-zh-v1.5.tokenizer.json` | 分词文件 |
| `embedding_cache_name` | `memory.vec.npz` | 向量缓存，与 `memory.md` 同目录，可随时删 |
| `embedding_min_cos` | 0.55 | 语义补召回的相似度门槛（只补最像的那一条，且必须过身份闸） |
| `embedding_min_margin` | 0.21 | 最像的一条要比"全库平均相似度"高出这么多；0 关闭 |
| `embedding_max_query_chars` / `embedding_max_memory_chars` | 256 / 200 | 送模型的文本截断长度 |

- **全局记忆**：`global_memory=true` 时只改记忆库位置，消息库仍按会话隔离——谁说的话还在各自
  `messages.md` 里，只有**提炼结果**进同一个 `_global/memory.md`。各会话的 `cursor` 仍按会话记，
  所以不会互相"跳过"没总结的消息；
- **聊天白名单**：`memory_whitelist` 同时约束 `build`（读）与 `tick`/`on_user`（写与计数）。
  不在名单里的会话照常聊天，只是**记忆完全空白**（既不注入、也不提炼）；留空 = 所有会话都允许，
  升级后行为不变。只写 QQ 也能命中 `private-<QQ>`；
- 两者可以叠加：全局记忆 + 白名单 = 只有名单里的会话读写那份共享记忆。

检索面 = **key 三段 + summary 字面 + 语义相似**：模型给第三段起的名字不稳定（同一事实会写成
`美式咖啡` / `咖啡` / `早晨喝美式`），所以 summary 里的双字词也参与匹配（权重 0.6，需配合
实体或属性才够门槛；"数字 + 时间/量词"的双字词不参与，避免 `周三` 被 `每周三次` 命中）。
同一实体、同一属性下的近义 key 会在写入时合并（`memory_store.find_similar`），避免同一事实被
拆成多条、遗忘不同步。

语义检索只在"字面没命中、但说话人（或被点名的人）对得上"时补召回：说话人已知时，别人的记忆
必须被点名才会进候选，属性词不再单独开闸（否则 A 说"我喜欢手冲咖啡"会把 B 的咖啡捞进来）。
补召回只从过闸门的候选里取**最像的一条**——先取全局最像再判闸门，会让别人的记忆把本人那条挤掉。
模型或分词文件缺失、依赖没装、推理出错都会静默退回字面匹配（`lib/embedding.py` 不抛异常）。
依赖：`pip install onnxruntime tokenizers`。向量缓存只补算新增/改过的记忆，日常一轮没有编码开销。

模型选择（16 条短句样本实测）：`bge-base-zh-v1.5`（768 维，388MB，单句约 9ms）在同阈值下比
`bge-small-zh-v1.5`（512 维，90MB，单句约 2ms）多召回 1–2 条且不增加误召，故配置默认用 base；
要换回 small 只改 `embedding_model_path` / `embedding_tokenizer_path` 并把 `embedding_min_cos` 降到 0.55。
两个模型对短口语句的绝对相似度都不高（0.4–0.56），所以这一层定位是"兜底召回"，不是主力。

阈值来历（64 条校园记忆库 + 25 条无关闲聊句实测）：无关句的 max-cos 中位 0.505、p90 0.604、
最高 0.648，而真正的改述正例是 0.568–0.660 —— **绝对相似度分不开噪声和正例**，单靠阈值要么
漏正例、要么放进闲聊。改成"绝对门槛 0.55 + 比全库均值高 0.21"（`embedding_min_margin`）后，
25 条闲聊里语义层一条都没再召（噪声句的突出度普遍 0.10–0.20，正例 0.22–0.29），正例保留 4/5。
两个参数是配套的：只调低 `min_cos`、把 `margin` 归零，误召立刻回到 19/25。

遗忘口径：`R = exp(-Δt_hours / stability_h)`；命中一次 `recalls +1`、稳定度 `×recall_growth`；
`R < forget_threshold` 且 `importance < immortal_importance` → `status=forgotten`（记录保留、
不再检索）。记忆库 `<message_path>/<群>/memory.md` 一行一条 JSON，全量原子重写，不删记录。

### proactive（`src/ai/module/modules/proactive.py`）
主动消息的"条件中心"：只决定**什么时候可以开口**，文案生成与发送都交给 `Brain`（保证 persona / 风格 / 记忆一致）。
配置（`configs/modules/proactive.json`）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `quiet_hours` | `[]` | 免打扰时段，支持跨天 |
| `max_per_hour` | 2 | 每小时最多主动几条；0 = 不限 |
| `failure_backoff` | `[300,900,1800]` | 失败后的重试退避（按次数递增） |
| `rules[]` | — | 规则列表，按 `priority` 从高到低匹配，命中一条即停 |

单条规则：`id` / `type` / `enabled` / `priority` / `window` / `cooldown` / `expires_in` / `instruction` 或 `text` / `params`。
内置条件类型：`schedule`（时段触发）；新增类型只需 `register_condition(name, handler)`，不用改 Brain / Hub。
当前规则：`morning`（08:00–09:30）、`night`（22:30–23:30），各自 24h 冷却。

发送结果回抛后才推进冷却：`sent` 提交、`error` 退避、`skipped` 不计冷却。主动消息的落库同样由记录类模块负责（`on_proactive`）。

### qq_channel（`src/ai/module/modules/qq_channel.py`）

QQ 通道（群聊 + 私聊），按 **OneBot 11** 实现"上报接收端 + API 调用端"。QQ 登录由协议端
负责（NapCatQQ / LLOneBot / Lagrange.Core），换协议端不用改代码：协议端登录后主动上报事件，
Python 侧直接收发，不需要额外的适配进程。

- 收：协议端 `POST /`（也接受 `/event`），请求头 `X-Signature: sha1=<HMAC-SHA1(secret, body)>`
  按 OneBot 标准验签 → 解析消息段 → 按 `message_type` 分群聊 / 私聊；
- 发：宿主调 `send_text(target, text)`，`target` 是群号或 `private-<QQ>`，内部路由到
  `send_group_msg` / `send_private_msg`；用**消息段数组**而不是字符串，避免正文里的
  `[CQ:...]` 被协议端当指令解析；
- 宿主接口：`next_event` / `send_text` / `ignored_groups` / `denied_privates` /
  `unknown_senders` / `shutdown` / `status`。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `bind_host` / `bind_port` | `127.0.0.1` / 8786 | 只允许回环；`0` 表示随机端口 |
| `secret` | `""` | 与协议端的上报密钥一致；留空不校验（启动会告警） |
| `api_base` | `http://127.0.0.1:3000` | 协议端的 OneBot HTTP API 地址 |
| `access_token` | `""` | 调用 API 的 token（`Authorization: Bearer`），可为空 |
| `enable_group` / `enable_private` | `false` / `true` | 是否接收群消息 / 私聊（默认只开私聊） |
| `group_whitelist` | `[]` | 允许的群号；空 = 全部拒绝 |
| `private_sub_types` | `["friend"]` | 接受的私聊子类型；`group`（群临时会话）默认拒 |
| `private_merge_window_seconds` | 2.5 | 私聊连发合并窗口（秒）：静默这么久就把这几条合成一条；0 = 关闭 |
| `dedup_ttl_seconds` / `max_body_bytes` / `send_timeout` | 600 / 65536 / 10 | 去重窗口、请求体上限、发送超时 |
| `proactive_conversation_id` | `""` | 主动消息目标（群号或 `private-<QQ>`）；留空 = 丢弃 |
| `identity_path` | `configs/identity.json` | 身份库位置（相对项目根）；留空 = 用默认 |

约定：

- **两条链路各有开关**：关掉的那侧消息直接 `ignored`（`group_disabled` / `private_disabled`），
  不解析、不入队；非 `message` 事件（心跳等）也忽略；
- **私聊白名单就是身份库**：`identity.json` 里登记过、且 `enable` 为真的 QQ 才能私聊，其余
  直接忽略并进 `denied_privates()`（`QQ → 未登记 / 已停用`）——既挡住陌生人烧 token，也保证
  每个聊起来的人都认得出是谁；
- **`enable: false` 是黑名单**：这个人发来的群消息也会被忽略（`sender_blocked`），与私聊同一
  套口径；只是没登记的人不受影响，群里仍按群名片收；
- **私聊必回**：私聊没有 @ 的概念，事件固定 `mentioned=True`，并带 `chat_type="private"`；
  宿主把它转成 `Brain.chat(private=True)`，`wake` 先判 `ctx.private` 再走群聊规则；
- **私聊连发合并**：人在私聊里常"一句一句发"，通道先攒 `private_merge_window_seconds` 秒，
  静默后把同一会话的几条按 `\n` 合成一条（`event["merged"]` = 条数）再交给宿主，避免
  同一段话被回三遍。窗口每来一条就重置；不同会话（群消息、别人的私聊）不合并、按原顺序
  放回。**这一步放在通道侧而不是模块**：模块钩子是被动调用的，没有"延迟投递"的能力；
- **@ 判定**（仅群聊）：数组格式看 `at` 段的 `qq` 是否等于 `self_id`，字符串格式（CQ 码）看
  `[CQ:at,qq=self_id]`；剥掉 @ 后的纯文本进 prompt，`mentioned` 交给 `wake`；
- **文本消息段拼接**：只取 `text` 段，图片/语音等非文本段丢弃，整条没有文本就不入队；
- **按 QQ 号认人**：`speaker` = 身份库（`identity_path`）里这个 QQ 号的真名；群里没登记就沿用
  群名片，并把 QQ 号记进 `unknown_senders()`。`main_qq.py` 会把未登记的 QQ 号打一行到终端，
  补进 `identity.json` 后下一条消息即生效（不用重启）；
- **会话标识即记忆目录名**：群是纯数字群号，私聊是 `private-<QQ>`，非法时才退化成
  `qq-<hash>`。所以**私聊与群聊的记忆天然分开**（`<message_path>/private-<QQ>/`）；
- **单实例**：关掉了 `SO_REUSEADDR`（`_Server.allow_reuse_address = False`）。Windows 下
  它允许两个进程同时绑 8786，结果两个 `main_qq.py` 各自建一个日志目录、抢着处理同一份
  上报；现在第二个进程会直接 bind 失败并退出；
- 协议端侧只需配两处：OneBot 11 HTTP 服务（默认 3000）与事件上报地址
  `http://127.0.0.1:8786/`；上报密钥填 `secret`，access token 填 `access_token`。

## 10. 测试

全部离线，不需要 API key：

```bash
python -m unittest discover -s tests -v      # 单元 + 契约 + 整链路（假模型）
python -m pytest tests -q                    # 同一套用例（conftest 只补 sys.path）

python scripts/persona_group_test.py --mock  # 端到端脚本，假模型
python scripts/chat_style_test.py --mock
python scripts/persona_group_test.py         # 真实 API（会花钱，日常不跑）
python scripts/long_memory_test.py --group 长期记忆测试-复测   # 长期记忆全链路（真实 API）

python scripts/migrate_identity.py --map mita=wjy                       # 身份迁移：预演，只打印
python scripts/migrate_identity.py --map mita=wjy --group 949207129     # 只处理一个群
python scripts/migrate_identity.py --map mita=wjy --apply               # 真正写盘（自动备份 .bak-<时间戳>）
```

`migrate_identity.py` 只改 `messages.md` 里 `role=user` 行的 `speaker` 与 `memory.md` 的 key
第一段；映射由 `--map 旧名=真名` 显式给出（可重复），`我`（AI 自称）与没给映射的名字原样
保留并列进报告。同 key 冲突时合并（保留更新时间较新的一条）。

`long_memory_test.py` 依次验证：增量建库 → 本地召回 → 命中强化 → 模拟 +150 天遗忘 → 关闭模块仍可聊；
群数据、日志与 `data/test_reports/long_memory-<时间>.md/json` 默认保留（`--group` 换群便于复测）。
```

分层：

| 层 | 文件 | 测什么 |
| --- | --- | --- |
| 工具 | `tests/test_message_store.py` | 消息库读写、打码、群名校验 |
| 身份 | `tests/test_identity.py` | 身份库解析（只认 QQ / 未知返回空）、缓存按 mtime 重载、档案注入与截断 |
| 模块单测 | `tests/test_modules.py`、`test_chat_style.py`、`tests/test_recent_memory.py`、`tests/test_current_time.py` | 只给 `ctx`，断言单个钩子的输入输出 |
| 通道单测 | `tests/test_qq_channel.py` | 本地假事件打本地 HTTP：鉴权（`X-Signature`）、群白名单、私聊拒绝、去重、@ 判定、CQ 码解析、发送失败 |
| 插座契约 | `tests/test_hub_contract.py` | 顺序、异常隔离、非法返回值、状态回写、`plan_delays` 收到 `ctx` |
| 编排契约 | `tests/test_brain_contract.py`、`test_brain_prompt.py` | `should_wake → on_user → build → 送达 → on_reply`、零模块可运行 |
| 整链路 | `tests/test_pipeline_full.py` | 真实模块 + 假模型：人格/风格/唤醒/记忆/分句/延迟一起跑 |

**怎么证明"删模块不崩"**：`tests/test_pipeline_full.py::test_only_persona_enabled` 与
`::test_no_module_at_all` 就是两个极端——只留 persona、或全关，`chat()` 都能正常返回。
新增模块时照这个模式补一条即可。

## 11. 日志规范

| 时机 | 文案 |
| --- | --- |
| 模块系统加载完成 | `core_log.info("模块系统就绪, 已启用模块: ...")` |
| 模块加载失败 | `core_log.error("模块 X 加载失败, 原因: ...")` |
| 钩子执行失败 | `core_log.error("模块 X 的 build 执行失败, 原因: ...")` |
| 配置文件读取/写入失败 | `core_log.error("模块 X 的配置文件读取失败, 原因: ...")` |
| 模块主动调用模型 | `core_log.info("模块 X 发起模型请求: tag")` |
| persona 读取 | `core_log.info("读取Persona文件完成")` / `core_log.error("读取Persona文件失败, 原因: ...")` |

所有日志都写进本次运行的 `core.log`；`transport.log` 仍由 `post` 负责收发原文。

## 12. 注意

- 命令行测试入口：项目根 `main.py`（`python main.py`）——`Brain.chat()` 把回答分段入出站队列，
  `flush_outbox()` 排空队列，`notifier` 统一打印；延迟由 `ai_typing_delay.plan_delays(ctx)` 提供；
- 日记模块暂未实现；后续加定时任务时按 `tick` 约定写，并在主循环里调用 `hub.tick()`；
- 模块之间不要互相 import，需要共享能力就通过 `ctx` 或 `lib` 提供；
- 状态写进 `configs/` 是当前约定（便于和配置一起查看/备份），但不要把密钥等敏感信息写进 `state`；
- **长期记忆已落地**（`long_memory`）：只读 `messages.md`、只写自己的 `memory.md`，未改动
  `Brain` / `Hub` / `recent_memory` 的任何代码；召回率不足时优先调 `attribute_aliases` 与
  `min_score`，不要给它加"回答前再问一次模型"的链路（会让每轮回复多一次串行等待）。
