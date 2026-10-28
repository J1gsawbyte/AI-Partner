"""身份库：QQ 号 → 真名，顺带提供"这个人是谁"的固定档案。

为什么只认 QQ 号
----------------
``speaker`` 一旦用显示名（群名片/昵称）当主键，改名就会把同一个人的记忆劈成两份——
旧回忆挂在旧名片下，新名片一条都召不回。所以身份只做一件事：**QQ 号与真名一一绑定**，
名片只用来兜底显示。没有 QQ 的人认不出来，暂时沿用群名片（通道会提示补登记）；
别名映射看着方便，但它本身又是一种"会用错"的猜测（两个人都叫"网格"时谁是真的？），
认人只留一条确定的路。

数据契约（``configs/identity.json``）
------------------------------------
```json
{
  "people": [
    {
      "name": "王俊宇",
      "qq": "1027713979",
      "enable": true,
      "profile": {"生日": "2005-03-14", "出生地": "湖南岳阳"}
    }
  ]
}
```

* ``name``：真名，`messages.md` / `memory.md` 里只出现它；
* ``qq``：认人主键（数字字符串），改名、换群名片都不影响；
* ``enable``：是否在白名单里（缺省 ``true``）。``false`` = 这个人发来的消息一律不接，
  **群聊和私聊都适用**；注意它和"没登记"是两回事——没登记的人在群里仍按群名片收，
  只是认不出真名、不能私聊；
* ``profile``：几乎不变的固定事实，供 ``identity`` 模块拼进 prompt。

查不到 / 文件缺失 / JSON 损坏一律返回空，不抛异常。本模块是纯工具（无状态、不读环境
之外的东西），``identity`` 模块与通道模块共用。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = ["IdentityBook", "Person", "clear_cache", "load"]

#: 缓存：path -> (mtime, book)；改文件后自动重读，不用重启进程
_CACHE: Final[dict[str, tuple[float, "IdentityBook"]]] = {}
_CACHE_LOCK: Final[threading.Lock] = threading.Lock()


@dataclass(frozen=True)
class Person:
    """一条身份记录。"""

    name: str
    qq: str = ""
    profile: dict[str, str] = field(default_factory=dict)
    #: 是否在白名单里；``False`` = 这个人发来的消息一律不接（群聊与私聊都适用）
    enabled: bool = True


def _norm(text: Any) -> str:
    return str(text or "").strip()


def _key(text: Any) -> str:
    """按名字查档案用：去空白 + 忽略大小写。"""
    return _norm(text).casefold()


def _flag(value: Any, default: bool = True) -> bool:
    """把 ``enable`` 解析成 bool；手写 JSON 里的 ``"false"`` / ``0`` / ``"否"`` 也算关。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off", "否", "关闭"}
    return default


class IdentityBook:
    """身份库的只读视图；构造时建好 QQ / 名字两侧索引（只收 ``enable`` 的人）。"""

    def __init__(self, people: list[Person]) -> None:
        self.people: tuple[Person, ...] = tuple(person for person in people if person.name)
        self._by_qq: dict[str, Person] = {}
        self._by_name: dict[str, Person] = {}
        self._blocked: set[str] = set()
        for person in self.people:
            qq = _norm(person.qq)
            if not person.enabled:
                if qq:
                    self._blocked.add(qq)
                continue
            if qq:
                # 同一 QQ 登记两次时以先出现的为准，避免后一条笔误把人认错
                self._by_qq.setdefault(qq, person)
            self._by_name.setdefault(_key(person.name), person)

    def __len__(self) -> int:
        return len(self.people)

    @property
    def enabled(self) -> tuple[Person, ...]:
        """白名单里的成员（``enable`` 为真、去重后）。"""
        return tuple(self._by_name.values())

    def resolve(self, qq: Any) -> str:
        """QQ 号 → 真名；没登记或已停用返回空串（调用方沿用群名片）。"""
        person = self._by_qq.get(_norm(qq))
        return person.name if person is not None else ""

    def is_blocked(self, qq: Any) -> bool:
        """这个 QQ 是否登记过但被 ``enable: false`` 停用（与"没登记"要分开提示）。"""
        return _norm(qq) in self._blocked

    def person(self, name: Any) -> Person | None:
        key = _key(name)
        return self._by_name.get(key) if key else None

    def profile_lines(self, name: Any) -> list[str]:
        """当前发言人的固定档案，用于拼 prompt 片段；没有就返回空列表。"""
        person = self.person(name)
        if person is None or not person.profile:
            return []
        lines = [f"- {label}：{value}" for label, value in person.profile.items() if _norm(value)]
        if not lines:
            return []
        return [f"- {person.name}"] + [f"  {line}" for line in lines]


def _parse(payload: Any) -> IdentityBook:
    people: list[Person] = []
    raw_people = payload.get("people") if isinstance(payload, dict) else None
    if not isinstance(raw_people, list):
        return IdentityBook([])
    for item in raw_people:
        if not isinstance(item, dict):
            continue
        name = _norm(item.get("name"))
        if not name:
            continue
        raw_profile = item.get("profile")
        profile = (
            {str(key): _norm(value) for key, value in raw_profile.items() if _norm(value)}
            if isinstance(raw_profile, dict)
            else {}
        )
        people.append(
            Person(
                name=name,
                qq=_norm(item.get("qq")),
                profile=profile,
                enabled=_flag(item.get("enable", True)),
            )
        )
    return IdentityBook(people)


def load(path: Path) -> IdentityBook:
    """读身份库（带 mtime 缓存）；文件缺失/损坏返回空库，不抛异常。"""
    key = str(path)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        with _CACHE_LOCK:
            _CACHE.pop(key, None)
        return IdentityBook([])

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, json.JSONDecodeError):
        payload = {}
    book = _parse(payload)
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, book)
    return book


def clear_cache() -> None:
    """测试或手工改文件后强制重读。"""
    with _CACHE_LOCK:
        _CACHE.clear()
