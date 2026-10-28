## QQ快速开始

### 1. 环境要求

- Python **3.10+**（开发环境为 3.11）
- 一个兼容 OpenAI 接口的模型服务
- 第三方包：

```bash
pip install openai numpy 
可选:onnxruntime;tokenizers
```
其中onnxruntime、tokenizers包服务于embedding的bge模型，用于*long_memory*的长期记忆检索,不使用该模型则启用字面匹配

在napcat下有一个.zip文件夹 需要解压 此为接入QQ通道的工具

### 2. 准备配置文件

将以下文件改名：
- configs\config.example.json -> configs\config.json
- configs\identity.example.json -> configs\identity.json
并且补充完整
`configs/config.json` 主配置文件


| 字段 | 说明 |
| --- | --- |
| `base_url` | 模型服务的接口地址，如 `https://api.deepseek.com` |
| `api_key` | 你的 API Key |
| `model` | 模型名，如 `deepseek-chat` |
| `message_path` | 聊天记录目录，默认 `data/groups` |

`configs/identity.json` 身份库
- `name` 用于提示词里区分发言人，也是 `@` 唤醒匹配的名字, 建议为真实姓名，或者是一个ta对你的认识上的称呼
- `qq` 只对 QQ 通道有效：登记过的人才能私聊，未登记会被忽略
- `enable: false` 相当于拉黑，消息不进上下文
- `profile`: 目前还没想好干啥用 XD

`configs/modules\long_memory.json` 长期记忆
请

### 3. 写人设

你的所有性格文件保存在prompts/xxxxx文件中, 切换性格文件请在configs/modules/persona.json中更改path以匹配你的性格文件

例如：我的文件存在prompts\mypersona.md 则在persona.json中的config.path = "prompts\mypersona.md"

### 4. 检查模块开关

`configs/modules.json` 里每个模块一个布尔值，可随时关掉不想要的能力：

| 模块 | 作用 |
| --- | --- |
| `persona` | 注入人设 |
| `identity` | 注入发言人身份信息 |
| `chat_style` | 注入聊天风格约束 |
| `wake` | 判断是否该回复（@ / 私聊） |
| `recent_memory` | 最近 N 轮上下文 |
| `long_memory` | 长期记忆的抽取、检索、遗忘 |
| `proactive` | 主动消息（早安 / 晚安等） |
| `reply_split` | 长回复拆成多条短句 |
| `ai_typing_delay` | 模拟打字延迟 |
| `qq_channel` | QQ 通道（仅 `main_qq.py` 需要） |
| `current_time` | 注入当前真实时间 |


本人建议：
1. 只开启私聊模式，不开启群聊
在`configs/modules/qq_channel.json`中将
  `enable_private` 改为 `true`
  `enable_group` 改为 `false`

2. 关闭 主动消息(proactive)模块 此功能尚未完善
在`configs/modules.json`中将
  `proactive` 改为 `false`
---

### 5. 运行
运行main_qq.py
运行你的napcat下的launch.bat 并且扫码登录你的qq
应该就差不多可以运行了吧()

