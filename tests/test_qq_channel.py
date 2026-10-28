"""qq_channel 模块单测：本地假上报打本地 HTTP，不连协议端、不联网。"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))

import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

_SECRET = "qq-secret-0123456789"
_GROUP = "123456789"
_SELF = "10001"


def _load_module() -> Any:
    path = _ROOT / "src" / "ai" / "module" / "modules" / "qq_channel.py"
    spec = importlib.util.spec_from_file_location("qq_channel_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


channel = _load_module()


class FakeCtx:
    def __init__(self, **params: Any) -> None:
        self.params = params
        self.state: dict[str, Any] = {}
        self.now = datetime(2026, 10, 27, 12, 0, 0)
        self.message_path: Path | None = None
        self.logs: list[tuple[str, str]] = []

    def log_info(self, message: object) -> None:
        self.logs.append(("info", str(message)))

    def log_error(self, message: object) -> None:
        self.logs.append(("error", str(message)))

    def save_state(self) -> None:
        return None


def _group_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "time": 1761542400,
        "self_id": int(_SELF),
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 501,
        "group_id": int(_GROUP),
        "user_id": 20002,
        "raw_message": "@学委 周末有空吗",
        "message": [
            {"type": "at", "data": {"qq": _SELF}},
            {"type": "text", "data": {"text": " 周末有空吗"}},
        ],
        "sender": {"user_id": 20002, "nickname": "张天宇", "card": ""},
    }
    payload.update(overrides)
    return payload


def _private_event(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "time": 1761542400,
        "self_id": int(_SELF),
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 601,
        "user_id": 1027713979,
        "raw_message": "在吗",
        "message": [{"type": "text", "data": {"text": "在吗"}}],
        "sender": {"user_id": 1027713979, "nickname": "wjy"},
    }
    payload.update(overrides)
    return payload


class QqChannelTest(unittest.TestCase):
    def setUp(self) -> None:
        channel.shutdown()
        self.base = ""

    def tearDown(self) -> None:
        channel.shutdown()

    # -- 工具 -------------------------------------------------------------- #

    def start(self, **overrides: Any) -> FakeCtx:
        params: dict[str, Any] = {
            "bind_host": "127.0.0.1",
            "bind_port": 0,
            "secret": _SECRET,
            "api_base": "http://127.0.0.1:1",
            "access_token": "api-token",
            "group_whitelist": [_GROUP],
            # 用例要覆盖两条链路，两个开关都打开；默认值（私聊开/群聊关）另有用例
            "enable_group": True,
            "enable_private": True,
            # 默认关掉合并窗口：单个用例不想为"等静默"多花时间，合并另有用例
            "private_merge_window_seconds": 0,
            # 默认指向不存在的身份库：用例不读真实 configs/identity.json（避免被人名数据影响）
            "identity_path": str(_ROOT / "tests" / "_no_identity.json"),
        }
        params.update(overrides)
        ctx = FakeCtx(**params)
        channel.on_start(ctx)
        info = channel.status()
        if info["running"]:
            self.base = f"http://{info['listen']}"
        return ctx

    def post(
        self,
        payload: Any,
        *,
        secret: str | None = _SECRET,
        raw: bytes | None = None,
        signed: bool = True,
    ) -> tuple[int, dict[str, Any]]:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if signed and secret:
            digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
            headers["X-Signature"] = f"sha1={digest}"
        request = urllib.request.Request(f"{self.base}/", data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return int(response.status), json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return int(exc.code), json.loads(exc.read().decode("utf-8") or "{}")

    # -- 启动 -------------------------------------------------------------- #

    def test_start_rejects_non_loopback(self) -> None:
        ctx = self.start(bind_host="0.0.0.0")
        self.assertFalse(channel.status()["running"])
        self.assertTrue(any("回环" in text for _kind, text in ctx.logs), ctx.logs)

    def test_missing_secret_warns_but_runs(self) -> None:
        ctx = self.start(secret="")
        self.assertTrue(channel.status()["running"])
        self.assertFalse(channel.status()["signed"])
        self.assertTrue(any("secret" in text for _kind, text in ctx.logs), ctx.logs)

    def test_start_reports_listen_and_groups(self) -> None:
        self.start()
        info = channel.status()
        self.assertTrue(info["listen"].startswith("127.0.0.1:"))
        self.assertEqual(info["groups"], 1)
        self.assertTrue(info["signed"])

    # -- 鉴权 -------------------------------------------------------------- #

    def test_bad_signature_rejected(self) -> None:
        self.start()
        code, body = self.post(_group_event(), secret="wrong-secret")
        self.assertEqual(code, 401)
        self.assertEqual(body["reason"], "bad_signature")
        self.assertIsNone(channel.next_event(0.05))

    def test_missing_signature_rejected(self) -> None:
        self.start()
        code, _body = self.post(_group_event(), signed=False)
        self.assertEqual(code, 401)

    def test_no_secret_accepts_unsigned_report(self) -> None:
        self.start(secret="")
        code, body = self.post(_group_event(), signed=False)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "accepted")

    # -- 收 ---------------------------------------------------------------- #

    def test_heartbeat_ignored(self) -> None:
        self.start()
        code, body = self.post({"post_type": "meta_event", "meta_event_type": "heartbeat"})
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "not_message_event")

    def test_unsupported_message_type_ignored(self) -> None:
        self.start()
        code, body = self.post(_group_event(message_type="discuss"))
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "unsupported_message_type")

    # -- 群聊 / 私聊开关 ---------------------------------------------------- #

    def test_default_switches_private_on_group_off(self) -> None:
        """默认只开私聊：群聊要显式打开。"""
        ch = channel._Channel({}, lambda _m: None, lambda _m: None)
        self.assertFalse(ch.enable_group)
        self.assertTrue(ch.enable_private)

    def test_group_disabled_ignores_group_message(self) -> None:
        self.start(enable_group=False)
        code, body = self.post(_group_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "group_disabled")
        self.assertIsNone(channel.next_event(0.05))

    def test_private_disabled_ignores_private_message(self) -> None:
        path = self.identity_file([{"name": "wjy", "qq": "1027713979"}])
        self.start(enable_private=False, identity_path=str(path))
        code, body = self.post(_private_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "private_disabled")

    # -- 私聊 -------------------------------------------------------------- #

    def test_private_message_is_queued(self) -> None:
        path = self.identity_file([{"name": "wjy", "qq": "1027713979"}])
        self.start(identity_path=str(path))
        code, body = self.post(_private_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "accepted")

        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["chat_type"], "private")
        self.assertEqual(event["conversation_id"], "private-1027713979")
        self.assertEqual(event["group"], "private-1027713979")
        self.assertEqual(event["sender_name"], "wjy")
        self.assertTrue(event["mentioned"])
        self.assertEqual(event["text"], "在吗")

    def test_private_and_group_memory_are_separate(self) -> None:
        """同一个人：群里一个记忆目录，私聊另一个（两个目录互不干扰）。"""
        path = self.identity_file([{"name": "wjy", "qq": "1027713979"}])
        self.start(identity_path=str(path))
        self.post(
            _group_event(
                user_id=1027713979,
                sender={"user_id": 1027713979, "nickname": "wjy", "card": "mita"},
            )
        )
        self.post(_private_event())

        group_event = channel.next_event(1.0)
        private_event = channel.next_event(1.0)
        assert group_event is not None and private_event is not None
        self.assertEqual(group_event["chat_type"], "group")
        self.assertEqual(group_event["group"], _GROUP)
        self.assertEqual(private_event["group"], "private-1027713979")
        self.assertNotEqual(group_event["group"], private_event["group"])

    def test_private_unknown_qq_is_denied(self) -> None:
        """私聊白名单就是身份库：没登记直接忽略，并记进 denied_privates()。"""
        self.start()  # 身份库指向不存在的文件
        code, body = self.post(_private_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "private_not_allowed")
        self.assertEqual(channel.denied_privates(), {"1027713979": "未登记"})
        self.assertIsNone(channel.next_event(0.05))

    def test_blocked_sender_ignored_in_group(self) -> None:
        """``enable: false`` 是黑名单：群里发话也不接。"""
        path = self.identity_file([{"name": "许斌", "qq": "2457390492", "enable": False}])
        self.start(identity_path=str(path))
        code, body = self.post(
            _group_event(
                user_id=2457390492,
                sender={"user_id": 2457390492, "nickname": "x", "card": "废物"},
            )
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "sender_blocked")
        self.assertIsNone(channel.next_event(0.05))

    def test_blocked_sender_denied_in_private(self) -> None:
        path = self.identity_file([{"name": "许斌", "qq": "2457390492", "enable": False}])
        self.start(identity_path=str(path))
        code, body = self.post(
            _private_event(user_id=2457390492, sender={"user_id": 2457390492, "nickname": "x"})
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "private_blocked")
        denied = channel.denied_privates()
        self.assertIn("已停用", denied["2457390492"])
        self.assertIsNone(channel.next_event(0.05))

    def test_private_group_session_sub_type_denied(self) -> None:
        """群临时会话（sub_type=group）默认不收。"""
        path = self.identity_file([{"name": "wjy", "qq": "1027713979"}])
        self.start(identity_path=str(path))
        code, body = self.post(_private_event(sub_type="group"))
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "private_sub_type_not_allowed")

    def test_private_without_user_id(self) -> None:
        path = self.identity_file([{"name": "wjy", "qq": "1027713979"}])
        self.start(identity_path=str(path))
        code, body = self.post(_private_event(user_id=None))
        self.assertEqual(code, 400)
        self.assertEqual(body["reason"], "missing_user_id")

    # -- 私聊连发合并 -------------------------------------------------------- #

    def test_private_burst_is_merged_into_one_message(self) -> None:
        """一次说三句 → 静默窗口后合成一条，Brain 只被叫一次。"""
        path = self.identity_file([{"name": "李贵龙", "qq": "1027713979"}])
        self.start(identity_path=str(path), private_merge_window_seconds=0.2)
        for index, text in enumerate(("明天", "有空吗", "一起去图书馆")):
            self.post(
                _private_event(
                    message_id=700 + index, message=[{"type": "text", "data": {"text": text}}]
                )
            )

        event = channel.next_event(2.0)
        assert event is not None
        self.assertEqual(event["text"], "明天\n有空吗\n一起去图书馆")
        self.assertEqual(event["merged"], 3)
        self.assertTrue(event["mentioned"])
        self.assertIsNone(channel.next_event(0.2))

    def test_merge_window_zero_keeps_messages_apart(self) -> None:
        path = self.identity_file([{"name": "李贵龙", "qq": "1027713979"}])
        self.start(identity_path=str(path), private_merge_window_seconds=0)
        self.post(_private_event(message_id=800, message=[{"type": "text", "data": {"text": "在吗"}}]))
        self.post(_private_event(message_id=801, message=[{"type": "text", "data": {"text": "喂"}}]))

        first = channel.next_event(1.0)
        second = channel.next_event(1.0)
        assert first is not None and second is not None
        self.assertEqual((first["text"], second["text"]), ("在吗", "喂"))
        self.assertNotIn("merged", first)

    def test_merge_does_not_swallow_group_message(self) -> None:
        """合并窗口里混进来的群消息不能被吞掉，也不能打乱顺序。"""
        path = self.identity_file([{"name": "李贵龙", "qq": "1027713979"}])
        self.start(identity_path=str(path), private_merge_window_seconds=0.3)
        self.post(_private_event(message_id=900, message=[{"type": "text", "data": {"text": "私聊一句"}}]))
        time.sleep(0.05)
        self.post(_group_event(message_id=901))

        private_event = channel.next_event(2.0)
        group_event = channel.next_event(1.0)
        assert private_event is not None and group_event is not None
        self.assertEqual(private_event["chat_type"], "private")
        self.assertEqual(private_event["text"], "私聊一句")
        self.assertEqual(group_event["chat_type"], "group")
        self.assertEqual(group_event["conversation_id"], _GROUP)

    def test_group_not_whitelisted(self) -> None:
        self.start(group_whitelist=[])
        code, body = self.post(_group_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "group_not_allowed")
        self.assertEqual(channel.ignored_groups(), [_GROUP])

    def test_group_event_is_queued(self) -> None:
        self.start()
        code, body = self.post(_group_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "accepted")

        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["conversation_id"], _GROUP)
        self.assertEqual(event["group"], store.validate_file_stem(_GROUP))
        self.assertEqual(event["sender_name"], "张天宇")
        self.assertEqual(event["sender_id"], "20002")
        self.assertEqual(event["text"], "周末有空吗")
        self.assertTrue(event["mentioned"])

    def test_card_preferred_over_nickname(self) -> None:
        self.start()
        self.post(_group_event(sender={"user_id": 20002, "nickname": "张天宇", "card": "天宇"}))
        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["sender_name"], "天宇")

    # -- 身份 -------------------------------------------------------------- #

    def identity_file(self, people: list[dict[str, Any]]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "identity.json"
        path.write_text(json.dumps({"people": people}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_sender_id_resolves_to_real_name(self) -> None:
        """群名片改成什么都按 QQ 号认人（speaker 不再随名片漂移）。"""
        path = self.identity_file([{"name": "王俊宇", "qq": "1027713979"}])
        self.start(identity_path=str(path))
        self.post(
            _group_event(
                user_id=1027713979,
                sender={"user_id": 1027713979, "nickname": "随便改的名", "card": "mita"},
            )
        )
        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["sender_name"], "王俊宇")
        self.assertEqual(event["sender_id"], "1027713979")
        self.assertEqual(channel.unknown_senders(), {})

    def test_person_without_qq_is_not_recognized(self) -> None:
        """没登记 QQ 的人不按名字认：名片叫什么都只当名片。"""
        path = self.identity_file([{"name": "王俊宇", "qq": ""}])
        self.start(identity_path=str(path))
        self.post(_group_event(sender={"user_id": 20002, "nickname": "x", "card": "王俊宇"}))
        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["sender_name"], "王俊宇")
        self.assertIn("20002", channel.unknown_senders())

    def test_unknown_sender_keeps_card_and_is_reported(self) -> None:
        self.start(identity_path=str(self.identity_file([])))
        self.post(_group_event(sender={"user_id": 20002, "nickname": "张天宇", "card": "天宇"}))
        event = channel.next_event(1.0)
        assert event is not None
        self.assertEqual(event["sender_name"], "天宇")
        self.assertIn("20002", channel.unknown_senders())

    def test_unknown_senders_empty_without_runtime(self) -> None:
        self.assertEqual(channel.unknown_senders(), {})

    def test_server_binding_is_exclusive(self) -> None:
        """Windows 的 SO_REUSEADDR 允许两个进程绑同一端口，必须关掉，否则会双实例。"""
        self.assertFalse(channel._Server.allow_reuse_address)

    def test_at_someone_else_is_not_mentioned(self) -> None:
        self.start()
        self.post(
            _group_event(
                message=[
                    {"type": "at", "data": {"qq": "99999"}},
                    {"type": "text", "data": {"text": " 在吗"}},
                ]
            )
        )
        event = channel.next_event(1.0)
        assert event is not None
        self.assertFalse(event["mentioned"])
        self.assertEqual(event["text"], "在吗")

    def test_string_cq_message(self) -> None:
        self.start()
        self.post(
            _group_event(
                message=f"[CQ:at,qq={_SELF}] 周末去图书馆吗",
                raw_message=f"[CQ:at,qq={_SELF}] 周末去图书馆吗",
            )
        )
        event = channel.next_event(1.0)
        assert event is not None
        self.assertTrue(event["mentioned"])
        self.assertEqual(event["text"], "周末去图书馆吗")

    def test_image_only_message_ignored(self) -> None:
        self.start()
        code, body = self.post(_group_event(message=[{"type": "image", "data": {"file": "a.jpg"}}]))
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "no_text")

    def test_chunked_report_is_accepted(self) -> None:
        """协议端用 Transfer-Encoding: chunked 上报时也要能读（BaseHTTPRequestHandler 不会自己解）。"""
        self.start()
        body = json.dumps(_group_event()).encode("utf-8")
        digest = hmac.new(_SECRET.encode("utf-8"), body, hashlib.sha1).hexdigest()
        chunked = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        import http.client

        host, port = self.base.removeprefix("http://").split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.putrequest("POST", "/")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Signature", f"sha1={digest}")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(chunked)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(payload["status"], "accepted")
        self.assertIsNotNone(channel.next_event(1.0))

    def test_duplicate_message_ignored(self) -> None:
        self.start()
        self.post(_group_event())
        code, body = self.post(_group_event())
        self.assertEqual(code, 200)
        self.assertEqual(body["reason"], "duplicate")
        self.assertIsNotNone(channel.next_event(0.5))
        self.assertIsNone(channel.next_event(0.05))

    # -- 发 ---------------------------------------------------------------- #

    def test_send_text_calls_onebot_api(self) -> None:
        self.start()
        with mock.patch.object(
            channel, "_call_action", return_value=(True, "")
        ) as action:
            self.assertTrue(channel.send_text(_GROUP, "在的"))
        api_base, action_name, params, token, _timeout = action.call_args[0]
        self.assertEqual(api_base, "http://127.0.0.1:1")
        self.assertEqual(action_name, "send_group_msg")
        self.assertEqual(params["group_id"], int(_GROUP))
        self.assertEqual(params["message"], [{"type": "text", "data": {"text": "在的"}}])
        self.assertEqual(token, "api-token")

    def test_send_failure_returns_false_and_logs(self) -> None:
        ctx = self.start()
        with mock.patch.object(channel, "_call_action", return_value=(False, "retcode 100")):
            self.assertFalse(channel.send_text(_GROUP, "在的"))
        self.assertTrue(any("发送失败" in text for _kind, text in ctx.logs), ctx.logs)

    def test_send_without_api_base(self) -> None:
        ctx = self.start(api_base="")
        self.assertFalse(channel.send_text(_GROUP, "在的"))
        self.assertTrue(any("api_base" in text for _kind, text in ctx.logs), ctx.logs)

    def test_send_text_to_private_uses_private_api(self) -> None:
        self.start()
        with mock.patch.object(
            channel, "_call_action", return_value=(True, "")
        ) as action:
            self.assertTrue(channel.send_text("private-1027713979", "在的"))
        _api, action_name, params, _token, _timeout = action.call_args[0]
        self.assertEqual(action_name, "send_private_msg")
        self.assertEqual(params["user_id"], 1027713979)
        self.assertNotIn("group_id", params)
        self.assertEqual(params["message"], [{"type": "text", "data": {"text": "在的"}}])

    def test_send_bad_target(self) -> None:
        ctx = self.start()
        self.assertFalse(channel.send_text("not-a-number", "在的"))
        self.assertFalse(channel.send_text("private-abc", "在的"))
        self.assertTrue(any("会话标识不合法" in text for _kind, text in ctx.logs), ctx.logs)

    def test_send_without_runtime(self) -> None:
        self.assertFalse(channel.send_text(_GROUP, "在的"))
        self.assertIsNone(channel.next_event(0.01))
        self.assertEqual(channel.ignored_groups(), [])

    def test_shutdown_stops_listening(self) -> None:
        self.start()
        channel.shutdown()
        self.assertFalse(channel.status()["running"])
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(f"{self.base}/health", timeout=1)


class CallActionTest(unittest.TestCase):
    """``_call_action`` 的响应解析（不经过 HTTP 服务器）。"""

    def test_retcode_error_is_failure(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"status":"failed","retcode":100,"wording":"bad params"}'
        response.__enter__ = lambda self: response
        response.__exit__ = lambda self, *args: False
        with mock.patch.object(urllib.request, "urlopen", return_value=response):
            ok, reason = channel._call_action("http://127.0.0.1:1", "send_group_msg", {}, "", 3)
        self.assertFalse(ok)
        self.assertIn("retcode 100", reason)

    def test_ok_response_is_success(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b'{"status":"ok","retcode":0,"data":{"message_id":7}}'
        response.__enter__ = lambda self: response
        response.__exit__ = lambda self, *args: False
        with mock.patch.object(urllib.request, "urlopen", return_value=response):
            ok, reason = channel._call_action("http://127.0.0.1:1", "send_group_msg", {}, "", 3)
        self.assertTrue(ok)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
