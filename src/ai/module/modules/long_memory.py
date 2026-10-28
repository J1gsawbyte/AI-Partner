"""长期记忆模块：把聊天记录总结成 ``{用户名}.{属性}.{次级属性}`` 的记忆库。

为什么它只读不收
----------------
``messages.md`` 归 ``recent_memory`` 独占写入，本模块只读；记忆库 ``memory.md``
归本模块独占写入。删掉任一方，另一方读不到文件只返回空，不会报错。

钩子
----
| 钩子 | 何时 | 做什么 |
| --- | --- | --- |
| ``on_start`` | 启动 | 校验记忆库路径，打印可用 / 已遗忘条数 |
| ``on_user`` | 收到成员发言 | 未总结轮数 +1（O(1)，不读文件） |
| ``build`` | 需要回复时 | 本地匹配（**零模型请求**）→ 注入 ``[长期记忆]``，命中的就地强化 |
| ``tick`` | 周期性 | 遗忘扫描；满足「轮数或时间」时增量总结建库 |

数据契约
--------
文件：``<message_path>/<群>/memory.md``，一行一条 JSON（按 key 合并，全量原子重写）。
开了 ``global_memory`` 时所有会话共用 ``<message_path>/_global/memory.md``；此时消息库
仍按会话隔离（谁说的话存在各自的 ``messages.md`` 里），只有**提炼结果**进同一个文件。

```json
{"key": "张天宇.喜好.咖啡", "summary": "喜欢手冲咖啡，不加糖", "importance": 0.6,
 "status": "active", "created_at": "...", "updated_at": "...", "last_recall_at": "...",
 "recalls": 0, "stability_h": 67.2, "first_seen": "...", "updated_from": "..."}
```

``status=forgotten`` 的记录**保留在文件里**但不再参与检索；把 ``status`` 手动改回
``active`` 即可复活。衰减 / 强化 / 匹配的公式在 ``src/ai/module/lib/memory_store.py``。

配置（``configs/modules/long_memory.json`` 的 ``config``）
---------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `global_memory` | `false` | 所有私聊/群聊共用一份记忆库（``<message_path>/_global/memory.md``） |
| `memory_whitelist` | `[]` | 允许**读且写**记忆的会话（群号 / ``private-<QQ>``，也可只写 QQ）；留空 = 不限制 |
| `group` | `""` | 群标识；留空用 ``ctx.group``，都为空回退单群兜底 / ``default`` |
| `default_user` | `默认用户` | 没有发言人时的实体名（建库与检索两侧同口径） |
| `summarize_rounds` | 10 | 未总结轮数达到该值即总结 |
| `summarize_interval_minutes` | 30 | 距上次总结超过该分钟数且有待总结消息即总结 |
| `max_increment_messages` | 200 | 单次最多读入的增量消息行数 |
| `prompt_path` | `prompts/memory_extract.md` | 提炼提示词，相对路径按项目根解析 |
| `inject_top_k` | 3 | 每轮最多注入几条 |
| `inject_max_chars` | 600 | 注入片段字符预算 |
| `min_score` | 0.8 | 字面匹配分门槛（不达标不注入） |
| `enabled_phases` | `["chat"]` | 只在列出的场景注入；空数组表示全场景 |
| `recall_growth` | 1.8 | 每次命中稳定度乘数 |
| `stability_base_hours` / `stability_max_hours` | 24 / 720 | 稳定度基准与上限（小时） |
| `importance_factor` | 3 | 重要度对初始稳定度的影响系数 |
| `forget_threshold` | 0.25 | 可提取度低于该值即遗忘（免疫记忆除外） |
| `immortal_importance` | 0.8 | 重要度达标的记忆永不遗忘 |
| `immortal_floor` | 0.4 | 免疫记忆参与排序时 ``R`` 的下限 |
| `summary_max_chars` | 60 | 单条 summary 截断长度 |
| `attribute_aliases` | `{}` | 追加属性别名（如 ``{"喜好": ["上头"]}``） |
| `privacy_block_patterns` | `[]` | 追加隐私规则（写入前打码，内置规则始终生效） |
| `embedding_enabled` | `true` | 语义检索总开关；关掉或模型不可用时自动回退字面匹配 |
| `embedding_model_path` | `embedding/bge-base-zh-v1.5.onnx` | ONNX 模型，相对路径按项目根解析 |
| `embedding_tokenizer_path` | `embedding/bge-base-zh-v1.5.tokenizer.json` | 分词文件 |
| `embedding_cache_name` | `memory.vec.npz` | 向量缓存（与 `memory.md` 同目录，可随时删） |
| `embedding_min_cos` | 0.55 | 语义补召回的相似度门槛（只补最像的那一条，且必须过身份闸） |
| `embedding_min_margin` | 0.21 | 最像的一条要比"全库平均相似度"高出这么多才算数；0 关闭 |
| `embedding_max_query_chars` / `embedding_max_memory_chars` | 256 / 200 | 送进模型的文本截断长度 |

运行态 ``state``：``{"groups": {"<群>": {"cursor": 0, "pending_rounds": 0,
"last_summarize_at": ""}}}``。``cursor`` 是"已总结到的消息行数"，只在总结成功后推进。
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final

# 复用项目既有工具（与其它模块一致：按目录补 sys.path）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LIB_DIR: Final[Path] = _SRC_DIR / "ai" / "module" / "lib"
for _dir in (_LIB_DIR, _SRC_DIR / "logger"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import embedding as embedding_store  # noqa: E402  # pyright: ignore[reportMissingImports]
import memory_store as mem  # noqa: E402  # pyright: ignore[reportMissingImports]
import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

__all__ = ["build", "on_start", "on_user", "tick"]

#: 项目根目录（src/ai/module/modules/long_memory.py 的上五级）
PROJECT_ROOT: Final[Path] = _SRC_DIR.parent

#: 提炼提示词的默认路径
DEFAULT_PROMPT: Final[Path] = PROJECT_ROOT / "prompts" / "memory_extract.md"

#: 语义检索的默认模型与分词文件（相对项目根）
DEFAULT_EMBEDDING_MODEL: Final[Path] = PROJECT_ROOT / "embedding" / "bge-base-zh-v1.5.onnx"
DEFAULT_EMBEDDING_TOKENIZER: Final[Path] = (
    PROJECT_ROOT / "embedding" / "bge-base-zh-v1.5.tokenizer.json"
)

#: ``tick`` 与 ``build`` 可能分别跑在不同线程，记忆库的读改写必须串行
_LOCK: Final[threading.RLock] = threading.RLock()

#: 兜底显示名（消息里没有发言人时）
_UNKNOWN_LABEL: Final[str] = "对方"
#: AI 自己那一行在提炼输入里的显示名
_SELF_LABEL: Final[str] = "我"
#: 已有记忆清单最多带多少条给模型（避免提示词无限膨胀）
_MAX_EXISTING_LINES: Final[int] = 60
#: 提炼输入单行截断
_MAX_LINE_CHARS: Final[int] = 200
#: 全局记忆的目录名：``<message_path>/_global/memory.md``
_GLOBAL_MEMORY_DIR: Final[str] = "_global"
#: 私聊会话标识前缀（与 ``qq_channel`` 的约定一致；这里只用于"白名单只写 QQ"的兼容）
_PRIVATE_PREFIX: Final[str] = "private-"


# -- 配置与路径 ------------------------------------------------------------ #


def _params(ctx: Any) -> dict[str, Any]:
    raw = getattr(ctx, "params", None)
    return raw if isinstance(raw, dict) else {}


def _int_param(ctx: Any, key: str, default: int) -> int:
    try:
        return int(_params(ctx).get(key, default))
    except (TypeError, ValueError):
        return default


def _float_param(ctx: Any, key: str, default: float) -> float:
    try:
        return float(_params(ctx).get(key, default))
    except (TypeError, ValueError):
        return default


def _bool_param(ctx: Any, key: str, default: bool) -> bool:
    value = _params(ctx).get(key)
    return value if isinstance(value, bool) else default


def _whitelist(ctx: Any) -> set[str]:
    """允许读写记忆的会话集合（小写）；空集合表示不限制。"""
    raw = _params(ctx).get("memory_whitelist")
    if not isinstance(raw, (list, tuple)):
        return set()
    return {str(item).strip().casefold() for item in raw if str(item).strip()}


def _allowed(ctx: Any, group: str) -> bool:
    """白名单只管"记忆"的读与写，不影响聊天本身；留空则不限制。"""
    allowed = _whitelist(ctx)
    if not allowed:
        return True
    # 白名单里写群号、`private-<QQ>` 或裸 QQ 都认（私聊标识去掉前缀再比一次）
    names = {group.casefold(), group.removeprefix(_PRIVATE_PREFIX).casefold()}
    return bool(names & allowed)


def _now(ctx: Any) -> datetime:
    moment = getattr(ctx, "now", None)
    return moment if isinstance(moment, datetime) else datetime.now()


def _phases(ctx: Any) -> list[str]:
    raw = _params(ctx).get("enabled_phases")
    if not isinstance(raw, (list, tuple)):
        return ["chat"]
    return [str(item) for item in raw if str(item).strip()]


def _default_user(ctx: Any) -> str:
    return str(_params(ctx).get("default_user") or "默认用户").strip() or "默认用户"


def _project_path(raw: str, default: Path) -> Path:
    """配置里的路径：留空用默认值，相对路径按项目根解析。"""
    path = Path(raw) if raw else default
    return path if path.is_absolute() else PROJECT_ROOT / path


def _entity(raw: Any, fallback: str) -> str:
    """发言人归一：空 / "对方" 一律落到 ``default_user``，保证建库与检索两侧同口径。"""
    name = str(raw or "").strip()
    if not name or name == _UNKNOWN_LABEL:
        return fallback
    return name


def _group(ctx: Any) -> str:
    """群标识：``ctx.group`` > ``config.group``；都没有返回空串。"""
    for candidate in (
        str(getattr(ctx, "group", "") or "").strip(),
        str(_params(ctx).get("group") or "").strip(),
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


def _paths(ctx: Any) -> tuple[str, Path, Path] | None:
    """返回 ``(群, 消息库, 记忆库)``；没有注入消息库根目录时返回 ``None``。"""
    root = getattr(ctx, "message_path", None)
    if root is None:
        return None
    base = Path(root)
    group = _group(ctx) or _discover_group(base) or "default"
    # 全局记忆：消息库仍按会话隔离，只有记忆库落到同一个 `_global/memory.md`
    mem_group = _GLOBAL_MEMORY_DIR if _bool_param(ctx, "global_memory", False) else group
    return group, store.message_file(base, group), mem.memory_file(base, mem_group)


def _group_state(ctx: Any, group: str) -> dict[str, Any]:
    """``state.groups[群]`` 的读写入口（缺字段按默认值补齐）。"""
    groups = ctx.state.get("groups")
    if not isinstance(groups, dict):
        groups = {}
        ctx.state["groups"] = groups
    entry = groups.get(group)
    if not isinstance(entry, dict):
        entry = {}
        groups[group] = entry
    entry.setdefault("cursor", 0)
    entry.setdefault("pending_rounds", 0)
    entry.setdefault("last_summarize_at", "")
    return entry


def _state_int(entry: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(entry.get(key) or 0))
    except (TypeError, ValueError):
        return 0


# -- 生命周期 -------------------------------------------------------------- #


def on_start(ctx: Any) -> None:
    """校验记忆库路径并打印统计；路径缺失时静默降级。"""
    try:
        paths = _paths(ctx)
        if paths is None:
            ctx.log_error("未注入 message_path，长期记忆不可用")
            return
        group, _, mem_path = paths
        items = mem.load_memory(mem_path)
        active = sum(
            1
            for record in items
            if str(record.get("status") or mem.STATUS_ACTIVE) == mem.STATUS_ACTIVE
        )
        scope = "全局共享" if _bool_param(ctx, "global_memory", False) else f"群 {group}"
        limited = _whitelist(ctx)
        note = f"；白名单 {len(limited)} 个会话" if limited else ""
        ctx.log_info(
            f"就绪: {scope} 记忆库 {mem_path}（可用 {active} / 共 {len(items)} 条{note}）"
        )
        _warm_vectors(ctx, mem_path, items)
    except Exception as exc:  # noqa: BLE001 - 启动自检失败不能挡住主流程
        ctx.log_error(f"启动自检失败, 原因: {type(exc).__name__}: {exc}")


def on_user(ctx: Any) -> None:
    """未总结轮数 +1；首次发言时记下时间锚点，让"时间条件"从这一刻起算。"""
    try:
        paths = _paths(ctx)
        if paths is None or not _allowed(ctx, paths[0]):
            return
        entry = _group_state(ctx, paths[0])
        entry["pending_rounds"] = _state_int(entry, "pending_rounds") + 1
        if not str(entry.get("last_summarize_at") or "").strip():
            entry["last_summarize_at"] = store.format_time(_now(ctx))
    except Exception as exc:  # noqa: BLE001
        ctx.log_error(f"计数失败, 原因: {type(exc).__name__}: {exc}")


# -- 注入 ------------------------------------------------------------------ #


def build(ctx: Any) -> Any:
    """本地匹配 → 注入 ``[长期记忆]``；命中的记录就地强化。任何异常都返回空。"""
    try:
        return _build(ctx)
    except Exception as exc:  # noqa: BLE001 - 记忆只是背景，坏了也不能挡住回复
        ctx.log_error(f"注入失败, 原因: {type(exc).__name__}: {exc}")
        return ""


def _build(ctx: Any) -> Any:
    phases = _phases(ctx)
    if phases and str(getattr(ctx, "phase", "chat") or "chat") not in phases:
        return ""

    text = str(getattr(ctx, "user_text", "") or "").strip()
    if not text:
        return ""
    paths = _paths(ctx)
    if paths is None or not _allowed(ctx, paths[0]):
        return ""

    _, _, mem_path = paths
    params = _params(ctx)
    speaker = _entity(getattr(ctx, "speaker", ""), _default_user(ctx))
    now = _now(ctx)

    with _LOCK:
        items = mem.load_memory(mem_path)
        if not items:
            return ""
        similarity = _similarity(ctx, mem_path, items, text)
        picked = mem.select_records(items, text, speaker, now, params, similarity=similarity)
        if not picked:
            return ""
        # 命中即强化：这是"被提及时加强"的唯一入口
        for _score, record, _hits in picked:
            mem.reinforce(record, now, params)
        try:
            mem.save_memory(mem_path, items)
        except OSError:
            pass  # 已由 memory_store 记日志，本轮的注入照常进行

    lines = _format_lines(ctx, picked)
    if not lines:
        return ""
    semantic = sum(1 for _score, _record, hits in picked if "sim" in hits)
    extra = f"，其中语义补召回 {semantic} 条" if semantic else ""
    ctx.log_info(f"注入 {len(lines)} 条（候选 {len(picked)} 条{extra}）")
    return {
        "role": "user",
        "content": "[长期记忆]\n" + "\n".join(lines) + "\n（已确认的长期事实，仅作背景）",
    }


def _format_lines(
    ctx: Any, picked: list[tuple[float, dict[str, Any], dict[str, float]]]
) -> list[str]:
    """按 ``inject_max_chars`` 预算拼行，超出预算丢后面的（排序已按相关度降序）。"""
    budget = _int_param(ctx, "inject_max_chars", 600)
    extra = _params(ctx).get("privacy_block_patterns")
    patterns = extra if isinstance(extra, (list, tuple)) else []

    lines: list[str] = []
    used = 0
    for _score, record, _hits in picked:
        summary = store.mask_privacy(str(record.get("summary") or ""), patterns)[0].strip()
        if not summary:
            continue
        line = f"- {record.get('key')}：{summary}"
        if lines and budget > 0 and used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return lines


def _memory_text(ctx: Any, record: dict[str, Any]) -> str:
    """给向量模型看的记忆文本：只用 ``summary``，按配置截断。

    不要把 key 拼进来：key 里有人名（``张天宇.身份.生日``），会抬高所有句子的相似度基线，
    实测无关句也能到 0.69；只喂 summary 时，正例 0.50–0.65、负例 0.35–0.52，区分度明显更好。
    """
    limit = max(32, _int_param(ctx, "embedding_max_memory_chars", 200))
    summary = str(record.get("summary") or "").strip()
    return (summary or str(record.get("key") or ""))[:limit]


def _vector_inputs(
    ctx: Any, mem_path: Path, items: list[dict[str, Any]]
) -> tuple[list[tuple[str, str]], Path, Path, Path] | None:
    """语义检索需要的四样东西：待编码文本、模型、分词、缓存路径；没开则 ``None``。"""
    params = _params(ctx)
    if not params.get("embedding_enabled"):
        return None
    pairs = [
        (str(item.get("key")), _memory_text(ctx, item))
        for item in items
        if str(item.get("status") or mem.STATUS_ACTIVE) == mem.STATUS_ACTIVE
    ]
    if not pairs:
        return None
    model = _project_path(
        str(params.get("embedding_model_path") or "").strip(), DEFAULT_EMBEDDING_MODEL
    )
    tokenizer = _project_path(
        str(params.get("embedding_tokenizer_path") or "").strip(), DEFAULT_EMBEDDING_TOKENIZER
    )
    cache = mem_path.with_name(str(params.get("embedding_cache_name") or "memory.vec.npz"))
    return pairs, model, tokenizer, cache


def _warm_vectors(ctx: Any, mem_path: Path, items: list[dict[str, Any]]) -> None:
    """提前把向量缓存算好（启动时 / 后台 tick），别让第一句回复等编码。"""
    inputs = _vector_inputs(ctx, mem_path, items)
    if inputs is None:
        return
    pairs, model, tokenizer, cache = inputs
    embedding_store.sync(cache, pairs, model, tokenizer)


def _similarity(
    ctx: Any, mem_path: Path, items: list[dict[str, Any]], text: str
) -> Callable[[dict[str, Any]], float] | None:
    """构造"这条记忆与本轮说法有多像"的回调；没开或不可用时返回 ``None``。

    向量缓存与 ``memory.md`` 同目录，只补算新增/改过的记忆，所以日常一轮没有编码开销。
    """
    inputs = _vector_inputs(ctx, mem_path, items)
    if inputs is None:
        return None
    pairs, model, tokenizer, cache = inputs
    vectors = embedding_store.sync(cache, pairs, model, tokenizer)
    if not vectors:
        return None

    limit = max(16, _int_param(ctx, "embedding_max_query_chars", 256))
    scores = embedding_store.similarities(text[:limit], vectors, model, tokenizer)
    if not scores:
        return None
    # 绝对门槛之外再要"相对突出度"：短句的余弦基线普遍 0.45–0.55，
    # 只看绝对值，泛问句（"中午吃什么"）也能越过任何合理阈值。
    best = max(scores, key=lambda key: scores[key])
    margin = _float_param(ctx, "embedding_min_margin", 0.21)
    if margin > 0 and len(scores) >= 4:
        average = sum(scores.values()) / len(scores)
        if scores[best] - average < margin:
            return None
    # 回调给"每条记录自己的分"；"只取最像的一条"由 select_records 在过闸门的候选里做
    return lambda record: scores.get(str(record.get("key")), 0.0)


# -- 维护（遗忘扫描 + 增量总结） ------------------------------------------- #


def tick(ctx: Any) -> None:
    """周期性维护：先扫描遗忘，再按「轮数或时间」增量总结。"""
    try:
        _tick(ctx)
    except Exception as exc:  # noqa: BLE001 - 后台维护失败不能打断主循环
        ctx.log_error(f"维护失败, 原因: {type(exc).__name__}: {exc}")


def _tick(ctx: Any) -> None:
    paths = _paths(ctx)
    if paths is None:
        return
    group, msg_path, mem_path = paths
    if not _allowed(ctx, group):
        return  # 不在白名单的会话：不遗忘、不提炼、也不碰共享记忆库
    params = _params(ctx)
    now = _now(ctx)

    with _LOCK:
        items = mem.load_memory(mem_path)
        if items:
            marked = mem.decay_scan(items, now, params)
            if marked:
                mem.save_memory(mem_path, items)
                ctx.log_info(f"遗忘 {marked} 条（可提取度低于阈值）")
            _warm_vectors(ctx, mem_path, items)  # 新记忆在后台补向量，别卡回复

    _summarize(ctx, group, msg_path, mem_path)


def _summarize(ctx: Any, group: str, msg_path: Path, mem_path: Path) -> None:
    """满足条件时把"游标之后的新消息"总结进记忆库。"""
    entry = _group_state(ctx, group)
    pending = _state_int(entry, "pending_rounds")
    if pending <= 0:
        return

    params = _params(ctx)
    now = _now(ctx)
    rounds = max(1, _int_param(ctx, "summarize_rounds", 10))
    minutes = max(0, _int_param(ctx, "summarize_interval_minutes", 30))

    due = pending >= rounds
    if not due and minutes > 0:
        last = store.parse_time(entry.get("last_summarize_at"))
        if last is not None and (now - last).total_seconds() >= minutes * 60:
            due = True
    if not due:
        return

    cursor = _state_int(entry, "cursor")
    limit = max(1, _int_param(ctx, "max_increment_messages", 200))
    rows = store.read_messages(msg_path, limit=limit, offset=cursor)
    if not rows:
        # 没有新消息（例如 recent_memory 被关掉）：清计数，不调模型
        entry["pending_rounds"] = 0
        entry["last_summarize_at"] = store.format_time(now)
        return

    if not any(str(row.get("role") or "") == "user" for row in rows):
        _advance(entry, cursor, rows, now)  # 只有 AI 发言，没有可提炼的事实
        return

    prompt_text = _prompt_text(ctx)
    if not prompt_text:
        ctx.log_error("提炼提示词不可用，本次跳过（下一轮重试）")
        return

    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": _increment_block(ctx, mem_path, rows)},
    ]
    try:
        response = ctx.ask(
            messages, tag="memory_extract", response_format={"type": "json_object"}
        )
    except Exception as exc:  # noqa: BLE001 - 模型失败属于预期分支，下一轮重试
        ctx.log_error(f"提炼请求失败, 原因: {type(exc).__name__}: {exc}（保留进度，稍后重试）")
        return

    payload = _json_object(_content(response))
    memories = payload.get("memories") if isinstance(payload, dict) else None
    if not isinstance(memories, list):
        # 模型没按格式输出：推进游标，避免每个 tick 重复请求同一批
        ctx.log_error("提炼返回格式非法，已跳过该批")
        _advance(entry, cursor, rows, now)
        return

    first_seen = _first_time(rows, store.format_time(now))
    latest = _last_time(rows, store.format_time(now))
    added, updated, skipped = _merge(ctx, mem_path, memories, now, first_seen, latest)
    _advance(entry, cursor, rows, now)
    ctx.log_info(
        f"总结完成: 新增 {added} 条, 更新 {updated} 条, 丢弃 {skipped} 条, 读入 {len(rows)} 行"
    )


def _advance(entry: dict[str, Any], cursor: int, rows: list[dict[str, Any]], now: datetime) -> None:
    """推进游标与计数（只在"这一批已经处理完"时调用）。"""
    entry["cursor"] = cursor + len(rows)
    entry["pending_rounds"] = 0
    entry["last_summarize_at"] = store.format_time(now)


def _merge(
    ctx: Any,
    mem_path: Path,
    memories: list[Any],
    now: datetime,
    first_seen: str,
    updated_from: str,
) -> tuple[int, int, int]:
    """把模型输出合并进记忆库：同 key 更新、新 key 建立、非法项丢弃。"""
    params = _params(ctx)
    max_chars = _int_param(ctx, "summary_max_chars", 60)
    raw_extra = params.get("privacy_block_patterns")
    patterns = raw_extra if isinstance(raw_extra, (list, tuple)) else []
    now_text = store.format_time(now)

    added = updated = skipped = 0
    with _LOCK:
        items = mem.load_memory(mem_path)
        index = {str(record.get("key")): record for record in items}
        for raw in memories:
            if not isinstance(raw, dict):
                skipped += 1
                continue
            parts = mem.parse_key(raw.get("key"))
            if parts is None:
                skipped += 1
                ctx.log_error(f"记忆 key 非法，已丢弃: {raw.get('key')!r}")
                continue
            key = ".".join(parts)
            summary = store.mask_privacy(str(raw.get("summary") or ""), patterns)[0]
            summary = " ".join(summary.split())[:max_chars]
            if not summary:
                skipped += 1
                continue
            importance = _importance(raw.get("importance"))

            record = index.get(key)
            if record is None:
                # 同一事实换了说法（例如 习惯.美式咖啡 / 喜好.咖啡）：合并，不新建
                record = mem.find_similar(items, key)
                if record is not None:
                    index[key] = record
                    ctx.log_info(f"合并近义 key: {key} -> {record.get('key')}")
            if record is None:
                record = {
                    "key": key,
                    "summary": summary,
                    "importance": importance,
                    "status": mem.STATUS_ACTIVE,
                    "created_at": now_text,
                    "updated_at": now_text,
                    "last_recall_at": now_text,
                    "recalls": 0,
                    "stability_h": round(mem.initial_stability(importance, params), 2),
                    "first_seen": first_seen,
                    "updated_from": updated_from,
                }
                items.append(record)
                index[key] = record
                added += 1
                continue

            # 同一事实被重新确认：更新内容、刷新时间、视作一次复习（稳定度不降级）
            record["summary"] = summary
            record["importance"] = max(_importance(record), importance)
            record["status"] = mem.STATUS_ACTIVE  # 被重新提到 = 复活
            record.pop("forgotten_at", None)  # 复活后不该留着遗忘时间戳
            record["updated_at"] = now_text
            record["updated_from"] = updated_from
            record["last_recall_at"] = now_text
            fresh = mem.initial_stability(_importance(record), params)
            current = record.get("stability_h")
            try:
                current_value = float(current or 0.0)
            except (TypeError, ValueError):
                current_value = 0.0
            record["stability_h"] = round(max(current_value, fresh), 2)
            updated += 1

        if added or updated:
            mem.save_memory(mem_path, items)
    return added, updated, skipped


def _importance(raw: Any) -> float:
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.5


# -- 提示词与响应 ---------------------------------------------------------- #


def _prompt_text(ctx: Any) -> str:
    """读提炼提示词；相对路径按项目根解析（与 persona 模块同口径）。"""
    path = _project_path(str(_params(ctx).get("prompt_path") or "").strip(), DEFAULT_PROMPT)
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError) as exc:
        ctx.log_error(f"提示词读取失败（{path}）, 原因: {type(exc).__name__}: {exc}")
        return ""


def _increment_block(ctx: Any, mem_path: Path, rows: list[dict[str, Any]]) -> str:
    """拼提炼输入：已有 key 清单 + 本批新增聊天。"""
    fallback = _default_user(ctx)
    existing = [
        f"- {record.get('key')}：{record.get('summary')}"
        for record in mem.load_memory(mem_path)[:_MAX_EXISTING_LINES]
        if str(record.get("status") or mem.STATUS_ACTIVE) == mem.STATUS_ACTIVE
    ]

    lines: list[str] = []
    for row in rows:
        role = str(row.get("role") or "")
        speaker = _entity(
            row.get("speaker"), _SELF_LABEL if role == "assistant" else fallback
        )
        content = " ".join(str(row.get("content") or "").split())
        if not content:
            continue
        lines.append(f"- [{row.get('time') or ''}] {speaker}：{content[:_MAX_LINE_CHARS]}")

    parts: list[str] = []
    if existing:
        parts.append("已有记忆（同 key 请输出更新后的 summary）:\n" + "\n".join(existing))
    if lines:
        parts.append("新增聊天:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def _content(response: Any) -> str:
    """从 OpenAI 原始响应里取文本；取不到返回空串。"""
    if isinstance(response, str):
        return response
    choices = getattr(response, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if content is not None:
            return str(content)
    return ""


def _json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里取 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw[:4].lower() == "json":
            raw = raw[4:].strip()
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            loaded = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return loaded if isinstance(loaded, dict) else None


def _first_time(rows: list[dict[str, Any]], fallback: str) -> str:
    for row in rows:
        text = str(row.get("time") or "").strip()
        if text:
            return text
    return fallback


def _last_time(rows: list[dict[str, Any]], fallback: str) -> str:
    for row in reversed(rows):
        text = str(row.get("time") or "").strip()
        if text:
            return text
    return fallback
