# post 模块（src/ai/post/post.py）

> 改代码前先读本文件。只负责把一段 `messages` 发给 OpenAI 并取回原始响应；不做业务判断、不维护记忆。

## 配置（`configs/config.json`，经 `jsonReader.config` 读取）
启动时读一次，缺失即失败：

| 键 | 含义 | 必填 |
| --- | --- | --- |
| `base_url` | OpenAI 兼容接口地址，如 `https://api.openai.com/v1` | 是 |
| `api_key` | 接口密钥（仅用于构造客户端，**不写日志**） | 是 |
| `model` | 模型名，如 `gpt-4o-mini` | 是 |

缺项或空字符串 → 客户端创建失败（`原因: config.json 缺少配置项：api_key`）。

## API

```python
import sys; sys.path.insert(0, "src/ai/post")
from post import post

memory = [{"role": "system", "content": "你是助手"}]
def chat(text):
    memory.append({"role": "user", "content": text})
    resp = post(memory)                      # 顺序即对话顺序；记忆由调用方持有
    reply = resp.choices[0].message.content  # 文本结果
    memory.append({"role": "assistant", "content": reply})
    return reply

# 兼容旧用法：post("你好") 等价于 post([{"role": "user", "content": "你好"}])
```

**参数**：`post(messages)`，`messages` 为**非空 list**，每项 dict：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `role` | `str` | 是 | `system` / `developer` / `user` / `assistant` / `tool` / `function` |
| `content` | `str` | 是 | 消息正文（多模态内容原样透传） |

- 顺序即模型看到的对话顺序；未知字段原样透传，函数只校验不改写（浅拷贝后发送）。
- 函数**不维护记忆**：system、历史轮次、assistant 回复都由调用方拼进列表。
- 返回 **OpenAI 原始响应对象**（`ChatCompletion`），不裁剪；文本取 `resp.choices[0].message.content`，`id`/`usage`/`model` 等原样可用。
- 结构非法（非 str/list、空列表、缺 `role`/`content`、role 不在允许集合）→ 记 `core_log.error` 后抛 `TypeError`/`ValueError`，**不发请求**。
- 调用/接收失败 → 抛 `RuntimeError`（含原因）并已写 `core_log.error`；不要靠返回值判断成败。

## 客户端单例
- 模块 **import 时创建一次**，存 `_client`；`post()` 只复用，任何路径不二次创建。
- 创建失败**不阻断启动**：记 `core_log.error`，`_client=None`；后续 `post()` 直接抛 `RuntimeError` 并再记一条；改配置后重启即可。
- 不要 `importlib.reload` 本模块或重复执行模块代码，否则会创建第二个客户端。

## 日志规范
| 时机 | 对象 | 级别 | 文案 |
| --- | --- | --- | --- |
| 客户端创建成功 | `core_log` | info | `OpenAI 客户端创建成功, base_url: xxx, model: xxx` |
| 客户端创建失败 | `core_log` | error | `OpenAI 客户端创建失败, 原因: xxx` |
| `post` 发送前 | `transport_log` | info | `发送的原始信息: <messages 的 JSON>` |
| 收到响应后 | `transport_log` | info | `接收的原始信息: xxx` |
| 发送/接收失败 | `core_log` | error | `调用 OpenAI 失败, 原因: xxx` |

- `core_log`/`transport_log` 来自 `logger`，分别写 `core.log`/`transport.log`。
- 本模块自动把 `src/logger`、`src/json` 补进 `sys.path`，调用方无需配置。
- 原文不脱敏、不加前后缀（便于回放）。

## 依赖
`openai`