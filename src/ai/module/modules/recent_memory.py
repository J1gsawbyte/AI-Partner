"""近期记忆模块：最近 N 轮对话的**完整**闭环——自己记账，自己读账。

为什么它要自己写
----------------
以前"写消息"放在长期记忆模块里，删掉那个模块消息库就再也不增长了，近期窗口直接
失效——一个模块的删除连带废掉另一个能力。现在 ``messages.md`` 归本模块独占：

* ``on_user``：把这一轮的成员发言追加一行；
* ``on_reply``：把真正送出的 AI 回复追加一行（送达之后才记，避免"记忆里说过、其实没发"）；
* ``build``：读最近 ``history_rounds`` 轮，**逐条**还原成 ``{"role", "content"}`` 消息注入。

为什么逐条注入而不是拼成一段
----------------------------
拼成一整段塞进一条 user 消息，模型看到的是"一段带 - 前缀的文本"，分不清哪句是自己说的；
逐条还原成 ``user`` / ``assistant`` 后，历史就是真正的对话轮次：该谁说的、说过几轮都由
role 表达，模型不必再从"我："这种文字前缀去猜。

删掉本模块时，别的模块读不到消息库只会拿到空内容并静默跳过，不会报错。

数据契约
--------
文件：``<message_path>/<群>/messages.md``，一行一个 JSON：

```json
{"time": "2026-10-26 14:45:41", "role": "user", "speaker": "张天宇", "content": "..."}
{"time": "2026-10-26 14:45:43", "role": "assistant", "speaker": "我", "content": "..."}
```

读写工具在 ``src/ai/module/lib/message_store.py``（无状态纯工具，不属于任何模块）。

钩子
----
| 钩子 | 何时 | 做什么 |
| --- | --- | --- |
| ``on_start`` | 启动 | 校验消息库路径，打一条日志 |
| ``on_user`` | 收到成员发言 | 追加一行 ``role=user`` |
| ``on_reply`` | 回复**真正送出后** | 追加一行 ``role=assistant`` |
| ``build`` | 需要回复时 | 读最近 N 轮，返回逐条 ``{"role", "content"}`` 列表或 ``""`` |

配置（``configs/modules/recent_memory.json`` 的 ``config``）
------------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `group` | `""` | 群标识；留空时用 ``ctx.group``，都为空回退 ``default`` |
| `history_rounds` | 20 | 注入最近 N 轮（成员 + AI，不含本轮），0 = 关闭 |
| `max_chars` | 1200 | 片段字符预算，超出丢旧留新 |
| `max_line_chars` | 200 | 单行截断长度，0 = 不截断 |
| `record` | `true` | 是否记录新消息；关掉就是只读窗口 |
| `privacy_block_patterns` | `[]` | 追加隐私规则（内置规则始终生效） |
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

# 复用项目的消息库工具（与其它模块一致：按目录补 sys.path）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
for _dir in (_SRC_DIR / "ai" / "module" / "lib",):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

__all__ = ["build", "on_reply", "on_start", "on_user"]

#: AI 自己的发言落盘时用的 speaker（注入时靠 role 表达，不再显示这个前缀）
_SELF_LABEL: Final[str] = "我"
#: 不知道发言人时显示成"对方"
_UNKNOWN_LABEL: Final[str] = "对方"


def _int_param(ctx: Any, path: str, default: int) -> int:
    """读取模块配置里的整数（支持 ``budget.recent`` 这种点路径）。"""
    value: Any = getattr(ctx, "params", None) or {}
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_param(ctx: Any, key: str, default: bool) -> bool:
    params = getattr(ctx, "params", None) or {}
    value = params.get(key) if isinstance(params, dict) else None
    return value if isinstance(value, bool) else default


def _mask(ctx: Any, text: str) -> str:
    """按模块配置追加的隐私规则打码（内置规则始终生效）。"""
    raw = getattr(ctx, "params", None) or {}
    extra = raw.get("privacy_block_patterns") if isinstance(raw, dict) else []
    masked, bad = store.mask_privacy(text, extra if isinstance(extra, (list, tuple)) else [])
    for pattern in bad:
        ctx.log_error(f"隐私规则不是合法正则, 已忽略: {pattern}")
    return masked


def _group(ctx: Any) -> str:
    """群标识：``ctx.group`` > ``config.group``；都没有返回空串。"""
    params = getattr(ctx, "params", None) or {}
    for candidate in (
        str(getattr(ctx, "group", "") or "").strip(),
        str(params.get("group") or "").strip(),
    ):
        if not candidate:
            continue
        valid = store.validate_file_stem(candidate)
        if valid:
            return valid
        ctx.log_error(f"群名非法，已回退 default: {candidate!r}")
        return "default"
    return ""


def _discover_group(root: Path) -> str:
    """未指定群时，若根目录下只有一个群有消息文件，就用它（单群部署的兜底）。"""
    try:
        candidates = sorted(
            path.name
            for path in root.iterdir()
            if path.is_dir() and (path / "messages.md").is_file()
        )
    except OSError:
        return ""
    return candidates[0] if len(candidates) == 1 else ""


def _message_file(ctx: Any) -> Path | None:
    """本群的消息库文件：``<message_path>/<群>/messages.md``。"""
    root = getattr(ctx, "message_path", None)
    if root is None:
        return None
    group = _group(ctx) or _discover_group(Path(root)) or "default"
    return store.message_file(root, group)


# -- 记录 ---------------------------------------------------------------- #


def _record(ctx: Any, role: str, content: str, speaker: str) -> None:
    """追加一行消息；任何失败都只记日志，绝不打断对话。"""
    path = _message_file(ctx)
    if path is None:
        ctx.log_error("未注入 message_path，本轮消息无法落盘")
        return

    text = _mask(ctx, str(content or "")).strip()
    if not text:
        return

    moment = getattr(ctx, "now", None)
    row = {
        "time": store.format_time(moment if isinstance(moment, datetime) else datetime.now()),
        "role": role,
        "speaker": speaker,
        "content": text,
    }
    try:
        store.append_messages(path, [row])
    except OSError as exc:
        ctx.log_error(f"消息落盘失败, 原因: {type(exc).__name__}: {exc}")


def on_start(ctx: Any) -> None:
    """校验消息库路径：路径由程序配置注入，缺失时本模块静默降级。"""
    path = _message_file(ctx)
    if path is None:
        ctx.log_error("未注入 message_path，近期记忆不可用")
        return
    ctx.log_info(f"就绪: 消息库 {path}")


def on_user(ctx: Any) -> None:
    """成员发言后落盘（休眠轮同样会走到这里，所以"没回复的话"也留得住）。"""
    if not _bool_param(ctx, "record", True):
        return
    speaker = str(getattr(ctx, "speaker", "") or "").strip() or _UNKNOWN_LABEL
    _record(ctx, "user", str(getattr(ctx, "user_text", "") or ""), speaker)


def on_reply(ctx: Any) -> None:
    """回复真正送出后落盘（Brain 在最后一段经 ``notifier`` 发出后才调用）。"""
    if not _bool_param(ctx, "record", True):
        return
    _record(ctx, "assistant", str(getattr(ctx, "reply", "") or ""), _SELF_LABEL)


# -- 注入 ---------------------------------------------------------------- #


def build(ctx: Any) -> Any:
    """组装最近 ``history_rounds`` 轮对话（逐条消息）；异常时返回空串，不影响对话。"""
    path = _message_file(ctx)
    rounds = _int_param(ctx, "history_rounds", 20)
    if path is None or rounds <= 0:
        return ""
    try:
        return _build_block(ctx, path, rounds)
    except Exception as exc:  # noqa: BLE001 - 窗口只是背景，坏了也不能挡住回复
        ctx.log_error(f"近期记忆组装失败, 原因: {type(exc).__name__}: {exc}")
        return ""


def _build_block(ctx: Any, path: Path, rounds: int) -> Any:
    """读消息库 → 截到最近 N 轮 → 按预算丢旧留新 → 逐条还原成对话消息。"""
    total = store.count_messages(path)
    if total <= 0:
        return ""

    # 1 轮最多 2 条（成员 + AI），多读 2 条用于覆盖"本轮输入已落盘"的情况
    sample = min(total, rounds * 2 + 2)
    rows = store.read_messages(path, limit=sample, offset=max(0, total - sample))
    if not rows:
        return ""

    # ``on_user`` 已把本轮输入写进库，这里跳过，避免和 prompt 末尾重复
    # （落盘时按隐私规则打码，这里同时比对原文与打码文）
    current = str(getattr(ctx, "user_text", "") or "").strip()
    last_content = str(rows[-1].get("content") or "").strip()
    if current and last_content and last_content in {current, _mask(ctx, current).strip()}:
        rows = rows[:-1]

    # 从末尾数 user 行，截到 rounds 轮
    seen = 0
    for index in range(len(rows) - 1, -1, -1):
        if str(rows[index].get("role") or "") == "assistant":
            continue
        seen += 1
        if seen >= rounds:
            rows = rows[index:]
            break

    line_limit = _int_param(ctx, "max_line_chars", 200)
    messages: list[dict[str, str]] = []
    for row in rows:
        assistant = str(row.get("role") or "") == "assistant"
        content = _mask(ctx, str(row.get("content") or "")).strip().replace("\n", " ")
        if line_limit > 0 and len(content) > line_limit:
            content = content[:line_limit] + "…"
        if not content:
            continue
        if assistant:
            # 自己说过的话不加"我："，role 已经说明是谁说的
            messages.append({"role": "assistant", "content": content})
            continue
        speaker = str(row.get("speaker") or "").strip() or _UNKNOWN_LABEL
        messages.append({"role": "user", "content": f"{speaker}：{content}"})
    if not messages:
        return ""

    # 超预算时保留最新：会话窗口优先保留刚发生的对话
    limit = _int_param(ctx, "max_chars", 1200)
    if limit > 0:
        kept: list[dict[str, str]] = []
        used = 0
        for message in reversed(messages):
            size = len(message["content"])
            if kept and used + size > limit:
                break
            kept.append(message)
            used += size
        messages = list(reversed(kept))

    ctx.log_info(f"近期记忆: {len(messages)} 条（上限 {rounds} 轮 / {limit} 字）")
    return messages
