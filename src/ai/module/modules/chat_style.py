"""聊天风格模块：只提供「聊天软件输出格式」约束，不改写模型回复。

分工
----
* 本模块只负责**说什么格式**（纯文本、不写括号动作、不堆语气词）；
* 判断"该不该说、说什么"由别的模块负责；Brain 只负责把片段按顺序拼进 prompt。

钩子
----
| 钩子 | 何时 | 返回 |
| --- | --- | --- |
| ``build`` | 组装 prompt | 一段 ``system`` 文本 |

两种场景靠 ``ctx.phase`` 区分（由 Brain 告知，模块决定细节）：

* ``chat``：只给 ``STYLE_RULES``；
* ``proactive``：``STYLE_RULES`` + ``PROACTIVE_RULES``（主动开口的额外约束）。

配置（``configs/modules/chat_style.json`` 的 ``config``）
--------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled_phases` | `["chat", "proactive"]` | 只在列出的场景生效；留空表示全场景 |

设计取舍：不做程序删减。程序删减容易把"呀/哦/呢"这类用户想保留的语气一起删掉，
也会让回复变生硬；是否保留语气词交给 persona 与模型判断。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LOGGER_DIR: Final[Path] = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["build", "STYLE_RULES", "PROACTIVE_RULES"]

#: 普通回复的输出约束
STYLE_RULES: Final[str] = """【聊天软件纯文本】
你现在是在手机聊天软件里打字，只输出纯文本对话，遵守：
1. 不要写任何括号动作、神态、心理描写或旁白（例如“（抬眼）”“（顿了顿）”“（心里有点雀跃）”），也不要写“*叹气*”这类星号动作；
2. 句尾不要堆叠语气词（呀、哦、呢、嘛、啦、哈、呗、咯、哟、喔、诶、欸、哇、嗷、捏、咧、嘞、哒、惹、啊）；偶尔一个可以，不要每句都带；
3. 不要分点、不要标题、不要 markdown，不要解释自己在扮演；
4. 保留“吗、吧”等必要的疑问/祈使词；句子短一点，像真人发消息。"""

#: 主动开口时的额外约束（只用于 ``ctx.phase == "proactive"``）
PROACTIVE_RULES: Final[str] = """【这次是你主动开口】
1. 直接说内容，不要解释你为什么会主动说话，也不要提"系统/触发/定时"；
2. 一两句话就够，像平时随手发的一条消息；
3. 不要重复最近对话里已经说过的内容，也不要追问没人提过的事；
4. 对方没回你上一条时，不要连续催问。"""

#: 默认生效场景
_DEFAULT_PHASES: Final[tuple[str, ...]] = ("chat", "proactive")


def _phases(ctx: Any) -> tuple[str, ...]:
    """读 ``enabled_phases``：非法或缺失时用默认值，空列表表示全场景。"""
    params = getattr(ctx, "params", None)
    raw = params.get("enabled_phases") if isinstance(params, dict) else None
    if raw is None:
        return _DEFAULT_PHASES
    if not isinstance(raw, (list, tuple)):
        core_log.error("chat_style 配置项 enabled_phases 非法（需字符串数组），已回退默认值")
        return _DEFAULT_PHASES
    values = tuple(str(item).strip() for item in raw if str(item).strip())
    return values


def build(ctx: Any) -> str:
    """ModuleHub 钩子：按 ``ctx.phase`` 返回输出格式约束；不适用时返回空串。"""
    phase = str(getattr(ctx, "phase", "chat") or "chat")
    phases = _phases(ctx)
    if phases and phase not in phases:
        return ""

    if phase == "proactive":
        return f"{STYLE_RULES}\n\n{PROACTIVE_RULES}"
    return STYLE_RULES
