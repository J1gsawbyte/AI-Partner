"""身份库（lib/identity_store）与 identity 模块单测：只用临时文件，不读真实配置。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))

import identity_store  # noqa: E402  # pyright: ignore[reportMissingImports]


def _load_module(name: str, relative: str) -> Any:
    path = _ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


identity = _load_module("ai_module_identity", "src/ai/module/modules/identity.py")

_PEOPLE: dict[str, Any] = {
    "people": [
        {
            "name": "王俊宇",
            "qq": "1027713979",
            "profile": {"生日": "2005-03-14", "出生地": "湖南岳阳"},
        },
        {"name": "张天宇", "qq": "", "profile": {}},
        {
            "name": "许斌",
            "qq": "2457390492",
            "enable": False,
            "profile": {"生日": "2005-01-01"},
        },
    ]
}


class FakeCtx:
    def __init__(self, **params: Any) -> None:
        self.params = params
        self.state: dict[str, Any] = {}
        self.speaker = ""
        self.logs: list[tuple[str, str]] = []

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))

    def save_state(self) -> None:
        return None


class _TempFileTest(unittest.TestCase):
    def setUp(self) -> None:
        identity_store.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "identity.json"
        self.write(_PEOPLE)

    def write(self, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.path.write_text(text, encoding="utf-8")


class IdentityStoreTest(_TempFileTest):
    def book(self) -> identity_store.IdentityBook:
        return identity_store.load(self.path)

    def test_resolve_by_qq(self) -> None:
        self.assertEqual(self.book().resolve("1027713979"), "王俊宇")
        self.assertEqual(self.book().resolve(" 1027713979 "), "王俊宇")

    def test_only_qq_is_used(self) -> None:
        """认人只看 QQ 号：没登记 QQ 的人、把名字当 QQ 传进来，都返回空。"""
        book = self.book()
        self.assertEqual(book.resolve("20002"), "")
        self.assertEqual(book.resolve("张天宇"), "")
        self.assertEqual(book.resolve(""), "")

    def test_disabled_person_is_not_resolved(self) -> None:
        """``enable: false`` = 不认识（但能区分出"已停用"与"没登记"）。"""
        book = self.book()
        self.assertEqual(book.resolve("2457390492"), "")
        self.assertTrue(book.is_blocked("2457390492"))
        self.assertFalse(book.is_blocked("1027713979"))
        self.assertFalse(book.is_blocked("20002"))

    def test_enabled_property_counts_only_whitelisted(self) -> None:
        book = self.book()
        self.assertEqual(len(book), 3)
        self.assertEqual([person.name for person in book.enabled], ["王俊宇", "张天宇"])

    def test_disabled_profile_is_not_injected(self) -> None:
        self.assertEqual(self.book().profile_lines("许斌"), [])

    def test_enable_accepts_string_values(self) -> None:
        """手写 JSON 容易写成字符串，``"false"`` / ``"off"`` 也要算关。"""
        self.write(
            {
                "people": [
                    {"name": "甲", "qq": "50005", "enable": "false"},
                    {"name": "乙", "qq": "50006", "enable": "true"},
                    {"name": "丙", "qq": "50007", "enable": "off"},
                ]
            }
        )
        identity_store.clear_cache()
        book = self.book()
        self.assertEqual(book.resolve("50005"), "")
        self.assertEqual(book.resolve("50007"), "")
        self.assertEqual(book.resolve("50006"), "乙")

    def test_duplicate_qq_keeps_first(self) -> None:
        """同一 QQ 登记两次时以先出现的为准，避免后一条笔误把人认错。"""
        self.write(
            {
                "people": [
                    {"name": "甲", "qq": "40004"},
                    {"name": "乙", "qq": "40004"},
                ]
            }
        )
        identity_store.clear_cache()
        self.assertEqual(self.book().resolve("40004"), "甲")

    def test_profile_lines_include_name_and_fields(self) -> None:
        lines = self.book().profile_lines("王俊宇")
        self.assertEqual(lines[0], "- 王俊宇")
        self.assertIn("  - 生日：2005-03-14", lines)

    def test_profile_lines_empty_without_profile(self) -> None:
        self.assertEqual(self.book().profile_lines("张天宇"), [])
        self.assertEqual(self.book().profile_lines("路人甲"), [])

    def test_missing_file_is_empty(self) -> None:
        empty = identity_store.load(Path(self._tmp.name) / "nope.json")
        self.assertEqual(len(empty), 0)

    def test_broken_json_is_empty(self) -> None:
        self.write("{ not json")
        identity_store.clear_cache()
        self.assertEqual(len(self.book()), 0)

    def test_invalid_entries_skipped(self) -> None:
        self.write({"people": ["x", {"qq": "1"}, {"name": "  "}, {"name": "甲", "qq": "2"}]})
        identity_store.clear_cache()
        book = self.book()
        self.assertEqual(len(book), 1)
        self.assertEqual(book.resolve("2"), "甲")

    def test_cache_reloads_after_file_change(self) -> None:
        self.assertEqual(self.book().resolve("30003"), "")
        self.write(
            {
                "people": [
                    {"name": "甲", "qq": "30003"},
                ]
            }
        )
        stamp = self.path.stat().st_mtime + 10
        os.utime(self.path, (stamp, stamp))
        self.assertEqual(self.book().resolve("30003"), "甲")


class IdentityModuleTest(_TempFileTest):
    def ctx(self, **params: Any) -> FakeCtx:
        data: dict[str, Any] = {"people_path": str(self.path), "inject_profile": True}
        data.update(params)
        ctx = FakeCtx(**data)
        ctx.speaker = "王俊宇"
        return ctx

    def test_build_injects_profile(self) -> None:
        text = identity.build(self.ctx())
        self.assertTrue(text.startswith("[成员档案]"), text)
        self.assertIn("- 王俊宇", text)
        self.assertIn("生日：2005-03-14", text)

    def test_build_without_speaker_is_empty(self) -> None:
        ctx = self.ctx()
        ctx.speaker = ""
        self.assertEqual(identity.build(ctx), "")

    def test_build_unknown_speaker_is_empty(self) -> None:
        ctx = self.ctx()
        ctx.speaker = "路人甲"
        self.assertEqual(identity.build(ctx), "")

    def test_build_respects_switch(self) -> None:
        self.assertEqual(identity.build(self.ctx(inject_profile=False)), "")

    def test_build_truncates_to_max_chars(self) -> None:
        text = identity.build(self.ctx(max_chars=16))
        self.assertLessEqual(len(text), 16)
        self.assertTrue(text.endswith("…"), text)

    def test_build_tolerates_missing_file(self) -> None:
        ctx = self.ctx(people_path=str(Path(self._tmp.name) / "nope.json"))
        self.assertEqual(identity.build(ctx), "")

    def test_on_start_logs_ready_and_missing(self) -> None:
        ok = self.ctx()
        identity.on_start(ok)
        self.assertTrue(any("身份表就绪" in text for _kind, text in ok.logs), ok.logs)

        missing = self.ctx(people_path=str(Path(self._tmp.name) / "nope.json"))
        identity.on_start(missing)
        self.assertTrue(any("不存在" in text for _kind, text in missing.logs), missing.logs)


if __name__ == "__main__":
    unittest.main()
