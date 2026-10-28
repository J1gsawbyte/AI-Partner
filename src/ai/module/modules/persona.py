"""persona 模块：读取 AI 人格文件，作为 system prompt 的一段。

* 人格文件默认 ``<项目根>/prompts/persona.md``，可在 ``configs/modules/persona.json``
  的 ``config.path`` 里改（相对路径按项目根解析）；
* 对外：``get_persona(path=None)`` 直接取文本；``build(ctx)`` 供 ModuleHub 调用；
* 读取成功写 ``core_log.info``，失败写 ``core_log.error`` 并抛 ``RuntimeError``；
* 不缓存：每次调用都重新读文件，改完 markdown 立即生效。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

# 复用本项目的 logger 模块（项目按目录加 sys.path 的方式导入）
_SRC_DIR: Final[Path] = Path(__file__).resolve().parents[3]
_LOGGER_DIR: Final[Path] = _SRC_DIR / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

__all__ = ["get_persona", "build", "PERSONA_FILE", "PROJECT_ROOT"]

#: 项目根目录（src/ai/module/modules/persona.py 的上四级）
PROJECT_ROOT: Final[Path] = _SRC_DIR.parent

#: 人格文件默认路径 prompts/persona.md
PERSONA_FILE: Final[Path] = PROJECT_ROOT / "prompts" / "persona.md"


def _resolve(path: str | None) -> Path:
    """把配置里的路径解析成绝对路径：空值用默认值，相对路径按项目根。"""
    if not path or not str(path).strip():
        return PERSONA_FILE
    candidate = Path(str(path).strip())
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _fail(reason: str) -> RuntimeError:
    """记录 error 日志并返回待抛出的异常，供 ``raise ... from exc`` 使用。"""
    core_log.error(f"读取Persona文件失败, 原因: {reason}")
    return RuntimeError(f"读取Persona文件失败, 原因: {reason}")


def get_persona(path: str | None = None) -> str:
    """读取人格文件并返回正文（去掉首尾空白，内部换行与缩进保留）。

    - 成功：写 ``core_log.info("读取Persona文件完成")`` 并返回正文；
    - 文件不存在 / 读取出错：写 ``core_log.error`` 并抛 ``RuntimeError``（消息含路径与原因）；
    - 文件为空：视为读取成功，返回空字符串，由调用方决定是否使用默认人格。
    """
    target = _resolve(path)
    try:
        text = target.read_text(encoding="utf-8-sig")  # 兼容带 BOM 的文件
    except FileNotFoundError as exc:
        raise _fail(f"文件不存在：{target}") from exc
    except OSError as exc:
        raise _fail(f"读取文件出错：{exc}") from exc

    core_log.info("读取Persona文件完成")
    return text.strip()


def build(ctx: Any) -> str:
    """ModuleHub 钩子：返回人格文本，路径取 ``ctx.params["path"]``（可省略）。"""
    params = getattr(ctx, "params", {}) or {}
    return get_persona(params.get("path"))
