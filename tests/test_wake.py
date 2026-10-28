"""唤醒规则单测：只有 ``@名字`` 才回，光叫名字不回。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]


def _load_wake():
    path = _ROOT / "src" / "ai" / "module" / "modules" / "wake.py"
    spec = importlib.util.spec_from_file_location("wake_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wake = _load_wake()

_PARAMS = {"require_mention": True, "names": ["宋文秀", "秀秀"], "private_always": True}


def ctx(
    text: str,
    *,
    group: str = "群",
    mentioned: bool = False,
    params: dict | None = None,
    private: bool = False,
):
    return SimpleNamespace(
        user_text=text,
        group=group,
        mentioned=mentioned,
        private=private,
        params=_PARAMS if params is None else params,
    )


class WakeTest(unittest.TestCase):
    def test_at_name_wakes(self) -> None:
        self.assertTrue(wake.should_wake(ctx("@宋文秀 在吗")))
        self.assertTrue(wake.should_wake(ctx("@秀秀 帮我看下")))

    def test_name_only_does_not_wake(self) -> None:
        self.assertFalse(wake.should_wake(ctx("宋文秀，作业写了吗")))
        self.assertFalse(wake.should_wake(ctx("秀秀今天怎么没来")))

    def test_at_someone_else_does_not_wake(self) -> None:
        self.assertFalse(wake.should_wake(ctx("@张天宇 你好", mentioned=True)))

    def test_fullwidth_at(self) -> None:
        self.assertTrue(wake.should_wake(ctx("＠宋文秀 在吗")))

    def test_platform_mention_with_name(self) -> None:
        """平台把 @ 从正文剥掉、只留名字并置 mentioned=True 时仍要能唤醒。"""
        self.assertTrue(wake.should_wake(ctx("宋文秀 在吗", mentioned=True)))

    def test_platform_mention_without_at_in_text(self) -> None:
        """QQ 的 @ 是独立消息段，适配剥掉后正文里既没有 @ 也没有名字。"""
        self.assertTrue(wake.should_wake(ctx("周末有空吗", mentioned=True)))

    def test_private_chat_always_wakes(self) -> None:
        self.assertTrue(wake.should_wake(ctx("在吗", group="")))

    def test_private_with_memory_dir_still_always_wakes(self) -> None:
        """私聊也有记忆目录（``private-<QQ>``），不能因此被当成群聊要求 @。"""
        self.assertTrue(
            wake.should_wake(ctx("在吗", group="private-1027713979", private=True))
        )

    def test_private_text_with_at_still_wakes(self) -> None:
        """私聊正文里出现 @（邮箱、占位）不能被判成"@了别人"而吞掉整轮。"""
        self.assertTrue(
            wake.should_wake(ctx("我邮箱 a@b.com", group="private-1027713979", private=True))
        )

    def test_private_always_off(self) -> None:
        params = {"require_mention": True, "names": ["宋文秀"], "private_always": False}
        self.assertFalse(
            wake.should_wake(
                ctx("在吗", group="private-1", private=True, params=params)
            )
        )

    def test_require_mention_off(self) -> None:
        params = {"require_mention": False, "names": ["宋文秀"], "private_always": True}
        self.assertTrue(wake.should_wake(ctx("随便说点什么", params=params)))


class HubWiringTest(unittest.TestCase):
    """Hub 把 ``private`` 透传到 ``ctx``——漏了这一层，通道传了也白传。"""

    def setUp(self) -> None:
        sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "module_hub"))
        from module_hub import ModuleHub  # noqa: PLC0415 - 路径要先补

        self.hub = ModuleHub(switches={"wake": True})

    def test_private_flag_reaches_wake(self) -> None:
        text = "我邮箱 a@b.com"
        self.assertTrue(
            self.hub.should_wake(
                text, group="private-1", mentioned=True, private=True
            )
        )
        # 同样的话在群里就是"@了别人"，不唤醒
        self.assertFalse(
            self.hub.should_wake(
                text, group="private-1", mentioned=True, private=False
            )
        )


if __name__ == "__main__":
    unittest.main()
