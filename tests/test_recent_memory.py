"""recent_memory 的组装单测：只给 ctx，断言逐条消息的 role / content。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))


def _load_module(name: str, relative: str) -> Any:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


recent = _load_module("ai_module_recent_memory", "src/ai/module/modules/recent_memory.py")


class FakeCtx:
    def __init__(self, root: Path, *, group: str = "g1", **params: Any) -> None:
        self.params = params
        self.message_path = root
        self.group = group
        self.speaker = "李贵龙"
        self.user_text = ""
        self.reply = ""
        self.logs: list[tuple[str, str]] = []

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))

    def save_state(self) -> None:
        return None


class BuildTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.file = self.root / "g1" / "messages.md"
        self.file.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rows: list[tuple[str, str, str]]) -> None:
        lines = [
            json.dumps(
                {"time": "2026-10-27 12:00:00", "role": role, "speaker": speaker, "content": text},
                ensure_ascii=False,
            )
            for role, speaker, text in rows
        ]
        self.file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def ctx(self, **params: Any) -> FakeCtx:
        return FakeCtx(self.root, **params)

    def test_build_returns_one_message_per_line(self) -> None:
        self.write(
            [
                ("user", "张天宇", "在吗"),
                ("assistant", "我", "在的"),
                ("user", "张天宇", "周末有空吗"),
            ]
        )
        messages = recent.build(self.ctx())
        self.assertIsInstance(messages, list)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant", "user"])
        self.assertEqual(messages[0]["content"], "张天宇：在吗")
        self.assertEqual(messages[1]["content"], "在的")
        self.assertEqual(messages[2]["content"], "张天宇：周末有空吗")

    def test_speaker_missing_falls_back_to_label(self) -> None:
        self.write([("user", "", "喂")])
        messages = recent.build(self.ctx())
        self.assertEqual(messages[0]["content"], "对方：喂")

    def test_current_input_is_skipped(self) -> None:
        """本轮输入已由 on_user 落盘，build 不能再注入一遍。"""
        self.write(
            [
                ("user", "张天宇", "在吗"),
                ("assistant", "我", "在的"),
                ("user", "张天宇", "周末有空吗"),
            ]
        )
        ctx = self.ctx()
        ctx.user_text = "周末有空吗"
        messages = recent.build(ctx)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["role"], "assistant")

    def test_budget_keeps_newest(self) -> None:
        self.write(
            [
                ("user", "张天宇", "在吗"),
                ("assistant", "我", "在的"),
                ("user", "张天宇", "周末有空吗"),
            ]
        )
        messages = recent.build(self.ctx(max_chars=10))
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["content"].endswith("周末有空吗"))

    def test_line_limit_truncates_long_content(self) -> None:
        self.write([("user", "张天宇", "一二三四五六七八")])
        messages = recent.build(self.ctx(max_line_chars=5))
        self.assertEqual(messages[0]["content"], "张天宇：一二三四五…")

    def test_history_rounds_limits_turns(self) -> None:
        self.write(
            [
                ("user", "张天宇", "第一句"),
                ("assistant", "我", "第一答"),
                ("user", "张天宇", "第二句"),
                ("assistant", "我", "第二答"),
            ]
        )
        messages = recent.build(self.ctx(history_rounds=1))
        self.assertEqual([item["content"] for item in messages], ["张天宇：第二句", "第二答"])

    def test_newlines_are_flattened(self) -> None:
        self.write([("user", "张天宇", "第一行\n第二行")])
        messages = recent.build(self.ctx())
        self.assertEqual(messages[0]["content"], "张天宇：第一行 第二行")

    def test_on_user_and_on_reply_roundtrip(self) -> None:
        """写入侧与注入侧要能对上：user 行带名字，assistant 行只留正文。"""
        ctx = self.ctx()
        ctx.user_text = "在吗"
        recent.on_user(ctx)
        ctx.reply = "在的"
        recent.on_reply(ctx)

        ctx.user_text = ""
        messages = recent.build(ctx)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "李贵龙：在吗")
        self.assertEqual(messages[1]["content"], "在的")

    def test_missing_file_returns_empty_string(self) -> None:
        self.assertEqual(recent.build(self.ctx()), "")

    def test_zero_rounds_returns_empty_string(self) -> None:
        self.write([("user", "张天宇", "在吗")])
        self.assertEqual(recent.build(self.ctx(history_rounds=0)), "")


if __name__ == "__main__":
    unittest.main()
