"""把一句话变成一串数字（向量），用来判断"意思有多像"。

模型文件与分词文件由配置指定（默认 ``embedding/bge-base-zh-v1.5.onnx`` 与
``embedding/bge-base-zh-v1.5.tokenizer.json``）。缺依赖、缺文件、加载失败时
:func:`available` 返回 ``False``、:func:`encode` 返回 ``None``，调用方退回字面匹配
——**这里不抛异常**，记忆只是背景，不能挡住回复。

对外四个函数：

| 函数 | 用途 |
| --- | --- |
| ``available`` | 模型能不能用（装没装、文件在不在、加载成功没） |
| ``encode`` | 文字 → 已归一化的向量（长度 1，点积即余弦相似度） |
| ``sync`` | 按 key 维护向量缓存（只补算新增/改过的，存 ``*.npz``） |
| ``similarities`` | 给一句话和一批向量算相似度 |

缓存文件里存三样：``keys`` / ``prints``（内容指纹）/ ``vectors``。指纹变了才重算，
所以手改 ``memory.md`` 的摘要会自动触发对应条目重算。
"""

from __future__ import annotations

import hashlib
import sys
import threading
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

_LIB_DIR = Path(__file__).resolve().parent
_SRC_DIR = _LIB_DIR.parents[2]
for _dir in (_LIB_DIR, _SRC_DIR / "logger"):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["available", "encode", "similarities", "sync"]

#: 模型单次最多看多少 token（bge 训练上限 512）
_MAX_TOKENS = 512
#: 向量缓存文件名里的固定后缀检查用，避免 np.savez 自动补 .npz 造成路径不一致
_LOCK = threading.Lock()
_STATE: dict[str, Any] = {"key": "", "status": "idle"}


def _clean(text: Any) -> str:
    """统一全角/半角、去控制字符、压掉多余空白。"""
    if not isinstance(text, str):
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    kept = "".join(ch for ch in normalized if ch.isprintable())
    return " ".join(kept.split())


def _load(model_path: str | Path, tokenizer_path: str | Path) -> dict[str, Any] | None:
    """加载模型（同一进程只加载一次；失败后不再重试，避免每轮刷日志）。"""
    key = f"{model_path}|{tokenizer_path}"
    with _LOCK:
        if _STATE.get("key") == key:
            return _STATE if _STATE.get("status") == "ok" else None

        _STATE.update(key=key, status="failed", session=None, tokenizer=None, inputs=())
        model, tokenizer = Path(model_path), Path(tokenizer_path)
        if not model.is_file() or not tokenizer.is_file():
            core_log.error(
                f"语义检索不可用: 模型或分词文件不存在（{model} / {tokenizer}），已回退字面匹配"
            )
            return None
        try:
            import onnxruntime as ort  # pyright: ignore[reportMissingImports]
            from tokenizers import Tokenizer  # pyright: ignore[reportMissingImports]
        except ImportError as exc:
            core_log.error(f"语义检索不可用: 缺少依赖（pip install onnxruntime tokenizers），原因 {exc}")
            return None
        try:
            session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
            tokenizer_obj = Tokenizer.from_file(str(tokenizer))
            tokenizer_obj.enable_truncation(max_length=_MAX_TOKENS)
            tokenizer_obj.enable_padding(pad_id=0, pad_token="[PAD]")
        except Exception as exc:  # noqa: BLE001 - 模型文件坏了也不能打断聊天
            core_log.error(f"语义检索不可用: 加载失败 {type(exc).__name__}: {exc}")
            return None

        _STATE.update(
            key=key,
            status="ok",
            session=session,
            tokenizer=tokenizer_obj,
            inputs=tuple(item.name for item in session.get_inputs()),
        )
        return _STATE


def available(model_path: str | Path, tokenizer_path: str | Path) -> bool:
    """模型是否可用。"""
    return _load(model_path, tokenizer_path) is not None


def encode(
    texts: Sequence[str], model_path: str | Path, tokenizer_path: str | Path
) -> np.ndarray | None:
    """文字 → 归一化向量；不可用或输入为空时返回 ``None``。

    返回形状 ``(len(texts), dim)`` 的 ``float32``，每行长度为 1。
    """
    if not texts:
        return None
    state = _load(model_path, tokenizer_path)
    if state is None:
        return None

    cleaned = [_clean(text) or " " for text in texts]
    encodings = state["tokenizer"].encode_batch(cleaned)
    ids = np.array([item.ids for item in encodings], dtype=np.int64)
    feed: dict[str, np.ndarray] = {
        "input_ids": ids,
        "attention_mask": np.array([item.attention_mask for item in encodings], dtype=np.int64),
    }
    if "token_type_ids" in state["inputs"]:
        feed["token_type_ids"] = np.zeros_like(ids)

    try:
        hidden = state["session"].run(None, feed)[0]
    except Exception as exc:  # noqa: BLE001
        core_log.error(f"语义检索推理失败, 原因: {type(exc).__name__}: {exc}")
        return None

    vectors = hidden[:, 0] if hidden.ndim == 3 else hidden  # bge 取 [CLS]，再归一化
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _fingerprint(model_path: str | Path, text: str) -> str:
    raw = f"{Path(model_path).name}\x00{text}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _load_cache(
    cache_path: Path, model_path: str | Path
) -> tuple[dict[str, np.ndarray], dict[str, str]]:
    """读向量缓存；文件缺失/损坏/换了模型 → 当作空缓存。"""
    try:
        with np.load(cache_path, allow_pickle=False) as data:
            keys = [str(key) for key in data["keys"]]
            prints = [str(item) for item in data["prints"]]
            matrix = np.asarray(data["vectors"], dtype=np.float32)
            stored_model = str(data["model"]) if "model" in data else ""
    except (OSError, KeyError, ValueError):
        return {}, {}
    if stored_model != Path(model_path).name or len(keys) != len(matrix):
        return {}, {}
    return {key: matrix[index] for index, key in enumerate(keys)}, dict(zip(keys, prints))


def _save_cache(
    cache_path: Path,
    model_path: str | Path,
    vectors: dict[str, np.ndarray],
    prints: dict[str, str],
) -> None:
    """原子写缓存（临时文件 + 替换），失败只记日志。"""
    keys = list(vectors)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp = cache_path.with_name(cache_path.name + ".tmp")
        with open(temp, "wb") as handle:
            np.savez_compressed(
                handle,
                keys=np.array(keys, dtype=str),
                prints=np.array([prints[key] for key in keys], dtype=str),
                vectors=np.stack([vectors[key] for key in keys]).astype(np.float32),
                model=np.array(Path(model_path).name),
            )
        temp.replace(cache_path)
    except (OSError, ValueError) as exc:
        core_log.error(f"向量缓存写入失败, 原因: {type(exc).__name__}: {exc}")


def sync(
    cache_path: Path,
    pairs: Iterable[tuple[str, str]],
    model_path: str | Path,
    tokenizer_path: str | Path,
    encode_fn: Callable[[Sequence[str]], np.ndarray | None] | None = None,
) -> dict[str, np.ndarray]:
    """按 key 维护向量缓存，返回 ``key → 向量``；不可用时返回空字典。

    只对"新出现的 key"或"内容变了的 key"重新编码，其余复用缓存。
    ``encode_fn`` 仅用于测试注入。
    """
    items = [(str(key), str(text)) for key, text in pairs if str(key).strip()]
    if not items or not (encode_fn is not None or available(model_path, tokenizer_path)):
        return {}

    cache_path = Path(cache_path)
    vectors, prints = _load_cache(cache_path, model_path)
    wanted = {key: _fingerprint(model_path, text) for key, text in items}

    dropped = [key for key in vectors if key not in wanted]
    for key in dropped:
        vectors.pop(key, None)
        prints.pop(key, None)
    missing = [
        (key, text) for key, text in items if key not in vectors or prints.get(key) != wanted[key]
    ]

    if missing:
        encoder = encode_fn or (lambda texts: encode(texts, model_path, tokenizer_path))
        fresh = encoder([text for _key, text in missing])
        if fresh is None:
            return {}
        for index, (key, _text) in enumerate(missing):
            vectors[key] = np.asarray(fresh[index], dtype=np.float32)
            prints[key] = wanted[key]
    if missing or dropped:
        _save_cache(cache_path, model_path, vectors, prints)
    return {key: vectors[key] for key, _text in items if key in vectors}


def similarities(
    text: str,
    vectors: dict[str, np.ndarray],
    model_path: str | Path,
    tokenizer_path: str | Path,
) -> dict[str, float]:
    """一句话与一批向量的相似度（余弦，越大越像）；不可用时返回空字典。"""
    if not text or not vectors:
        return {}
    query = encode([text], model_path, tokenizer_path)
    if query is None:
        return {}
    keys = list(vectors)
    matrix = np.stack([vectors[key] for key in keys])
    scores = matrix @ query[0]  # 两边都已归一化，点积即余弦
    return {key: float(score) for key, score in zip(keys, scores)}
