"""模块系统（ModuleHub）：模块发现、开关、钩子调度、配置与运行态管理。

统一约定（详见 ``src/ai/module/README.md``）：

* 模块文件：``src/ai/module/modules/<name>.py``，一个文件就是一个模块；
* 开关：``configs/modules.json``，只放 ``{"<name>": true}``；**没列出来或为 false 的模块一律不加载**；
* 详细配置 + 运行态：``configs/modules/<name>.json``，结构 ``{"config": {...}, "state": {...}}``；
* 钩子（模块按需实现，签名统一为 ``hook(ctx)``）：
  ``build`` / ``on_start`` / ``on_user`` / ``on_reply`` / ``tick``；
* hub 只负责"机制"，什么时候调用钩子由调用方（brain / 入口脚本）决定；
* 模块出错只记 ``core_log.error`` 并跳过，不影响其它模块与主流程。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Final

# 复用本项目的 logger / jsonReader 模块（项目按目录加 sys.path 的方式导入）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_HUB_DIR: Final[Path] = Path(__file__).resolve().parent
for _module_dir in (_HUB_DIR, _SRC_DIR / "logger", _SRC_DIR / "json"):
    if str(_module_dir) not in sys.path:
        sys.path.insert(0, str(_module_dir))

from jsonReader import config as _app_config  # noqa: E402  # pyright: ignore[reportMissingImports]
from jsonReader import modules as _module_switches  # noqa: E402  # pyright: ignore[reportMissingImports]
from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

from module_context import ModuleContext  # noqa: E402  # pyright: ignore[reportImplicitRelativeImport, reportMissingImports]

__all__ = ["ModuleHub", "DEFAULT_MODULES_DIR", "DEFAULT_CONFIG_DIR"]

#: 项目根目录（src/ai/module/module_hub/module_hub.py 的上四级）
PROJECT_ROOT: Final[Path] = _SRC_DIR.parent

#: 模块目录：src/ai/module/modules
DEFAULT_MODULES_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "modules"

#: 模块详细配置目录：configs/modules
DEFAULT_CONFIG_DIR: Final[Path] = PROJECT_ROOT / "configs" / "modules"

#: 消息库根目录的默认值（程序配置 ``configs/config.json`` 的 ``message_path``）
DEFAULT_MESSAGE_PATH: Final[str] = "data/groups"

#: 模块可实现的钩子
HOOKS: Final[tuple[str, ...]] = (
    "build",
    "split",
    "on_start",
    "on_user",
    "on_reply",
    "tick",
    "proactive",
    "on_proactive",
)


def _is_enabled(raw: Any) -> bool:
    """开关支持 ``true`` 或 ``{"enabled": true}`` 两种写法。"""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, dict):
        return bool(raw.get("enabled", False))
    return False


def _is_valid_name(name: object) -> bool:
    """模块名必须是合法 Python 标识符且不以下划线开头（与文件名一致）。"""
    return (
        isinstance(name, str)
        and name.isidentifier()
        and not name.startswith("_")
    )


def _resolve_message_path(raw: str | Path | None) -> Path:
    """解析消息库**根目录**：显式入参 > 程序配置 > 默认值；相对路径按项目根解析。"""
    value = raw if raw is not None else (_app_config.get("message_path") or DEFAULT_MESSAGE_PATH)
    path = Path(str(value))
    return path if path.is_absolute() else PROJECT_ROOT / path


@dataclass
class _ModuleHandle:
    """一个已启用模块的运行时句柄。"""

    name: str
    module: ModuleType
    ctx: ModuleContext
    state_snapshot: str = field(default="", repr=False)

    def hook(self, hook_name: str) -> Callable[..., Any] | None:
        fn = getattr(self.module, hook_name, None)
        return fn if callable(fn) else None


class ModuleHub:
    """模块系统入口：加载模块、按钩子调度、读写模块配置与运行态。

    典型用法（由 brain 或入口脚本调用）::

        hub = ModuleHub(model_caller=post, transcript_provider=conv.history)
        hub.on_start()                            # 启动钩子
        fragments = hub.collect_fragments(text)   # 组装片段
        delays = hub.plan_delays(segments)        # 打字延迟（可选模块）
        hub.on_user(text); hub.on_reply(reply)    # 事件钩子
        hub.tick(datetime.now())                  # 定时钩子（主循环里周期性调用）
        hub.collect_proactive(now, snapshot)      # 主动消息：只提名
        hub.notify_proactive(result, now)         # 回执：提交/回滚冷却
    """

    def __init__(
        self,
        switches: dict[str, Any] | None = None,
        *,
        modules_dir: str | Path | None = None,
        config_dir: str | Path | None = None,
        model_caller: Callable[..., Any] | None = None,
        transcript_provider: Callable[[int], list[dict[str, Any]]] | None = None,
        message_path: str | Path | None = None,
    ) -> None:
        self.modules_dir = Path(modules_dir) if modules_dir else DEFAULT_MODULES_DIR
        self.config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
        #: 消息库根目录：各群的消息文件为 ``<message_path>/<群>/messages.md``
        #: （程序配置，不进模块配置；模块用 ``message_store.message_file`` 拼）
        self.message_path = _resolve_message_path(message_path)

        #: 由调用方注入：通常是 ``post``，保证全局只有一个 OpenAI 客户端
        self.model_caller = model_caller
        #: 由调用方注入：返回最近 n 轮对话
        self.transcript_provider = transcript_provider

        self._switches: dict[str, Any] = dict(
            switches if switches is not None else _module_switches.data
        )
        self._loaded: dict[str, ModuleType] = {}
        self._handles: list[_ModuleHandle] = []

        self.reload()

    # -- 加载 -------------------------------------------------------------- #

    def reload(self) -> None:
        """重读开关、重新扫描并加载模块（已加载的模块对象复用，不重复 import）。"""
        self._handles = []
        for name, raw in self._switches.items():
            if not _is_valid_name(name):
                core_log.error(f"模块名非法，已跳过: {name!r}")
                continue
            if not _is_enabled(raw):
                continue
            try:
                module = self._load(name)
            except Exception as exc:
                core_log.error(
                    f"模块 {name} 加载失败, 原因: {type(exc).__name__}: {exc}"
                )
                continue

            data = self._read_module_config(name)
            ctx = ModuleContext(name, data["config"], data["state"], self)
            ctx.message_path = self.message_path
            handle = _ModuleHandle(name, module, ctx)
            handle.state_snapshot = _dump(ctx.state)
            self._handles.append(handle)

        enabled = ", ".join(handle.name for handle in self._handles) or "无"
        core_log.info(f"模块系统就绪, 已启用模块: {enabled}")

    def _load(self, name: str) -> ModuleType:
        cached = self._loaded.get(name)
        if cached is not None:
            return cached

        path = self.modules_dir / f"{name}.py"
        if not path.is_file():
            raise FileNotFoundError(f"模块文件不存在：{path}")

        spec = importlib.util.spec_from_file_location(f"ai_module_{name}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载模块：{path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self._loaded[name] = module
        return module

    # -- 钩子调度 ---------------------------------------------------------- #

    def collect_fragments(
        self,
        user_text: str = "",
        *,
        speaker: str | None = None,
        group: str | None = None,
        mentioned: bool | None = None,
        private: bool | None = None,
        phase: str = "chat",
    ) -> list[Any]:
        """依次执行已启用模块的 ``build``，按模块顺序返回片段列表。

        片段可以是 ``str``、``{"role", "content"}`` 或它们的列表；非法返回值记
        ``core_log.error`` 并跳过，不影响其它模块。

        ``speaker`` / ``group`` / ``mentioned`` 为群聊场景的可选身份信息，
        传 ``None`` 时保持 ``ctx`` 中原有值（默认空 / True）。
        ``phase`` 告知模块这次是普通回复还是主动开口（模块据此决定内容）。
        """
        fragments: list[Any] = []
        for handle in self._handles:
            handle.ctx.user_text = user_text
            handle.ctx.phase = str(phase)
            if speaker is not None:
                handle.ctx.speaker = speaker
            if group is not None:
                handle.ctx.group = group
            if mentioned is not None:
                handle.ctx.mentioned = mentioned
            if private is not None:
                handle.ctx.private = private
            result = self._call_hook(handle, "build")
            if result is None:
                continue
            if not _is_valid_fragment(result):
                core_log.error(
                    f"模块 {handle.name} 的 build 返回值格式非法（应为 str / "
                    f"{{role, content}} / 它们的列表），已忽略"
                )
                continue
            fragments.append(result)
        return fragments

    def plan_delays(self, segments: list[str]) -> list[float]:
        """让实现了 ``plan_delays`` 钩子的模块给出每段发送前的等待秒数。

        钩子签名与其它钩子一致：``plan_delays(ctx)``，待发送的分段放在
        ``ctx.segments``，模块自己的参数在 ``ctx.params``（配置副本因此也能生效）。

        没有该模块就全返回 0（不分句、不延迟）。模块抛异常按 0 处理，不影响发送。
        """
        texts = [str(segment) for segment in segments]
        for handle in self._handles:
            planner = getattr(handle.module, "plan_delays", None)
            if not callable(planner):
                continue
            handle.ctx.segments = list(texts)
            try:
                planned = [max(0.0, float(value)) for value in planner(handle.ctx)]
            except Exception as exc:  # noqa: BLE001 - 延迟只是节奏，算不出来就立即发
                core_log.error(
                    f"模块 {handle.name} 的 plan_delays 执行失败, "
                    f"原因: {type(exc).__name__}: {exc}"
                )
                return [0.0] * len(texts)
            if len(planned) != len(texts):
                core_log.error(
                    f"模块 {handle.name} 的 plan_delays 返回 {len(planned)} 个值，"
                    f"与 {len(texts)} 段不一致，按 0 处理"
                )
                return [0.0] * len(texts)
            return planned
        return [0.0] * len(texts)

    def split_reply(self, reply: str) -> list[str]:
        """把一段回复拆成多段：第一个返回非空列表的模块生效，否则原样返回。

        ``split`` 钩子用于"像人一样分多条发消息"，只做切分、不产生新请求。
        """
        for handle in self._handles:
            handle.ctx.reply = reply
            result = self._call_hook(handle, "split")
            if result is None:
                continue
            if not isinstance(result, list) or not all(
                isinstance(item, str) for item in result
            ):
                core_log.error(
                    f"模块 {handle.name} 的 split 返回值格式非法（应为 str 列表），已忽略"
                )
                continue
            segments = [item.strip() for item in result if item.strip()]
            if segments:
                return segments
        return [reply] if reply else []

    # -- 模块访问 ---------------------------------------------------------- #

    def get_module(self, name: str) -> ModuleType | None:
        """返回已启用模块的模块对象；未启用或未加载返回 ``None``。"""
        for handle in self._handles:
            if handle.name == name:
                return handle.module
        return None

    def get_params(self, name: str) -> dict[str, Any]:
        """返回已启用模块的 ``config`` 参数；未启用返回空 dict。"""
        for handle in self._handles:
            if handle.name == name:
                return handle.ctx.params
        return {}

    def get_state(self, name: str) -> dict[str, Any]:
        """返回已启用模块的 ``state`` 运行态；未启用返回空 dict。"""
        for handle in self._handles:
            if handle.name == name:
                return handle.ctx.state
        return {}
        return {}

    def on_start(self) -> None:
        self._dispatch("on_start")

    def on_user(
        self,
        text: str,
        *,
        speaker: str | None = None,
        group: str | None = None,
        mentioned: bool | None = None,
        private: bool | None = None,
    ) -> None:
        """触发 ``on_user``；群聊场景可附带发言人、群标识与是否被 @。"""
        attrs: dict[str, Any] = {"user_text": text}
        if speaker is not None:
            attrs["speaker"] = speaker
        if group is not None:
            attrs["group"] = group
        if mentioned is not None:
            attrs["mentioned"] = mentioned
        if private is not None:
            attrs["private"] = private
        self._dispatch("on_user", **attrs)

    def should_wake(
        self,
        text: str,
        *,
        speaker: str | None = None,
        group: str | None = None,
        mentioned: bool | None = None,
        private: bool | None = None,
    ) -> bool:
        """询问各模块本轮是否应当回复；任一模块返回 False 即视为休眠。

        只有实现了 ``should_wake`` 的模块参与判定；模块异常按“唤醒”处理，
        避免旁路模块故障导致主对话不回复。
        """
        awake = True
        for handle in self._handles:
            fn = getattr(handle.module, "should_wake", None)
            if not callable(fn):
                continue
            handle.ctx.user_text = text
            if speaker is not None:
                handle.ctx.speaker = speaker
            # 显式置空表示单聊轮；不能沿用 config.group，否则单聊会被当成群聊
            handle.ctx.group = group or ""
            if mentioned is not None:
                handle.ctx.mentioned = mentioned
            if private is not None:
                handle.ctx.private = private
            handle.ctx.now = datetime.now()
            try:
                if fn(handle.ctx) is False:
                    awake = False
            except Exception as exc:  # noqa: BLE001 - 判定失败不应阻断对话
                core_log.error(
                    f"模块 {handle.name} 的 should_wake 执行失败, "
                    f"原因: {type(exc).__name__}: {exc}"
                )
        return awake

    def on_reply(self, reply: str, now: datetime | None = None) -> None:
        """触发 ``on_reply``：由 Brain 在回复**真正送出后**调用（``now`` 为送达时刻）。"""
        self._dispatch("on_reply", now=now, reply=reply)

    def tick(self, now: datetime | None = None) -> None:
        self._dispatch("tick", now=now or datetime.now())

    def collect_proactive(
        self,
        now: datetime | None = None,
        conversation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """收集各模块提名的主动消息请求（只提名，不发送）。

        返回值已校验、补上 ``module`` 字段，并按 ``priority`` 从高到低排序；非法项
        记 ``core_log.error`` 后跳过，不影响其它模块。``conversation`` 是只读会话快照
        （最近发言时间、是否有人等回复等），由调用方（Brain）算好，模块不自己读库。
        """
        moment = now or datetime.now()
        snapshot = dict(conversation or {})
        requests: list[dict[str, Any]] = []

        for handle in self._handles:
            handle.ctx.now = moment
            handle.ctx.conversation = snapshot
            result = self._call_hook(handle, "proactive", now=moment)
            if result is None:
                continue
            items = result if isinstance(result, (list, tuple)) else [result]
            for item in items:
                if not _is_valid_proactive(item):
                    core_log.error(
                        f"模块 {handle.name} 的 proactive 返回值格式非法"
                        f"（需要 rule_id / attempt_id / instruction|text），已忽略"
                    )
                    continue
                request = dict(item)
                request["module"] = handle.name
                requests.append(request)

        requests.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)
        return requests

    def notify_proactive(
        self, result: dict[str, Any], now: datetime | None = None
    ) -> None:
        """把主动消息的执行结果回抛给模块（提交/回滚冷却状态、落盘等）。"""
        self._dispatch("on_proactive", now=now, result=result)

    def _dispatch(self, hook_name: str, now: datetime | None = None, **attrs: Any) -> None:
        moment = now or datetime.now()
        for handle in self._handles:
            for key, value in attrs.items():
                setattr(handle.ctx, key, value)
            self._call_hook(handle, hook_name, now=moment)

    def _call_hook(
        self, handle: _ModuleHandle, hook_name: str, now: datetime | None = None
    ) -> Any:
        """调用单个钩子：未实现则跳过；抛异常则记 error 并继续；结束后按需回写状态。"""
        fn = handle.hook(hook_name)
        if fn is None:
            return None

        # 时间由调用方决定（便于测试注入），未指定时用当前时间
        handle.ctx.now = now or datetime.now()

        try:
            return fn(handle.ctx)
        except Exception as exc:
            core_log.error(
                f"模块 {handle.name} 的 {hook_name} 执行失败, "
                f"原因: {type(exc).__name__}: {exc}"
            )
            return None
        finally:
            self._flush_state(handle)

    # -- 配置与运行态 ------------------------------------------------------ #

    def _config_path(self, name: str) -> Path:
        return self.config_dir / f"{name}.json"

    def _read_module_config(self, name: str) -> dict[str, dict[str, Any]]:
        """读取 ``configs/modules/<name>.json``，缺文件或坏文件都退回空配置。"""
        path = self._config_path(name)
        if not path.is_file():
            return {"config": {}, "state": {}}

        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            core_log.error(
                f"模块 {name} 的配置文件读取失败, 原因: {type(exc).__name__}: {exc}"
            )
            return {"config": {}, "state": {}}

        if not isinstance(raw, dict):
            core_log.error(f"模块 {name} 的配置文件顶层必须是对象：{path}")
            return {"config": {}, "state": {}}

        params = raw.get("config", {})
        state = raw.get("state", {})
        if not isinstance(params, dict):
            core_log.error(f"模块 {name} 的 config 字段必须是对象，已按空处理")
            params = {}
        if not isinstance(state, dict):
            core_log.error(f"模块 {name} 的 state 字段必须是对象，已按空处理")
            state = {}
        return {"config": params, "state": state}

    def save_state(self, name: str) -> None:
        """把指定模块的 ``ctx.state`` 立刻写回配置文件（供模块主动调用）。"""
        for handle in self._handles:
            if handle.name == name:
                self._write_state(handle)
                handle.state_snapshot = _dump(handle.ctx.state)
                return

    def _flush_state(self, handle: _ModuleHandle) -> None:
        """状态有变化才写文件，避免每轮对话都产生磁盘写入。"""
        if _dump(handle.ctx.state) == handle.state_snapshot:
            return
        self._write_state(handle)
        handle.state_snapshot = _dump(handle.ctx.state)

    def _write_state(self, handle: _ModuleHandle) -> None:
        path = self._config_path(handle.name)
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                raw = {}  # 坏文件已在上游报错，这里按空处理重建

        if "config" not in raw:
            raw["config"] = handle.ctx.params
        raw["state"] = handle.ctx.state

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            core_log.error(
                f"模块 {handle.name} 的状态写入失败, 原因: {type(exc).__name__}: {exc}"
            )


def _dump(value: Any) -> str:
    """把状态序列化成可比较的字符串（用于判断是否有变化）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _is_valid_message(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("role"), str)
        and bool(value["role"].strip())
        and isinstance(value.get("content"), str)
    )


def _is_valid_fragment(value: Any) -> bool:
    """``build`` 的返回值：str、单条消息、或一组消息。"""
    if isinstance(value, str):
        return True
    if isinstance(value, dict):
        return _is_valid_message(value)
    if isinstance(value, (list, tuple)):
        return all(_is_valid_message(item) for item in value)
    return False


def _is_valid_proactive(value: Any) -> bool:
    """``proactive`` 的返回值：必须能定位规则、能去重、且有内容来源。"""
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("rule_id"), str) or not value["rule_id"].strip():
        return False
    if not isinstance(value.get("attempt_id"), str) or not value["attempt_id"].strip():
        return False
    instruction = str(value.get("instruction") or "").strip()
    text = str(value.get("text") or "").strip()
    return bool(instruction or text)



