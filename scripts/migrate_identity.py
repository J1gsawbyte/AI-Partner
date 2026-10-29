"""把历史数据里的「旧名字」换成「真名」（迁移脚本，按需手动跑）。

为什么需要迁移
--------------
``qq_channel`` 以前直接拿群名片当 ``speaker``，于是 ``messages.md`` 的 ``speaker`` 与
``memory.md`` 的 key 前缀都是"当时的名字"（``mita`` / ``国家一级不保护废物``）。改成按
QQ 号认人之后，新数据写真名，旧数据还挂在旧名字下——不迁移就等于记忆被劈成两截。

改哪些、不改哪些
----------------
* 改 ``messages.md``：``role=user`` 行的 ``speaker``（旧名 → 真名）；
* 改 ``memory.md``：key 的第一段 ``{用户名}.{属性}.{次级属性}``；
* **不改** ``role=assistant`` 的行与 ``我.*`` 的 key：``我`` 是 AI 自称，不是群成员；
* **不改** 没给映射的名字：只列进报告，避免把陌生人硬塞进某个人的档案。

映射只认命令行（身份库不再存别名）：``--map 旧名=真名``，可重复。所以改完
``configs/identity.json`` 里的 ``name`` 也要用 ``--map 旧名=新名`` 把历史数据跟过去。

默认是**预演**（只看报告、不写盘）；加 ``--apply`` 才真正改，改前自动备份成
``messages.md.bak-<时间戳>``，改后如有同 key 冲突会合并（保留更新时间较新的一条）。

用法::

    python scripts/migrate_identity.py --map mita=wjy                      # 预演
    python scripts/migrate_identity.py --map mita=wjy --group 949207129     # 只处理一个群
    python scripts/migrate_identity.py --map mita=wjy --apply               # 落盘（带备份）
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_LIB_DIR: Final[Path] = _ROOT / "src" / "ai" / "module" / "lib"
for _dir in (_LIB_DIR, _ROOT / "src" / "logger"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

import identity_store  # noqa: E402  # pyright: ignore[reportMissingImports]
import memory_store  # noqa: E402  # pyright: ignore[reportMissingImports]

#: 群聊会话目录（每个群一个子目录）；私聊在 ``data/private`` 下，需要时用 --root 指过去
DEFAULT_ROOT: Final[str] = "data/groups"

#: 身份表默认位置
DEFAULT_IDENTITY: Final[str] = "configs/identity.json"

#: AI 自称：assistant 行的 speaker 与 memory key 的第一段，迁移时必须原样保留
_ASSISTANT_NAMES: Final[frozenset[str]] = frozenset({"我", "AI", "助手"})


class _Report:
    """一次迁移的统计，最后统一打印（预演与落盘共用同一份口径）。"""

    def __init__(self) -> None:
        self.groups: list[str] = []
        self.message_hits: dict[tuple[str, str], int] = {}
        self.key_hits: dict[tuple[str, str], int] = {}
        self.merged: list[str] = []
        self.unknown: dict[str, int] = {}

    def add_unknown(self, name: str) -> None:
        self.unknown[name] = self.unknown.get(name, 0) + 1

    @property
    def changed(self) -> bool:
        return bool(self.message_hits or self.key_hits or self.merged)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _build_lookup(
    pairs: list[str], book: identity_store.IdentityBook
) -> tuple[dict[str, str], set[str]]:
    """``--map 旧名=真名`` → 映射表；身份库里的真名也算"认识"（避免误报认不出）。"""
    lookup: dict[str, str] = {}
    known = {person.name.casefold() for person in book.people if person.name}
    for raw in pairs:
        old, _, new = str(raw).partition("=")
        old, new = _norm(old), _norm(new)
        if not old or not new or old == new:
            print(f"[忽略] 映射无效: {raw!r}（格式：--map 旧名=真名）", file=sys.stderr)
            continue
        lookup[old.casefold()] = new
        known.update({old.casefold(), new.casefold()})
    return lookup, known


def _lookup_name(lookup: dict[str, str], name: str) -> str:
    return lookup.get(name.casefold(), "") if name else ""


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def migrate_messages(
    path: Path, lookup: dict[str, str], known: set[str], report: _Report, *, apply: bool
) -> None:
    """重写 ``messages.md`` 里 user 行的 speaker；未改动的行原样保留。"""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        print(f"[跳过] {path} 读取失败: {type(exc).__name__}: {exc}")
        return

    out: list[str] = []
    touched = 0
    for line in lines:
        text = line.strip()
        if not text:
            out.append(line)
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError:
            out.append(line)
            continue
        if not isinstance(item, dict) or str(item.get("role") or "") == "assistant":
            out.append(line)
            continue

        speaker = _norm(item.get("speaker"))
        real = _lookup_name(lookup, speaker)
        if not real or real == speaker:
            if speaker and speaker.casefold() not in known and speaker not in _ASSISTANT_NAMES:
                report.add_unknown(speaker)
            out.append(line)
            continue

        item["speaker"] = real
        out.append(json.dumps(item, ensure_ascii=False))
        touched += 1
        report.message_hits[(speaker, real)] = report.message_hits.get((speaker, real), 0) + 1

    if not touched:
        print(f"[消息] {path} 无需改动")
        return
    print(f"[消息] {path} 改写 {touched} 行 speaker")
    if apply:
        _backup(path)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _merge_into(target: dict[str, Any], extra: dict[str, Any]) -> None:
    """同 key 冲突合并：新的那条当主体，保留更大的重要度与召回次数。"""
    newer, older = (extra, target) if _is_newer(extra, target) else (target, extra)
    merged = dict(newer)
    try:
        merged["importance"] = max(float(newer.get("importance", 0)) or 0.0,
                                   float(older.get("importance", 0)) or 0.0)
    except (TypeError, ValueError):
        pass
    try:
        merged["recalls"] = int(newer.get("recalls") or 0) + int(older.get("recalls") or 0)
    except (TypeError, ValueError):
        pass
    merged["first_seen"] = min(
        str(newer.get("first_seen") or ""), str(older.get("first_seen") or "")
    ) or str(newer.get("first_seen") or "")
    target.clear()
    target.update(merged)


def _is_newer(left: dict[str, Any], right: dict[str, Any]) -> bool:
    stamp_left = str(left.get("updated_at") or left.get("created_at") or "")
    stamp_right = str(right.get("updated_at") or right.get("created_at") or "")
    return stamp_left >= stamp_right


def migrate_memory(
    path: Path, lookup: dict[str, str], known: set[str], report: _Report, *, apply: bool
) -> None:
    """重写 ``memory.md`` 的 key 前缀；不同旧名落到同一 key 时合并。"""
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        print(f"[跳过] {path} 读取失败: {type(exc).__name__}: {exc}")
        return

    records = memory_store.load_memory(path)
    by_key: dict[str, dict[str, Any]] = {}
    touched = 0
    for record in records:
        parts = memory_store.parse_key(record.get("key"))
        key = str(record.get("key") or "").strip()
        if parts is None:
            by_key[key] = record
            continue

        user, attr, sub = parts
        real = "" if user in _ASSISTANT_NAMES else _lookup_name(lookup, user)
        if not real or real == user:
            if user and user.casefold() not in known and user not in _ASSISTANT_NAMES:
                report.add_unknown(user)
            by_key[key] = record
            continue

        new_key = memory_store.make_key(real, attr, sub)
        if not new_key:
            by_key[key] = record
            continue
        record["key"] = new_key
        touched += 1
        report.key_hits[(user, real)] = report.key_hits.get((user, real), 0) + 1
        if new_key in by_key:
            _merge_into(by_key[new_key], record)
            report.merged.append(new_key)
        else:
            by_key[new_key] = record

    if not touched:
        print(f"[记忆] {path} 无需改动")
        return
    print(
        f"[记忆] {path} 改写 {touched} 条 key"
        + (f"，合并 {len(report.merged)} 条重复" if report.merged else "")
        + f"（读入 {len(raw_lines)} 行 / 有效 {len(records)} 条）"
    )
    if apply:
        _backup(path)
        memory_store.save_memory(path, list(by_key.values()))


def _group_dirs(root: Path, groups: list[str]) -> list[Path]:
    if groups:
        return [root / name for name in groups]
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _print_report(report: _Report, *, apply: bool) -> None:
    print("-" * 60)
    for (old, new), count in sorted(report.message_hits.items()):
        print(f"speaker: {old} → {new}（{count} 行）")
    for (old, new), count in sorted(report.key_hits.items()):
        print(f"key 前缀: {old} → {new}（{count} 条）")
    for key in report.merged:
        print(f"合并重复 key: {key}")
    if report.unknown:
        items = ", ".join(f"{name}({count})" for name, count in sorted(report.unknown.items()))
        print(f"认不出、未改动: {items}")
    total = sum(report.message_hits.values()) + sum(report.key_hits.values())
    mode = "已落盘" if apply else "预演（未写盘，加 --apply 生效）"
    print(f"合计改写 {total} 处，群 {len(report.groups)} 个 | {mode}")
    if apply and report.changed:
        print("提示：memory.vec.npz 无需手动处理，下次检索会按新 key 自动补算。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把历史消息/记忆里的旧名字迁移成真名")
    parser.add_argument("--group", action="append", default=[], help="只处理指定群（可重复）")
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"消息库根目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument(
        "--identity", default=DEFAULT_IDENTITY, help=f"身份库（默认 {DEFAULT_IDENTITY}，只用来判断认识谁）"
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="旧名=真名",
        help="要改的名字映射（可重复），如 --map mita=wjy",
    )
    parser.add_argument("--apply", action="store_true", help="真正写盘（默认只预演）")
    args = parser.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
        except (AttributeError, OSError, ValueError):
            pass

    root = Path(args.root) if Path(args.root).is_absolute() else _ROOT / args.root
    identity = Path(args.identity) if Path(args.identity).is_absolute() else _ROOT / args.identity
    book = identity_store.load(identity)
    if not identity.is_file():
        print(f"提示：身份库不存在（{identity}），本次只按 --map 迁移", file=sys.stderr)
    lookup, known = _build_lookup(args.map, book)
    if not lookup:
        print("没有要改的名字：用 --map 旧名=真名 指定，如 --map mita=wjy", file=sys.stderr)
        return 2

    report = _Report()
    for group_dir in _group_dirs(root, args.group):
        messages = group_dir / "messages.md"
        memory = group_dir / "memory.md"
        if not messages.is_file() and not memory.is_file():
            continue
        report.groups.append(group_dir.name)
        if messages.is_file():
            migrate_messages(messages, lookup, known, report, apply=args.apply)
        if memory.is_file():
            migrate_memory(memory, lookup, known, report, apply=args.apply)

    if not report.groups:
        print(f"没有找到群数据: {root}", file=sys.stderr)
        return 1
    _print_report(report, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
