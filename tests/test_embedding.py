"""语义检索工具单测：不加载真模型，用假编码器验证缓存与相似度。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))

import embedding  # noqa: E402  # pyright: ignore[reportMissingImports]

_MODEL = "bge-small-zh-v1.5.onnx"


def fake_encoder(calls: list[list[str]], dim: int = 4):
    """按文本内容生成确定性的单位向量，并记录每次被要求编码的文本。"""

    def encode(texts):
        calls.append(list(texts))
        rows = []
        for text in texts:
            vector = np.zeros(dim, dtype=np.float32)
            vector[sum(ord(ch) for ch in text) % dim] = 1.0
            rows.append(vector)
        return np.stack(rows)

    return encode


class EmbeddingTest(unittest.TestCase):
    def test_unavailable_without_model_files(self) -> None:
        missing_model = Path("no-such-model.onnx")
        missing_tokenizer = Path("no-such-tokenizer.json")
        self.assertFalse(embedding.available(missing_model, missing_tokenizer))
        self.assertIsNone(embedding.encode(["你好"], missing_model, missing_tokenizer))
        self.assertEqual(embedding.sync(Path("x.npz"), [("k", "t")], missing_model, missing_tokenizer), {})
        self.assertEqual(embedding.similarities("你好", {}, missing_model, missing_tokenizer), {})

    def test_empty_input(self) -> None:
        self.assertIsNone(embedding.encode([], _MODEL, "t.json"))
        self.assertEqual(embedding.sync(Path("x.npz"), [], _MODEL, "t.json"), {})
        self.assertEqual(embedding.similarities("", {"a": np.zeros(4)}, _MODEL, "t.json"), {})

    def test_sync_encodes_once_then_reuses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "memory.vec.npz"
            calls: list[list[str]] = []
            pairs = [("张天宇.喜好.咖啡", "张天宇.喜好.咖啡 喜欢手冲咖啡")]

            first = embedding.sync(cache, pairs, _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(len(first), 1)
            self.assertEqual(len(calls), 1)

            second = embedding.sync(cache, pairs, _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(len(calls), 1, "内容没变不该重新编码")
            np.testing.assert_allclose(first["张天宇.喜好.咖啡"], second["张天宇.喜好.咖啡"])

    def test_sync_reencodes_only_changed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "memory.vec.npz"
            calls: list[list[str]] = []
            first_pairs = [
                ("张天宇.喜好.咖啡", "喜欢手冲咖啡"),
                ("张天宇.厌恶.香菜", "讨厌香菜"),
            ]
            embedding.sync(cache, first_pairs, _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(len(calls[0]), 2)

            second_pairs = [
                ("张天宇.喜好.咖啡", "喜欢手冲咖啡"),
                ("张天宇.厌恶.香菜", "讨厌香菜，闻到就难受"),
            ]
            embedding.sync(cache, second_pairs, _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(calls[-1], ["讨厌香菜，闻到就难受"])

    def test_sync_drops_removed_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "memory.vec.npz"
            calls: list[list[str]] = []
            embedding.sync(
                cache,
                [("a.b.c", "一"), ("x.y.z", "二")],
                _MODEL,
                "t.json",
                encode_fn=fake_encoder(calls),
            )
            kept = embedding.sync(cache, [("a.b.c", "一")], _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(list(kept), ["a.b.c"])
            self.assertEqual(len(calls), 1, "只删不增不该重新编码")

    def test_corrupted_cache_is_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "memory.vec.npz"
            cache.write_bytes(b"not a npz")
            calls: list[list[str]] = []
            vectors = embedding.sync(cache, [("a.b.c", "一")], _MODEL, "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(list(vectors), ["a.b.c"])
            self.assertEqual(len(calls), 1)

    def test_switching_model_rebuilds_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "memory.vec.npz"
            calls: list[list[str]] = []
            pairs = [("a.b.c", "一")]
            embedding.sync(cache, pairs, "v1.onnx", "t.json", encode_fn=fake_encoder(calls))
            embedding.sync(cache, pairs, "v2.onnx", "t.json", encode_fn=fake_encoder(calls))
            self.assertEqual(len(calls), 2, "换了模型必须重算，不能混用")

    def test_similarities_are_cosine(self) -> None:
        vectors = {
            "same": np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            "side": np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
        }
        query = np.array([[1.0, 0.0, 0.0, 0.0]], dtype=np.float32)
        with mock.patch.object(embedding, "encode", return_value=query):
            scores = embedding.similarities("随便", vectors, _MODEL, "t.json")
        self.assertAlmostEqual(scores["same"], 1.0)
        self.assertAlmostEqual(scores["side"], 0.0)


if __name__ == "__main__":
    unittest.main()
