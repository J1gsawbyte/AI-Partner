"""项目日志模块。

设计要点（详细说明见同目录 README.md）：

* 每次程序启动（即本模块被首次 import）时，在项目根目录的 ``logs`` 下创建
  一个以启动时间命名的会话目录 ``logs/YYYY-MM-DD-HH-MM-SS/``；
* 本次运行期间创建的所有 logger 对象，都把日志写进这个会话目录；
* logger 是一个类，可创建多个实例，实例名即文件名（``logger("123")`` -> ``123.log``）；
* 每次调用写接口都直接打开文件追加、写完立即关闭，不使用缓冲，调用即落盘；
* 模块底部预置了程序需要的全部 logger 对象，外部只应
  ``from ... import core_log, transport_log``，不要在外部自行构造实例。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

__all__ = ["core_log", "transport_log"]


# --------------------------------------------------------------------------- #
# 会话目录：启动时间决定目录名，模块被 import（程序启动）时立即创建
# --------------------------------------------------------------------------- #

#: 日志根目录，固定在项目根（src/logger/logger.py 的上三级）下的 logs/
LOG_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "logs"

#: 本次运行的启动时间，用作会话目录名
STARTUP_TIME: Final[datetime] = datetime.now()


def _create_session_dir() -> Path:
    """计算并创建本次运行的会话目录，返回其路径。

    创建失败直接抛出 ``RuntimeError``，避免日志静默丢失。
    """
    path = LOG_ROOT / STARTUP_TIME.strftime("%Y-%m-%d-%H-%M-%S")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 仅在目录不可写时触发
        raise RuntimeError(f"无法创建日志目录：{path}") from exc
    return path


#: 本次运行的日志目录，例如 ``logs/2026-10-24-21-05-33``；import 时即创建
SESSION_DIR: Final[Path] = _create_session_dir()


# --------------------------------------------------------------------------- #
# 日志名合法性校验（遵循 Windows 文件/文件夹命名约定）
# --------------------------------------------------------------------------- #

_ILLEGAL_CHARS: Final[frozenset[str]] = frozenset('<>:"/\\|?*')

_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

#: Windows 单个文件名上限 255 个字符，减去 ".log" 后缀
_MAX_STEM_LENGTH: Final[int] = 251


def _validate_name(name: object) -> str:
    """校验日志名，返回去掉 ``.log`` 后缀的文件名主体。

    不合法时抛出 ``TypeError`` / ``ValueError``，并说明具体原因。
    """
    if not isinstance(name, str):
        raise TypeError(f"日志名必须是 str，收到 {type(name).__name__}")

    stem = name.strip()
    if stem.lower().endswith(".log"):
        # 允许传入 "core.log"，避免生成 core.log.log
        stem = stem[: -len(".log")].strip()

    if not stem:
        raise ValueError("日志名不能为空")

    illegal = sorted({ch for ch in stem if ch in _ILLEGAL_CHARS})
    if illegal:
        raise ValueError(
            f"日志名 {name!r} 含 Windows 不允许的字符：{' '.join(illegal)}"
        )

    if any(ord(ch) < 32 for ch in stem):
        raise ValueError(f"日志名 {name!r} 含控制字符")

    if len(stem) > _MAX_STEM_LENGTH:
        raise ValueError(
            f"日志名过长：{len(stem)} 个字符，上限 {_MAX_STEM_LENGTH}"
        )

    if stem.endswith(".") or set(stem) == {"."}:
        raise ValueError(f"日志名 {name!r} 不能以点结尾或全为点")

    if stem.split(".")[0].strip().upper() in _RESERVED_NAMES:
        raise ValueError(f"日志名 {name!r} 是 Windows 保留设备名")

    return stem


# --------------------------------------------------------------------------- #
# logger
# --------------------------------------------------------------------------- #


class logger:
    """单个日志文件的写入器。

    实例化时只校验名字、计算目标路径，不占用文件句柄；每次写日志时以追加模式
    打开、写入、关闭，因此不存在待刷新缓冲，进程异常退出也不会丢失已写内容。

    典型用法（业务代码只使用模块底部的预置对象）::

        from logger import core_log, transport_log

        core_log.info("程序启动")            # 写入 core.log
        transport_log.error("连接失败")      # 写入 transport.log

    需要新的日志文件时，在本模块底部再加一行实例声明即可。
    """

    def __init__(self, name: str) -> None:
        """以日志名创建实例，名字须符合 Windows 命名约定（非法时抛 ValueError）。"""
        self._name: str = _validate_name(name)
        self._path: Path = SESSION_DIR / f"{self._name}.log"

    # -- 只读属性 ---------------------------------------------------------- #

    @property
    def name(self) -> str:
        """日志名（不含 .log 后缀）。"""
        return self._name

    @property
    def path(self) -> Path:
        """该 logger 对应的完整文件路径。"""
        return self._path

    # -- 写日志接口 -------------------------------------------------------- #

    def info(self, message: object) -> None:
        """记录 INFO 级别日志。"""
        self._emit("INFO", message)

    def warn(self, message: object) -> None:
        """记录 WARN 级别日志。"""
        self._emit("WARN", message)

    def error(self, message: object) -> None:
        """记录 ERROR 级别日志。"""
        self._emit("ERROR", message)

    def debug(self, message: object) -> None:
        """记录 DEBUG 级别日志。"""
        self._emit("DEBUG", message)

    # -- 内部实现 ---------------------------------------------------------- #

    def _emit(self, level: str, message: object) -> None:
        """按 ``[HH:MM:SS] [LEVEL] message`` 格式追加一行并立即关闭文件。"""
        stamp = datetime.now().strftime("%H:%M:%S")
        text = "" if message is None else str(message)

        # 一次调用固定写一行：消息原样拼在级别标签后，不拆分、不加工换行
        # 每次调用独立开关文件：无缓冲、无残留句柄，返回即已落盘
        with open(self._path, "a", encoding="utf-8", newline="\n") as handle:
            print(f"[{stamp}] [{level}] {text}", file=handle)


# --------------------------------------------------------------------------- #
# 预置 logger 对象：程序需要的全部日志实例都在此声明，外部只 import 对象
# --------------------------------------------------------------------------- #

core_log: Final[logger] = logger("core")
transport_log: Final[logger] = logger("transport")
