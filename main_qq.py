"""QQ 宿主入口：把群消息 / 私聊送进 ``Brain``，再把每段回复发回原会话。

``Brain`` 只负责编排，通道在 ``qq_channel`` 模块里；群聊与私聊的开关、白名单都在那边配。
QQ 侧不需要额外的适配进程：协议端（NapCatQQ / LLOneBot / Lagrange）自己登录 QQ，
按 OneBot 11 把事件 ``POST`` 给本进程，本进程再调它的 HTTP API 发消息。

用法::

    # 1) 启动协议端，配好 OneBot 11 HTTP 服务 + 事件上报地址 http://127.0.0.1:8786/
    # 2) 填 configs/modules/qq_channel.json（secret / access_token / 开关 / 群白名单）
    #    私聊白名单不用配：能私聊的人就是 configs/identity.json 里登记过 QQ 的人
    # 3) configs/modules.json 里把 "qq_channel" 设为 true
    python main_qq.py

协议端的上报密钥与 ``secret`` 要一致，API 的 access token 与 ``access_token`` 要一致，
否则会出现在「收到了但发不出去」或「上报被 401」的情况。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Final

_ROOT: Final[Path] = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src" / "ai" / "brain"))

def _use_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
        except (AttributeError, OSError, ValueError):
            pass


def _startup_failed(exc: Exception) -> None:
    """启动期异常兜底：打印可读原因 + 写一条 core.log + 等回车。

    新电脑上常见两类问题（没装第三方库、没建 config.json）过去都是裸 traceback：
    在终端里一闪而过、日志目录还没建，看起来就是"什么都不显示、直接退出"。
    """
    _use_utf8_console()
    print(f"[启动失败] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    if isinstance(exc, ImportError):
        print(
            "缺少第三方库，请先安装：pip install openai numpy onnxruntime tokenizers",
            file=sys.stderr,
            flush=True,
        )
    print("完整原因也会写入 logs/<启动时间>/core.log", file=sys.stderr, flush=True)
    try:  # logger 是项目内模块，没有第三方依赖，能独立写一条
        sys.path.insert(0, str(_ROOT / "src" / "logger"))
        from logger import core_log

        core_log.error(f"启动失败, 原因: {type(exc).__name__}: {exc}")
    except Exception:  # noqa: BLE001 - 兜底提示本身不能再失败
        pass
    try:
        input("按回车键退出...")
    except (EOFError, KeyboardInterrupt):
        pass


try:
    from brain import Brain  # noqa: E402  # pyright: ignore[reportMissingImports]
except Exception as exc:  # noqa: BLE001 - 缺依赖 / 缺配置都走同一条可读提示
    _startup_failed(exc)
    raise SystemExit(1)

#: 后台 tick 间隔（秒）
_TICK_INTERVAL: Final[float] = 5.0

#: 当前这轮回复该发回哪个会话（群号或 ``private-<QQ>``）；``chat`` 与 ``flush_outbox`` 串行，用锁保护
_ROUTE_LOCK: Final[threading.Lock] = threading.Lock()
_ROUTE: dict[str, str] = {"target": ""}

_CHANNEL: Any = None
_PROACTIVE_GROUP: str = ""

#: 已经提示过的未授权群
_SEEN_IGNORED: set[str] = set()

#: 已经提示过的未登记发言人（QQ 号）
_SEEN_UNKNOWN: set[str] = set()

#: 已经提示过的未授权私聊 QQ
_SEEN_DENIED: set[str] = set()


def _preview(text: str, limit: int = 40) -> str:
    """日志只看开头，避免把整段聊天记录落盘。"""
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _send(text: str, meta: dict[str, Any]) -> None:
    """``Brain.notifier``：一段调用一次，发回该段所属的会话（群或私聊）。"""
    kind = str(meta.get("kind") or "")
    with _ROUTE_LOCK:
        target = _PROACTIVE_GROUP if kind == "proactive" else _ROUTE["target"]
    if not target:
        print(f"[发送失败] 没有可用的会话路由（kind={kind}）", flush=True)
        return
    if _CHANNEL is None or not _CHANNEL.send_text(target, text):
        print(f"[发送失败] {target}，请查 logs 里的 core.log", flush=True)


def _report_ignored(channel: Any) -> None:
    """白名单没配时把实际收到的群号打到终端（模块日志只写进 logs/）。"""
    for group in channel.ignored_groups():
        if group in _SEEN_IGNORED:
            continue
        _SEEN_IGNORED.add(group)
        print(f"[白名单] 群 {group} 未授权，已忽略；填进 group_whitelist 后重启", flush=True)


def _report_unknown(channel: Any) -> None:
    """身份库没登记过的发言人：打一行到终端，方便直接补进 configs/identity.json。"""
    report = getattr(channel, "unknown_senders", None)
    if not callable(report):
        return
    for sender_id, detail in report().items():
        if sender_id in _SEEN_UNKNOWN:
            continue
        _SEEN_UNKNOWN.add(sender_id)
        print(
            f"[身份] QQ {sender_id}：{detail}；补进 configs/identity.json 后下一条消息即生效",
            flush=True,
        )


def _report_denied_private(channel: Any) -> None:
    """私聊白名单就是身份库：没登记的 QQ 被拒时提示一次，登记后即可聊。"""
    report = getattr(channel, "denied_privates", None)
    if not callable(report):
        return
    for sender_id, reason in report().items():
        if sender_id in _SEEN_DENIED:
            continue
        _SEEN_DENIED.add(sender_id)
        print(
            f"[私聊] QQ {sender_id}（{reason}），已忽略；"
            f"在 configs/identity.json 里 enable=true 后即可私聊",
            flush=True,
        )


def _start_tick_thread(brain: Brain) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(_TICK_INTERVAL):
            try:
                brain.tick()
            except Exception as exc:  # noqa: BLE001 - 单次调度失败不能拖垮进程
                print(f"[tick] 异常：{type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=loop, name="brain-tick", daemon=True).start()
    return stop


def _handle(brain: Brain, event: dict[str, Any]) -> None:
    target = str(event["conversation_id"])
    private = str(event.get("chat_type") or "group") == "private"
    where = "私聊" if private else f"群{target}"
    merged = int(event.get("merged") or 0)
    note = f"（合并 {merged} 条）" if merged > 1 else ""
    print(
        f"[收到] {event['sender_name']}@{where}{note}: {_preview(event['text'])}", flush=True
    )

    with _ROUTE_LOCK:
        _ROUTE["target"] = target
    try:
        reply = brain.chat(
            str(event["text"]),
            speaker=str(event["sender_name"]),
            group=str(event["group"]),
            mentioned=bool(event["mentioned"]),
            private=private,
        )
        if reply.strip():
            brain.flush_outbox(timeout=120)
    except RuntimeError as exc:
        print(f"[错误] {type(exc).__name__}: {exc}", flush=True)
    except ValueError as exc:
        print(f"[参数错误] {exc}", flush=True)
    finally:
        with _ROUTE_LOCK:
            _ROUTE["target"] = ""


def main() -> int:
    global _CHANNEL, _PROACTIVE_GROUP
    _use_utf8_console()

    brain = Brain(notifier=_send)
    brain.start()

    channel = brain.hub.get_module("qq_channel")
    if channel is None:
        print('未启用 QQ 通道：把 configs/modules.json 里的 "qq_channel" 改成 true', flush=True)
        return 1
    info = channel.status()
    if not info.get("running"):
        print(
            "QQ 通道未启动：端口多半已被另一个 main_qq.py 占用（也可能是 bind_host 不是"
            "回环地址）。先确认只有一个实例在跑，详情见 logs 里的 core.log",
            flush=True,
        )
        channel.shutdown()
        return 1
    _CHANNEL = channel
    _PROACTIVE_GROUP = str(
        brain.hub.get_params("qq_channel").get("proactive_conversation_id") or ""
    ).strip()

    print("=== AI Partner QQ 通道 ===")
    print(
        f"上报接收：{info['listen']} | "
        f"群聊：{'开' if info['group_enabled'] else '关'} | "
        f"私聊：{'开' if info['private_enabled'] else '关'}"
    )
    if not info.get("signed"):
        print("提示：secret 为空，任何本机进程都能伪造上报；建议与协议端配置一致", flush=True)
    if info["group_enabled"] and not info["groups"]:
        print("白名单为空：先去群里发一条消息，终端会打印群号，填进 group_whitelist 后重启",
              flush=True)
    if not info["group_enabled"]:
        print("群聊已关闭：要启用把 enable_group 改成 true 并填好 group_whitelist", flush=True)
    if info["private_enabled"]:
        print("私聊白名单 = 身份库：只有 configs/identity.json 里登记过 QQ 的人能聊", flush=True)
    if brain.hub.get_module("proactive") and not _PROACTIVE_GROUP:
        print("提示：proactive 已启用但没配 proactive_conversation_id，主动消息会被丢弃",
              flush=True)

    stop_tick = _start_tick_thread(brain)
    try:
        while True:
            event = channel.next_event(1.0)
            _report_ignored(channel)
            _report_unknown(channel)
            _report_denied_private(channel)
            if event is not None:
                _handle(brain, event)
    except KeyboardInterrupt:
        print("\n再见。", flush=True)
        return 0
    finally:
        stop_tick.set()
        channel.shutdown()


if __name__ == "__main__":
    sys.exit(main())
