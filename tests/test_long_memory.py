"""long_memory 的路径与白名单单测：不调模型，只验证"读哪份、能不能读写"。"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))

import memory_store as mem  # noqa: E402  # pyright: ignore[reportMissingImports]
import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]


def _load_module(name: str, relative: str) -> Any:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


long_memory = _load_module("ai_module_long_memory", "src/ai/module/modules/long_memory.py")

_NOW = "2026-10-28 12:00:00"


class FakeCtx:
    def __init__(self, root: Path, *, group: str = "949207129", **params: Any) -> None:
        self.params = params
        self.state: dict[str, Any] = {"groups": {}}
        self.message_path = root
        self.group = group
        self.speaker = "李贵龙"
        self.user_text = ""
        self.reply = ""
        self.phase = "chat"
        self.now = datetime(2026, 10, 28, 12, 0, 0)
        self.ask_calls = 0
        self.logs: list[tuple[str, str]] = []

    def ask(self, *_args: Any, **_kwargs: Any) -> Any:
        self.ask_calls += 1
        raise RuntimeError("单测里不应该真的调用模型")

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))

    def save_state(self) -> None:
        return None


class _TempTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def ctx(self, *, group: str = "949207129", **params: Any) -> FakeCtx:
        return FakeCtx(self.root, group=group, **params)

    def write_memory(self, path: Path, key: str = "李贵龙.喜好.咖啡", summary: str = "喜欢手冲咖啡") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        mem.save_memory(
            path,
            [
                {
                    "key": key,
                    "summary": summary,
                    "importance": 0.6,
                    "status": mem.STATUS_ACTIVE,
                    "created_at": _NOW,
                    "updated_at": _NOW,
                    "last_recall_at": _NOW,
                    "recalls": 0,
                    "stability_h": 67.2,
                    "first_seen": _NOW,
                    "updated_from": "",
                }
            ],
        )


class PathTest(_TempTest):
    def test_memory_is_per_session_by_default(self) -> None:
        left = long_memory._paths(self.ctx(group="949207129"))
        right = long_memory._paths(self.ctx(group="private-1027713979"))
        assert left is not None and right is not None
        self.assertEqual(left[2], self.root / "949207129" / "memory.md")
        self.assertEqual(right[2], self.root / "private-1027713979" / "memory.md")
        self.assertNotEqual(left[2], right[2])

    def test_global_memory_shares_one_file(self) -> None:
        left = long_memory._paths(self.ctx(group="949207129", global_memory=True))
        right = long_memory._paths(self.ctx(group="private-1027713979", global_memory=True))
        assert left is not None and right is not None
        self.assertEqual(left[2], self.root / "_global" / "memory.md")
        self.assertEqual(left[2], right[2])
        # 消息库仍按会话隔离：只是"提炼结果"共享
        self.assertNotEqual(left[1], right[1])

    def test_global_memory_is_injected_for_every_session(self) -> None:
        self.write_memory(self.root / "_global" / "memory.md")
        params = {"global_memory": True, "embedding_enabled": False, "min_score": 0.0}
        for group in ("949207129", "private-1027713979"):
            ctx = self.ctx(group=group, **params)
            ctx.user_text = "咖啡"
            fragment = long_memory.build(ctx)
            self.assertTrue(fragment, group)
            self.assertIn("咖啡", fragment["content"])


class StartLogTest(_TempTest):
    def test_on_start_reports_global_mode_and_whitelist(self) -> None:
        ctx = self.ctx(group="", global_memory=True, memory_whitelist=["949207129"])
        long_memory.on_start(ctx)
        text = " ".join(message for _kind, message in ctx.logs)
        self.assertIn("全局共享", text)
        self.assertIn("白名单 1 个会话", text)


class WhitelistTest(_TempTest):
    def test_empty_whitelist_allows_everything(self) -> None:
        self.assertTrue(long_memory._allowed(self.ctx(memory_whitelist=[]), "949207129"))
        self.assertTrue(long_memory._allowed(self.ctx(), "private-1027713979"))

    def test_group_must_be_listed(self) -> None:
        ctx = self.ctx(memory_whitelist=["1027713979"])
        self.assertFalse(long_memory._allowed(ctx, "949207129"))
        self.assertTrue(long_memory._allowed(ctx, "private-1027713979"))

    def test_private_matches_both_forms(self) -> None:
        self.assertTrue(
            long_memory._allowed(self.ctx(memory_whitelist=["private-1027713979"]), "private-1027713979")
        )
        self.assertTrue(
            long_memory._allowed(self.ctx(memory_whitelist=["1027713979"]), "private-1027713979")
        )

    def test_ignores_case_and_spaces(self) -> None:
        self.assertTrue(
            long_memory._allowed(self.ctx(memory_whitelist=[" Private-1027713979 "]), "private-1027713979")
        )

    def test_blocked_session_does_not_read(self) -> None:
        self.write_memory(self.root / "_global" / "memory.md")
        params = {
            "global_memory": True,
            "embedding_enabled": False,
            "min_score": 0.0,
            "memory_whitelist": ["949207129"],
        }
        allowed = self.ctx(group="949207129", **params)
        allowed.user_text = "咖啡"
        self.assertTrue(long_memory.build(allowed))

        blocked = self.ctx(group="private-1027713979", **params)
        blocked.user_text = "咖啡"
        self.assertEqual(long_memory.build(blocked), "")

    def test_blocked_session_is_not_counted(self) -> None:
        ctx = self.ctx(group="private-1027713979", memory_whitelist=["949207129"])
        long_memory.on_user(ctx)
        self.assertEqual(ctx.state.get("groups"), {})

        allowed = self.ctx(group="949207129", memory_whitelist=["949207129"])
        long_memory.on_user(allowed)
        self.assertEqual(allowed.state["groups"]["949207129"]["pending_rounds"], 1)

    def test_blocked_session_never_calls_model(self) -> None:
        msg_path = store.message_file(self.root, "private-1027713979")
        store.append_messages(
            msg_path,
            [
                {
                    "time": _NOW,
                    "role": "user",
                    "speaker": "李贵龙",
                    "content": "我每天七点起床",
                }
            ],
        )
        ctx = self.ctx(
            group="private-1027713979",
            memory_whitelist=["949207129"],
            summarize_rounds=1,
            embedding_enabled=False,
        )
        entry = long_memory._group_state(ctx, "private-1027713979")
        entry["pending_rounds"] = 10

        long_memory.tick(ctx)
        self.assertEqual(ctx.ask_calls, 0)
        self.assertFalse((self.root / "_global" / "memory.md").exists())


if __name__ == "__main__":
    unittest.main()
