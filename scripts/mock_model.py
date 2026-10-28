"""测试用假模型：不联网、不花钱，只保证"有回复且各不相同"。

只给 ``scripts/*_test.py --mock`` 用；真实运行永远走 ``post``。
"""

from __future__ import annotations

import types
from typing import Any, Final

__all__ = ["CALLS", "mock_post", "reset"]

#: 每次请求的 messages（便于脚本里核对 prompt）
CALLS: list[Any] = []

_LIMIT: Final[int] = 12


def reset() -> None:
    """清空调用记录（同一进程里跑多段测试时用）。"""
    CALLS.clear()


def _last_user_text(messages: Any) -> str:
    for item in reversed(list(messages or [])):
        if isinstance(item, dict) and item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def mock_post(messages: Any, **options: Any) -> types.SimpleNamespace:
    """返回 ``收到，<输入末尾几个字>``，让不同输入得到不同回复。"""
    CALLS.append(messages)
    text = _last_user_text(messages).replace("\n", " ").strip()
    content = f"收到，{text[-_LIMIT:]}" if text else "收到"
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
    )
