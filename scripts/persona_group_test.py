"""真实群聊验收：persona 三人物态度差异 + wake 判定 + recent_memory 注入 + 打字延迟实测。

用法::

    python scripts/persona_group_test.py          # 真实 DeepSeek API
    python scripts/persona_group_test.py --mock   # 假模型，不联网、不花钱

口径
----
* 启用模块与线上 ``configs/modules.json`` 一致：persona / chat_style / wake /
  recent_memory / reply_split / ai_typing_delay；
* 使用用户当前配置；运行态配置复制到 ``configs/test_persona_group/``，不污染用户配置；
* 消息库 ``data/groups/测试专用/messages.md`` 由 **recent_memory 自己写**（不再手工造数据），
  这里只读它来核对"记了没有 / 注入了没有"；
* 延迟实测走真实出站队列（``chat → 队列 → flush_outbox → notifier 时间戳``），不再手工 sleep；
* ``--mock`` 时内容类断言（人格态度）记为跳过，只验证链路与节奏；
* 产物（日志、群数据、报告）保留在 ``logs/``、``data/groups/测试专用/``、``data/test_reports/``。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Final

ROOT: Final[Path] = Path(__file__).resolve().parents[1]
LOG_ROOT: Final[Path] = ROOT / "logs"


def _wait_free_log_slot() -> None:
    """日志目录按秒命名，避开已存在目录，保证本次运行独立。"""
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        if not (LOG_ROOT / stamp).exists():
            return
        time.sleep(0.4)


_wait_free_log_slot()

_SRC: Path = ROOT / "src"
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
from jsonReader import config as app_config  # noqa: E402
from logger import SESSION_DIR  # noqa: E402
from mock_model import CALLS as MOCK_CALLS, mock_post  # noqa: E402
from module_hub import ModuleHub  # noqa: E402
from post import post  # noqa: E402
import ai_typing_delay as typing_delay  # noqa: E402
import message_store as store  # noqa: E402

GROUP: Final[str] = "测试专用"
#: persona.md 里的人格名（wake 配置、消息前缀都按这个名字）
AI_NAME: Final[str] = "宋文秀"
TEST_CFG_DIR: Final[Path] = ROOT / "configs" / "test_persona_group"
REPORT_DIR: Final[Path] = ROOT / "data" / "test_reports"

MOCK: Final[bool] = "--mock" in sys.argv

_SWITCHES: Final[dict[str, bool]] = {
    "persona": True,
    "chat_style": True,
    "wake": True,
    "recent_memory": True,
    "reply_split": True,
    "ai_typing_delay": True,
}


def _build_brain() -> tuple[Brain, list[dict[str, Any]], ModuleHub]:
    """按用户真实配置建 hub（独立 config 副本），返回 brain、发送时间线与 hub。"""
    TEST_CFG_DIR.mkdir(parents=True, exist_ok=True)
    for name in _SWITCHES:
        shutil.copyfile(
            ROOT / "configs" / "modules" / f"{name}.json",
            TEST_CFG_DIR / f"{name}.json",
        )

    if MOCK:
        # 假模型模式下把节奏压到毫秒级，方便快速跑完整链路
        (TEST_CFG_DIR / "ai_typing_delay.json").write_text(
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

    timeline: list[dict[str, Any]] = []

    def notifier(text: str, meta: dict[str, Any]) -> None:
        timeline.append(
            {
                "at": datetime.now(),
                "monotonic": time.monotonic(),
                "text": text,
                "kind": meta.get("kind"),
            }
        )

    hub = ModuleHub(
        dict(_SWITCHES),
        config_dir=TEST_CFG_DIR,
        model_caller=mock_post if MOCK else post,
    )
    brain = Brain(hub=hub, notifier=notifier)
    hub.transcript_provider = brain.transcript
    brain.start()
    return brain, timeline, hub


class Recorder:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.skipped: list[str] = []
        self.notes: list[str] = []
        self.started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def step(self, kind: str, **payload: Any) -> None:
        self.steps.append({"t": datetime.now().strftime("%H:%M:%S"), "kind": kind, **payload})

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}")

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        print(f"[SKIP] {name} :: {reason}")

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"[note ] {text}")


def _message_file(group: str) -> Path:
    """本群消息库：``<message_path>/<群>/messages.md``（相对路径按项目根解析）。"""
    raw = str(app_config.get("message_path") or "data/groups")
    root = Path(raw)
    if not root.is_absolute():
        root = ROOT / raw
    return store.message_file(root, group)


def _last_payload() -> list[dict[str, Any]]:
    """取最后一次真正发给模型的 messages（mock 时读假模型调用记录）。"""
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


def _last_injection() -> str:
    """最后一次请求里被注入的 ``[最近对话]`` 片段。"""
    for item in _last_payload():
        content = str(item.get("content") or "")
        if "[最近对话]" in content:
            return content
    return ""


def _segment_delays(cumulative: list[float]) -> list[float]:
    """累计延迟 → 每段自身的等待秒数（``cumulative`` 以"chat 返回时刻"为 0）。"""
    if not cumulative:
        return []
    return [cumulative[0]] + [round(later - earlier, 3) for earlier, later in zip(cumulative, cumulative[1:])]


def _plan(brain: Brain, since: datetime) -> tuple[list[str], list[float]]:
    """队列里的分段文本，以及相对 ``since`` 的累计计划延迟（不清空队列）。"""
    items = brain.outbox.snapshot()
    return [item.text for item in items], [round((item.ready_at - since).total_seconds(), 3) for item in items]


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    mode = "mock（假模型）" if MOCK else "真实 API"
    print(f"=== persona 群聊测试 | {mode} | 日志目录 {SESSION_DIR} ===")
    rec = Recorder()
    brain, timeline, hub = _build_brain()
    persona_text = (ROOT / "prompts" / "persona.md").read_text(encoding="utf-8-sig").strip()
    msg_file = _message_file(GROUP)

    rec.step("env", group=GROUP, ai_name=AI_NAME, mode=mode, modules=sorted(_SWITCHES))
    print(f"persona 长度: {len(persona_text)} | 消息库: {msg_file}")

    rec.check("persona 文件非空且含人格名", bool(persona_text) and AI_NAME in persona_text, f"len={len(persona_text)}")
    for name in _SWITCHES:
        rec.check(f"{name} 模块已启用", hub.get_module(name) is not None, "")

    # -- 1. 休眠轮：没 @ 也没叫名字 ----------------------------------------
    dormant_lines = [
        ("张天宇", "这周的影像诊断作业谁写了？我还没动"),
        ("许斌", "新出的那个二次元手游你们玩了吗"),
        ("汪敬杨", "明天早八的课记得带学生证"),
    ]
    before = len(store.read_messages(msg_file))
    for speaker, text in dormant_lines:
        reply = brain.chat(f"{speaker}：{text}", speaker=speaker, group=GROUP, mentioned=False)
        rec.step("dormant", speaker=speaker, text=text, reply=reply)
        print(f"[休眠] {speaker}: {text} -> {'（未回复）' if not reply else reply[:20]}")
        rec.check(f"群聊未 @ AI 不回复（{speaker}）", reply == "", f"reply={reply[:20]!r}")

    after = len(store.read_messages(msg_file))
    rec.check(
        "休眠轮同样被记录（wake 不回复不影响落盘）",
        after - before == len(dormant_lines),
        f"{before} -> {after}",
    )

    # -- 2. 同句式对照：三人各问一次（第 1 轮真等延迟，其余只读队列） ------
    # 发言人写在消息前缀里：没有模块把 ctx.speaker 渲染进 prompt，模型只能从文本看出谁在说。
    requests = [
        ("张天宇", f"{AI_NAME}，周末有空吗？帮我把实验报告的数据整理一下吧"),
        ("许斌", f"{AI_NAME}，周末有空吗？帮我看看这个新番值不值得追"),
        ("汪敬杨", f"{AI_NAME}，周末有空吗？一起去图书馆复习影像诊断？"),
    ]
    replies: dict[str, str] = {}
    queue_stats: list[dict[str, Any]] = []
    for index, (speaker, text) in enumerate(requests):
        line = f"{speaker}：{text}"
        reply = brain.chat(line, speaker=speaker, group=GROUP, mentioned=True)
        returned_at = datetime.now()
        returned_mono = time.monotonic()
        replies[speaker] = reply

        queued, planned = _plan(brain, returned_at)
        queue_stats.append({"speaker": speaker, "queued": queued, "planned": planned})

        print(f"\n[对话] {line}")
        print(f"[回复] {reply}")
        print(f"[分段] {len(queued)} 段 | 计划延迟 {planned}")

        if index == 0 and queued:
            sent = brain.flush_outbox(timeout=planned[-1] + 60)
            actual = [round(item["monotonic"] - returned_mono, 3) for item in timeline]
            rec.step(
                "flush",
                speaker=speaker,
                queued=queued,
                planned=planned,
                delivered=[item["text"] for item in timeline],
                actual=actual,
                sent=len(sent),
            )
            print(f"[发送] {len(timeline)} 段, 实际延迟 {actual}")
        else:
            brain.outbox.clear()

        rec.step("chat", speaker=speaker, text=line, reply=reply, queued=queued, planned=planned)

    for speaker, _ in requests:
        rec.check(f"{speaker} 获得回复", bool(replies.get(speaker, "").strip()),
                  f"len={len(replies.get(speaker, ''))}")

    texts = {name: replies.get(name, "") for name, _ in requests}
    rec.check("三人回复内容不同（非模板化）", len(set(texts.values())) == 3,
              f"长度={ {k: len(v) for k, v in texts.items()} }")
    rec.check("回复被 reply_split 拆成多条", all(len(item["queued"]) >= 1 for item in queue_stats),
              f"段数={[len(item['queued']) for item in queue_stats]}")

    if MOCK:
        rec.skip("对张天宇偏冷淡/推脱", "假模型不模拟人格")
        rec.skip("对汪敬杨偏配合", "假模型不模拟人格")
    else:
        zhang = texts.get("张天宇", "")
        wang = texts.get("汪敬杨", "")
        rec.check(
            "对张天宇偏冷淡/推脱",
            any(word in zhang for word in ("不一定", "不太", "不情愿", "没空", "不想", "算了", "挑时候", "作业")),
            zhang,
        )
        rec.check("对汪敬杨偏配合", any(word in wang for word in ("有空", "可以", "行", "好")), wang)

    # -- 3. 直接评价三人（多一轮，观察人格是否稳定） -----------------------
    evaluate = brain.chat(
        f"许斌：{AI_NAME}，你怎么看张天宇、许斌、汪敬杨这三个人？分别说说",
        speaker="许斌",
        group=GROUP,
        mentioned=True,
    )
    brain.outbox.clear()
    rec.step("chat", speaker="许斌", text="评价三人", reply=evaluate)
    print(f"\n[评价三人] {evaluate}")

    # -- 4. recent_memory：写了没有、注入了没有 ---------------------------
    rows = store.read_messages(msg_file)
    block = _last_injection()
    rec.step("snapshot", rows=len(rows), block=block)

    speakers = {str(row.get("speaker") or "") for row in rows}
    rec.check("消息库已记录三位成员", {"张天宇", "许斌", "汪敬杨"}.issubset(speakers), f"speakers={sorted(speakers)}")
    rec.check("消息库含 AI 自己的回复", "我" in speakers, f"speakers={sorted(speakers)}")
    rec.check("注入块含 [最近对话]", "[最近对话]" in block, f"block={block[:120]!r}")
    rec.check(
        "注入块含休眠轮的发言",
        "这周的影像诊断作业谁写了" in block,
        "休眠消息也应进入窗口",
    )
    rec.check("注入块只作背景（有约束提示）", "仅作背景" in block, "")

    # -- 5. 发送顺序与延迟实测 ---------------------------------------------
    flush = next((step for step in rec.steps if step.get("kind") == "flush"), None)
    if flush is None:
        rec.check("出站队列已真实发出", False, "本轮没有 flush 样本")
    else:
        rec.check("发送段数 == 入队段数", flush["sent"] == len(flush["queued"]),
                  f"{flush['sent']}/{len(flush['queued'])}")
        rec.check("发送顺序与入队顺序一致", flush["delivered"] == flush["queued"], "")
        pairs = list(zip(flush["planned"], flush["actual"]))
        deltas = [round(abs(plan - actual), 3) for plan, actual in pairs]
        rec.check("实际等待贴合计划延迟（±0.25s）", bool(pairs) and all(delta <= 0.25 for delta in deltas),
                  f"planned={flush['planned']}, actual={flush['actual']}, 偏差={deltas}")

    # -- 6. ai_typing_delay 参数与纯函数 -----------------------------------
    params = hub.get_params("ai_typing_delay")
    base_low, base_high = (params.get("msg_delay_base_range") or [5, 10])[:2]
    limit = float(params.get("msg_delay_max") or 30)
    samples = [typing_delay.delay_for("好", params) for _ in range(5)]

    rec.check("延迟下限符合配置", all(value >= float(base_low) for value in samples),
              f"samples={samples}, base_range=[{base_low},{base_high}]")
    rec.check("延迟随文本长度增长", typing_delay.delay_for("嗯", params) < typing_delay.delay_for("嗯" * 20, params),
              f"短={typing_delay.delay_for('嗯', params)}, 长={typing_delay.delay_for('嗯' * 20, params)}")
    rec.check("超长文本被 msg_delay_max 封顶", typing_delay.delay_for("字" * 2000, params) == limit, f"max={limit}")
    rec.check("标点与空白不计入字数", typing_delay.count_chars("你好，世界！ ") == 4,
              f"count={typing_delay.count_chars('你好，世界！ ')}")
    rec.check("每条计划延迟不超过 msg_delay_max",
              all(0 < value <= limit for item in queue_stats for value in _segment_delays(item["planned"])),
              f"limit={limit}, 每段={[_segment_delays(item['planned']) for item in queue_stats]}")
    rec.note("仅第 1 轮真等（flush_outbox），其余轮只读队列，避免跑几分钟。")

    # -- 输出报告 ---------------------------------------------------------
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = {
        "scenario": "persona_group",
        "mode": "mock" if MOCK else "real",
        "started": rec.started,
        "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "log_dir": str(SESSION_DIR),
        "group": GROUP,
        "config_dir": str(TEST_CFG_DIR),
        "persona": persona_text,
        "message_file": str(msg_file),
        "steps": rec.steps,
        "checks": rec.checks,
        "skipped": rec.skipped,
        "notes": rec.notes,
        "typing_delay_params": params,
    }
    target = REPORT_DIR / f"persona_group-{SESSION_DIR.name}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    passed = sum(1 for item in rec.checks if item["ok"])
    print(f"\n=== 完成: {passed}/{len(rec.checks)} 项通过 | 跳过 {len(rec.skipped)} 项 | 报告 {target} ===")


if __name__ == "__main__":
    main()
