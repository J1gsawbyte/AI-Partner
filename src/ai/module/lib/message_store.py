"""消息库读写工具：``<message_path>/<群>/messages.md``（一行一个 JSON）。

只做"会话消息"的存取与隐私打码，**不含任何长期记忆/原子逻辑**。
长期记忆重构时可以直接复用这一层，替换上面的检索与总结实现。

| 函数 | 用途 |
| --- | --- |
| ``message_file`` | 拼出某群的消息文件路径 |
| ``append_messages`` | 追加消息行（user / assistant / 主动消息） |
| ``read_messages`` | 读取消息（跳过坏行，支持切片） |
| ``count_messages`` | 统计有效消息行数 |
| ``mask_privacy`` | 落盘前打码（内置规则 + 模块追加规则） |
| ``validate_file_stem`` | 校验群名可作目录名 |
| ``format_time`` / ``parse_time`` | 统一时间格式 ``YYYY-MM-DD HH:MM:SS`` |
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

# 复用本项目的 logger（与 post.py / jsonReader.py / module_context.py 一致）
_SRC_DIR = Path(__file__).resolve().parents[3]
_LOGGER_DIR = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = [
    "BUILTIN_PRIVACY_PATTERNS",
    "TIME_FMT",
    "append_messages",
    "count_messages",
    "format_time",
    "mask_privacy",
    "message_file",
    "parse_time",
    "read_messages",
    "validate_file_stem",
]

#: 统一时间格式（与 alpha 一致，便于人工核对）
TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: 内置隐私检测规则（始终生效，模块配置只能追加）
BUILTIN_PRIVACY_PATTERNS: tuple[str, ...] = (
    r"(密码|口令|passwd|password)\s*[:：=]?\s*\S+",
    r"(验证码|校验码)\s*[:：]?\s*\d{4,8}",
    r"\b\d{17}[\dXx]\b",
    r"\b\d{16,19}\b",
    r"\b1[3-9]\d{9}\b",
)

#: 脱敏规则（比检测规则更宽：连"是/为"和值一起替换，避免残缺泄漏）
MASK_RULES: tuple[tuple[str, str], ...] = (
    (r"(密码|口令|passwd|password)\s*(?:是|为|[:：=])?\s*[^\s，。；,;、]{1,24}", r"\1：[已隐去]"),
    (r"(验证码|校验码)\s*(?:是|为|[:：])?\s*\d{4,8}", r"\1：[已隐去]"),
    (r"\b\d{17}[\dXx]\b", "[已隐去]"),
    (r"\b\d{16,19}\b", "[已隐去]"),
    (r"\b1[3-9]\d{9}\b", "[已隐去]"),
)

#: 正则元字符（用于判断配置项是"关键词"还是"正则"）
_REGEX_META: set[str] = set("\\^$.*+?()[]{}|")

#: 文件名禁止字符（Windows 约定，与 logger / jsonReader 同款）
_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')
_MAX_STEM_LENGTH = 50
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def format_time(value: datetime) -> str:
    """把 ``datetime`` 格式化为 ``YYYY-MM-DD HH:MM:SS``。"""
    return value.strftime(TIME_FMT)


def parse_time(text: Any) -> datetime | None:
    """解析时间字符串；失败返回 ``None``（调用方按"未过期"保守处理）。"""
    if isinstance(text, datetime):
        return text
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.strptime(text.strip(), TIME_FMT)
    except ValueError:
        return None


def message_file(root: str | Path, group: str) -> Path:
    """某个群的消息库文件：``<root>/<group>/messages.md``。

    ``root`` 是程序配置 ``message_path`` 指向的目录；每个群一个文件，互不干扰。
    """
    return Path(root) / str(group) / "messages.md"


def mask_privacy(text: str, extra_patterns: Iterable[str] = ()) -> tuple[str, list[str]]:
    """把隐私片段就地打码（内置规则 + 追加规则）。

    返回 ``(打码后的文本, 非法正则列表)``；内置规则始终生效，追加规则不含正则
    元字符时按「关键词 + 可选值」整体打码，避免只删词留下值。
    """
    if not text:
        return "", []

    masked = str(text)
    for pattern, repl in MASK_RULES:
        masked = re.sub(pattern, repl, masked, flags=re.IGNORECASE)

    bad: list[str] = []
    for raw in extra_patterns:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        candidate = pattern
        if not (set(pattern) & _REGEX_META):
            candidate = re.escape(pattern) + r"(?:\s*(?:是|为|[:：=])?\s*[^，。；,;、]{0,24})?"
        try:
            masked = re.sub(candidate, "[已隐去]", masked, flags=re.IGNORECASE)
        except re.error:
            bad.append(pattern)
    return masked, bad


def validate_file_stem(name: Any) -> str:
    """校验可作为目录名的标识；合规返回去空白后的字符串，否则返回 ``""``。"""
    if not isinstance(name, str):
        return ""
    stem = name.strip()
    if not stem or len(stem) > _MAX_STEM_LENGTH:
        return ""
    if stem in {".", ".."} or stem.endswith(".") or stem.startswith("."):
        return ""
    if any(ch in _ILLEGAL_CHARS for ch in stem):
        return ""
    if any(ord(ch) < 32 for ch in stem):
        return ""
    if stem.split(".")[0].strip().upper() in _RESERVED_NAMES:
        return ""
    return stem


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def append_messages(path: Path, rows: Sequence[dict[str, Any]]) -> int:
    """追加消息行（一行一个 JSON），返回写入条数；失败抛 ``OSError``。"""
    if not rows:
        return 0
    _ensure_parent(path)
    lines = [json.dumps(row, ensure_ascii=False) for row in rows]
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            for line in lines:
                handle.write(line + "\n")
            handle.flush()
    except OSError as exc:
        core_log.error(f"message_store 消息写入失败, 原因: {type(exc).__name__}: {exc}")
        raise
    return len(lines)


def read_messages(path: Path, limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    """读取消息（跳过坏行），支持切片；读失败返回空列表。"""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except OSError as exc:
        core_log.error(f"message_store 消息读取失败, 原因: {type(exc).__name__}: {exc}")
        return []

    rows: list[dict[str, Any]] = []
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
        if isinstance(item, dict):
            rows.append(item)

    if bad:
        core_log.error(f"message_store 消息文件有 {bad} 行无法解析, 已跳过: {path}")

    start = max(0, int(offset or 0))
    if limit is None:
        return rows[start:]
    return rows[start : start + max(0, int(limit))]


def count_messages(path: Path) -> int:
    """统计有效消息行数；读失败返回 0。"""
    return len(read_messages(path))
