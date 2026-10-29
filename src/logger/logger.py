"""项目日志模块。

设计要点（详细说明见同目录 README.md）：

* 每次程序启动（即本模块被首次 import）时，在项目根目录的 ``logs`` 下创建
  一个以启动时间命名的会话目录 ``logs/YYYY-MM-DD-HH-MM-SS/``；
* 会话目录保留数量有上限，取 ``configs/config.json`` 的 ``logger_folders_max``
  （缺失或非法时按 10 处理）；启动时若已达上限，先删除最旧的目录再创建本次的；
* 本次运行期间创建的所有 logger 对象，都把日志写进这个会话目录；
* logger 是一个类，可创建多个实例，实例名即文件名（``logger("123")`` -> ``123.log``）；
* 每次调用写接口都直接打开文件追加、写完立即关闭，不使用缓冲，调用即落盘；
* 模块底部预置了程序需要的全部 logger 对象，外部只应
  ``from ... import core_log, transport_log``，不要在外部自行构造实例。
"""

from __future__ import annotations

import json
import shutil
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

#: 会话目录名格式；固定宽度因此**字典序等于时间序**
_SESSION_FORMAT: Final[str] = "%Y-%m-%d-%H-%M-%S"

#: 程序配置里控制会话目录数量的字段名
MAX_FOLDERS_KEY: Final[str] = "logger_folders_max"

#: 未配置或配置非法时保留的会话目录数量
DEFAULT_MAX_FOLDERS: Final[int] = 10

#: 程序配置文件（与 jsonReader 的 configs/config.json 是同一份）
_CONFIG_FILE: Final[Path] = Path(__file__).resolve().parents[2] / "configs" / "config.json"


def _load_max_folders() -> int:
    """读取会话目录保留上限，缺失或非法时返回 :data:`DEFAULT_MAX_FOLDERS`。

    这里直接读文件而不复用 ``jsonReader``：``jsonReader`` 反过来 import 本模块，
    反向依赖会形成循环导入。读取失败一律静默回退，绝不能因为配置问题让程序起不来。
    """
    try:
        data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):  # 文件不存在 / 无权限 / 不是合法 JSON
        return DEFAULT_MAX_FOLDERS

    value = data.get(MAX_FOLDERS_KEY) if isinstance(data, dict) else None
    # bool 是 int 的子类，须先排除，避免 true 被当成 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return DEFAULT_MAX_FOLDERS
    return value


#: 本次运行允许保留的会话目录数量（含本次新建的；至少 1）
MAX_FOLDERS: Final[int] = _load_max_folders()

#: 本次启动被清理掉的旧会话目录名，供模块底部补一条日志
_PRUNED_DIRS: list[str] = []


def _is_session_dir(path: Path) -> bool:
    """只认严格符合时间戳格式的子目录，避免误删用户放进 ``logs`` 的其他内容。"""
    try:
        parsed = datetime.strptime(path.name, _SESSION_FORMAT)
    except ValueError:
        return False
    return parsed.strftime(_SESSION_FORMAT) == path.name


def _prune_session_dirs(keep: int) -> list[str]:
    """删除最旧的会话目录，使本次启动后总数不超过 ``keep``；返回被删目录名。"""
    try:
        sessions = sorted(
            p for p in LOG_ROOT.iterdir() if p.is_dir() and _is_session_dir(p)
        )
    except OSError:  # logs/ 还不存在（或无权限）：留给后面的 mkdir 处理
        return []

    # 目录名是固定宽度时间戳，排在最前的就是最旧的
    surplus = len(sessions) - keep + 1  # +1 是给本次新建的目录留位置
    removed: list[str] = []
    for path in sessions[: max(0, surplus)]:
        try:
            shutil.rmtree(path)
        except OSError:  # 被占用或权限不足：跳过，不能让启动失败
            continue
        removed.append(path.name)
    return removed


def _create_session_dir() -> Path:
    """清理超限的旧会话目录，再创建本次运行的会话目录并返回其路径。

    创建失败直接抛出 ``RuntimeError``，避免日志静默丢失。
    """
    path = LOG_ROOT / STARTUP_TIME.strftime(_SESSION_FORMAT)
    _PRUNED_DIRS.extend(_prune_session_dirs(MAX_FOLDERS))
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

# 启动时若清理过旧会话目录，在新会话的 core.log 里留一条痕迹
if _PRUNED_DIRS:
    core_log.warn(
        f"日志目录数超过上限 {MAX_FOLDERS}, 已删除最旧目录: {', '.join(_PRUNED_DIRS)}"
    )
