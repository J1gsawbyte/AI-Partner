"""JSON 配置读取模块。

要点（详见同目录 README.md）：

* 配置文件全部放在 ``<项目根>/configs``，均为 ``.json``；
* ``jsonReader("config")`` 读取 ``configs/config.json``；
* 模块被 import（程序启动）时即预构造并加载全部配置对象，之后访问只读内存缓存，
  不再重复读文件；
* 一个配置文件对应一个对象，同名对象全局唯一；
* 外部只 import 本模块底部预置的对象，不要自行构造实例。
"""

from __future__ import annotations

# 配置内容本质是动态 JSON，data/get 返回 Any 属于刻意设计，故在本文件关闭相关严格检查
# pyright: reportAny=false, reportExplicitAny=false, reportUnknownVariableType=false, reportUnknownMemberType=false

import json
import sys
from pathlib import Path
from typing import Any, ClassVar, Final

__all__ = ["config", "modules"]

# 日志统一走 logger 模块预置的 core_log 对象（写入 core.log）。
# 本项目按目录加 sys.path 导入，这里补齐同级 logger 目录，避免调用方额外配置。
_LOGGER_DIR: Final[Path] = Path(__file__).resolve().parents[1] / "logger"
if str(_LOGGER_DIR) not in sys.path:
    sys.path.insert(0, str(_LOGGER_DIR))

from logger import core_log  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

#: 配置根目录，固定在项目根（src/json/jsonReader.py 的上三级）下的 configs/
CONFIG_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "configs"

_ILLEGAL_CHARS: Final[frozenset[str]] = frozenset('<>:"/\\|?*')

#: Windows 单个文件名上限 255 个字符，减去 ".json" 后缀
_MAX_STEM_LENGTH: Final[int] = 250


def _validate_name(name: object) -> str:
    """校验配置名，返回去掉 ``.json`` 后缀的文件名主体。"""
    if not isinstance(name, str):
        raise TypeError(f"配置名必须是 str，收到 {type(name).__name__}")

    stem = name.strip()
    if stem.lower().endswith(".json"):
        # 允许传入 "config.json"，避免拼成 config.json.json
        stem = stem[: -len(".json")].strip()

    if not stem:
        raise ValueError("配置名不能为空")

    illegal = sorted({ch for ch in stem if ch in _ILLEGAL_CHARS})
    if illegal:
        raise ValueError(f"配置名 {name!r} 含路径分隔符或非法字符：{' '.join(illegal)}")

    if any(ord(ch) < 32 for ch in stem):
        raise ValueError(f"配置名 {name!r} 含控制字符")

    if len(stem) > _MAX_STEM_LENGTH:
        raise ValueError(f"配置名过长：{len(stem)} 个字符，上限 {_MAX_STEM_LENGTH}")

    if stem.endswith(".") or set(stem) == {"."}:
        raise ValueError(f"配置名 {name!r} 不能以点结尾或全为点")

    return stem


def _fail(name: str, reason: str) -> RuntimeError:
    """记录 error 日志并返回待抛出的异常，供 ``raise ... from exc`` 使用。"""
    core_log.error(f"读取{name}配置文件失败, 原因: {reason}")
    return RuntimeError(f"读取{name}配置文件失败, 原因: {reason}")


def _read_json(path: Path, name: str) -> Any:
    """读取并解析配置文件；空文件视为 ``{}``，成功记 info、失败记 error 并抛异常。"""
    try:
        text = path.read_text(encoding="utf-8-sig")  # 兼容带 BOM 的文件
    except FileNotFoundError as exc:
        raise _fail(name, f"文件不存在：{path}") from exc
    except OSError as exc:
        raise _fail(name, f"读取文件出错：{exc}") from exc

    if not text.strip():
        core_log.info(f"读取{name}配置文件完成")
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _fail(
            name,
            f"第 {exc.lineno} 行第 {exc.colno} 列不是合法 JSON：{exc.msg}",
        ) from exc

    core_log.info(f"读取{name}配置文件完成")
    return data


class jsonReader:
    """单个配置文件的读取器，构造时一次性读入并缓存。

    同名对象全局唯一（``jsonReader("config") is config``），所以同一个配置文件
    只会读一次；所有取配置操作都走内存，不再访问磁盘。

    典型用法（业务代码只使用模块底部的预置对象）::

        from jsonReader import config

        timeout = config.get("timeout", 30)        # 点号路径
        host = config["server.host"]               # 缺失抛 KeyError

    需要新的配置文件时，在本模块底部再加一行实例声明即可。
    """

    #: 已构造的对象，保证一个配置文件只对应一个实例、只读一次文件
    _cache: ClassVar[dict[str, "jsonReader"]] = {}

    # 实例属性由 __new__ 一次性赋值（命中缓存时不重复读文件），此处仅为类型声明
    _name: str = ""
    _path: Path = CONFIG_DIR
    _data: Any = None

    def __new__(cls, name: str) -> "jsonReader":
        stem = _validate_name(name)
        cached = cls._cache.get(stem)
        if cached is not None:
            return cached

        path = CONFIG_DIR / f"{stem}.json"
        instance = super().__new__(cls)
        instance._name = stem
        instance._path = path
        instance._data = _read_json(path, stem)
        cls._cache[stem] = instance
        return instance

    def __init__(self, name: str) -> None:
        """空实现：加载与校验都在 ``__new__`` 中完成，重复构造会命中缓存。"""

    # -- 只读属性 ---------------------------------------------------------- #

    @property
    def name(self) -> str:
        """配置名（不含 .json 后缀）。"""
        return self._name

    @property
    def path(self) -> Path:
        """该配置对应的完整文件路径。"""
        return self._path

    @property
    def data(self) -> Any:
        """缓存中的完整配置数据（只读用途，请勿原地修改）。"""
        return self._data

    # -- 取配置接口 -------------------------------------------------------- #

    def get(self, path: str = "", default: Any = None) -> Any:
        """按点号路径取值，例如 ``get("server.host")``；缺失返回 ``default``。

        传空字符串返回整份配置。键名本身含点时优先整键匹配，其次按点号逐级拆。
        """
        try:
            return self._resolve(path)
        except KeyError:
            return default

    def __getitem__(self, path: str) -> Any:
        """同 :meth:`get`，但路径不存在时抛 ``KeyError``。"""
        return self._resolve(path)

    # -- 内部实现 ---------------------------------------------------------- #

    def _resolve(self, path: object) -> Any:
        """按点号路径逐级取值；dict 用键、list 用下标，取不到抛 ``KeyError``。"""
        if not isinstance(path, str):
            raise TypeError(f"配置路径必须是 str，收到 {type(path).__name__}")

        node: Any = self._data
        if path == "":
            return node

        parts = path.split(".")
        index = 0
        while index < len(parts):
            if isinstance(node, dict):
                # 优先最长匹配，兼容 "a.b" 这种键名本身带点的写法
                for end in range(len(parts), index, -1):
                    key = ".".join(parts[index:end])
                    if key in node:
                        node = node[key]
                        index = end
                        break
                else:
                    raise KeyError(path)
            elif isinstance(node, list):
                try:
                    node = node[int(parts[index])]
                except (ValueError, IndexError):
                    raise KeyError(path) from None
                index += 1
            else:
                raise KeyError(path)

        return node


# --------------------------------------------------------------------------- #
# 预置配置对象：程序需要的全部配置实例都在此声明，外部只 import 对象
# --------------------------------------------------------------------------- #

config: Final[jsonReader] = jsonReader("config")
modules: Final[jsonReader] = jsonReader("modules")
