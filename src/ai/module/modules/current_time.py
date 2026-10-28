"""当前时间：每轮在 prompt 末尾注入一条 ``user`` 消息，让模型知道"现在几点"。

为什么不用 system
------------------
``prompt_construct`` 会把所有 ``system`` 片段合并成一条放在**最前面**，和人格、档案、
格式约束挤在一起。prompt 一长，模型对最前面那一行的注意力很弱——实测问"现在几点"答不出来。
所以这里返回 ``user``：非 system 片段统一排在历史之后、本轮输入之前，正好是 prompt 的
末尾区域，紧贴着用户这句话。

``role`` 为什么是 user 而不是 assistant：它是**输入侧**的背景信息，不是模型说过的话。

配置（``configs/modules/current_time.json``）
--------------------------------------------
| 键 | 默认 | 说明 |
| --- | --- | --- |
| ``format`` | ``%Y-%m-%d %H:%M`` | ``strftime`` 格式；默认到分钟（秒只会让 prompt 不稳定） |
| ``template`` | ``当前真实时间为：{time}`` | 整句模板，``{time}`` 会被替换 |
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

__all__ = ["build"]

#: 默认时间格式：到分钟就够用
DEFAULT_FORMAT: Final[str] = "%Y-%m-%d %H:%M"
#: 默认整句模板
DEFAULT_TEMPLATE: Final[str] = "当前真实时间为：{time}"


def _params(ctx: Any) -> dict[str, Any]:
    params = getattr(ctx, "params", None)
    return params if isinstance(params, dict) else {}


def _now(ctx: Any) -> datetime:
    """优先用本轮的时间戳（``hub`` 会写 ``ctx.now``，测试里可控），否则取系统时间。"""
    moment = getattr(ctx, "now", None)
    return moment if isinstance(moment, datetime) else datetime.now()


def build(ctx: Any) -> dict[str, str]:
    """返回一条末尾的 ``user`` 消息；模板写错时退回默认格式，不打断回复。"""
    params = _params(ctx)
    moment = _now(ctx)
    fmt = str(params.get("format") or DEFAULT_FORMAT)
    template = str(params.get("template") or DEFAULT_TEMPLATE)
    try:
        text = template.format(time=moment.strftime(fmt))
    except (KeyError, IndexError, ValueError) as exc:
        ctx.log_error(f"时间模板不合法, 已用默认: {type(exc).__name__}: {exc}")
        text = DEFAULT_TEMPLATE.format(time=moment.strftime(DEFAULT_FORMAT))
    return {"role": "user", "content": text}
