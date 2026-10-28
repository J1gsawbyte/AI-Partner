"""long_memory 模块钩子单测：假 ctx + 注入 ctx.now 模拟时间流逝，不联网。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_LIB_DIR = _ROOT / "src" / "ai" / "module" / "lib"
sys.path.insert(0, str(_LIB_DIR))

import memory_store as mem  # noqa: E402  # pyright: ignore[reportMissingImports]
import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

_NOW = datetime(2026, 10, 26, 12, 0, 0)
_GROUP = "测试群"


def _load_module() -> Any:
    path = _ROOT / "src" / "ai" / "module" / "modules" / "long_memory.py"
    spec = importlib.util.spec_from_file_location("long_memory_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


long_memory = _load_module()


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class FakeCtx:
    """够用的 ctx 替身：只提供 long_memory 用到的成员。"""

    def __init__(
        self,
        root: Path,
        *,
        params: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> None:
        self.params = params or {}
        self.state = state if state is not None else {"groups": {}}
        self.now = now or _NOW
        self.user_text = ""
        self.reply = ""
        self.speaker = ""
        self.group = _GROUP
        self.mentioned = True
        self.phase = "chat"
        self.segments: list[str] = []
        self.message_path = root
        self.answer = '{"memories": []}'
        self.asked: list[tuple[list[dict[str, str]], str, dict[str, Any]]] = []
        self.logs: list[tuple[str, str]] = []

    def ask(self, messages: Any, tag: str = "", **options: Any) -> Any:
        self.asked.append((messages, tag, options))
        return _response(self.answer)

    def transcript(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def save_state(self) -> None:
        return None

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))


def make_record(
    key: str = "张天宇.喜好.咖啡",
    importance: float = 0.5,
    stability: float = 24.0,
    last: datetime | None = None,
    status: str = mem.STATUS_ACTIVE,
    summary: str = "喜欢手冲咖啡",
) -> dict[str, Any]:
    text = store.format_time(last or _NOW)
    return {
        "key": key,
        "summary": summary,
        "importance": importance,
        "status": status,
        "created_at": text,
        "updated_at": text,
        "last_recall_at": text,
        "recalls": 0,
        "stability_h": stability,
    }


class LongMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.group_dir = self.root / _GROUP
        self.group_dir.mkdir(parents=True)
        self.mem_path = self.group_dir / "memory.md"
        self.msg_path = self.group_dir / "messages.md"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 工具 -------------------------------------------------------------- #

    def write_memory(self, records: list[dict[str, Any]]) -> None:
        mem.save_memory(self.mem_path, records)

    def write_messages(self, rounds: int = 2) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index in range(rounds):
            moment = store.format_time(_NOW + timedelta(minutes=index))
            rows.append({"time": moment, "role": "user", "speaker": "张天宇", "content": f"我喜欢手冲咖啡 {index}"})
            rows.append({"time": moment, "role": "assistant", "speaker": "我", "content": "记住了"})
        store.append_messages(self.msg_path, rows)
        return rows

    def state_with(self, pending: int, cursor: int, last: datetime | None = None) -> dict[str, Any]:
        return {
            "groups": {
                _GROUP: {
                    "cursor": cursor,
                    "pending_rounds": pending,
                    "last_summarize_at": store.format_time(last or _NOW),
                }
            }
        }

    # -- build ------------------------------------------------------------- #

    def test_build_injects_and_reinforces(self) -> None:
        self.write_memory([make_record()])
        ctx = FakeCtx(self.root)
        ctx.user_text = "张天宇还喜欢喝手冲咖啡吗"
        ctx.speaker = "张天宇"

        fragment = long_memory.build(ctx)

        self.assertIsInstance(fragment, dict)
        self.assertIn("[长期记忆]", fragment["content"])
        self.assertIn("张天宇.喜好.咖啡", fragment["content"])
        stored = mem.load_memory(self.mem_path)[0]
        self.assertEqual(stored["recalls"], 1)
        self.assertAlmostEqual(stored["stability_h"], 43.2)
        self.assertEqual(stored["last_recall_at"], store.format_time(_NOW))

    def test_build_without_memory_returns_empty(self) -> None:
        ctx = FakeCtx(self.root)
        ctx.user_text = "在吗"
        self.assertEqual(long_memory.build(ctx), "")

    def test_build_skips_forgotten(self) -> None:
        self.write_memory([make_record(status=mem.STATUS_FORGOTTEN)])
        ctx = FakeCtx(self.root)
        ctx.user_text = "张天宇喜欢咖啡吗"
        ctx.speaker = "张天宇"
        self.assertEqual(long_memory.build(ctx), "")

    def test_build_falls_back_when_embedding_unavailable(self) -> None:
        """开了语义检索但模型不在：不许报错，照常走字面匹配。"""
        self.write_memory([make_record()])
        ctx = FakeCtx(
            self.root,
            params={
                "embedding_enabled": True,
                "embedding_model_path": "embedding/no-such.onnx",
                "embedding_tokenizer_path": "embedding/no-such.json",
            },
        )
        ctx.user_text = "张天宇还喜欢喝手冲咖啡吗"
        ctx.speaker = "张天宇"

        fragment = long_memory.build(ctx)

        self.assertIn("[长期记忆]", fragment["content"])
        self.assertFalse(any(kind == "error" for kind, _ in ctx.logs), ctx.logs)

    def test_build_uses_semantic_scores(self) -> None:
        """字面完全没对上时，靠语义分补召回，并把缓存写到 memory.md 旁边。"""
        self.write_memory([make_record(key="汪敬杨.习惯.晨间美式", summary="起床后必须来一杯")])
        ctx = FakeCtx(self.root, params={"embedding_enabled": True})
        ctx.user_text = "汪敬杨，我早上都喝点什么好"
        ctx.speaker = "汪敬杨"
        vector = np.zeros(4, dtype=np.float32)

        with mock.patch.object(
            long_memory.embedding_store, "sync", return_value={"汪敬杨.习惯.晨间美式": vector}
        ) as sync_mock, mock.patch.object(
            long_memory.embedding_store, "similarities", return_value={"汪敬杨.习惯.晨间美式": 0.66}
        ):
            fragment = long_memory.build(ctx)

        self.assertIn("汪敬杨.习惯.晨间美式", fragment["content"])
        self.assertEqual(sync_mock.call_args[0][0].name, "memory.vec.npz")
        self.assertTrue(any("语义补召回 1 条" in text for _kind, text in ctx.logs), ctx.logs)

    def test_semantic_recall_only_takes_best_candidate(self) -> None:
        """语义补召回只放最像的一条，避免把一串"都有点像"的记忆灌进 prompt。"""
        self.write_memory(
            [
                make_record(key="汪敬杨.习惯.晨间美式", summary="每天早上喝美式"),
                make_record(key="汪敬杨.约定.图书馆复习", summary="周六去图书馆复习"),
            ]
        )
        ctx = FakeCtx(self.root, params={"embedding_enabled": True})
        ctx.user_text = "汪敬杨，我上午总犯困"
        ctx.speaker = "汪敬杨"
        vector = np.zeros(4, dtype=np.float32)

        with mock.patch.object(
            long_memory.embedding_store, "sync", return_value={"a": vector, "b": vector}
        ), mock.patch.object(
            long_memory.embedding_store,
            "similarities",
            return_value={"汪敬杨.习惯.晨间美式": 0.72, "汪敬杨.约定.图书馆复习": 0.68},
        ):
            fragment = long_memory.build(ctx)

        self.assertIn("晨间美式", fragment["content"])
        self.assertNotIn("图书馆复习", fragment["content"])

    def test_build_skips_proactive_phase(self) -> None:
        self.write_memory([make_record()])
        ctx = FakeCtx(self.root)
        ctx.phase = "proactive"
        ctx.user_text = "张天宇喜欢咖啡吗"
        ctx.speaker = "张天宇"
        self.assertEqual(long_memory.build(ctx), "")

    # -- on_user / tick ---------------------------------------------------- #

    def test_on_user_counts_round_and_starts_clock(self) -> None:
        ctx = FakeCtx(self.root, state={"groups": {}})
        long_memory.on_user(ctx)
        long_memory.on_user(ctx)
        entry = ctx.state["groups"][_GROUP]
        self.assertEqual(entry["pending_rounds"], 2)
        self.assertEqual(entry["cursor"], 0)
        self.assertEqual(entry["last_summarize_at"], store.format_time(_NOW))

    def test_tick_summarizes_when_rounds_reached(self) -> None:
        rows = self.write_messages(rounds=2)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 2, "summarize_interval_minutes": 0},
            state=self.state_with(pending=2, cursor=0),
        )
        ctx.answer = json.dumps(
            {"memories": [{"key": "张天宇.喜好.咖啡", "summary": "喜欢手冲咖啡", "importance": 0.6}]}
        )

        long_memory.tick(ctx)

        self.assertEqual(len(ctx.asked), 1)
        self.assertEqual(ctx.asked[0][2].get("response_format"), {"type": "json_object"})
        entry = ctx.state["groups"][_GROUP]
        self.assertEqual(entry["cursor"], len(rows))
        self.assertEqual(entry["pending_rounds"], 0)
        records = mem.load_memory(self.mem_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key"], "张天宇.喜好.咖啡")
        self.assertEqual(records[0]["status"], mem.STATUS_ACTIVE)
        self.assertAlmostEqual(records[0]["stability_h"], 67.2)

    def test_tick_summarizes_when_interval_reached(self) -> None:
        self.write_messages(rounds=1)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 10, "summarize_interval_minutes": 30},
            state=self.state_with(pending=1, cursor=0, last=_NOW - timedelta(minutes=31)),
        )
        long_memory.tick(ctx)
        self.assertEqual(len(ctx.asked), 1)

    def test_tick_waits_when_no_condition_met(self) -> None:
        self.write_messages(rounds=1)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 10, "summarize_interval_minutes": 30},
            state=self.state_with(pending=1, cursor=0, last=_NOW - timedelta(minutes=5)),
        )
        long_memory.tick(ctx)
        self.assertEqual(ctx.asked, [])

    def test_tick_without_new_messages_skips_model(self) -> None:
        rows = self.write_messages(rounds=2)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 2, "summarize_interval_minutes": 0},
            state=self.state_with(pending=5, cursor=len(rows)),
        )
        long_memory.tick(ctx)
        self.assertEqual(ctx.asked, [])
        self.assertEqual(ctx.state["groups"][_GROUP]["pending_rounds"], 0)

    def test_tick_bad_json_advances_cursor(self) -> None:
        rows = self.write_messages(rounds=1)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 1, "summarize_interval_minutes": 0},
            state=self.state_with(pending=1, cursor=0),
        )
        ctx.answer = "我不太想按格式输出"
        long_memory.tick(ctx)
        self.assertEqual(len(ctx.asked), 1)
        self.assertEqual(ctx.state["groups"][_GROUP]["cursor"], len(rows))
        self.assertFalse(self.mem_path.exists())

    def test_tick_drops_invalid_key(self) -> None:
        rows = self.write_messages(rounds=1)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 1, "summarize_interval_minutes": 0},
            state=self.state_with(pending=1, cursor=0),
        )
        ctx.answer = json.dumps(
            {
                "memories": [
                    {"key": "只有两段", "summary": "无效", "importance": 0.5},
                    {"key": "张天宇.偏好.咖啡", "summary": "喜欢手冲咖啡", "importance": 0.6},
                ]
            }
        )
        long_memory.tick(ctx)
        records = mem.load_memory(self.mem_path)
        self.assertEqual([item["key"] for item in records], ["张天宇.喜好.咖啡"])
        self.assertEqual(ctx.state["groups"][_GROUP]["cursor"], len(rows))

    def test_tick_merges_rewording_key(self) -> None:
        self.write_messages(rounds=1)
        self.write_memory([make_record(key="汪敬杨.习惯.美式咖啡", importance=0.5)])
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 1, "summarize_interval_minutes": 0},
            state=self.state_with(pending=1, cursor=0),
        )
        ctx.answer = json.dumps(
            {"memories": [{"key": "汪敬杨.喜好.咖啡", "summary": "每天早上要喝美式", "importance": 0.5}]}
        )

        long_memory.tick(ctx)

        records = mem.load_memory(self.mem_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["key"], "汪敬杨.习惯.美式咖啡")  # 保留既有 key
        self.assertEqual(records[0]["summary"], "每天早上要喝美式")

    def test_tick_updates_existing_key(self) -> None:
        self.write_messages(rounds=1)
        revived = make_record(importance=0.5, status=mem.STATUS_FORGOTTEN)
        revived["forgotten_at"] = "2026-01-01 00:00:00"
        self.write_memory([revived])
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 1, "summarize_interval_minutes": 0},
            state=self.state_with(pending=1, cursor=0),
        )
        ctx.answer = json.dumps(
            {"memories": [{"key": "张天宇.喜好.咖啡", "summary": "只喝浅烘手冲", "importance": 0.7}]}
        )
        long_memory.tick(ctx)
        records = mem.load_memory(self.mem_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["summary"], "只喝浅烘手冲")
        self.assertAlmostEqual(records[0]["importance"], 0.7)
        self.assertEqual(records[0]["recalls"], 0)
        self.assertEqual(records[0]["status"], mem.STATUS_ACTIVE)  # 重新提到 = 复活
        self.assertNotIn("forgotten_at", records[0])

    def test_tick_marks_forgotten_but_keeps_immortal(self) -> None:
        old = _NOW - timedelta(days=90)
        self.write_memory(
            [
                make_record(importance=0.3, stability=24.0, last=old),
                make_record(key="张天宇.身份.生日", importance=0.9, stability=24.0, last=old),
            ]
        )
        ctx = FakeCtx(self.root, state={"groups": {_GROUP: {"pending_rounds": 0, "cursor": 0, "last_summarize_at": ""}}})

        long_memory.tick(ctx)

        statuses = {item["key"]: item["status"] for item in mem.load_memory(self.mem_path)}
        self.assertEqual(statuses["张天宇.喜好.咖啡"], mem.STATUS_FORGOTTEN)
        self.assertEqual(statuses["张天宇.身份.生日"], mem.STATUS_ACTIVE)
        self.assertEqual(ctx.asked, [])

    def test_tick_survives_model_failure(self) -> None:
        rows = self.write_messages(rounds=1)
        ctx = FakeCtx(
            self.root,
            params={"summarize_rounds": 1, "summarize_interval_minutes": 0},
            state=self.state_with(pending=1, cursor=0),
        )

        def boom(messages: Any, tag: str = "", **options: Any) -> Any:
            raise RuntimeError("接口挂了")

        ctx.ask = boom  # type: ignore[method-assign]
        long_memory.tick(ctx)
        entry = ctx.state["groups"][_GROUP]
        self.assertEqual(entry["cursor"], 0)  # 失败不推进，下一轮重试
        self.assertEqual(entry["pending_rounds"], 1)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
