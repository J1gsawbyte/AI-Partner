"""模块上下文（ModuleContext）：模块钩子能用到的全部能力都由这里提供。

模块只依赖 ``ctx``，不要直接 import ``brain`` / ``openai`` / ``logger``：

* ``ctx.params``：本模块在 ``configs/modules/<name>.json`` 里的 ``config``；
* ``ctx.state``：本模块的运行态（同样存放在该文件，钩子结束后由 hub 回写）；
* ``ctx.ask(messages, tag)``：走 hub 注入的模型调用器（通常是 ``post``），不进对话主线；
* ``ctx.transcript(n)``：最近 n 轮对话（由 hub 注入的 provider 提供）；
* ``ctx.user_text`` / ``ctx.reply`` / ``ctx.now``：本次钩子的触发信息；
* ``ctx.speaker`` / ``ctx.group`` / ``ctx.mentioned``：群聊场景的身份信息
  （单人为空 / 默认 ``mentioned=True``，由 brain 或入口脚本注入）；
* ``ctx.phase``：``chat``（普通回复）或 ``proactive``（主动开口），由调用方告知；
* ``ctx.segments``：本轮待发送的分段（``plan_delays`` 钩子用）；
* ``ctx.log_info`` / ``ctx.log_error`` / ``ctx.save_state``。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Final

# 复用本项目的 logger 模块（项目按目录加 sys.path 的方式导入）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LOGGER_DIR: Final[Path] = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["ModuleContext"]


class ModuleContext:
    """传给模块钩子的上下文对象，每次调度前由 hub 填充触发信息。"""

    def __init__(
        self,
        name: str,
        params: dict[str, Any],
        state: dict[str, Any],
        hub: Any,
    ) -> None:
        self.name = name
        self.params = params
        self.state = state
        self._hub = hub

        #: 本次钩子对应的用户输入（build / on_user 时有值）
        self.user_text: str = ""
        #: 最近一次模型回复（on_reply 时有值）
        self.reply: str = ""
        #: 本轮发言人（群聊场景；单人为空，由模块自行解析）
        self.speaker: str = ""
        #: 记忆范围 / 群标识（群聊场景；单人为空）
        self.group: str = ""
        #: 本轮是否被 @（通道判定；默认 True 以保持单聊行为不变）
        self.mentioned: bool = True
        #: 是否是私聊（1 对 1）。与 ``group`` 不是一回事：私聊也有自己的记忆目录，
        #: 但唤醒策略不同（私聊必回），所以单独给一个标志，别让模块去猜目录名。
        self.private: bool = False
        #: 本次钩子属于哪个场景：``chat``（普通回复）/ ``proactive``（主动开口）
        self.phase: str = "chat"
        #: 本轮要发送的分段（``plan_delays`` 钩子用）
        self.segments: list[str] = []
        #: 本次钩子的触发时间
        self.now: datetime = datetime.now()
        #: data 根目录（``configs/config.json`` 的 ``message_path``，由 hub 注入）；
        #: 会话文件用 ``message_store.message_file(root, 会话)`` 拼，群聊 / 私聊自动分层
        self.message_path: Path | None = None

    # -- 模型与对话记录 ---------------------------------------------------- #

    def ask(
        self,
        messages: str | list[dict[str, Any]],
        tag: str = "",
        **options: Any,
    ) -> Any:
        """调用模型（旁路请求），返回原始响应对象。

        走 hub 注入的 ``model_caller``（通常是 ``post``），因此仍是全局唯一的
        OpenAI 客户端；``**options`` 原样透传（如 ``response_format={"type": "json_object"}``）。
        调用失败由 ``post`` 记 ``core_log.error`` 并抛 ``RuntimeError``。
        """
        caller = self._hub.model_caller
        if caller is None:
            message = f"模块 {self.name} 无法调用模型：hub 未注入 model_caller"
            core_log.error(message)
            raise RuntimeError(message)

        core_log.info(f"模块 {self.name} 发起模型请求: {tag or '未命名'}")
        return caller(messages, **options)

    def transcript(self, limit: int = 20) -> list[dict[str, Any]]:
        """取最近 ``limit`` 轮对话；hub 未注入 provider 时返回空列表。"""
        provider = self._hub.transcript_provider
        if provider is None:
            return []
        return provider(limit)

    # -- 状态与日志 -------------------------------------------------------- #

    def save_state(self) -> None:
        """立刻把 ``ctx.state`` 回写到 ``configs/modules/<name>.json``。"""
        self._hub.save_state(self.name)

    def log_info(self, message: object) -> None:
        core_log.info(f"[{self.name}] {message}")

    def log_error(self, message: object) -> None:
        core_log.error(f"[{self.name}] {message}")
