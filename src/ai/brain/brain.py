"""对话编排（Brain）：模块片段 → messages → post → 历史与钩子。

一次 ``chat()`` 的流程：

1. ``hub.on_user(text)``：事件钩子（计数、打标签等）；
2. ``hub.collect_fragments(text)``：收集所有已启用模块的 ``build`` 片段；
3. ``prompt_construct(片段, 本轮输入)``：合并 system、拼接片段、返回 message 列表；
4. ``post(messages)``：发送并拿回原始响应，取 ``choices[0].message.content`` 作为回复；
5. 回复按模块分句后**入队**；等最后一段真正经 ``notifier`` 送出，才写本地记录并执行
   ``hub.on_reply(reply)``（落库、总结等旁路动作都发生在"送达之后"）。

会话窗口口径
------------
"最近 N 轮对话"属于记忆能力，由 ``recent_memory`` 自己记录、自己读出并注入（配置项
``history_rounds``），消息库路径取程序配置 ``message_path``；Brain 不拼历史。
``self.history`` 只是模块可读的进程内记录（``ctx.transcript()``）与调试用途，不参与
prompt 组装。

职责边界（模块独立）
--------------------
Brain 只负责**流程**：判唤醒 → 通知模块 → 取片段 → 组 prompt → 调模型 → 分句 →
入队 → 送达后回调。它不含任何业务文本：人格、格式风格、记忆、唤醒规则、打字节奏
全部由模块通过钩子贡献。所有开关关闭时，``chat()`` 仍然能跑通（只把用户那句话发给
模型，整段回复一条发出），不会因为缺少某个模块而报错。

主动消息与出站队列
------------------
模块在 ``proactive`` 钩子里**提名**（不调模型、不发送）；Brain 在 :meth:`tick` 中
拉取并生成文案，然后**入队**，由唯一的发送入口 :meth:`drain` 按 ``ready_at``
出队、经 ``notifier(text, meta)`` 发出，再用 ``on_proactive`` 把结果回抛给模块
提交或回滚冷却。

所有对外发言（回复分段、主动消息）都走同一个 :class:`Outbox`，队列严格 FIFO：
队首没到点，后面的条目一律等待，因此"分三段回复"不会被主动消息插队。用户一开口
（:meth:`chat` 入口）就把还没发出的主动消息撤掉。

模块只管"贡献片段 / 做旁路逻辑"，Brain 不解释模块内容；模型调用与对话历史由模块自行注入。
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Final, cast

# 复用本项目的既有模块（项目按目录加 sys.path 的方式导入）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[2]
for _module_dir in (
    _SRC_DIR / "logger",
    _SRC_DIR / "json",
    _SRC_DIR / "ai" / "post",
    _SRC_DIR / "ai" / "prompt",
    _SRC_DIR / "ai" / "module" / "module_hub",
):
    if str(_module_dir) not in sys.path:
        sys.path.insert(0, str(_module_dir))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]
from module_hub import ModuleHub  # noqa: E402  # pyright: ignore[reportMissingImports]
from outbox import KIND_PROACTIVE, KIND_REPLY, Outbox, OutboxItem  # noqa: E402  # pyright: ignore[reportMissingImports]
from post import post  # noqa: E402  # pyright: ignore[reportMissingImports]
from prompt_construct import prompt_construct  # noqa: E402  # pyright: ignore[reportMissingImports]

__all__ = ["Brain"]


def _reply_text(response: Any) -> str:
    """从 OpenAI 原始响应里取回复文本；取不到返回空串。"""
    choices = getattr(response, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if content is not None:
            return str(content)
    return ""


def _as_datetime(value: Any) -> datetime | None:
    """把 ``datetime`` / ISO 字符串转成 datetime；其它情况返回 None。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _first_per_attempt(items: list[OutboxItem]) -> list[OutboxItem]:
    """同一次主动消息会被拆成多段入队；回执只取每段 attempt 的第一条。"""
    seen: set[str] = set()
    picked: list[OutboxItem] = []
    for item in items:
        key = str(item.meta.get("attempt_id") or item.item_id)
        if key in seen:
            continue
        seen.add(key)
        picked.append(item)
    return picked


class Brain:
    """对话编排器：持有对话历史，按「模块片段 → messages → post」跑一轮对话。

    典型用法::

        brain = Brain()                  # 默认用 post 与 configs/modules.json 的开关
        brain.start()                    # 启动钩子
        reply = brain.chat("你好")        # 一轮对话
        brain.tick()                     # 主循环里周期性调用（定时型模块）

    测试时可注入：``Brain(hub=ModuleHub({"persona": True}, model_caller=fake))``。
    """

    def __init__(
        self,
        hub: ModuleHub | None = None,
        *,
        model_caller: Callable[[Any], Any] | None = None,
        notifier: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        #: 进程内对话记录（只存 user / assistant 成品消息，供模块 ``ctx.transcript()`` 读取；
        #: 不参与 prompt 组装——模型看到的"最近 N 轮"由记忆模块自己注入）
        self.history: list[dict[str, Any]] = []

        #: 主动消息的唯一发送出口：``notifier(文本, meta)``，一段一条；未注入则不发送
        self.notifier = notifier
        #: 休眠开关（``/sleep``）：为 True 时只记录、不回复、不发主动消息
        self.dormant = False

        #: 出站队列：回复分段与主动消息都从这里排队，只有一个发送入口（:meth:`drain`）
        self.outbox = Outbox()

        #: 对话与主动消息「生成 + 发送」互斥（同一线程可重入）
        self._proactive_lock = threading.RLock()
        self._started_at = datetime.now()
        self._last_user_at: datetime | None = None
        self._last_reply_at: datetime | None = None
        self._last_proactive_at: datetime | None = None

        caller = model_caller or post
        self.hub = (
            hub
            if hub is not None
            else ModuleHub(model_caller=caller, transcript_provider=self.transcript)
        )
        self._caller = cast(Callable[..., Any], self.hub.model_caller or caller)

    # -- 生命周期 ---------------------------------------------------------- #

    def start(self) -> None:
        """程序启动时调用一次：触发各模块的 ``on_start``。"""
        self.hub.on_start()

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """周期驱动：模块 ``tick`` → 丢弃过期条目 → 提名主动消息 → 发送到点条目。

        返回本次实际发送的结果列表（没事可做时为 ``[]``）。

        ``now`` 只用于测试注入。真实运行（``now is None``）时，发送阶段重新取「此刻」：
        主动消息可能要先调模型生成，用 tick 开头的时刻会把冷却 / 限流锚点提前。
        """
        moment = now or datetime.now()
        with self._proactive_lock:
            self.hub.tick(moment)
            dropped = self._drop_expired(moment)
            immediate = self._run_proactive(moment)
            return dropped + immediate + self.drain(now if now is not None else datetime.now())

    def set_dormant(self, dormant: bool) -> None:
        """切换休眠：进入休眠时撤掉还没发出的主动消息（会通知模块回滚）。"""
        with self._proactive_lock:
            self.dormant = bool(dormant)
            if self.dormant:
                self.cancel_proactive("dormant")

    # -- 出站队列 ---------------------------------------------------------- #

    def drain(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """把队首到点的条目发出去——整个程序唯一的发送入口。"""
        moment = now or datetime.now()
        with self._proactive_lock:
            items = self.outbox.take_ready(moment)
            return [self._deliver(item, moment) for item in items]

    def flush_outbox(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """阻塞到队列清空（宿主每轮对话结束后调用），返回所有发送结果。

        ``timeout`` 只是兜底：正常情况队列会在最后一段的 ``ready_at`` 后立即清空。
        """
        results: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout if timeout else None
        while self.outbox.pending():
            results.extend(self.drain(datetime.now()))
            if not self.outbox.pending():
                break
            if deadline is not None and time.monotonic() >= deadline:
                core_log.error(
                    f"出站队列未在 {timeout}s 内清空，剩余 {self.outbox.pending()} 条"
                )
                break
            time.sleep(0.02)
        return results

    def cancel_proactive(self, reason: str = "cancelled") -> int:
        """撤掉尚未发出的主动消息（用户开口 / 休眠 / 重置时调用）。"""
        moment = datetime.now()
        removed = self.outbox.cancel(kind=KIND_PROACTIVE, reason=reason)
        for item in _first_per_attempt(removed):
            self._notify_proactive(item, "skipped", reason, moment)
        if removed:
            core_log.info(f"[proactive] 撤销 {len(removed)} 段未发送消息, 原因={reason}")
        return len(removed)

    def _deliver(self, item: OutboxItem, moment: datetime) -> dict[str, Any]:
        """发送一条出站消息；主动消息在这里才回执给模块。"""
        if item.is_expired(moment):
            if item.kind == KIND_PROACTIVE and item.meta.get("notify"):
                return self._notify_proactive(item, "skipped", "expired", moment)
            return {"status": "skipped", "kind": item.kind, "reason": "expired", "text": ""}

        try:
            if self.notifier is not None:
                self.notifier(item.text, dict(item.meta))
        except Exception as exc:  # noqa: BLE001 - 宿主发送失败不应打断队列
            core_log.error(f"出站消息发送失败, 条目={item.item_id}, 原因: {type(exc).__name__}: {exc}")
            if item.kind == KIND_PROACTIVE and item.meta.get("notify"):
                return self._notify_proactive(item, "error", "send_error", moment)
            return {"status": "error", "kind": item.kind, "reason": "send_error", "text": ""}

        if item.kind != KIND_PROACTIVE:
            # 回复的落库点：最后一段真正发出去之后才写历史 / 触发 on_reply
            if item.meta.get("last"):
                self._record_reply(
                    str(item.meta.get("text") or item.text),
                    str(item.meta.get("user_text") or ""),
                    moment,
                )
            return {"status": "sent", "kind": item.kind, "text": item.text, "item_id": item.item_id}

        self._last_proactive_at = moment
        if not item.meta.get("notify"):
            return {"status": "sent", "kind": item.kind, "text": item.text, "item_id": item.item_id}
        return self._notify_proactive(item, "sent", "", moment)

    def _notify_proactive(
        self, item: OutboxItem, status: str, reason: str, moment: datetime
    ) -> dict[str, Any]:
        """把主动消息的最终状态回抛给模块（提交/回滚冷却）。

        ``sent_at`` 是本次回执的判定时刻（``sent`` 时就是真实送达时刻），模块记
        冷却 / 限流时应优先用它，而不是被 ``ctx.now`` 之类的调度时刻带偏。
        """
        meta = item.meta
        result = {
            "status": status,
            "kind": KIND_PROACTIVE,
            "rule_id": str(meta.get("rule_id") or ""),
            "attempt_id": str(meta.get("attempt_id") or ""),
            "module": str(meta.get("module") or ""),
            "reason": reason,
            "text": str(meta.get("text") or ""),
            "item_id": item.item_id,
            "sent_at": moment.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            self.hub.notify_proactive(result, moment)
        except Exception as exc:  # noqa: BLE001
            core_log.error(f"回抛主动消息结果失败, 原因: {type(exc).__name__}: {exc}")
        if status == "sent":
            core_log.info(f"[proactive] 已发送 规则={result['rule_id']} 字数={len(result['text'])}")
        return result

    def _drop_expired(self, moment: datetime) -> list[dict[str, Any]]:
        """清掉过期条目；主动消息过期必须回执，否则模块的 pending 永远清不掉。"""
        results: list[dict[str, Any]] = []
        for item in self.outbox.purge_expired(moment):
            if item.kind == KIND_PROACTIVE and item.meta.get("notify"):
                results.append(self._notify_proactive(item, "skipped", "expired", moment))
        return results

    # -- 主动消息 ---------------------------------------------------------- #

    def _run_proactive(self, moment: datetime) -> list[dict[str, Any]]:
        """拉取模块提名，取最高优先级生成文案并入队；任何异常都不外溢。

        已有未发出的主动消息时不再提名，避免多轮 tick 把队列堆满。
        返回"当场回执"的结果（生成失败 / 空文案 / 已过期），正常入队的由 :meth:`drain` 回执。
        """
        if self.notifier is None or self.dormant:
            return []
        if self.outbox.pending(KIND_PROACTIVE) > 0:
            return []

        try:
            requests = self.hub.collect_proactive(
                moment, self._conversation_snapshot(moment)
            )
        except Exception as exc:  # noqa: BLE001 - 主动消息是增强项，不能打断主循环
            core_log.error(f"收集主动消息失败, 原因: {type(exc).__name__}: {exc}")
            return []

        if not requests:
            return []
        return self._enqueue_proactive(requests[0], moment)

    def _enqueue_proactive(
        self, request: dict[str, Any], moment: datetime
    ) -> list[dict[str, Any]]:
        """生成文案并入队；生成失败 / 空文案 / 已过期直接回执给模块。"""
        rule_id = str(request.get("rule_id") or "")
        attempt_id = str(request.get("attempt_id") or "")
        module = str(request.get("module") or "")
        expires = _as_datetime(request.get("expires_at"))
        placeholder = OutboxItem(
            kind=KIND_PROACTIVE,
            text="",
            ready_at=moment,
            meta={"notify": True, "module": module, "rule_id": rule_id, "attempt_id": attempt_id},
            expires_at=expires,
        )

        if expires is not None and expires <= moment:
            return [self._notify_proactive(placeholder, "skipped", "expired", moment)]

        try:
            text = self._compose_proactive(request)
        except Exception as exc:  # noqa: BLE001 - 模型失败属于预期分支
            core_log.error(f"主动消息生成失败, 规则={rule_id}, 原因: {type(exc).__name__}: {exc}")
            return [self._notify_proactive(placeholder, "error", "api_error", moment)]

        if not text:
            return [self._notify_proactive(placeholder, "skipped", "empty", moment)]

        segments = self.split_reply(text) or [text]
        # 和普通回复复用同一套打字节奏：主动消息也不该在同一毫秒连发多段
        delays = self.hub.plan_delays(segments)
        offset = 0.0
        for index, segment in enumerate(segments):
            offset += max(0.0, float(delays[index])) if index < len(delays) else 0.0
            self.outbox.put(
                KIND_PROACTIVE,
                segment,
                ready_at=moment + timedelta(seconds=offset),
                meta={
                    "kind": KIND_PROACTIVE,
                    "notify": index == 0,  # 只由第一段回执，避免重复推进冷却
                    "part": index,
                    "total": len(segments),
                    "module": module,
                    "rule_id": rule_id,
                    "attempt_id": attempt_id,
                    "text": text,
                },
                expires_at=expires,
            )
        return []

    def _compose_proactive(self, request: dict[str, Any]) -> str:
        """有成品 ``text`` 就直接用，否则按 ``instruction`` 走完整生成链路。"""
        text = str(request.get("text") or "").strip()
        if text:
            return text

        instruction = str(request.get("instruction") or "").strip()
        if not instruction:
            return ""

        speaker = str(request.get("speaker") or "")
        group = str(request.get("group") or "")
        # 场景通过 ctx.phase 告知模块（风格、人格等模块自行决定返回什么）
        fragments = list(
            self.hub.collect_fragments(
                instruction, speaker=speaker, group=group, mentioned=True, phase="proactive"
            )
        )
        messages = prompt_construct(fragments, instruction)
        return _reply_text(self._caller(messages)).strip()

    def _conversation_snapshot(self, moment: datetime) -> dict[str, Any]:
        """给 ``proactive`` 模块的只读会话快照（只给事实，不做静默时长推导）。

        静默时长 / 是否有人等回复这类判断已移除：时段规则不该被"刚聊过天"挡住。
        需要时由 ``proactive`` 模块按这些时间戳自己算。
        """
        return {
            "now": moment,
            "started_at": self._started_at,
            "last_user_at": self._last_user_at,
            "last_reply_at": self._last_reply_at,
            "last_proactive_at": self._last_proactive_at,
            "dormant": self.dormant,
        }

    # -- 对话 -------------------------------------------------------------- #

    def chat(
        self,
        user_text: str,
        *,
        speaker: str = "",
        group: str = "",
        mentioned: bool = True,
        private: bool = False,
    ) -> str:
        """跑一轮对话，返回 AI 的完整回答文本。

        原始响应不对外暴露（需要时直接用 ``post``）；一轮只调用一次模型。
        想把回答拆成多条发出去，用 :meth:`chat_segments` 或 :meth:`split_reply`。

        ``speaker`` / ``group`` / ``mentioned`` 供群聊场景使用：默认值等价于原来的
        单聊行为（无发言人、无群标识、视为被 @）。``private=True`` 表示这是 1 对 1
        私聊：``group`` 仍是记忆目录，但唤醒策略按"私聊必回"走（``wake`` 不再要求 @）。
        ``dormant=True`` 时退化为 :meth:`observe`（只记录，不回复）。

        回答会按模块分句后**入队**（带打字延迟），由 :meth:`drain` / :meth:`flush_outbox`
        真正发出；最后一段送出后才写历史并触发 ``hub.on_reply``（先发后记）。
        用户一开口，还没发出的主动消息会被撤销。
        """
        text = "" if user_text is None else str(user_text)
        if not text.strip():
            raise ValueError("user_text 不能为空")

        # 与主动消息的「生成」互斥：整轮对话期间 tick 线程会等待
        with self._proactive_lock:
            self._last_user_at = datetime.now()
            # 用户开口 → 撤掉还没发出的主动消息（正在发送的那条不受影响）
            self.cancel_proactive("user_spoke")

            if self.dormant:
                self.observe(text, speaker=speaker, group=group, private=private)
                return ""

            awake = self.hub.should_wake(
                text, speaker=speaker, group=group, mentioned=mentioned, private=private
            )
            self.hub.on_user(
                text, speaker=speaker, group=group, mentioned=mentioned, private=private
            )
            if not awake:
                # 群聊休眠轮：只落盘 + 推进定时任务，不组装 prompt、不调模型
                self.hub.tick()
                return ""

            # 片段完全由模块提供（顺序 = modules.json 顺序），Brain 不含任何业务文本
            fragments = list(
                self.hub.collect_fragments(
                    text,
                    speaker=speaker,
                    group=group,
                    mentioned=mentioned,
                    private=private,
                    phase="chat",
                )
            )
            messages = prompt_construct(fragments, text)

            response = self._caller(messages)
            reply = _reply_text(response)
            if not reply.strip():
                core_log.error("模型回复为空，本轮不写入历史")
                return ""

            # 先入队：真正发出去之后才写历史并触发 on_reply（见 _deliver）
            self._enqueue_reply(reply, text)
            return reply

    def _enqueue_reply(self, reply: str, user_text: str = "") -> None:
        """把回答按模块分句后入队；延迟由 ``ai_typing_delay`` 提供（没有就是 0）。

        **入队不落库**：最后一段真正经 ``notifier`` 送出后，才由 :meth:`_record_reply`
        写历史并触发 ``hub.on_reply``，避免"记忆里已说过、实际还没发出去"。
        没注入 ``notifier`` 的调用方（脚本 / 测试）没有发送环节，生成即视为送达。
        """
        if self.notifier is None:
            self._record_reply(reply, user_text, datetime.now())
            return
        segments = self.split_reply(reply) or [reply]
        delays = self.hub.plan_delays(segments)
        moment = datetime.now()
        offset = 0.0
        last_index = len(segments) - 1
        for index, segment in enumerate(segments):
            offset += max(0.0, float(delays[index])) if index < len(delays) else 0.0
            self.outbox.put(
                KIND_REPLY,
                segment,
                ready_at=moment + timedelta(seconds=offset),
                meta={
                    "kind": KIND_REPLY,
                    "part": index,
                    "total": len(segments),
                    "last": index == last_index,
                    "text": reply,
                    "user_text": user_text,
                },
            )

    def _record_reply(self, reply: str, user_text: str, moment: datetime) -> None:
        """回复真正送出后：写进程内历史，再通知模块（落库 / 总结都在这里）。"""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self._last_reply_at = moment
        self.hub.on_reply(reply, now=moment)

    def chat_segments(
        self,
        user_text: str,
        *,
        speaker: str = "",
        group: str = "",
        mentioned: bool = True,
        private: bool = False,
    ) -> list[str]:
        """跑一轮对话，返回按模块分句后的多段回答（无分句模块时只有一段）。"""
        reply = self.chat(
            user_text, speaker=speaker, group=group, mentioned=mentioned, private=private
        )
        return self.split_reply(reply)

    def observe(
        self, user_text: str, *, speaker: str = "", group: str = "", private: bool = False
    ) -> None:
        """只记录、不回复（群聊休眠轮）。

        执行 ``on_user``（由记录类模块落盘）与 ``tick``（让定时型模块推进总结、过期），
        不组装 prompt、不调用模型、不写本地对话记录，因此不会产生任何回复。
        休眠消息会不会被记住，取决于当前启用的模块（例如 ``recent_memory`` 会写盘），
        Brain 不决定这件事。
        """
        text = "" if user_text is None else str(user_text)
        if not text.strip():
            raise ValueError("user_text 不能为空")

        with self._proactive_lock:
            self._last_user_at = datetime.now()
            self.hub.on_user(
                text, speaker=speaker, group=group, mentioned=False, private=private
            )
            self.hub.tick()

    def split_reply(self, reply: str) -> list[str]:
        """把已有的回答交给模块分句，不产生新的模型请求。

        用于"拿到完整回答后，再按人类习惯分多条发出去"的场景。
        """
        if not reply or not reply.strip():
            return []
        return self.hub.split_reply(reply)

    # -- 模块可见的对话记录 ------------------------------------------------ #

    def transcript(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近 ``limit`` 条消息的副本（供 ``ctx.transcript()`` 使用）。"""
        if limit <= 0:
            return []
        return [dict(item) for item in self.history[-limit:]]

    def reset(self) -> None:
        """清空对话历史与待发送队列（模块状态不受影响）。"""
        with self._proactive_lock:
            self.history.clear()
            self.cancel_proactive("reset")
            self.outbox.clear()
