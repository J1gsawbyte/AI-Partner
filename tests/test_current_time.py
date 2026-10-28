"""current_time 单测：只给 ctx，断言注入的那条末尾 user 消息。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, relative: str) -> Any:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


clock = _load_module("ai_module_current_time", "src/ai/module/modules/current_time.py")


class FakeCtx:
    def __init__(self, now: Any = None, **params: Any) -> None:
        self.params = params
        if now is not None:
            self.now = now
        self.logs: list[tuple[str, str]] = []

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))


class BuildTest(unittest.TestCase):
    def test_default_message_is_user_role(self) -> None:
        ctx = FakeCtx(datetime(2026, 10, 27, 21, 5))
        message = clock.build(ctx)
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["content"], "当前真实时间为：2026-10-27 21:05")

    def test_custom_format_and_template(self) -> None:
        ctx = FakeCtx(datetime(2026, 10, 27, 21, 5), format="%Y/%m/%d", template="现在是 {time}")
        self.assertEqual(clock.build(ctx)["content"], "现在是 2026/10/27")

    def test_falls_back_to_system_clock(self) -> None:
        message = clock.build(FakeCtx())
        self.assertRegex(message["content"], r"^当前真实时间为：\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_broken_template_falls_back(self) -> None:
        ctx = FakeCtx(datetime(2026, 10, 27, 21, 5), template="当前时间：{time")
        message = clock.build(ctx)
        self.assertEqual(message["content"], "当前真实时间为：2026-10-27 21:05")
        self.assertTrue(any("时间模板不合法" in text for _kind, text in ctx.logs), ctx.logs)

    def test_lands_at_the_end_before_user_input(self) -> None:
        """末尾一条是时间，最后一条仍是本轮输入；system 块里不应再有时间。"""
        sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "module_hub"))
        from module_hub import ModuleHub  # noqa: PLC0415 - 路径要先补

        hub = ModuleHub(switches={"persona": True, "chat_style": True, "current_time": True})
        hub.on_start()
        sys.path.insert(0, str(_ROOT / "src" / "ai" / "prompt"))
        from prompt_construct import prompt_construct  # noqa: PLC0415

        messages = prompt_construct(hub.collect_fragments("现在几点", phase="chat"), "现在几点")
        self.assertEqual(messages[0]["role"], "system")
        self.assertNotIn("当前真实时间为", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "现在几点"})
        self.assertEqual(messages[-2]["role"], "user")
        self.assertRegex(
            messages[-2]["content"], r"^当前真实时间为：\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"
        )


if __name__ == "__main__":
    unittest.main()
