"""QQ 通道（群聊）：按 OneBot 11 协议收群消息，调协议端 API 发回复。

为什么是 OneBot 11
-------------------
QQ 登录协议由协议端负责（NapCatQQ、LLOneBot、Lagrange.Core 等），它们统一把事件
``POST`` 给业务端、并通过 HTTP API 接受发送请求。所以本模块只实现 OneBot 11 的
"上报接收端 + API 调用端"，换协议端不用改一行代码。

职责边界
--------
只管通道（接收、群白名单、去重、会话标识、发送失败日志）。
不 import ``brain``，不判唤醒（``wake`` 负责），不碰记忆。

* 收：协议端 ``POST /``（``X-Signature`` 用 ``secret`` 做 HMAC-SHA1 校验）→ 解析消息段 → 入队；
* 发：宿主调 :func:`send_text` → ``POST <api_base>/send_group_msg``（消息段数组，避免 CQ 码注入）。

群聊与私聊
----------
两条链路各有开关（``enable_group`` / ``enable_private``），默认**私聊开、群聊关**：

* 群：群号必须在 ``group_whitelist`` 里，空 = 全部拒绝；是否回复由 ``wake`` 按
  ``mentioned``（消息段里是否 @ 了 ``self_id``）决定；
* 私聊：**白名单就是身份库**——``identity.json`` 里登记过、且 ``enable`` 为真的 QQ 才能
  私聊，其余直接忽略（既挡住陌生人烧 token，也保证每个聊起来的人都认得出是谁）；
  私聊不 @，一律唤醒。人常说"一句一句发"，所以私聊消息会先攒
  ``private_merge_window_seconds`` 秒，静默后合成一条再交给宿主（``event["merged"]`` 是
  合并条数），避免同一段话被回三遍。
* **``enable: false`` 是黑名单**：这个人发来的消息**群里也不接**（群聊与私聊同一套口径），
  用来临时"闭麦"某个人；只是没登记的人不受影响，群里仍按群名片收。

会话标识（``send_text`` 的目标）用前缀区分：群是纯数字群号，私聊是 ``private-<QQ>``。
它同时是记忆目录名，所以**私聊与群聊的记忆天然分开**
（``<message_path>/private-<QQ>/`` 对 ``<message_path>/<群号>/``）。

对外接口（宿主 ``main_qq.py`` 通过 ``hub.get_module("qq_channel")`` 调用）
--------------------------------------------------------------------------
| 函数 | 用途 |
| --- | --- |
| ``next_event(timeout)`` | 取一条已校验的消息；没有则返回 ``None`` |
| ``send_text(target, text)`` | 发回原会话（群号或 ``private-<QQ>``），失败返回 ``False`` |
| ``ignored_groups()`` | 最近被拒绝的群号，宿主用来提示该填什么 |
| ``denied_privates()`` | 最近被拒绝的私聊 QQ（没在身份库登记） |
| ``unknown_senders()`` | 群里没登记身份的 QQ（沿用群名片当 speaker） |
| ``shutdown()`` / ``status()`` | 关监听 / 启动自检 |

配置（``configs/modules/qq_channel.json`` 的 ``config``）
--------------------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| `bind_host` / `bind_port` | `127.0.0.1` / 8786 | 只允许回环；`0` 表示随机端口（测试用） |
| `secret` | `""` | 与协议端的上报密钥一致，用于校验 `X-Signature`；留空则不校验（会告警） |
| `api_base` | `http://127.0.0.1:3000` | 协议端的 OneBot HTTP API 地址 |
| `access_token` | `""` | 调用 API 的 access token（与协议端配置一致，可为空） |
| `group_whitelist` | `[]` | 允许的群号（数字或字符串）；空 = 全部拒绝 |
| `dedup_ttl_seconds` | 600 | 同一 `message_id` 的去重窗口 |
| `max_body_bytes` | 65536 | 上报请求体上限 |
| `send_timeout` | 10 | 发送超时（秒） |
| `proactive_conversation_id` | `""` | 主动消息目标（群号或 `private-<QQ>`）；留空 = 丢弃 |
| `identity_path` | `configs/identity.json` | 身份库位置，相对路径按项目根解析；留空=用默认 |
| `enable_group` / `enable_private` | `false` / `true` | 是否接收群消息 / 私聊 |
| `private_sub_types` | `["friend"]` | 接受的私聊子类型；`group`（群临时会话）默认拒 |
| `private_merge_window_seconds` | 2.5 | 私聊连发的合并窗口（秒）：静默这么久就把这几条合成一条；0 = 关闭 |

为什么 speaker 不直接用群名片
------------------------------
群名片会改（`mita` → `汪敬杨`），而 ``messages.md`` 的 ``speaker`` 与 ``memory.md`` 的
key 前缀都以它为准：一改名，同一个人的记忆就被劈成两半。所以认人以 **QQ 号**为准：

* 身份库里有这个 QQ 号 → ``speaker`` 用真名（名片改了也认得出）；
* 没有 → 沿用群名片，并把 QQ 号记进「未知发言人」，宿主可在终端提示补登记。

为什么收侧用签名、发侧用 token：OneBot 11 标准里上报只能用 ``X-Signature`` 验签，
API 调用则是 ``Authorization: Bearer``。两边都不是我们的自定义协议，按标准来即可。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Final

_LIB_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import identity_store  # noqa: E402  # pyright: ignore[reportMissingImports]
import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

__all__ = [
    "denied_privates",
    "ignored_groups",
    "next_event",
    "on_start",
    "send_text",
    "shutdown",
    "status",
    "unknown_senders",
]

#: 项目根目录（``src/ai/module/modules/qq_channel.py`` 的上四级）
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[4]

#: 身份库默认位置：全局一份，所有群共用
DEFAULT_IDENTITY: Final[Path] = PROJECT_ROOT / "configs" / "identity.json"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

#: OneBot 上报默认打在根路径，也兼容 ``/event``
_REPORT_PATHS: Final[frozenset[str]] = frozenset({"/", "/event"})

#: CQ 码（字符串格式消息）的转义表
_CQ_UNESCAPE: Final[dict[str, str]] = {"&#91;": "[", "&#93;": "]", "&#44;": ",", "&amp;": "&"}

#: 去重表上限
_DEDUP_MAX: Final[int] = 512

#: 私聊会话标识前缀：群用纯数字群号，私聊用 ``private-<QQ>``，同时是记忆目录名
_PRIVATE_PREFIX: Final[str] = "private-"


def _bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    return value if isinstance(value, bool) else default


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_param(params: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(params.get(key, default))
    except (TypeError, ValueError):
        return default


def _safe_group(group_id: Any) -> str:
    """群号作记忆目录名：QQ 群号通常是纯数字，不合规时兜底成哈希。"""
    text = str(group_id).strip()
    stem = store.validate_file_stem(text)
    if stem:
        return stem
    return f"qq-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"


def _route(target: Any) -> tuple[str, str, int] | None:
    """会话标识 → ``(OneBot action, 参数名, 数字 ID)``；不合法返回 ``None``。

    ``private-<QQ>`` 走私聊 API，其余按群号；两个 ID 都是数字，所以必须靠前缀区分。
    """
    text = str(target or "").strip()
    if text.startswith(_PRIVATE_PREFIX):
        action, key, raw = "send_private_msg", "user_id", text[len(_PRIVATE_PREFIX) :]
    else:
        action, key, raw = "send_group_msg", "group_id", text
    return (action, key, int(raw)) if raw.isdigit() else None


def _identity_path(params: dict[str, Any]) -> Path:
    """身份库路径：留空用默认值，相对路径按项目根解析。"""
    raw = str(params.get("identity_path") or "").strip()
    path = Path(raw) if raw else DEFAULT_IDENTITY
    return path if path.is_absolute() else PROJECT_ROOT / path


def _unescape_cq(text: str) -> str:
    for escaped, plain in _CQ_UNESCAPE.items():
        text = text.replace(escaped, plain)
    return text


def _extract_text(message: Any, self_id: str) -> tuple[str, bool]:
    """从消息段里取纯文本与"是否 @ 了机器人"。

    数组格式直接读 ``text`` 段；字符串格式（CQ 码）则先去码再取文本。
    """
    if isinstance(message, str):
        mentioned = bool(self_id) and f"[CQ:at,qq={self_id}]" in message
        text = _unescape_cq(re.sub(r"\[CQ:[^\]]*\]", "", message)).strip()
        return text, mentioned

    parts: list[str] = []
    mentioned = False
    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                continue
            kind = segment.get("type")
            data = segment.get("data") or {}
            if kind == "text":
                parts.append(str(data.get("text") or ""))
            elif kind == "at" and self_id and str(data.get("qq")) == self_id:
                mentioned = True
    return "".join(parts).strip(), mentioned


def _sender_id(payload: dict[str, Any]) -> str:
    """发言人 QQ 号（认人主键）；上报缺字段时为空串。"""
    return str(payload.get("user_id") or "").strip()


def _sender_name(payload: dict[str, Any]) -> str:
    sender = payload.get("sender") or {}
    if not isinstance(sender, dict):
        return str(payload.get("user_id") or "")
    return str(sender.get("card") or sender.get("nickname") or payload.get("user_id") or "")


class _Handler(BaseHTTPRequestHandler):
    """处理协议端的上报；其它路径 404。"""

    server_version = "qq_channel/1"
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定名
        channel: _Channel = self.server.channel  # type: ignore[attr-defined]
        if self.path.split("?")[0] not in _REPORT_PATHS:
            self._reply(404, {"status": "error", "reason": "not_found"})
            return

        try:
            raw = self._read_body(channel)
        except ValueError as exc:
            reason = str(exc) or "bad_body"
            self._reject(channel, 413 if reason == "too_large" else 400, reason)
            return
        if not raw:
            self._reject(channel, 400, "empty_body")
            return
        signature = self.headers.get("X-Signature")
        if not channel.signature_ok(raw, signature):
            self._reject(channel, 401, "bad_signature", f"sig={'present' if signature else 'absent'}")
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(channel, 400, "bad_json")
            return
        if not isinstance(payload, dict):
            self._reject(channel, 400, "bad_json")
            return

        code, body = channel.accept(payload)
        if code >= 400:
            keys = ",".join(sorted(str(key) for key in payload)[:12])
            channel.reject(f"{code} {body.get('reason')} | keys={keys}")
        self._reply(code, body)

    def _read_body(self, channel: "_Channel") -> bytes:
        """读请求体：``Content-Length`` 和 ``Transfer-Encoding: chunked`` 都要能读。

        ``BaseHTTPRequestHandler`` 不会自己解 chunked，协议端用分块传输时只会读到 0 字节，
        然后被当成空请求拒掉。
        """
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                size_text = line.strip().split(b";")[0]
                if not size_text:
                    continue
                try:
                    size = int(size_text, 16)
                except ValueError as exc:
                    raise ValueError("bad_chunk") from exc
                if size == 0:
                    self.rfile.readline(65536)  # 最后一段的 CRLF
                    break
                total += size
                if total > channel.max_body_bytes:
                    raise ValueError("too_large") from None
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            return b"".join(chunks)

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("bad_length") from exc
        if length <= 0:
            return b""
        if length > channel.max_body_bytes:
            raise ValueError("too_large") from None
        return self.rfile.read(length)

    def _reject(self, channel: "_Channel", code: int, reason: str, extra: str = "") -> None:
        detail = f"{code} {reason}"
        if extra:
            detail += f" | {extra}"
        channel.reject(
            f"{detail} | CL={self.headers.get('Content-Length')} "
            f"TE={self.headers.get('Transfer-Encoding')}"
        )
        self._reply(code, {"status": "error", "reason": reason})

    def do_GET(self) -> None:  # noqa: N802
        channel: _Channel = self.server.channel  # type: ignore[attr-defined]
        if self.path.split("?")[0] != "/health":
            self._reply(404, {"status": "error", "reason": "not_found"})
            return
        self._reply(200, {"status": "ok", "groups": len(channel.groups)})

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return None

    def handle(self) -> None:
        """协议端关长连接时会抛 ConnectionReset，属正常噪声，不打堆栈。"""
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    channel: "_Channel"
    # Windows 的 SO_REUSEADDR 允许两个进程同时绑同一个端口，于是"第二个 main_qq.py"
    # 会静默启动、各自建一个日志目录、抢着处理同一份上报。这里关掉复用，让第二个
    # 进程直接 bind 失败、报错退出。
    allow_reuse_address = False


class _Channel:
    """一个进程内唯一的 QQ 通道：接收上报 + 调用协议端 API。"""

    def __init__(self, params: dict[str, Any], log_info: Any, log_error: Any) -> None:
        self.host = str(params.get("bind_host") or "127.0.0.1").strip()
        self.port = _int_param(params, "bind_port", 8786)
        self.secret = str(params.get("secret") or "").strip()
        self.api_base = str(params.get("api_base") or "").strip().rstrip("/")
        self.access_token = str(params.get("access_token") or "").strip()
        self.groups = {
            str(item).strip() for item in (params.get("group_whitelist") or []) if str(item).strip()
        }
        self.dedup_ttl = max(0, _int_param(params, "dedup_ttl_seconds", 600))
        self.max_body_bytes = max(1024, _int_param(params, "max_body_bytes", 65536))
        self.send_timeout = max(1.0, _float_param(params, "send_timeout", 10.0))
        self.identity_path = _identity_path(params)
        #: 群聊 / 私聊开关：默认关群、开私聊
        self.enable_group = _bool_param(params, "enable_group", False)
        self.enable_private = _bool_param(params, "enable_private", True)
        self.private_sub_types = {
            str(item).strip()
            for item in (params.get("private_sub_types") or ["friend"])
            if str(item).strip()
        }
        #: 私聊连发合并的静默窗口（秒）：0 = 关闭，来一条交一条
        self.merge_window = max(
            0.0, _float_param(params, "private_merge_window_seconds", 2.5)
        )
        self._log_info = log_info
        self._log_error = log_error

        self._inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        #: 合并时取出来却没交付给宿主的事件（群消息 / 别人的私聊），按原顺序放回
        self._carry: list[dict[str, Any]] = []
        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._warned: set[str] = set()
        self._warned_private: set[str] = set()
        self._warned_blocked: set[str] = set()
        self.ignored: list[str] = []
        #: 身份库没登记过的 QQ 号 → 当时的群名片（宿主据此提示补登记）
        self.unknown: dict[str, str] = {}
        #: 被拒绝的私聊 QQ → 原因（未登记 / 已停用）
        self.denied: dict[str, str] = {}
        self._server: _Server | None = None

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> str:
        if self.host not in _LOOPBACK_HOSTS:
            return f"bind_host 只能是回环地址，当前为 {self.host!r}"
        if self.port < 0 or self.port > 65535:
            return f"bind_port 非法：{self.port}"
        try:
            server = _Server((self.host, self.port), _Handler)
        except OSError as exc:
            return (
                f"监听 {self.host}:{self.port} 失败：{type(exc).__name__}: {exc}"
                "（端口被占用多半是已经有一个 main_qq.py 在跑，先关掉它再启动）"
            )
        server.channel = self
        self._server = server
        threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="qq-http",
            daemon=True,
        ).start()
        if not self.secret:
            self._log_error("未配置 secret：本机任何进程都能伪造上报，建议与协议端一起配上")
        if not self.api_base:
            self._log_error("api_base 为空：只能收消息，发不出去")
        return ""

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            self._server.shutdown()
            self._server.server_close()
        except OSError as exc:
            self._log_error(f"关闭监听失败，原因: {type(exc).__name__}: {exc}")
        self._server = None

    @property
    def listen(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def signature_ok(self, raw: bytes, header: Any) -> bool:
        """校验 ``X-Signature: sha1=<hex>``；没配 secret 时直接放行。"""
        if not self.secret:
            return True
        if not isinstance(header, str) or not header.startswith("sha1="):
            return False
        digest = hmac.new(self.secret.encode("utf-8"), raw, hashlib.sha1).hexdigest()
        return hmac.compare_digest(header[len("sha1=") :], digest)

    # -- 收 ---------------------------------------------------------------- #

    def accept(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """校验一条上报；群消息 / 私聊过开关、白名单、去重才入队。"""
        if str(payload.get("post_type")) != "message":
            return 200, {"status": "ignored", "reason": "not_message_event"}

        message_type = str(payload.get("message_type") or "")
        if message_type not in ("group", "private"):
            return 200, {"status": "ignored", "reason": "unsupported_message_type"}

        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            return 400, {"status": "error", "reason": "missing_message_id"}
        if not self._remember(message_id):
            return 200, {"status": "ignored", "reason": "duplicate"}

        self_id = str(payload.get("self_id") or "")
        text, mentioned = _extract_text(payload.get("message"), self_id)
        if not text:
            return 200, {"status": "ignored", "reason": "no_text"}

        if message_type == "group":
            return self._accept_group(payload, message_id, text, mentioned)
        return self._accept_private(payload, message_id, text)

    def _accept_group(
        self, payload: dict[str, Any], message_id: str, text: str, mentioned: bool
    ) -> tuple[int, dict[str, Any]]:
        if not self.enable_group:
            return 200, {"status": "ignored", "reason": "group_disabled"}

        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return 400, {"status": "error", "reason": "missing_group_id"}
        if group_id not in self.groups:
            self._warn_group(group_id)
            return 200, {"status": "ignored", "reason": "group_not_allowed"}

        sender_id = _sender_id(payload)
        book = self._book()
        if book is not None and sender_id and book.is_blocked(sender_id):
            self._warn_blocked(sender_id, "群消息")
            return 200, {"status": "ignored", "reason": "sender_blocked"}

        self._inbox.put(
            {
                "platform_message_id": message_id,
                "chat_type": "group",
                "conversation_id": group_id,
                "group": _safe_group(group_id),
                "sender_id": sender_id,
                "sender_name": self.speaker_of(sender_id, _sender_name(payload)),
                "text": text,
                "mentioned": mentioned,
            }
        )
        return 200, {"status": "accepted"}

    def _accept_private(
        self, payload: dict[str, Any], message_id: str, text: str
    ) -> tuple[int, dict[str, Any]]:
        """私聊：白名单就是身份库——登记过的 QQ 才能聊，其他一律忽略。"""
        if not self.enable_private:
            return 200, {"status": "ignored", "reason": "private_disabled"}

        sender_id = _sender_id(payload)
        if not sender_id:
            return 400, {"status": "error", "reason": "missing_user_id"}
        sub_type = str(payload.get("sub_type") or "friend")
        if sub_type not in self.private_sub_types:
            return 200, {"status": "ignored", "reason": "private_sub_type_not_allowed"}

        book = self._book()
        if book is not None and book.is_blocked(sender_id):
            self._warn_private(sender_id, "已停用 enable=false")
            return 200, {"status": "ignored", "reason": "private_blocked"}

        name = book.resolve(sender_id) if book is not None else ""
        if not name:
            self._warn_private(sender_id, "未登记")
            return 200, {"status": "ignored", "reason": "private_not_allowed"}

        # 私聊的记忆目录独立：`private-<QQ>`，与群号目录互不干扰
        conversation = f"{_PRIVATE_PREFIX}{sender_id}"
        self._inbox.put(
            {
                "platform_message_id": message_id,
                "chat_type": "private",
                "conversation_id": conversation,
                "group": _safe_group(conversation),
                "sender_id": sender_id,
                "sender_name": name,
                "text": text,
                # 私聊没有 @ 的概念，等同于被叫到（唤醒策略还另有 ctx.private 兜底）
                "mentioned": True,
            }
        )
        return 200, {"status": "accepted"}

    def _book(self) -> identity_store.IdentityBook | None:
        """读身份库（带 mtime 缓存）；读失败只记日志，按"谁也不认识"处理。"""
        try:
            return identity_store.load(self.identity_path)
        except Exception as exc:  # noqa: BLE001 - 身份库坏了不能挡住收消息
            self._log_error(f"身份库读取失败: {type(exc).__name__}: {exc}")
            return None

    def _resolve_qq(self, sender_id: str) -> str:
        """QQ 号 → 真名；身份库读不到或已停用时返回空。"""
        book = self._book()
        return book.resolve(sender_id) if book is not None else ""

    def speaker_of(self, sender_id: str, display_name: str) -> str:
        """发言人名字：身份库里有这个 QQ 号就用真名，否则沿用群名片。

        认人只看 QQ 号：名片改了、昵称换了都不影响 ``speaker``，记忆库的 key 前缀
        也就不会分裂。没登记的 QQ 号只用名片兜底，并进 ``unknown_senders()`` 提示补登记。
        """
        if not sender_id:
            return display_name
        name = self._resolve_qq(sender_id)
        if name:
            return name
        self._note_unknown(sender_id, f"群名片 {display_name}，暂用名片当 speaker")
        return display_name

    def _note_unknown(self, sender_id: str, detail: str) -> None:
        """同一 QQ 号只提示一次，避免每句话都刷日志。"""
        if sender_id in self.unknown:
            return
        self.unknown[sender_id] = detail
        self._log_info(
            f"身份未登记: QQ {sender_id}（{detail}）→ 补进 {self.identity_path} 后改名也能认出来"
        )

    def reject(self, detail: str) -> None:
        """记录被拒绝的上报：只记状态码、原因和字段名，不记正文。"""
        self._log_error(f"上报被拒: {detail}")

    def _warn_group(self, group_id: str) -> None:
        if group_id in self._warned:
            return
        self._warned.add(group_id)
        if len(self.ignored) < 5:
            self.ignored.append(group_id)
        self._log_info(
            f"未授权群 {group_id}（不在 group_whitelist），已忽略；把它加进白名单后重启即可"
        )

    def _warn_private(self, sender_id: str, reason: str) -> None:
        """没登记或已停用的人私聊过来：提示一次，不回复。"""
        if sender_id in self._warned_private:
            return
        self._warned_private.add(sender_id)
        if len(self.denied) < 5:
            self.denied[sender_id] = reason
        self._log_info(
            f"未授权私聊 QQ {sender_id}（{reason}），已忽略；"
            f"在 {self.identity_path.name} 里登记并 enable=true 后即可私聊"
        )

    def _warn_blocked(self, sender_id: str, where: str) -> None:
        """身份库登记了但 ``enable: false`` 的人：群聊消息也不接，只提示一次。"""
        if sender_id in self._warned_blocked:
            return
        self._warned_blocked.add(sender_id)
        self._log_info(
            f"已停用的身份 QQ {sender_id} 发来{where}，已忽略"
            f"（{self.identity_path.name} 里 enable=false）"
        )

    def _remember(self, message_id: str) -> bool:
        stamp = time.monotonic()
        with self._seen_lock:
            if len(self._seen) >= _DEDUP_MAX:
                cutoff = stamp - self.dedup_ttl
                self._seen = {key: value for key, value in self._seen.items() if value >= cutoff}
            if message_id in self._seen:
                return False
            self._seen[message_id] = stamp
            return True

    def next(self, timeout: float) -> dict[str, Any] | None:
        event = self._take(timeout)
        if event is None or self.merge_window <= 0:
            return event
        if str(event.get("chat_type") or "") != "private":
            return event
        return self._merge_private(event)

    def _take(self, timeout: float) -> dict[str, Any] | None:
        """取一条事件：先消化上次合并时没消费的（保持顺序），再等队列。"""
        if self._carry:
            return self._carry.pop(0)
        try:
            return self._inbox.get(timeout=max(0.0, float(timeout)))
        except queue.Empty:
            return None

    def _merge_private(self, first: dict[str, Any]) -> dict[str, Any]:
        """私聊连发合并：静默 ``private_merge_window_seconds`` 秒后一次性交给宿主。

        为什么要合并：私聊里人常常"一句一句发"（"明天" / "有空吗" / "一起去图书馆"），
        逐条送进 Brain 会得到三次互不知情的回复。这里只在**通道侧**攒着，等到静默窗口
        结束再交付一条合并文本——Brain / 模块完全不用知道有这回事。

        窗口每来一条同一会话的消息就重置（debounce），所以"一直发"就一直等；不同会话
        （群消息、别人的私聊）不合并，按原顺序放回，下一轮再交付。
        """
        conversation = str(first.get("conversation_id") or "")
        texts = [str(first.get("text") or "")]
        deadline = time.monotonic() + self.merge_window
        while True:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            nxt = self._take(remain)
            if nxt is None:
                break
            same = (
                str(nxt.get("chat_type") or "") == "private"
                and str(nxt.get("conversation_id") or "") == conversation
            )
            if not same:
                self._carry.insert(0, nxt)
                break
            texts.append(str(nxt.get("text") or ""))
            deadline = time.monotonic() + self.merge_window

        if len(texts) > 1:
            first["text"] = "\n".join(texts)
            first["merged"] = len(texts)
            first["mentioned"] = True
        return first

    # -- 发 ---------------------------------------------------------------- #

    def send(self, target: str, text: str) -> bool:
        """按会话标识发送：``private-<QQ>`` 走私聊，其余按群号。"""
        if not self.api_base:
            self._log_error("api_base 为空，无法发送")
            return False
        if not str(text).strip():
            return True

        route = _route(target)
        if route is None:
            self._log_error(
                f"会话标识不合法（应为群号或 {_PRIVATE_PREFIX}<QQ>），无法发送：{target!r}"
            )
            return False
        action, key, number = route
        # 用消息段数组而不是字符串，避免正文里的 [CQ:...] 被协议端解析成指令
        ok, reason = _call_action(
            self.api_base,
            action,
            {key: number, "message": [{"type": "text", "data": {"text": text}}]},
            self.access_token,
            self.send_timeout,
        )
        if not ok:
            self._log_error(f"发送失败（{target}）: {reason}")
        return ok


def _call_action(
    api_base: str, action: str, params: dict[str, Any], token: str, timeout: float
) -> tuple[bool, str]:
    """调用 OneBot HTTP API；返回 ``(是否成功, 失败原因)``，不抛异常。"""
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_base}/{action}", data=data, method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 本机地址
            body = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, "响应不是 JSON"
    if not isinstance(payload, dict):
        return False, "响应格式异常"
    retcode = payload.get("retcode")
    if retcode not in (0, None):
        detail = payload.get("wording") or payload.get("message") or ""
        return False, f"retcode {retcode} {detail}".strip()
    return True, ""


_RUNTIME: _Channel | None = None


# -- ModuleHub 钩子 -------------------------------------------------------- #


def on_start(ctx: Any) -> None:
    """启动上报接收；失败只记日志，不抛出。"""
    global _RUNTIME
    channel = _Channel(ctx.params, ctx.log_info, ctx.log_error)
    reason = channel.start()
    if reason:
        ctx.log_error(f"QQ 通道未启动: {reason}")
        _RUNTIME = None
        return
    _RUNTIME = channel
    ctx.log_info(
        f"已监听 {channel.listen}（群聊 {'开' if channel.enable_group else '关'} / "
        f"私聊 {'开' if channel.enable_private else '关'}，"
        f"群白名单 {len(channel.groups)} 个，API {channel.api_base}）"
    )


# -- 宿主接口 -------------------------------------------------------------- #


def next_event(timeout: float = 0.5) -> dict[str, Any] | None:
    """取一条通过校验的消息（群或私聊）；通道未启动或超时返回 ``None``。"""
    channel = _RUNTIME
    if channel is None:
        return None
    return channel.next(timeout)


def send_text(target: str, text: str) -> bool:
    """把一段回复发回原会话（群号或 ``private-<QQ>``）；失败返回 ``False``（已记日志）。"""
    channel = _RUNTIME
    if channel is None:
        return False
    return channel.send(target, text)


def ignored_groups() -> list[str]:
    """最近被拒绝的群号（最多 5 个）：宿主据此提示用户该填进白名单。"""
    channel = _RUNTIME
    return list(channel.ignored) if channel is not None else []


def denied_privates() -> dict[str, str]:
    """最近被拒绝的私聊：``QQ → 原因``（未登记 / 已停用），最多 5 个。"""
    channel = _RUNTIME
    return dict(channel.denied) if channel is not None else {}


def unknown_senders() -> dict[str, str]:
    """身份库没登记的发言人：``QQ 号 → 提示``。宿主据此提示补登记。"""
    channel = _RUNTIME
    return dict(channel.unknown) if channel is not None else {}


def shutdown() -> None:
    """关闭上报监听（宿主退出时调用）。"""
    global _RUNTIME
    channel, _RUNTIME = _RUNTIME, None
    if channel is not None:
        channel.stop()


def status() -> dict[str, Any]:
    """宿主启动自检用：是否在跑、监听地址、白名单群数、两条链路的开关。"""
    channel = _RUNTIME
    return {
        "running": channel is not None,
        "listen": channel.listen if channel is not None else "",
        "groups": len(channel.groups) if channel is not None else 0,
        "signed": bool(channel.secret) if channel is not None else False,
        "group_enabled": bool(channel.enable_group) if channel is not None else False,
        "private_enabled": bool(channel.enable_private) if channel is not None else False,
    }
