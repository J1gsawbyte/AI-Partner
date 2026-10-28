"""打字延迟模块：算出「一条一条发出去」时每条消息该等多久，模拟人类打字节奏。

配置（``configs/modules/ai_typing_delay.json`` 的 ``config``）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `msg_delay_base_range` | `[5, 10]` | 每条消息的基础延迟（秒），区间内随机 |
| `msg_delay_per_char_range` | `[0.4, 0.8]` | 每条消息里每个非标点字符的延迟（秒），区间内随机 |
| `msg_delay_max` | `30` | **单条消息**的延迟上限（秒） |

口径很重要：三个值都作用在**分割后的一条消息**上，而不是整次回答。
即每条消息 `min(base + per_char × 该条有效字数, msg_delay_max)`，各自独立随机；
一次回答被拆成 5 条就各自等待，总等待是各条之和，**不额外封顶**。

三个数值唯一来源是上面那个配置文件：不传参数时 :func:`load_params` 现读现用，
改配置立即生效。文件缺失/字段非法时才回退兜底值，并记 ``core_log.error``，
绝不抛异常打断对话。代码里的 ``_DEFAULT_*`` 只是最后兜底，不是正常取值路径。

两种用法
--------
* 作为模块钩子：``plan_delays(ctx)`` —— 分段在 ``ctx.segments``，参数取 ``ctx.params``
  （也就是 ``configs/modules/ai_typing_delay.json`` 的 ``config``，换了配置目录照样生效）；
* 作为纯函数：``plan_for(segments, params)`` / ``delay_for(text, params)``，不依赖 hub。
"""

from __future__ import annotations

import json
import random
import sys
import unicodedata
from pathlib import Path
from typing import Any, Final, Sequence

_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LOGGER_DIR: Final[Path] = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = [
    "count_chars",
    "CONFIG_FILE",
    "delay_for",
    "load_params",
    "plan_delays",
    "plan_for",
]

#: 项目根目录（src/ai/module/modules/ai_typing_delay.py 的上四级）
PROJECT_ROOT: Final[Path] = _SRC_DIR.parent

#: 本模块的配置文件：configs/modules/ai_typing_delay.json
CONFIG_FILE: Final[Path] = PROJECT_ROOT / "configs" / "modules" / "ai_typing_delay.json"

# 配置文件缺失/字段非法时才会用到的兜底值（正常情况一律以配置文件为准）
_DEFAULT_BASE_RANGE: Final[tuple[float, float]] = (5.0, 10.0)
_DEFAULT_PER_CHAR_RANGE: Final[tuple[float, float]] = (0.4, 0.8)
_DEFAULT_MAX: Final[float] = 30.0

#: 保留的小数位，避免打印出 3.0000000000000004 这种数字
_PRECISION: Final[int] = 3


def load_params() -> dict[str, Any]:
    """读取 ``configs/modules/ai_typing_delay.json`` 里的 ``config`` 段。

    每次调用都重新读文件（改配置立即生效，不必重启）；读不到或格式不对时记
    ``core_log.error`` 并返回空 dict，由下游按兜底值计算，绝不打断对话。
    """
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig") or "{}")
    except FileNotFoundError:
        core_log.error(f"ai_typing_delay 配置文件不存在，改用兜底延迟参数：{CONFIG_FILE}")
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        core_log.error(
            f"ai_typing_delay 配置文件读取失败, 原因: {type(exc).__name__}: {exc}"
        )
        return {}

    if not isinstance(raw, dict):
        core_log.error(f"ai_typing_delay 配置文件顶层必须是对象：{CONFIG_FILE}")
        return {}

    config = raw.get("config", {})
    if not isinstance(config, dict):
        core_log.error("ai_typing_delay 配置文件的 config 字段必须是对象，已按空处理")
        return {}
    return config


def count_chars(text: str) -> int:
    """统计有效字符数：标点、空白都不算，其它字符（汉字/字母/数字/emoji）各算 1。"""
    count = 0
    for char in str(text):
        if char.isspace() or unicodedata.category(char).startswith("P"):
            continue
        count += 1
    return count


def _number_range(value: Any, default: tuple[float, float], name: str) -> tuple[float, float]:
    """校验 `[低, 高]` 形式的区间，非法回退默认值。"""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        low, high = value
        valid = all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in (low, high)
        )
        if valid and float(low) >= 0 and float(high) >= float(low):
            return (float(low), float(high))

    if value is not None:
        core_log.error(
            f"ai_typing_delay 配置项 {name} 非法（需 [低, 高] 且 0 ≤ 低 ≤ 高），已回退默认值"
        )
    return default


def _limit(value: Any) -> float:
    """校验最大延迟，非法回退默认值。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) > 0:
        return float(value)
    if value is not None:
        core_log.error("ai_typing_delay 配置项 msg_delay_max 非法（需大于 0），已回退默认值")
    return _DEFAULT_MAX


def _read_params(params: dict[str, Any]) -> tuple[float, float, float]:
    """读出（基础延迟区间上界随机值、单字延迟区间上界随机值、上限）三段配置。"""
    base_range = _number_range(
        params.get("msg_delay_base_range"), _DEFAULT_BASE_RANGE, "msg_delay_base_range"
    )
    per_char_range = _number_range(
        params.get("msg_delay_per_char_range"),
        _DEFAULT_PER_CHAR_RANGE,
        "msg_delay_per_char_range",
    )
    limit = _limit(params.get("msg_delay_max"))
    base = random.uniform(*base_range)
    per_char = random.uniform(*per_char_range)
    return base, per_char, limit


def delay_for(text: str, params: dict[str, Any] | None = None) -> float:
    """单条消息的延迟：``min(基础 + 单字 × 有效字数, msg_delay_max)``。

    ``params`` 不传时**读取本模块的配置文件**（``configs/modules/ai_typing_delay.json``）；
    每次调用都重新抽取基础延迟与单字延迟，所以同样长度的两句话等待也会略有差异（更像真人）。
    """
    resolved = load_params() if params is None else params
    base, per_char, limit = _read_params(resolved)
    delay = base + per_char * count_chars(text)
    return round(min(delay, limit), _PRECISION)


def plan_for(segments: Sequence[str], params: dict[str, Any] | None = None) -> list[float]:
    """逐条计算等待时间，返回与 ``segments`` 一一对应的秒数列表（纯函数）。

    ``params`` 不传时读配置文件（一次读取，供本次所有分段使用）。
    每条消息独立随机、独立受 ``msg_delay_max`` 约束；总等待是各条之和，
    不做整体封顶 —— 拆成 5 条就等 5 次，这正是"像人一样一条条发"的节奏。
    """
    resolved = load_params() if params is None else params
    return [delay_for(str(segment), resolved) for segment in segments]


def plan_delays(ctx: Any) -> list[float]:
    """ModuleHub 钩子：给 ``ctx.segments`` 逐条算出等待秒数。

    参数取 ``ctx.params``（本模块的 ``config``），因此测试/部署换掉配置目录时，
    节奏也跟着换；``ctx.params`` 为空时才回退到配置文件``CONFIG_FILE``。
    """
    segments = [str(item) for item in (getattr(ctx, "segments", None) or [])]
    params = getattr(ctx, "params", None)
    resolved = params if isinstance(params, dict) and params else None
    return plan_for(segments, resolved)
