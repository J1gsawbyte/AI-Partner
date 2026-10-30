"""命令行测试入口：在终端里和 AI 对话，体验「分多条 + 打字延迟」的节奏。

用法::

    python main.py

命令：``/help`` 看帮助，``/reset`` 清空对话历史，``/exit`` 退出。

工作方式：``Brain.chat()`` 生成回答后按模块分句、连同 ``ai_typing_delay`` 算出的
等待时间一起入**出站队列**；``brain.flush_outbox()`` 排空队列，``notifier`` 统一打印，
模拟人类连发多条消息。主动消息与回复走同一条队列，因此不会插队。

另有一个 daemon 线程每 5s 调一次 ``brain.tick()``，驱动 ``proactive`` 模块的
主动消息（早安 / 晚安等）。``/sleep`` 设置 ``brain.dormant``：只记录、不回复、
也不主动说话（队列里未发的主动消息会被撤销）。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any, Final

_ROOT: Final[Path] = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src" / "ai" / "brain"))


def _use_utf8_console() -> None:
    """Windows 控制台默认不是 UTF-8，能改就改，改不了不影响使用。"""
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

_HELP: Final[str] = (
    "/help     查看命令\n"
    "/as 名字  切换发言人（群聊测试）\n"
    "/sleep    休眠模式：只记录记忆，不回复也不主动发言\n"
    "/wake     唤醒模式：只回应 @名字（如 @宋文秀）的发言（默认）\n"
    "/reset    清空对话历史与待发送队列（模块状态不受影响）\n"
    "/exit     退出"
)


#: 主动消息的调度间隔（秒）；Brain 内部会与对话互斥
_TICK_INTERVAL: Final[float] = 5.0


def _send(text: str, meta: dict[str, Any]) -> None:
    """所有出站消息的唯一出口：Brain 已经分好段，一段调用一次。"""
    if meta.get("kind") == "proactive":
        rule = str(meta.get("rule_id") or "proactive")
        print(f"AI（主动·{rule}）: {text}", flush=True)
    else:
        print(f"AI: {text}", flush=True)


def _start_tick_thread(brain: Brain) -> threading.Event:
    """起一个 daemon 线程周期性调用 ``brain.tick()``；返回停止信号。"""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(_TICK_INTERVAL):
            try:
                brain.tick()
            except Exception as exc:  # noqa: BLE001 - 后台线程不能因单次异常退出
                print(f"[主动消息] 调度异常：{type(exc).__name__}: {exc}", flush=True)

    threading.Thread(target=loop, name="brain-tick", daemon=True).start()
    return stop


def _reply(brain: Brain, user_text: str, speaker: str = "", group: str = "") -> None:
    """跑一轮对话，然后把出站队列排空（打印由 ``_send`` 统一负责）。

    只有被唤醒才走回复链路：终端里用 ``@名字`` 模拟群聊 @（``@宋文秀``），
    由 ``wake`` 模块按 ``configs/modules/wake.json`` 的 ``names`` 判断；
    只写名字、不写 ``@`` 不会唤醒。没唤醒就只记录，到点由后台 ``tick`` 总结。
    """
    mentioned = "@" in user_text
    reply = brain.chat(user_text, speaker=speaker, group=group, mentioned=mentioned)
    if not reply.strip():
        print("（未唤醒：只记录，到点才总结；要用 @名字 才会回复）", flush=True)
        return

    sent = brain.flush_outbox(timeout=120)
    print(f"（{len(sent)} 段）", flush=True)


def main() -> None:
    _use_utf8_console()

    brain = Brain(notifier=_send)
    brain.start()
    stop_tick = _start_tick_thread(brain)

    enabled = [handle.name for handle in brain.hub._handles]  # 仅用于启动提示
    recent_params = brain.hub.get_params("recent_memory")
    group = str(recent_params.get("group") or "默认")
    speaker = ""

    print("=== AI Partner 命令行测试 ===")
    print(f"已启用模块：{', '.join(enabled) or '无'}")
    print(f"分句：{'开' if brain.hub.get_module('reply_split') else '关'} | "
          f"延迟：{'开' if brain.hub.get_module('ai_typing_delay') else '关'} | "
          f"主动：{'开' if brain.hub.get_module('proactive') else '关'}")
    wake_names = brain.hub.get_params("wake").get("names") or []
    wake_hint = "/".join(f"@{name}" for name in wake_names) or "（未配置名字）"
    print(f"记忆范围：{group} | 唤醒：{wake_hint} | 发言人：{speaker or '（默认）'}")
    print(_HELP)
    print()

    try:
        while True:
            try:
                user_text = input("你: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                return

            if not user_text:
                continue
            if user_text in {"/exit", "/quit"}:
                print("再见。")
                return
            if user_text == "/help":
                print(_HELP)
                continue
            if user_text == "/reset":
                brain.reset()
                print("（已清空对话历史与待发送队列）")
                continue
            if user_text.startswith("/as "):
                speaker = user_text[4:].strip()
                print(f"（发言人：{speaker or '（默认）'}）")
                continue
            if user_text == "/sleep":
                brain.set_dormant(True)
                print("（休眠：只记录记忆，不回复，也不主动发消息）")
                continue
            if user_text == "/wake":
                brain.set_dormant(False)
                print("（唤醒：正常回复）")
                continue

            if brain.dormant:
                try:
                    brain.observe(user_text, speaker=speaker, group=group)
                except RuntimeError as exc:
                    print(f"[错误] {exc}")
                    continue
                print("（休眠：已记录）")
                continue

            try:
                _reply(brain, user_text, speaker, group)
            except RuntimeError as exc:  # 模型不可用 / 调用失败，post 已写 core.log
                print(f"[错误] {exc}")
            except ValueError as exc:  # 参数问题
                print(f"[参数错误] {exc}")
    finally:
        stop_tick.set()


if __name__ == "__main__":
    main()
