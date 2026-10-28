"""delay 精度补测：同群追加一轮真实对话，对齐「计划延迟」与「实际发送间隔」。

只做一件事：``chat`` 入队后读取 ``outbox.snapshot()`` 的 ``ready_at``（计划），
再 ``flush_outbox`` 记录 notifier 的真实调用时刻（实际），逐段对比偏差。
不删除任何数据：消息仍落本群消息库（``configs/config.json`` 的 ``message_path``）。
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
for _dir in (
    _ROOT / "src",
    _ROOT / "src" / "logger",
    _ROOT / "src" / "json",
    _ROOT / "src" / "ai" / "module" / "module_hub",
    _ROOT / "src" / "ai" / "module" / "lib",
    _ROOT / "src" / "ai" / "brain",
    _ROOT / "src" / "ai" / "post",
    _ROOT / "src" / "ai" / "prompt",
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from brain import Brain  # noqa: E402
from jsonReader import modules as module_switches  # noqa: E402
from module_hub import ModuleHub  # noqa: E402
from post import post  # noqa: E402

GROUP: Final[str] = "测试专用"
SPEAKER: Final[str] = "林舟"
LOG_PATH: Final[Path] = _ROOT / "data" / "delay_probe.log"

_timeline: list[dict[str, Any]] = []


def log(message: str) -> None:
    line = f"[{datetime.now():%H:%M:%S.%f}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def notifier(text: str, meta: dict[str, Any]) -> None:
    _timeline.append({"at": datetime.now(), "monotonic": time.monotonic(), "text": text})
    log(f"发送 {text}")


def main() -> int:
    hub = ModuleHub(dict(module_switches.data), model_caller=post)
    brain = Brain(hub=hub, notifier=notifier)
    brain.start()

    text = "用三句话说说你今晚在干嘛，一句一个自然段"
    log(f"用户: {text}")
    reply = brain.chat(text, speaker=SPEAKER, group=GROUP, mentioned=True)
    log(f"模型回包({len(reply)} 字): {reply}")

    queued = brain.outbox.snapshot()
    planned = [
        round((item.ready_at - queued[0].ready_at).total_seconds(), 2) for item in queued
    ]
    log(f"入队 {len(queued)} 段, 计划偏移={planned}")
    for item, offset in zip(queued, planned):
        log(f"  计划 {offset:>6.2f}s  {item.text}")

    brain.flush_outbox(timeout=300)

    started = _timeline[0]["monotonic"]
    actual = [round(item["monotonic"] - started, 2) for item in _timeline]
    gaps = [round(actual[0], 2)] + [
        round(actual[i] - actual[i - 1], 2) for i in range(1, len(actual))
    ]
    log(f"实际偏移={actual}")
    deviations = [round(abs(actual[i] - planned[i]), 2) for i in range(min(len(actual), len(planned)))]
    log(f"偏差(实际-计划)={deviations}")
    ok = len(actual) == len(planned) and all(delta <= 1.0 for delta in deviations)
    log(f"结论: {'PASS' if ok else 'FAIL'} 段数 {len(actual)}/{len(planned)} 最大偏差 {max(deviations) if deviations else '-'}")
    log(f"段间隔={gaps[1:]}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
