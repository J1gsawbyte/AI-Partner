"""真实群聊观测：只靠 prompt 约束时的纯文本效果（不改写回复）。

用法::

    python scripts/chat_style_test.py          # 真实 DeepSeek API
    python scripts/chat_style_test.py --mock   # 假模型，不联网、不花钱

口径
----
* 启用 persona / chat_style / reply_split / ai_typing_delay（与线上一致，不含记忆类模块）；
* 只统计模型回复里的括号动作与句尾语气词数量，**不做程序删减**；
* 校验 ``chat_style`` 模块的规则排在 system 末尾（persona 之后），这是"约束力最强"的前提；
* 校验回复经 ``reply_split`` 分句后按序入队（只读出站队列，不真等延迟）；
* ``--mock`` 时内容类断言（语气、态度）记为跳过，只验证链路；
* 产物保留：``logs/``、``data/test_reports/``。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LOG_ROOT: Final[Path] = ROOT / "logs"
GROUP: Final[str] = "测试专用"
CFG_DIR: Final[Path] = ROOT / "configs" / "test_chat_style"

#: persona.md 里的人格名（wake / 提示词都按这个名字，不再从已下线的 atom_memory 取）
AI_NAME: Final[str] = "宋文秀"

MOCK: Final[bool] = "--mock" in sys.argv

_ACTION = re.compile(r"[（(][^（）()\n]{1,40}[）)]")
_PARTICLE = re.compile(r"[呀哦呢嘛啦哈呗咯哟喔诶欸哇嗷捏咧嘞哒惹啊](?=[，。！？!?…~～；;,\s]|$)")


def _wait_free_log_slot() -> None:
    """日志目录按秒命名，避开已存在目录，保证本次运行独立。"""
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        if not (LOG_ROOT / stamp).exists():
            return
        time.sleep(0.4)


_wait_free_log_slot()

_SRC = ROOT / "src"
for _dir in (
    _SRC / "ai" / "brain",
    _SRC / "logger",
    _SRC / "json",
    _SRC / "ai" / "module" / "module_hub",
    _SRC / "ai" / "module" / "lib",
    _SRC / "ai" / "module" / "modules",
    _SRC / "ai" / "post",
    _SRC / "ai" / "prompt",
    Path(__file__).resolve().parent,
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from brain import Brain  # noqa: E402
from logger import SESSION_DIR  # noqa: E402
from mock_model import CALLS as MOCK_CALLS, mock_post  # noqa: E402
from module_hub import ModuleHub  # noqa: E402
from post import post  # noqa: E402


def _build_brain() -> tuple[Brain, list[str], Any]:
    """建 hub（配置复制到独立目录），返回 brain、发送记录与 chat_style 模块。"""
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("persona", "chat_style", "reply_split", "ai_typing_delay"):
        shutil.copyfile(ROOT / "configs" / "modules" / f"{name}.json", CFG_DIR / f"{name}.json")

    if MOCK:
        # 假模型模式下把节奏压到毫秒级，方便快速跑链路
        (CFG_DIR / "ai_typing_delay.json").write_text(
            json.dumps(
                {
                    "config": {
                        "msg_delay_base_range": [0.2, 0.4],
                        "msg_delay_per_char_range": [0.01, 0.02],
                        "msg_delay_max": 1,
                    },
                    "state": {},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    sent: list[str] = []

    def notifier(text: str, _meta: dict[str, Any]) -> None:
        sent.append(text)

    hub = ModuleHub(
        {"persona": True, "chat_style": True, "reply_split": True, "ai_typing_delay": True},
        config_dir=CFG_DIR,
        model_caller=mock_post if MOCK else post,
    )
    brain = Brain(hub=hub, notifier=notifier)
    hub.transcript_provider = brain.transcript
    brain.start()
    return brain, sent, hub.get_module("chat_style")


def _last_payload() -> list[dict[str, Any]]:
    """取最后一次真正发给模型的 messages。

    真实运行时读 ``transport.log``（由 ``post`` 写）；mock 时读假模型自己的调用记录。
    """
    if MOCK:
        return list(MOCK_CALLS[-1]) if MOCK_CALLS else []
    path = SESSION_DIR / "transport.log"
    if not path.is_file():
        return []
    prefix = "发送的原始信息: "
    for line in reversed(path.read_text(encoding="utf-8", errors="ignore").splitlines()):
        index = line.find(prefix)
        if index < 0:
            continue
        try:
            payload = json.loads(line[index + len(prefix) :].strip())
        except json.JSONDecodeError:
            continue
        return payload if isinstance(payload, list) else []
    return []


def _system_text() -> str:
    """最后一次请求里的 system 正文（prompt_construct 已把 system 合并成一条）。"""
    for item in _last_payload():
        if item.get("role") == "system":
            return str(item.get("content") or "")
    return ""


def _delay_limit() -> float:
    """当前生效的 ``msg_delay_max``（取模块配置，与模块实际读到的一致）。"""
    raw = json.loads((CFG_DIR / "ai_typing_delay.json").read_text(encoding="utf-8-sig"))
    return float((raw.get("config") or {}).get("msg_delay_max") or 30)


def _deltas(offsets: list[float]) -> list[float]:
    """累计偏移 → 每段自身的等待秒数（第一段的偏移恒为 0，不算延迟）。"""
    return [round(later - earlier, 3) for earlier, later in zip(offsets, offsets[1:])]


def _queue(brain: Brain) -> tuple[list[str], list[float]]:
    """读出站队列里的分段文本与相对第一条的累计计划延迟，然后清空队列。"""
    items = brain.outbox.snapshot()
    if not items:
        return [], []
    base = items[0].ready_at
    texts = [item.text for item in items]
    offsets = [round((item.ready_at - base).total_seconds(), 3) for item in items]
    brain.outbox.clear()
    return texts, offsets


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    mode = "mock（假模型）" if MOCK else "真实 API"
    print(f"=== prompt-only 纯文本观测 | {mode} | 日志目录 {SESSION_DIR} ===")
    brain, sent, style_module = _build_brain()
    limit = _delay_limit()

    if style_module is None:
        print("[FAIL] chat_style 模块未加载，无法校验格式规则")
        return

    # 发言人写在消息前缀里：当前没有任何模块把 ctx.speaker 渲染进 prompt，
    # 群聊里模型只能从文本本身看出"谁在说话"。
    cases = [
        ("张天宇", f"{AI_NAME}，周末有空吗？帮我把实验报告的数据整理一下吧"),
        ("许斌", f"{AI_NAME}，周末有空吗？帮我看看这个新番值不值得追"),
        ("汪敬杨", f"{AI_NAME}，周末有空吗？一起去图书馆复习影像诊断？"),
    ]

    rows: list[dict[str, Any]] = []
    for speaker, text in cases:
        line = f"{speaker}：{text}"
        reply = brain.chat(line, speaker=speaker, group=GROUP, mentioned=True)
        segments = brain.split_reply(reply)
        queued, offsets = _queue(brain)
        rows.append(
            {
                "speaker": speaker,
                "text": line,
                "reply": reply,
                "segments": segments,
                "queued": queued,
                "offsets": offsets,
                "actions": len(_ACTION.findall(reply)),
                "particles": len(_PARTICLE.findall(reply)),
            }
        )
        print(f"\n[{speaker}] {reply}")
        print(
            f"  动作={rows[-1]['actions']} 语气词={rows[-1]['particles']} 长度={len(reply)} "
            f"分段={len(segments)} 计划延迟={offsets}"
        )

    checks: list[dict[str, Any]] = []
    skipped: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")

    def skip(name: str, reason: str) -> None:
        skipped.append(name)
        print(f"[SKIP] {name} :: {reason}")

    system = _system_text()
    rules = style_module.STYLE_RULES
    check("三条回复都有内容", all(row["reply"].strip() for row in rows), f"len={[len(row['reply']) for row in rows]}")
    check("三人回复内容不同", len({row["reply"] for row in rows}) == 3, "")
    check("persona 已注入 system", AI_NAME in system, f"system_len={len(system)}")
    check(
        "chat_style 规则排在 system 末尾（persona 之后）",
        system.rstrip().endswith(rules.strip()) and system.find(rules.splitlines()[0]) > system.find(AI_NAME),
        f"tail={system[-30:]!r}",
    )

    total_actions = sum(row["actions"] for row in rows)
    total_particles = sum(row["particles"] for row in rows)
    if MOCK:
        skip("prompt-only 下无括号动作（观测值）", "假模型不模拟风格")
        skip("prompt-only 下语气词很少（≤1 个/条）", "假模型不模拟风格")
        skip("对张天宇仍偏冷淡/推脱", "假模型不模拟人格")
        skip("对汪敬杨仍偏配合", "假模型不模拟人格")
    else:
        check("prompt-only 下无括号动作（观测值）", total_actions == 0, f"actions={total_actions}")
        check("prompt-only 下语气词很少（≤1 个/条）", total_particles <= len(rows), f"particles={total_particles}")

        zhang = next(row["reply"] for row in rows if row["speaker"] == "张天宇")
        wang = next(row["reply"] for row in rows if row["speaker"] == "汪敬杨")
        check(
            "对张天宇仍偏冷淡/推脱",
            any(word in zhang for word in ("不一定", "不太", "不情愿", "没空", "不想", "算了", "挑时候", "作业")),
            zhang,
        )
        check("对汪敬杨仍偏配合", any(word in wang for word in ("有空", "可以", "行", "好")), wang)

    check(
        "回复被 reply_split 分句后按序入队",
        all(row["segments"] and row["queued"] == row["segments"] for row in rows),
        f"段数={[len(row['segments']) for row in rows]}",
    )
    check(
        "分段之间的等待为正且不超过 msg_delay_max",
        all(0 < delta <= limit for row in rows for delta in _deltas(row["offsets"])),
        f"limit={limit}, 每段={[_deltas(row['offsets']) for row in rows]}",
    )
    check("队列已清空（未真等延迟）", not sent and brain.outbox.pending() == 0, f"sent={len(sent)}")

    report_dir = ROOT / "data" / "test_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / f"chat_style-{SESSION_DIR.name}.json"
    target.write_text(
        json.dumps(
            {
                "scenario": "chat_style_prompt_only",
                "mode": "mock" if MOCK else "real",
                "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "log_dir": str(SESSION_DIR),
                "group": GROUP,
                "ai_name": AI_NAME,
                "system": system,
                "rows": rows,
                "checks": checks,
                "skipped": skipped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    passed = sum(1 for item in checks if item["ok"])
    print(f"\n=== 完成: {passed}/{len(checks)} 项通过 | 跳过 {len(skipped)} 项 | 报告 {target} ===")


if __name__ == "__main__":
    main()
