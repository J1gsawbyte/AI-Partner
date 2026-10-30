"""OpenAI 访问模块：对外只暴露 ``post()``。

要点（详见同目录 README.md）：

* 用 ``import openai``（SDK 1.x），从 ``configs/config.json`` 读取
  ``base_url`` / ``api_key`` / ``model`` 三项配置；
* 客户端在模块 import（程序启动）时创建**一次**，之后所有调用复用同一个客户端；
* 创建失败不阻断程序启动，记 ``core_log.error`` 后由 ``post()`` 抛出；
* 发送/接收的原始信息写 ``transport_log.info``，失败写 ``core_log.error``。
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path
from typing import Any, Final

try:
    import openai
except ImportError as _exc:  # 新电脑还没装 SDK 时也要能启动，缺库原因留到 post() 再报
    openai = None  # pyright: ignore[reportConstantRedefinition]
    _OPENAI_ERROR = f"缺少第三方库 openai，请先安装：pip install openai（{_exc}）"
else:
    _OPENAI_ERROR = ""

# 复用本项目的 logger / jsonReader 模块（项目按目录加 sys.path 的方式导入）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[2]
for _module_dir in (_SRC_DIR / "logger", _SRC_DIR / "json"):
    if str(_module_dir) not in sys.path:
        sys.path.insert(0, str(_module_dir))

from jsonReader import config  # noqa: E402  # pyright: ignore[reportMissingImports]
from logger import core_log, transport_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["post"]

#: config.json 里必需的配置项
_REQUIRED_KEYS: Final[tuple[str, str, str]] = ("base_url", "api_key", "model")

#: messages 里允许的 role 取值（OpenAI 兼容接口通用集合）
_ALLOWED_ROLES: Final[frozenset[str]] = frozenset(
    {"system", "developer", "user", "assistant", "tool", "function"}
)

#: 允许透传给模型的可选参数（其余参数记错误后忽略）
_ALLOWED_OPTIONS: Final[frozenset[str]] = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "presence_penalty",
        "frequency_penalty",
        "stop",
        "seed",
        "response_format",
    }
)

#: 本次运行唯一可用的客户端；创建失败时保持 None
_client: Any = None
_client_error: str | None = None
_model: str | None = None


def _read_settings() -> tuple[str, str, str]:
    """从 config.json 取 ``base_url`` / ``api_key`` / ``model``，缺项或为空即失败。"""
    values: list[str] = []
    missing: list[str] = []
    for key in _REQUIRED_KEYS:
        value = config.get(key)
        text = "" if value is None else str(value).strip()
        if not text:
            missing.append(key)
        values.append(text)

    if missing:
        raise RuntimeError(f"config.json 缺少配置项：{', '.join(missing)}")

    base_url, api_key, model = values
    return base_url, api_key, model


def _create_client() -> None:
    """创建本次运行唯一的 OpenAI 客户端；成功记 core_log.info，失败记 core_log.error。"""
    global _client, _client_error, _model

    try:
        if openai is None:  # 没装 SDK：走统一的"启动不阻断、调用时报错"路径
            raise RuntimeError(_OPENAI_ERROR)
        base_url, api_key, model = _read_settings()
        _client = openai.OpenAI(base_url=base_url, api_key=api_key)
    except Exception as exc:  # 配置缺失、参数非法、SDK 异常等
        _client = None
        _client_error = f"{type(exc).__name__}: {exc}"
        core_log.error(f"OpenAI 客户端创建失败, 原因: {_client_error}")
        return

    _model = model
    # 只记录 base_url 与 model，api_key 不落日志
    core_log.info(f"OpenAI 客户端创建成功, base_url: {base_url}, model: {model}")


def _raw_content(response: Any) -> str:
    """取响应里模型返回的原始文本；取不到时退回整个响应的字符串形式。"""
    choices = getattr(response, "choices", None)
    if choices:
        content = getattr(getattr(choices[0], "message", None), "content", None)
        if content is not None:
            return str(content)
    return str(response)


def _normalize_messages(messages: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把入参整理成 OpenAI 要求的 messages 列表，并做结构校验。

    - ``str``：按旧的单轮用法包成 ``[{"role": "user", "content": 文本}]``；
    - ``list``：逐项校验 ``role`` / ``content``，原样浅拷贝后返回，顺序即对话顺序。
    """
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]

    if not isinstance(messages, list):
        raise TypeError(f"messages 必须是 str 或 list，收到 {type(messages).__name__}")

    if not messages:
        raise ValueError("messages 不能为空列表")

    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise TypeError(f"messages[{index}] 必须是 dict，收到 {type(item).__name__}")

        role = item.get("role")
        if not isinstance(role, str) or not role:
            raise ValueError(f"messages[{index}] 缺少 role 或 role 不是非空字符串")

        if role not in _ALLOWED_ROLES:
            allowed = ", ".join(sorted(_ALLOWED_ROLES))
            raise ValueError(f"messages[{index}].role 非法：{role!r}，可用值：{allowed}")

        if "content" not in item:
            raise ValueError(f"messages[{index}] 缺少 content")

        normalized.append(dict(item))

    return normalized


def post(messages: str | list[dict[str, Any]], **options: Any) -> Any:
    """把整段 messages 发给模型，返回 OpenAI 的原始响应对象。

    参数 ``messages`` 支持两种形式（内存、system 由调用方自行组装）：

    - 列表（推荐）：``[{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]``
    - 字符串（兼容旧调用）：等价于 ``[{"role": "user", "content": 字符串}]``

    可选参数 ``**options`` 透传给模型接口，支持
    ``temperature / top_p / max_tokens / presence_penalty / frequency_penalty /
    stop / seed / response_format``；其它键记 ``core_log.error`` 后忽略。

    - 发送前写 ``transport_log.info(f"发送的原始信息: {messages 的 JSON}")``；
    - 收到响应后写 ``transport_log.info(f"接收的原始信息: {content}")``；
    - 调用失败写 ``core_log.error`` 并抛 ``RuntimeError``。

    文本结果取 ``response.choices[0].message.content``。
    """
    if _client is None or _model is None:
        reason = _client_error or "未知原因"
        core_log.error(f"调用 OpenAI 失败, 原因: 客户端不可用（{reason}）")
        raise RuntimeError(f"OpenAI 客户端不可用：{reason}")

    try:
        payload = _normalize_messages(messages)
    except (TypeError, ValueError) as exc:
        core_log.error(f"调用 OpenAI 失败, 原因: {type(exc).__name__}: {exc}")
        raise

    transport_log.info(f"发送的原始信息: {_json.dumps(payload, ensure_ascii=False)}")

    extra: dict[str, Any] = {}
    for key, value in options.items():
        if key in _ALLOWED_OPTIONS:
            extra[key] = value
        else:
            core_log.error(f"post 收到不支持的参数，已忽略: {key}")

    kwargs: dict[str, Any] = {"model": _model, "messages": payload, **extra}

    try:
        response = _client.chat.completions.create(  # pyright: ignore[reportCallIssue]
            **kwargs,
        )
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        core_log.error(f"调用 OpenAI 失败, 原因: {reason}")
        raise RuntimeError(f"调用 OpenAI 失败, 原因: {reason}") from exc

    transport_log.info(f"接收的原始信息: {_raw_content(response)}")
    return response


# 程序启动（模块首次 import）即创建客户端，且只创建一次
_create_client()
