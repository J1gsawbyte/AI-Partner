"""唤醒模块：只回答「这一轮该不该我说话」。

为什么单独成模块
----------------
"什么时候开口"是**策略**，不是机制：私聊必回、群聊只有被 ``@名字`` 才回、或者以后可能
按概率回。这些规则不该写进 ``Brain``（那是把策略烧进编排层），
也不该塞进记忆/主动消息模块（一模块两职责，删一个就连带废掉另一个）。

本模块纯计算、零 I/O：输入 ``text / speaker / group / mentioned`` 和自己的配置，
输出一个 bool。删掉它时 Hub 默认「有问必回」，也就是单聊行为，不会崩。

钩子
----
| 钩子 | 何时 | 返回 |
| --- | --- | --- |
| ``should_wake`` | 每轮对话开始 | ``True`` 回，``False`` 只记录不回复 |

配置（``configs/modules/wake.json`` 的 ``config``）
-------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `require_mention` | `true` | 群聊里必须 ``@{名字}`` 才回 |
| `names` | `[]` | AI 的名字/别名；群聊里必须写成 ``@名字`` 才算唤醒 |
| `private_always` | `true` | 单聊（``ctx.group`` 为空）是否总是回复 |

名字放在本模块配置里，**不去读 persona.md**：读 persona 会让两个模块互相耦合。
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["should_wake"]

#: 默认名字（用户没有配置名字也不至于一句话都不回）
_DEFAULT_NAMES: Final[tuple[str, ...]] = ()


def _bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    return value if isinstance(value, bool) else default


def _names(params: dict[str, Any]) -> tuple[str, ...]:
    raw = params.get("names")
    if not isinstance(raw, (list, tuple)):
        return _DEFAULT_NAMES
    return tuple(str(item).strip() for item in raw if str(item).strip())


def should_wake(ctx: Any) -> bool:
    """ModuleHub 钩子：判断这轮要不要回复。

    判定顺序（任一命中即回）：
    1. 私聊（``ctx.private``）或没有群标识，且 ``private_always`` 为真；
    2. 文本里出现 ``@{名字}``——**必须显式 @，光叫名字不算**；
    3. 平台已标记被 @（``ctx.mentioned``），且正文里没有"@别人"的歧义：
       有名字就认名字；正文里连 ``@`` 都没有（QQ 把 ``at`` 当独立消息段，剥掉后只剩纯文本）
       就信平台标记；
    4. ``require_mention`` 为假（群聊也一律回）。

    私聊必须先判 ``ctx.private``、**不能只看 ``group`` 是否为空**：私聊同样有记忆目录
    （``private-<QQ>``），只看 group 会把它当群聊；那样用户正文里出现一个 ``@``
    （"我邮箱 a@b.com"）就会被判成"@了别人"，整轮不回复。
    """
    params = getattr(ctx, "params", None)
    params = params if isinstance(params, dict) else {}

    group = str(getattr(ctx, "group", "") or "").strip()
    text = str(getattr(ctx, "user_text", "") or "").replace("＠", "@")

    if bool(getattr(ctx, "private", False)) or not group:
        return _bool_param(params, "private_always", True)

    names = _names(params)
    if any(f"@{name}" in text for name in names):
        return True
    mentioned = bool(getattr(ctx, "mentioned", False))
    if mentioned and ("@" not in text or any(name in text for name in names)):
        return True
    if not _bool_param(params, "require_mention", True):
        return True
    return False
