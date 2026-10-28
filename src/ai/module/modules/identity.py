"""身份模块：把当前发言人的**固定档案**拼进 prompt，不参与记忆总结。

为什么要有它
------------
有些事实几乎不会变、答错代价又高：真名、生日、出生地、忌口。让模型从对话里总结这种
东西，既会漏、也会随遗忘曲线掉，改了还容易和旧值打架。这类内容放 ``configs/identity.json``
里由人维护，本模块负责注入；变化快的内容（经历、约定、计划）仍交给 ``long_memory``。

职责边界
--------
* **只读**身份表，不在运行时改写它（改档案要人改文件，避免模型把自己"认错人"写进去）；
* **只管当前发言人**：别人档案不注入，避免群聊里把他的生日灌给你；
* 不认人、不改 ``speaker``——把平台账号翻译成真名是通道模块的事（``qq_channel``），
  本模块拿到的 ``ctx.speaker`` 已经是真名。

配置（``configs/modules/identity.json`` 的 ``config``）
------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `people_path` | `configs/identity.json` | 身份表位置，相对路径按项目根解析 |
| `inject_profile` | `true` | 关掉后只保留身份解析（通道仍在用），prompt 里不再出现档案 |
| `max_chars` | 300 | 档案片段上限，超出截断，避免挤占对话预算 |

片段形如::

    [成员档案]
    - 王俊宇
      - 生日：2005-03-14
      - 出生地：湖南岳阳
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LIB_DIR: Final[Path] = _SRC_DIR / "ai" / "module" / "lib"
for _dir in (_LIB_DIR, _SRC_DIR / "logger"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import identity_store  # noqa: E402  # pyright: ignore[reportMissingImports]

__all__ = ["build", "on_start"]

#: 项目根目录（src/ai/module/modules/identity.py 的上四级再上一级）
PROJECT_ROOT: Final[Path] = _SRC_DIR.parent

#: 身份表默认位置
DEFAULT_PEOPLE: Final[Path] = PROJECT_ROOT / "configs" / "identity.json"

#: 片段标题：与其它模块的 ``[长期记忆]`` / ``[最近对话]`` 保持同一种风格
_TITLE: Final[str] = "[成员档案]"


def _params(ctx: Any) -> dict[str, Any]:
    params = getattr(ctx, "params", None)
    return params if isinstance(params, dict) else {}


def _bool_param(ctx: Any, key: str, default: bool) -> bool:
    value = _params(ctx).get(key)
    return value if isinstance(value, bool) else default


def _int_param(ctx: Any, key: str, default: int) -> int:
    try:
        return int(_params(ctx).get(key, default))
    except (TypeError, ValueError):
        return default


def _people_path(ctx: Any) -> Path:
    """身份表路径：留空用默认值，相对路径按项目根解析。"""
    raw = str(_params(ctx).get("people_path") or "").strip()
    path = Path(raw) if raw else DEFAULT_PEOPLE
    return path if path.is_absolute() else PROJECT_ROOT / path


def on_start(ctx: Any) -> None:
    """启动自检：身份表读不到时只提示，不影响对话（``identity`` 不是必需模块）。"""
    path = _people_path(ctx)
    if not path.is_file():
        ctx.log_error(f"身份表不存在: {path}（发言人将沿用群名片，档案不会注入）")
        return
    book = identity_store.load(path)
    if not len(book):
        ctx.log_error(f"身份表没有有效条目: {path}")
        return
    ctx.log_info(f"身份表就绪: {path.name}（{len(book.enabled)}/{len(book)} 人启用）")


def build(ctx: Any) -> str:
    """拼接当前发言人的档案片段；没有档案就返回空串（Hub 会跳过）。"""
    if not _bool_param(ctx, "inject_profile", True):
        return ""

    speaker = str(getattr(ctx, "speaker", "") or "").strip()
    if not speaker:
        return ""

    lines = identity_store.load(_people_path(ctx)).profile_lines(speaker)
    if not lines:
        return ""

    text = "\n".join([_TITLE, *lines])
    limit = _int_param(ctx, "max_chars", 300)
    if limit > 0 and len(text) > limit:
        text = text[: max(0, limit - 1)] + "…"
    return text
