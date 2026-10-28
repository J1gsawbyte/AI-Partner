"""回复分句模块：把 AI 的一次回答拆成多条短消息，模拟人类连发。

钩子：``split(ctx) -> list[str]``，读取 ``ctx.reply``（本轮回答）与 ``ctx.params``。

配置（``configs/modules/reply_split.json`` 的 ``config``）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `max_chars` | 25 | 每段最多多少个字符（含标点）；超长句按此硬切 |
| `max_sentences` | 3 | 每段最多几句 |
| `terminators` | `。！？!?；;…` | 句末标点集合，可按习惯增删 |

处理流程（每一步都是独立可复用的函数，方便测试与扩展）：

1. 按换行切段落（一个自然段至少单独成条）；
2. :func:`split_sentences`：段内按句末标点（含连续标点、``...``）切句；
3. :func:`group_segments`：贪心合并相邻句子，段长与句数都不超上限；
4. :func:`split_reply`：组合上面三步，返回最终分段。

设计取舍：只做"切分"，不改写文字（不删标点、不加语气词），因为改写属于内容生成，
应该由模型或别的模块负责；配置非法时回退默认值并记 ``core_log.error``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final, Iterable

_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LOGGER_DIR: Final[Path] = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["split", "split_reply", "split_sentences", "group_segments"]

_DEFAULT_MAX_CHARS: Final[int] = 25
_DEFAULT_MAX_SENTENCES: Final[int] = 3
_DEFAULT_TERMINATORS: Final[str] = "。！？!?；;…"

#: ASCII 省略号按整体处理，避免被拆成三个切点
_ASCII_ELLIPSIS: Final[str] = "..."

#: 超长句的次级切点（不单独成段，只用于避免把词切开）
_SECONDARY_BREAKS: Final[str] = "，,、：:；; "


def split_sentences(text: str, terminators: str = _DEFAULT_TERMINATORS) -> list[str]:
    """把文本切成句子列表：句末标点（连续的算一处）与换行都是切点，标点跟随前句。"""
    sentences: list[str] = []
    start = 0
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char in terminators or text.startswith(_ASCII_ELLIPSIS, index):
            index = _skip_terminators(text, index, terminators)
            sentences.append(text[start:index])
            start = index
        elif char == "\n":
            sentences.append(text[start:index])
            index += 1
            start = index
        else:
            index += 1

    sentences.append(text[start:])
    return [sentence.strip() for sentence in sentences if sentence.strip()]


def _skip_terminators(text: str, index: int, terminators: str) -> int:
    """跳过连续的句末标点（含 ``...``），把整串当作一个切点。"""
    while index < len(text):
        if text[index] in terminators:
            index += 1
        elif text.startswith(_ASCII_ELLIPSIS, index):
            index += len(_ASCII_ELLIPSIS)
        else:
            break
    return index


def _chunk(sentence: str, max_chars: int) -> list[str]:
    """超长句优先在次级标点处断开（该标点丢弃），实在没有标点才按 ``max_chars`` 硬切。"""
    if max_chars <= 0 or len(sentence) <= max_chars:
        return [sentence]

    pieces: list[str] = []
    rest = sentence
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max(window.rfind(char) for char in _SECONDARY_BREAKS)
        if cut <= 0:  # 窗口内没有次级标点：只能硬切
            pieces.append(rest[:max_chars])
            rest = rest[max_chars:]
            continue
        # 在次级标点前断开，并丢掉这个标点：消息以逗号结尾不像人说话
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut + 1 :].lstrip()
    if rest:
        pieces.append(rest)
    return [piece for piece in pieces if piece]


def group_segments(
    sentences: Iterable[str],
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_sentences: int = _DEFAULT_MAX_SENTENCES,
) -> list[str]:
    """贪心合并相邻句子：每段不超过 ``max_chars`` 个字符、``max_sentences`` 句。"""
    segments: list[str] = []
    current = ""
    count = 0

    for sentence in sentences:
        for piece in _chunk(sentence, max_chars):
            too_long = bool(current) and len(current) + len(piece) > max_chars
            too_many = bool(current) and count >= max_sentences
            if too_long or too_many:
                segments.append(current)
                current, count = piece, 1
            else:
                current += piece
                count += 1

    if current:
        segments.append(current)
    return segments


def split_reply(
    text: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    max_sentences: int = _DEFAULT_MAX_SENTENCES,
    terminators: str = _DEFAULT_TERMINATORS,
) -> list[str]:
    """把一段回复拆成多条消息；空文本返回空列表。

    换行视为分段边界（一个自然段至少单独成条），段内再按句子贪心合并，
    避免把不同段落拼成一条长消息。
    """
    if not text or not text.strip():
        return []

    segments: list[str] = []
    for paragraph in _paragraphs(text):
        sentences = split_sentences(paragraph, terminators)
        segments.extend(group_segments(sentences, max_chars, max_sentences))
    return segments


def _paragraphs(text: str) -> list[str]:
    """按换行切成非空段落，并去掉首尾空白。"""
    return [block.strip() for block in str(text).splitlines() if block.strip()]


def _positive_int(value: Any, default: int, name: str) -> int:
    """只接受正整数，其余回退默认值并记一条 error。"""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if value is not None:
        core_log.error(f"reply_split 配置项 {name} 非法（需正整数），已回退 {default}")
    return default


def _read_params(params: dict[str, Any]) -> tuple[int, int, str]:
    max_chars = _positive_int(params.get("max_chars"), _DEFAULT_MAX_CHARS, "max_chars")
    max_sentences = _positive_int(
        params.get("max_sentences"), _DEFAULT_MAX_SENTENCES, "max_sentences"
    )
    terminators = params.get("terminators")
    if not isinstance(terminators, str) or not terminators:
        if terminators is not None:
            core_log.error("reply_split 配置项 terminators 非法（需非空字符串），已回退默认值")
        terminators = _DEFAULT_TERMINATORS
    return max_chars, max_sentences, terminators


def split(ctx: Any) -> list[str]:
    """ModuleHub 钩子：按配置把 ``ctx.reply`` 拆成多条消息。"""
    params = getattr(ctx, "params", {}) or {}
    max_chars, max_sentences, terminators = _read_params(params)
    reply = getattr(ctx, "reply", "")
    return split_reply(str(reply), max_chars, max_sentences, terminators)
