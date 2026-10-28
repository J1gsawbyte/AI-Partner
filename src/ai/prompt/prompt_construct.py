"""prompt 组装：把各模块返回的片段拼成可直接交给 ``post`` 的 message 列表。

片段形态（模块 ``build(ctx)`` 的返回值）：

* ``str``：按 ``{"role": "system", "content": 文本}`` 处理；
* ``dict``：``{"role": ..., "content": ...}``，role 由模块自己决定（不限制种类）；
* ``list[dict]``：按顺序展开，等价于一次给多条消息。

组装规则：

1. 所有 ``system`` 片段按模块顺序合并成**一条** system 放在最前；
2. 其它 role 的片段原样保留顺序，统一放在历史之后；
3. 最后追加本轮用户输入（非空时）；
4. 空内容片段直接丢弃，不产生空消息。
"""

from __future__ import annotations

from typing import Any, Iterable, Final

__all__ = ["prompt_construct", "normalize_fragments", "Fragment"]

#: 模块可返回的片段类型
Fragment = str | dict[str, str] | list[dict[str, str]]

#: system 内容之间的分隔（模块之间的自然断行）
_SYSTEM_SEPARATOR: Final[str] = "\n\n"


def _normalize_message(message: Any) -> list[dict[str, str]]:
    """把单条消息归一成 ``{"role": str, "content": str}``，非法直接抛错。"""
    if not isinstance(message, dict):
        raise TypeError(f"消息片段必须是 dict，收到 {type(message).__name__}")

    role = message.get("role")
    if not isinstance(role, str) or not role.strip():
        raise ValueError(f"消息片段缺少合法的 role：{message!r}")

    content = message.get("content")
    if content is None:
        return []
    if not isinstance(content, str):
        content = str(content)
    if not content.strip():
        return []

    return [{"role": role.strip(), "content": content}]


def _normalize_fragment(fragment: Any) -> list[dict[str, str]]:
    """把一个模块片段展开成消息列表；``str`` 视为 system 消息。"""
    if isinstance(fragment, str):
        return _normalize_message({"role": "system", "content": fragment})
    if isinstance(fragment, dict):
        return _normalize_message(fragment)
    if isinstance(fragment, (list, tuple)):
        messages: list[dict[str, str]] = []
        for item in fragment:
            messages.extend(_normalize_message(item))
        return messages
    raise TypeError(
        "模块片段必须是 str / dict / dict 列表，"
        f"收到 {type(fragment).__name__}"
    )


def normalize_fragments(fragments: Iterable[Any]) -> list[dict[str, str]]:
    """把多个模块片段按顺序展开成一条扁平的消息列表（不做 system 合并）。"""
    messages: list[dict[str, str]] = []
    for fragment in fragments:
        messages.extend(_normalize_fragment(fragment))
    return messages


def prompt_construct(
    fragments: Iterable[Any],
    user_text: str = "",
    history: Iterable[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """组装最终 message 列表：合并 system → 历史 → 其它 role 片段 → 本轮输入。

    - ``fragments``：各模块 ``build`` 的返回值（已按启用顺序排好）；
    - ``history``：既有对话（``[{"role": "user"/"assistant", "content": ...}]``）；
    - ``user_text``：本轮用户输入，空字符串表示不追加。
    """
    system_parts: list[str] = []
    extras: list[dict[str, str]] = []

    for message in normalize_fragments(fragments):
        if message["role"] == "system":
            system_parts.append(message["content"])
        else:
            extras.append(message)

    messages: list[dict[str, str]] = []
    if system_parts:
        messages.append({"role": "system", "content": _SYSTEM_SEPARATOR.join(system_parts)})

    for item in history or ():
        messages.extend(_normalize_message(item))

    messages.extend(extras)

    text = "" if user_text is None else str(user_text)
    if text.strip():
        messages.append({"role": "user", "content": text})

    return messages
