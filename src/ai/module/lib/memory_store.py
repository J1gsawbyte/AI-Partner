"""长期记忆库读写与遗忘曲线工具：``<message_path>/<群>/memory.md``。

只做"记忆条目"的纯计算与存取，**不含**任何钩子、配置或运行态；调度归
``src/ai/module/modules/long_memory.py``，本文件可脱离 ``ctx`` 单测。

| 函数 | 用途 |
| --- | --- |
| ``parse_key`` / ``make_key`` | 三段式 key ``{用户名}.{属性}.{次级属性}`` 的解析与生成 |
| ``memory_file`` / ``load_memory`` / ``save_memory`` | 记忆库路径与读写（原子重写，不删记录） |
| ``initial_stability`` / ``retention`` / ``reinforce`` / ``decay_scan`` | 艾宾浩斯遗忘曲线 |
| ``score_record`` / ``select_records`` | 本地匹配打分（零模型请求） |

遗忘口径
--------
* 可提取度 ``R = exp(-Δt_hours / stability_h)``，``stability_h`` 是稳定度（小时）；
* 初始稳定度 ``stability_h = stability_base_hours * (1 + importance * importance_factor)``；
* 命中一次（被注入）视为一次成功回忆：``recalls += 1``、稳定度乘 ``recall_growth``（有上限）；
* ``R < forget_threshold`` 且 ``importance < immortal_importance`` → ``status = "forgotten"``，
  记录保留在文件里但不再参与检索；重要度达标的记忆免疫遗忘，只在排序里降权。

排序口径
--------
``score_record`` 给出**字面匹配分**（实体 / 属性别名 / 次级属性），``select_records`` 再乘
``R`` 得到排序分：衰减影响的是"先想起谁"，是否被遗忘由 ``status`` 决定。
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Final, Iterable

# 复用项目既有工具（与 modules/recent_memory.py 一致：按目录补 sys.path）
_LIB_DIR: Final[Path] = Path(__file__).resolve().parent
_SRC_DIR: Final[Path] = _LIB_DIR.parents[2]
for _dir in (_LIB_DIR, _SRC_DIR / "logger"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]
from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = [
    "ATTRS",
    "DEFAULT_ALIASES",
    "STATUS_ACTIVE",
    "STATUS_FORGOTTEN",
    "decay_scan",
    "find_similar",
    "initial_stability",
    "is_immortal",
    "load_memory",
    "make_key",
    "memory_file",
    "parse_key",
    "reinforce",
    "retention",
    "save_memory",
    "score_record",
    "select_records",
]

#: 属性枚举（key 的第二段）：固定取值，模型给出的枚举外属性一律归入"其他"
ATTRS: tuple[str, ...] = (
    "身份",
    "喜好",
    "厌恶",
    "关系",
    "经历",
    "约定",
    "习惯",
    "计划",
    "其他",
)

#: 状态：``active`` 参与检索；``forgotten`` 只保留记录
STATUS_ACTIVE: Final[str] = "active"
STATUS_FORGOTTEN: Final[str] = "forgotten"

#: 没有说话人时的兜底实体名（与模块配置 ``default_user`` 同默认值）：
#: 用它判断"说话人是否已知"，进而决定要不要拦跨人召回。
_DEFAULT_USER: Final[str] = "默认用户"

#: 属性别名表（检索用）：属性本身 + 常见口语说法。
#: 只收"能指代主题"的词；"记得 / 一般 / 总是"这类泛动词副词会造成误召，故不收。
DEFAULT_ALIASES: dict[str, tuple[str, ...]] = {
    "身份": ("生日", "年龄", "多大", "几岁", "哪年", "家乡", "老家", "住在", "工作", "职业", "做什么", "上学", "学校", "名字", "叫什么"),
    "喜好": ("喜欢", "爱好", "爱吃", "爱喝", "爱看", "偏爱", "迷上", "最爱", "爱玩", "口味"),
    "厌恶": ("讨厌", "不喜欢", "受不了", "怕", "忌讳", "不能吃", "过敏", "反感"),
    "关系": ("老婆", "老公", "女朋友", "男朋友", "妈妈", "爸", "妈", "孩子", "同事", "朋友", "兄弟", "姐妹", "领导", "老板"),
    "经历": ("上次", "那次", "以前", "曾经", "去过", "做过", "发生", "当时", "小时候"),
    "约定": ("说好", "答应", "约好", "定好", "约了", "承诺"),
    "习惯": ("平时", "每天", "经常", "作息", "规律"),
    "计划": ("打算", "准备", "要去", "未来", "明年", "下周", "目标"),
    "其他": (),
}

#: 模型偶尔会把属性写成近义词，落库前统一（未知属性归"其他"）
_ATTR_SYNONYMS: Final[dict[str, str]] = {
    "爱好": "喜好",
    "偏好": "喜好",
    "喜欢": "喜好",
    "讨厌": "厌恶",
    "不喜欢": "厌恶",
    "信息": "身份",
    "资料": "身份",
    "人设": "身份",
    "履历": "经历",
    "往事": "经历",
    "承诺": "约定",
    "作息": "习惯",
}

#: key 单段最大长度（超过视为噪声，直接丢弃）
_MAX_PART_CHARS: Final[int] = 20

#: 字面匹配权重（属性单独命中不足以召回，必须带上实体或次级属性）
_W_ENTITY: Final[float] = 1.0
_W_ATTR: Final[float] = 0.8
_W_SUB: Final[float] = 1.0

#: 次级属性"只被 summary 字面覆盖"时的权重：单靠它达不到 min_score，
#: 必须配合实体或属性一起命中才注入（避免摘要里的普通词把无关记忆拉进来）
_W_SUMMARY: Final[float] = 0.6


# -- key ------------------------------------------------------------------ #


def _clean(text: Any, limit: int = _MAX_PART_CHARS) -> str:
    """去掉控制字符与多余空白，``.`` 换成 ``·``（点号是 key 分隔符）。"""
    if not isinstance(text, str):
        return ""
    kept = "".join(ch for ch in text if ord(ch) >= 32)
    cleaned = " ".join(kept.replace(".", "·").split())
    return cleaned[:limit]


def normalize_attr(raw: Any) -> str:
    """把属性归一到 :data:`ATTRS`；枚举外的归入"其他"。"""
    word = _clean(raw)
    if word in ATTRS:
        return word
    return _ATTR_SYNONYMS.get(word, "其他")


def parse_key(raw: Any) -> tuple[str, str, str] | None:
    """解析 ``{用户名}.{属性}.{次级属性}``；不合法返回 ``None``。

    必须恰好三段、每段非空、单段不超过 20 字。
    """
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(".")
    if len(parts) != 3:
        return None
    if any(len(part.strip()) > _MAX_PART_CHARS for part in parts):
        return None
    user, attr, sub = (_clean(part) for part in parts)
    if not user or not attr or not sub:
        return None
    return user, normalize_attr(attr), sub


def make_key(user: Any, attr: Any, sub: Any) -> str:
    """拼出规范 key；任一段清洗后为空则返回 ``""``。"""
    head, middle, tail = _clean(user), normalize_attr(attr), _clean(sub)
    if not head or not tail:
        return ""
    return f"{head}.{middle}.{tail}"


def _related_sub(left: str, right: str) -> bool:
    """两个次级属性是否在说同一件事：相等 / 互为包含 / 共享一个双字词。"""
    if left == right or left in right or right in left:
        return True
    if len(left) < 2 or len(right) < 2:
        return False
    return any(left[index : index + 2] in right for index in range(len(left) - 1))


#: 语义相反的属性对：次级属性相同也不能合并（"喜欢篮球"和"讨厌篮球"是两件事）
_CONFLICTING_ATTRS: Final[frozenset[frozenset[str]]] = frozenset(
    {frozenset({"喜好", "厌恶"})}
)


def find_similar(records: Iterable[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """找"近义 key"的既有记录；用于合并而不是新建。

    模型两次总结可能把同一事实写成 ``汪敬杨.习惯.美式咖啡`` 和 ``汪敬杨.喜好.咖啡``，
    若不合并，记忆会被拆成多条：检索重复、遗忘不同步（一条遗忘了另一条还在）。
    判定口径：实体相同 + 次级属性相关 + 属性不冲突（``喜好``/``厌恶`` 视为相反）。
    """
    parts = parse_key(key)
    if parts is None:
        return None
    user, attr, sub = parts
    for record in records:
        other = parse_key(record.get("key"))
        if other is None:
            continue
        other_user, other_attr, other_sub = other
        if other_user.lower() != user.lower():
            continue
        if frozenset({attr, other_attr}) in _CONFLICTING_ATTRS:
            continue
        if _related_sub(sub, other_sub):
            return record
    return None


# -- 存取 ------------------------------------------------------------------ #


def memory_file(root: str | Path, group: str) -> Path:
    """某个群的记忆库文件：``<root>/<群>/memory.md``（与 ``messages.md`` 同目录）。"""
    return store.message_file(root, group).with_name("memory.md")


def load_memory(path: Path) -> list[dict[str, Any]]:
    """读取记忆库（坏行跳过，同 key 以最后一行为准）；读失败返回空列表。"""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        core_log.error(f"长期记忆库读取失败, 原因: {type(exc).__name__}: {exc}")
        return []

    records: dict[str, dict[str, Any]] = {}
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            bad += 1
            continue
        key = item["key"].strip()
        if not key:
            bad += 1
            continue
        item["key"] = key
        records.pop(key, None)  # 后出现的同名 key 覆盖前面的
        records[key] = item

    if bad:
        core_log.error(f"长期记忆库有 {bad} 行无法解析, 已跳过: {path}")
    return list(records.values())


def save_memory(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """全量重写记忆库（一行一个 JSON）；临时文件 + ``os.replace`` 原子替换。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")
            handle.flush()
        os.replace(temp, target)
    except OSError as exc:
        core_log.error(f"长期记忆库写入失败, 原因: {type(exc).__name__}: {exc}")
        raise


# -- 遗忘曲线 -------------------------------------------------------------- #


def _num(params: dict[str, Any] | None, key: str, default: float) -> float:
    """读取数值型配置；缺失或非法时用默认值。"""
    raw = (params or {}).get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _importance(record: dict[str, Any]) -> float:
    try:
        return _clamp(float(record.get("importance", 0.5)))
    except (TypeError, ValueError):
        return 0.5


def is_immortal(record: dict[str, Any], params: dict[str, Any] | None = None) -> bool:
    """重要度达到 ``immortal_importance`` 的记忆免疫遗忘。"""
    return _importance(record) >= _num(params, "immortal_importance", 0.8)


def initial_stability(importance: float, params: dict[str, Any] | None = None) -> float:
    """初始稳定度（小时）：底数 × (1 + 重要度 × 系数)。"""
    base = _num(params, "stability_base_hours", 24.0)
    factor = _num(params, "importance_factor", 3.0)
    return base * (1.0 + _clamp(importance) * factor)


def _last_touch(record: dict[str, Any]) -> datetime | None:
    """上次"被想起"的时刻：召回时间 > 更新时间 > 创建时间。"""
    for field in ("last_recall_at", "updated_at", "created_at"):
        moment = store.parse_time(record.get(field))
        if moment is not None:
            return moment
    return None


def retention(
    record: dict[str, Any], now: datetime, params: dict[str, Any] | None = None
) -> float:
    """当前可提取度 ``R = exp(-Δt_h / stability)``；没有时间戳按"刚想起"算 1.0。"""
    moment = _last_touch(record)
    if moment is None:
        return 1.0
    hours = max(0.0, (now - moment).total_seconds() / 3600.0)
    try:
        stability = float(record.get("stability_h") or 0.0)
    except (TypeError, ValueError):
        stability = 0.0
    if stability <= 0:
        stability = _num(params, "stability_base_hours", 24.0)
    return math.exp(-hours / stability)


def reinforce(
    record: dict[str, Any], now: datetime, params: dict[str, Any] | None = None
) -> None:
    """命中一次：召回次数 +1，稳定度按倍数增长（封顶 ``stability_max_hours``）。"""
    growth = _num(params, "recall_growth", 1.8)
    cap = _num(params, "stability_max_hours", 720.0)
    try:
        stability = float(record.get("stability_h") or 0.0)
    except (TypeError, ValueError):
        stability = 0.0
    if stability <= 0:
        stability = initial_stability(_importance(record), params)
    try:
        recalls = int(record.get("recalls") or 0)
    except (TypeError, ValueError):
        recalls = 0

    record["recalls"] = recalls + 1
    record["last_recall_at"] = store.format_time(now)
    record["stability_h"] = round(min(stability * growth, cap), 2)


def decay_scan(
    records: Iterable[dict[str, Any]], now: datetime, params: dict[str, Any] | None = None
) -> int:
    """扫描存活记忆：``R`` 低于阈值且非免疫 → 置 ``forgotten``；返回本次标记条数。"""
    threshold = _num(params, "forget_threshold", 0.25)
    marked = 0
    for record in records:
        if str(record.get("status") or STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        if is_immortal(record, params):
            continue
        if retention(record, now, params) < threshold:
            record["status"] = STATUS_FORGOTTEN
            record["forgotten_at"] = store.format_time(now)
            marked += 1
    return marked


# -- 检索 ------------------------------------------------------------------ #


def _merge_aliases(overrides: Any) -> dict[str, tuple[str, ...]]:
    """内置别名表 + 配置追加项（配置里的词追加，不覆盖内置）。"""
    aliases: dict[str, tuple[str, ...]] = {attr: words for attr, words in DEFAULT_ALIASES.items()}
    if not isinstance(overrides, dict):
        return aliases
    for raw_attr, words in overrides.items():
        attr = normalize_attr(raw_attr)
        extra = tuple(
            str(word).strip() for word in (words if isinstance(words, (list, tuple)) else [])
        )
        extra = tuple(word for word in extra if word)
        aliases[attr] = aliases.get(attr, ()) + extra
    return aliases


#: 数量词与时间/单位词：这两类字凑成的双字词（``周三``/``三次``/``一楼``/``两点``）
#: 在摘要检索里只表示"时间或数量"，跟着句子走，不构成主题命中。
_NUM_CHARS: Final[frozenset[str]] = frozenset("零一二三四五六七八九十百千万亿两半0123456789")
_TIME_UNIT_CHARS: Final[frozenset[str]] = frozenset("周星期月日号点次分秒届级班楼元块岁天年")


def _numeric_gram(gram: str) -> bool:
    """这个双字词是不是"数字 + 时间/单位"组合（如 ``周三`` / ``三次``）。"""
    return any(ch in _NUM_CHARS for ch in gram) and any(ch in _TIME_UNIT_CHARS for ch in gram)


def _attr_hit(attr: str, text: str, aliases: dict[str, tuple[str, ...]]) -> bool:
    """属性命中：属性名本身或它的别名出现在本轮文本里。"""
    for word in (attr, *aliases.get(attr, ())):
        if word and word in text:
            return True
    return False


def _sub_hit(sub: str, text: str) -> bool:
    """次级属性命中：短词直接包含，长词按相邻两字（中文双字词）判断。"""
    if sub in text:
        return True
    if len(sub) <= 2:
        return False
    return any(sub[index : index + 2] in text for index in range(len(sub) - 1))


#: 太通用的双字词，不参与"摘要检索"（否则"今天""什么"之类会把记忆乱召回）
_STOP_BIGRAMS: Final[frozenset[str]] = frozenset(
    {
        "你还", "还记", "记得", "得我", "我爱", "我们", "你们", "他们", "什么", "怎么",
        "今天", "明天", "昨天", "现在", "时候", "一个", "可以", "不是", "就是", "知道",
        "觉得", "真的", "已经", "还是", "然后", "这个", "那个", "有点", "一下", "一些",
        "最近", "小时", "刚才", "马上", "一直", "有时候",
    }
)


def _summary_hit(summary: str, text: str) -> bool:
    """摘要字面命中：本轮文本里的双字词出现在 summary 里。

    模型给 key 第三段起的名字不稳定（同一事实会写成 ``美式咖啡`` / ``咖啡`` /
    ``早晨喝美式``），只按 key 匹配会漏；summary 是自然语言，天然包含这些说法。
    只取"文本 → 摘要"方向，且过滤两类 gram：通用双字词（``今天``/``什么``），
    以及"数字 + 时间/量词"（``周三`` 会被 ``每周三次`` 命中，纯属跨词误配）。
    """
    if not summary or len(text) < 2:
        return False
    for index in range(len(text) - 1):
        gram = text[index : index + 2]
        if gram in _STOP_BIGRAMS:
            continue
        if _numeric_gram(gram):
            continue
        if gram in summary:
            return True
    return False


def score_record(
    record: dict[str, Any],
    text: str,
    speaker: str,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> tuple[float, dict[str, float]]:
    """字面匹配打分，返回 ``(分数, 命中项)``；不满足召回门槛时分数为 0。

    命中门槛：``次级属性命中`` 或 ``实体命中 + 属性命中``。
    """
    parts = parse_key(record.get("key"))
    if parts is None or not text:
        return 0.0, {}
    user, attr, sub = parts
    table = aliases if aliases is not None else DEFAULT_ALIASES
    lowered = text.lower()
    user_lower = user.lower()

    entity = 0.0
    if speaker and user_lower == speaker.strip().lower():
        entity = 1.0
    elif user_lower and user_lower in lowered:
        entity = 0.7

    attr_hit = 1.0 if _attr_hit(attr, lowered, table) else 0.0
    if _sub_hit(sub.lower(), lowered):
        sub_hit = 1.0
    elif _summary_hit(str(record.get("summary") or "").lower(), lowered):
        sub_hit = _W_SUMMARY  # 摘要里命中：算部分命中，权重略低于 key 本身
    else:
        sub_hit = 0.0

    if not sub_hit and not (entity and attr_hit):
        return 0.0, {}
    score = entity * _W_ENTITY + attr_hit * _W_ATTR + sub_hit * _W_SUB
    return round(score, 3), {"entity": entity, "attr": attr_hit, "sub": sub_hit}


def _identity_hit(
    record: dict[str, Any],
    text: str,
    speaker: str,
    aliases: dict[str, tuple[str, ...]],
    *,
    strict: bool = False,
) -> bool:
    """身份闸：说话人/被点名的人对得上；说话人未知时才退化为"属性词对得上"。

    语义检索只允许"补召回"，不允许"跨人召回"（汪敬杨的咖啡不能喂给张天宇）。
    ``strict``（说话人已知）下属性词不再单独开闸——否则 A 说一句"我喜欢手冲咖啡"，
    就会把 B 的咖啡记忆也捞进来。
    """
    parts = parse_key(record.get("key"))
    if parts is None:
        return False
    user, attr, _sub = parts
    lowered = text.lower()
    if speaker and user.lower() == speaker.strip().lower():
        return True
    if user.lower() and user.lower() in lowered:
        return True
    if strict:
        return False
    return _attr_hit(attr, lowered, aliases)


def select_records(
    records: Iterable[dict[str, Any]],
    text: str,
    speaker: str,
    now: datetime,
    params: dict[str, Any] | None = None,
    similarity: Callable[[dict[str, Any]], float] | None = None,
) -> list[tuple[float, dict[str, Any], dict[str, float]]]:
    """挑出本轮该注入的记忆：``status=active``、过门槛、按 ``分数 × R`` 排序取 top-k。

    ``similarity`` 是可选语义检索回调（返回 0–1 的相似度，按记录各自给分）。不传时行为
    与之前一致；传了也只用于"字面没命中、但身份对得上"的记录补召回，门槛是
    ``embedding_min_cos``，且**只从过闸门的候选里取最像的一条**——先取全局最像再判闸门，
    会让别人的记忆把本人那条挤掉。

    说话人已知（且不是 ``default_user``）时按"严格"口径：记的是别人又没被点名的条目
    不召回，避免 A 问自己的生日却把 B 的生日灌进去。

    返回 ``[(排序分, 记录, 命中项)]``；记录是原对象（调用方可就地强化）。
    """
    if not text or not text.strip():
        return []

    table = _merge_aliases((params or {}).get("attribute_aliases"))
    min_score = _num(params, "min_score", 0.8)
    min_cos = _num(params, "embedding_min_cos", 0.55)
    top_k = int(_num(params, "inject_top_k", 3))
    floor = _num(params, "immortal_floor", 0.4)
    default_user = str((params or {}).get("default_user") or _DEFAULT_USER).strip() or _DEFAULT_USER
    strict = bool(speaker and speaker.strip()) and speaker.strip() != default_user

    scored: list[tuple[float, dict[str, Any], dict[str, float], bool]] = []
    for record in records:
        if str(record.get("status") or STATUS_ACTIVE) != STATUS_ACTIVE:
            continue
        base, hits = score_record(record, text, speaker, table)
        if strict and not hits.get("entity"):
            base, hits = 0.0, {}  # 别人的记忆，本轮又没提到他
        passed = base > 0 and base >= min_score
        scored.append((base, record, hits, passed))

    if similarity is not None:
        best: tuple[float, dict[str, Any], dict[str, float]] | None = None
        for base, record, hits, passed in scored:
            if passed or not _identity_hit(record, text, speaker, table, strict=strict):
                continue
            try:
                cos = float(similarity(record) or 0.0)
            except Exception:  # noqa: BLE001 - 语义检索坏了只影响召回，不影响回复
                cos = 0.0
            if cos >= min_cos and (best is None or cos > best[0]):
                best = (cos, record, hits)
        if best is not None:
            cos, record, hits = best
            for index, (base, item, item_hits, _passed) in enumerate(scored):
                if item is record:
                    scored[index] = (max(base, cos), item, {**item_hits, "sim": round(cos, 3)}, True)
                    break

    picked: list[tuple[float, dict[str, Any], dict[str, float]]] = []
    for base, record, hits, passed in scored:
        if not passed:
            continue
        decay = retention(record, now, params)
        if is_immortal(record, params):
            decay = max(decay, floor)
        picked.append((round(base * decay, 4), record, hits))

    picked.sort(key=lambda item: item[0], reverse=True)
    return picked[:top_k] if top_k > 0 else picked
