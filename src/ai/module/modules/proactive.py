"""主动消息模块：只负责"什么时候可以开口"，不生成、不发送。

调用链（由 Brain 驱动）::

    Brain.tick()
      → hub.tick()                      维护型模块（总结、过期）
      → hub.collect_proactive()         本模块 proactive(ctx) 提名一条请求
      → Brain 生成 + notifier 发送
      → hub.notify_proactive()          本模块 on_proactive(ctx) 提交 / 回滚冷却

设计口径
--------
* **规则可配置**：`config.rules` 是规则列表，每条规则只描述"什么时候、说什么方向"；
* **条件可扩展**：条件类型走注册表 ``_CONDITIONS``，新增类型只需 ``register_condition``，
  不用改 Brain / Hub；
* **冷却与退避在模块侧**：只有 Brain 回抛 ``status == "sent"`` 才推进 ``last_fired_at``，
  失败按 ``failure_backoff`` 退避，避免模型抖动导致每 5s 重试；
* **不调模型**：本模块只提名，生成与发送统一走 Brain（保证 persona / 风格 / 记忆一致）。

配置（``configs/modules/proactive.json`` 的 ``config``）
------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `group` / `speaker` | `""` | 发送对象；规则里可覆盖 |
| `quiet_hours` | `[]` | 免打扰时段 `["23:30","07:00"]`，支持跨天 |
| `max_per_hour` | 2 | 每小时最多主动几条；0 = 不限 |
| `failure_backoff` | `[300,900,1800]` | 失败后的重试退避秒数（按次数递增） |
| `max_per_rule_per_hour` | 0 | 预留：单规则每小时上限，0 = 不限 |
| `rules[]` | 见下 | 规则列表（按 `priority` 从高到低匹配，命中一条即停） |

规则字段
--------
| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 规则标识（去重、冷却、日志都用它） |
| `type` | 是 | 条件类型，见 ``_CONDITIONS``（当前内置 `schedule`） |
| `enabled` | 否 | 默认 true |
| `priority` | 否 | 默认 0，多规则竞争时取最高 |
| `window` | 视类型 | 允许触发的时间窗 `["08:00","09:30"]`，跨天自动处理 |
| `cooldown` | 否 | 该规则两次触发的间隔秒数（默认 86400） |
| `expires_in` | 否 | 提名后多久不发送就作废（秒），默认 0 = 不作废 |
| `instruction` | 二选一 | 给模型的行动方向（走完整生成链路） |
| `text` | 二选一 | 成品文案（跳过模型） |
| `params` | 否 | 交给条件类型自由解释 |
| `speaker` / `group` | 否 | 覆盖配置里的默认发送对象 |

运行态（``state``）
-------------------
``rules.<id>`` = `{last_fired_at, next_retry_at, attempts}`；``pending`` = `{id: attempt_id}`
（已提名未回执）；``sent_log`` = 最近一小时的发送时间（限流用）；``stats`` 计数；
``warned`` = 已告警过的坏规则，避免每 5s 刷日志。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Callable, Final

__all__ = ["on_start", "proactive", "on_proactive", "register_condition"]

#: 条件类型注册表：`type -> handler(ctx, rule, now) -> bool`
_CONDITIONS: dict[str, Callable[[Any, dict[str, Any], datetime], bool]] = {}

#: 默认失败退避（秒）
_DEFAULT_BACKOFF: Final[tuple[int, ...]] = (300, 900, 1800)


def register_condition(
    name: str, handler: Callable[[Any, dict[str, Any], datetime], bool]
) -> None:
    """注册一种条件类型；后续细化规则时只加函数，不改 Brain / Hub。"""
    if not name or not callable(handler):
        raise ValueError("register_condition 需要非空名称与可调用对象")
    _CONDITIONS[str(name)] = handler


# --------------------------------------------------------------------------- #
# 钩子
# --------------------------------------------------------------------------- #


def on_start(ctx: Any) -> None:
    """校验规则配置、补齐运行态容器（不抛异常，坏规则只告警并跳过）。"""
    params = _params(ctx)
    state = _state(ctx)
    _rule_states(state)
    _pending(state)
    _sent_log(state)
    stats = state.get("stats")
    if not isinstance(stats, dict):
        state["stats"] = {"sent": 0, "skipped": 0, "errors": 0}

    rules = params.get("rules")
    if not isinstance(rules, list):
        ctx.log_error("rules 必须是列表，主动消息已禁用")
        return

    ready = 0
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            ctx.log_error(f"第 {index} 条规则不是对象，已跳过")
            continue
        rule_id = _text(rule.get("id"))
        rule_type = _text(rule.get("type"))
        if not rule_id or not rule_type:
            ctx.log_error(f"第 {index} 条规则缺少 id/type，已跳过")
            continue
        if rule_type not in _CONDITIONS:
            ctx.log_error(f"规则 {rule_id} 的类型 {rule_type} 未注册，已跳过")
            continue
        if not _span(rule.get("window")):
            ctx.log_error(f"规则 {rule_id} 缺少合法 window，已跳过")
            continue
        ready += 1

    ctx.log_info(f"就绪: {ready}/{len(rules)} 条规则, 免打扰={params.get('quiet_hours') or '无'}")


def proactive(ctx: Any) -> dict[str, Any] | None:
    """判断此刻是否该主动说话；命中则提名一条请求（只提名，不发送）。"""
    now = getattr(ctx, "now", None) or datetime.now()
    params = _params(ctx)
    state = _state(ctx)
    snapshot = getattr(ctx, "conversation", None) or {}

    # 1) 全局闸门：休眠 / 免打扰 / 每小时频率
    if snapshot.get("dormant"):
        return None
    quiet = _span(params.get("quiet_hours"))
    if quiet is not None and _in_span(now, *quiet):
        return None

    if not _rate_ok(state, now, _int(params.get("max_per_hour"), 2, 0)):
        return None

    rules = params.get("rules")
    if not isinstance(rules, list):
        return None

    # 2) 规则竞争：priority 高者优先，命中一条即停
    ordered = sorted(
        (rule for rule in rules if isinstance(rule, dict)),
        key=lambda rule: _int(rule.get("priority"), 0, -10**9),
        reverse=True,
    )
    pending = _pending(state)

    for rule in ordered:
        rule_id = _text(rule.get("id"))
        rule_type = _text(rule.get("type"))
        if not rule_id or not rule_type:
            continue
        if not _bool(rule.get("enabled"), True):
            continue
        if rule_id in pending:
            continue

        entry = _rule_state(state, rule_id)
        if _in_cooldown(entry, rule, now):
            continue
        if _in_backoff(entry, now):
            continue

        span = _span(rule.get("window"))
        if span is not None and not _in_span(now, *span):
            continue

        handler = _CONDITIONS.get(rule_type)
        if handler is None:
            _warn_once(ctx, state, f"type:{rule_type}", f"规则 {rule_id} 的类型 {rule_type} 未注册，已跳过")
            continue

        try:
            matched = bool(handler(ctx, rule, now))
        except Exception as exc:  # noqa: BLE001 - 单条规则出错不影响其它规则
            ctx.log_error(f"规则 {rule_id} 求值失败, 原因: {type(exc).__name__}: {exc}")
            continue
        if not matched:
            continue

        return _nominate(ctx, state, pending, rule, now)

    return None


def on_proactive(ctx: Any) -> None:
    """接收 Brain 的执行结果：成功提交冷却，失败记退避，其余计为跳过。"""
    result = getattr(ctx, "result", None)
    if not isinstance(result, dict):
        return

    state = _state(ctx)
    rule_id = _text(result.get("rule_id"))
    if not rule_id:
        return

    pending = _pending(state)
    pending.pop(rule_id, None)

    entry = _rule_state(state, rule_id)
    status = _text(result.get("status"))
    stats = state.get("stats")
    if not isinstance(stats, dict):
        stats = {"sent": 0, "skipped": 0, "errors": 0}
        state["stats"] = stats

    # 优先用 Brain 回执里的真实送达时刻（sent_at），避免生成耗时把冷却起点提前
    now = (
        _parse_stamp(_text(result.get("sent_at")))
        or getattr(ctx, "now", None)
        or datetime.now()
    )

    if status == "sent":
        entry["last_fired_at"] = _stamp(now)
        entry["attempts"] = 0
        entry["next_retry_at"] = ""
        _sent_log(state).append(_stamp(now))
        stats["sent"] = _int(stats.get("sent"), 0, 0) + 1
        ctx.log_info(f"已发送 rule={rule_id}")
    elif status == "error":
        attempts = _int(entry.get("attempts"), 0, 0) + 1
        entry["attempts"] = attempts
        delay = _backoff(ctx, attempts)
        entry["next_retry_at"] = _stamp(now + timedelta(seconds=delay))
        stats["errors"] = _int(stats.get("errors"), 0, 0) + 1
        ctx.log_error(f"发送失败 rule={rule_id}, {delay}s 后重试")
    else:
        stats["skipped"] = _int(stats.get("skipped"), 0, 0) + 1
        ctx.log_info(f"本次跳过 rule={rule_id}, 原因={result.get('reason') or 'unknown'}")

    ctx.save_state()


# --------------------------------------------------------------------------- #
# 内置条件类型
# --------------------------------------------------------------------------- #


def _condition_schedule(ctx: Any, rule: dict[str, Any], now: datetime) -> bool:
    """按时段触发：window 已在主流程校验，这里只确认它存在。"""
    return _span(rule.get("window")) is not None


register_condition("schedule", _condition_schedule)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #


def _nominate(
    ctx: Any,
    state: dict[str, Any],
    pending: dict[str, Any],
    rule: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """登记 pending 并返回请求；由 Brain 决定是否真的生成与发送。"""
    rule_id = _text(rule.get("id"))
    attempt_id = f"{rule_id}-{now:%Y%m%d%H%M%S}"
    pending[rule_id] = attempt_id
    ctx.save_state()

    expires_in = _int(rule.get("expires_in"), 0, 0)
    expires_at = _stamp(now + timedelta(seconds=expires_in)) if expires_in > 0 else ""
    params = _params(ctx)

    ctx.log_info(f"提名 rule={rule_id} type={rule.get('type')} attempt={attempt_id}")
    return {
        "rule_id": rule_id,
        "attempt_id": attempt_id,
        "priority": _int(rule.get("priority"), 0, -10**9),
        "instruction": _text(rule.get("instruction")),
        "text": _text(rule.get("text")),
        "speaker": _text(rule.get("speaker")) or _text(params.get("speaker")),
        "group": _text(rule.get("group")) or _text(params.get("group")),
        "expires_at": expires_at,
    }


def _params(ctx: Any) -> dict[str, Any]:
    params = getattr(ctx, "params", None)
    return params if isinstance(params, dict) else {}


def _state(ctx: Any) -> dict[str, Any]:
    state = getattr(ctx, "state", None)
    return state if isinstance(state, dict) else {}


def _rule_states(state: dict[str, Any]) -> dict[str, Any]:
    rules = state.get("rules")
    if not isinstance(rules, dict):
        rules = {}
        state["rules"] = rules
    return rules


def _rule_state(state: dict[str, Any], rule_id: str) -> dict[str, Any]:
    rules = _rule_states(state)
    entry = rules.get(rule_id)
    if not isinstance(entry, dict):
        entry = {}
        rules[rule_id] = entry
    entry.setdefault("last_fired_at", "")
    entry.setdefault("next_retry_at", "")
    entry.setdefault("attempts", 0)
    return entry


def _pending(state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        pending = {}
        state["pending"] = pending
    return pending


def _sent_log(state: dict[str, Any]) -> list[str]:
    log = state.get("sent_log")
    if not isinstance(log, list):
        log = []
        state["sent_log"] = log
    return log


def _rate_ok(state: dict[str, Any], now: datetime, max_per_hour: int) -> bool:
    """最近一小时发送次数是否仍有余量（0 = 不限）。"""
    if max_per_hour <= 0:
        return True
    log = _sent_log(state)
    # 顺手清理超过一小时的记录，避免状态无限增长
    alive = [item for item in log if _recent(item, now)]
    log[:] = alive
    return len(alive) < max_per_hour


def _recent(stamp: str, now: datetime) -> bool:
    moment = _parse_stamp(stamp)
    return moment is not None and (now - moment).total_seconds() < 3600


def _in_cooldown(entry: dict[str, Any], rule: dict[str, Any], now: datetime) -> bool:
    cooldown = _int(rule.get("cooldown"), 86400, 0)
    if cooldown <= 0:
        return False
    last = _parse_stamp(_text(entry.get("last_fired_at")))
    return last is not None and (now - last).total_seconds() < cooldown


def _in_backoff(entry: dict[str, Any], now: datetime) -> bool:
    retry = _parse_stamp(_text(entry.get("next_retry_at")))
    return retry is not None and retry > now


def _backoff(ctx: Any, attempts: int) -> int:
    params = _params(ctx)
    values = params.get("failure_backoff")
    seq = [int(item) for item in values if _int(item, 0, 0) > 0] if isinstance(values, list) else []
    if not seq:
        seq = list(_DEFAULT_BACKOFF)
    index = min(max(0, attempts - 1), len(seq) - 1)
    return seq[index]


def _warn_once(ctx: Any, state: dict[str, Any], key: str, message: str) -> None:
    warned = state.get("warned")
    if not isinstance(warned, dict):
        warned = {}
        state["warned"] = warned
    if key in warned:
        return
    warned[key] = _stamp(datetime.now())
    ctx.log_error(message)


def _span(value: Any) -> tuple[time, time] | None:
    """`["08:00","09:30"]` → (time, time)；非法返回 None。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start = _parse_clock(value[0])
    end = _parse_clock(value[1])
    if start is None or end is None:
        return None
    return start, end


def _parse_clock(value: Any) -> time | None:
    text = _text(value)
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        return time(int(parts[0]), int(parts[1]))
    except ValueError:
        return None


def _in_span(now: datetime, start: time, end: time) -> bool:
    """支持跨天时间窗（如 23:00–07:00）。"""
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _parse_stamp(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _int(value: Any, default: int, minimum: int | None = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number) if minimum is not None else number


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)
