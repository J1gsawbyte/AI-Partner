"""长期记忆（long_memory）真实 API 验收：建库 → 召回 → 强化 → 遗忘 → 可关闭性。

用法::

    python scripts/long_memory_test.py

口径
----
* 启用模块：persona / chat_style / wake / recent_memory / long_memory / reply_split；
  **ai_typing_delay 显式关闭**（本次只测记忆，不测节奏）；
* 使用用户真实 key（``configs/config.json``），测试群 ``长期记忆测试``；
  产物（群数据 / 日志 / 报告）全部保留，便于复查；
* 运行态配置复制到 ``configs/test_long_memory/``，不污染用户的 ``configs/modules/``；
* 建库阶段把 ``summarize_rounds`` 压到 4，让一次运行就能触发总结；进入召回阶段后
  把它调大并清零计数，避免"提问"被当成新事实重复总结（测试口径，不影响线上默认值）；
* 遗忘阶段直接 ``hub.tick(now + 150 天)`` 模拟时间流逝，不真等。
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime, timedelta
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

_SRC: Final[Path] = ROOT / "src"
for _dir in (
    _SRC / "ai" / "brain",
    _SRC / "logger",
    _SRC / "json",
    _SRC / "ai" / "module" / "module_hub",
    _SRC / "ai" / "module" / "lib",
    _SRC / "ai" / "module" / "modules",
    _SRC / "ai" / "post",
    _SRC / "ai" / "prompt",
):
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

from brain import Brain  # noqa: E402
from jsonReader import config as app_config  # noqa: E402
from logger import SESSION_DIR  # noqa: E402
from module_hub import ModuleHub  # noqa: E402
from post import post  # noqa: E402
import memory_store as mem  # noqa: E402
import message_store as store  # noqa: E402

AI_NAME: Final[str] = "宋文秀"


def _group_arg() -> str:
    """``--group 名字`` 指定测试群（默认 ``长期记忆测试``），便于复测时开新群。"""
    args = sys.argv[1:]
    if "--group" in args:
        index = args.index("--group")
        if index + 1 < len(args) and args[index + 1].strip():
            return args[index + 1].strip()
    return "长期记忆测试"


GROUP: Final[str] = _group_arg()
TEST_CFG_DIR: Final[Path] = ROOT / "configs" / "test_long_memory"
REPORT_DIR: Final[Path] = ROOT / "data" / "test_reports"

#: 本次启用的模块；ai_typing_delay 故意不列（= 关闭）
_SWITCHES: Final[dict[str, bool]] = {
    "persona": True,
    "chat_style": True,
    "wake": True,
    "recent_memory": True,
    "long_memory": True,
    "reply_split": True,
}

#: 建库阶段的总结门槛（线上默认 10，这里为成本与时长压到 4）
_BUILD_ROUNDS: Final[int] = 4


# -- 环境 ------------------------------------------------------------------ #


def _message_file(group: str) -> Path:
    raw = str(app_config.get("message_path") or "data")
    root = Path(raw)
    if not root.is_absolute():
        root = ROOT / raw
    return store.message_file(root, group)


def _memory_file(group: str) -> Path:
    return _message_file(group).with_name("memory.md")


def _prepare_config_dir() -> None:
    """把线上模块配置复制到独立目录，并只改本次测试需要的两项。"""
    TEST_CFG_DIR.mkdir(parents=True, exist_ok=True)
    for name in _SWITCHES:
        shutil.copyfile(
            ROOT / "configs" / "modules" / f"{name}.json",
            TEST_CFG_DIR / f"{name}.json",
        )

    path = TEST_CFG_DIR / "long_memory.json"
    raw = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    raw["config"]["summarize_rounds"] = _BUILD_ROUNDS
    raw["config"]["group"] = ""
    raw.setdefault("state", {})
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_brain(switches: dict[str, bool] | None = None) -> tuple[Brain, ModuleHub]:
    hub = ModuleHub(
        dict(switches if switches is not None else _SWITCHES),
        config_dir=TEST_CFG_DIR,
        model_caller=post,
    )
    brain = Brain(hub=hub)
    hub.transcript_provider = brain.transcript
    brain.start()
    return brain, hub


# -- 观测 ------------------------------------------------------------------ #


def _last_payload() -> list[dict[str, Any]]:
    """取最后一次真正发给模型的 messages（transport.log 里最后一条"发送"记录）。"""
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


def _fragment(payload: list[dict[str, Any]], tag: str) -> str:
    for item in payload:
        content = str(item.get("content") or "")
        if tag in content:
            return content
    return ""


def _injected(payload: list[dict[str, Any]]) -> list[dict[str, str]]:
    """从 ``[长期记忆]`` 片段里取出被注入的每一条（key + 整行文本）。"""
    block = _fragment(payload, "[长期记忆]")
    items: list[dict[str, str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        body = line[2:].strip()
        items.append({"key": body.split("：", 1)[0].strip(), "line": body})
    return items


def _dump_memory(path: Path) -> list[dict[str, Any]]:
    return mem.load_memory(path)


def _request_count() -> int:
    path = SESSION_DIR / "transport.log"
    if not path.is_file():
        return 0
    return sum(
        1
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if "发送的原始信息: " in line
    )


class Recorder:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.chats: list[dict[str, Any]] = []
        self.recalls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' :: ' + detail) if detail else ''}", flush=True)
        return bool(ok)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"[note ] {text}", flush=True)


def _say(brain: Brain, rec: Recorder, speaker: str, text: str) -> tuple[str, list[dict[str, Any]]]:
    """以成员身份发一句并取回复；返回 (回复, 本轮发出去的 payload)。"""
    line = f"{speaker}：{text}"
    reply = brain.chat(line, speaker=speaker, group=GROUP, mentioned=True)
    payload = _last_payload()
    rec.chats.append(
        {
            "speaker": speaker,
            "text": text,
            "reply": reply,
            "injected": _injected(payload),
            "recent": "[最近对话]" in _fragment(payload, "[最近对话]"),
        }
    )
    print(f"\n[{speaker}] {text}\n[回复] {reply}", flush=True)
    return reply, payload


# -- 测试步骤 -------------------------------------------------------------- #


_BUILD_LINES: Final[tuple[tuple[str, str], ...]] = (
    ("张天宇", "我生日是 3 月 14 日，到时候记得跟我说句生日快乐"),
    ("许斌", "我最近天天在打明日方舟，二次元手游是真上头"),
    ("汪敬杨", "我每天早上必须喝一杯美式咖啡，不然第一节课撑不住"),
    ("张天宇", "我最讨厌吃香菜，闻到那个味儿就难受"),
    ("汪敬杨", "周六一起去图书馆复习影像诊断吧，我提前占两个位置"),
    ("许斌", "我暑假去了趟青海湖，那边的风景是真好看"),
    ("张天宇", "今天好热啊，食堂空调又坏了，饭都没胃口吃"),
    ("汪敬杨", "期末考试我目标是 90 分以上，这学期不能掉绩点"),
)

#: (提问人, 问题, 期望命中的 key 关键字, 类型)
#: 类型：``hit`` = 本地匹配应命中；``miss`` = 不应注入；``probe`` = 观察项（只记录，不计分）
_RECALL_CASES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("汪敬杨", f"{AI_NAME}，你还记得我爱喝什么咖啡吗", "咖啡", "hit"),
    ("张天宇", f"{AI_NAME}，我生日是哪天来着", "生日", "hit"),
    ("张天宇", f"{AI_NAME}，我最不喜欢吃什么来着", "香菜", "hit"),
    ("汪敬杨", f"{AI_NAME}，周六我们说好去哪儿来着", "图书馆", "hit"),
    ("张天宇", f"{AI_NAME}，食堂今晚有糖醋排骨吗，想去看看", "", "miss"),
    ("许斌", f"{AI_NAME}，我最近玩的那个游戏你还记得吗", "游戏", "probe"),
    ("许斌", f"{AI_NAME}，我暑假去哪儿玩了你还记得不", "青海湖", "probe"),
)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    rec = Recorder()
    started = time.monotonic()
    _prepare_config_dir()
    brain, hub = _build_brain()
    msg_path = _message_file(GROUP)
    mem_path = _memory_file(GROUP)

    print(f"=== long_memory 验收 | 群 {GROUP} | 日志 {SESSION_DIR} ===", flush=True)
    rec.check("ai_typing_delay 已关闭", hub.get_module("ai_typing_delay") is None, "本次不测打字节奏")
    rec.check(
        "persona / recent_memory / long_memory 已启用",
        all(hub.get_module(name) is not None for name in ("persona", "recent_memory", "long_memory")),
        f"modules={[h.name for h in hub._handles]}",
    )

    # -- 1. 建库 ---------------------------------------------------------- #
    print("\n--- 1. 建库（每轮后 hub.tick，轮数 4 触发总结） ---", flush=True)
    for speaker, text in _BUILD_LINES:
        _say(brain, rec, speaker, text)
        hub.tick()

    rows = store.read_messages(msg_path)
    records = _dump_memory(mem_path)
    rec.check("messages.md 已记录本次对话", len(rows) == len(_BUILD_LINES) * 2, f"rows={len(rows)}")
    rec.check("memory.md 已生成", mem_path.is_file(), str(mem_path))
    rec.check("记忆库非空", bool(records), f"records={len(records)}")
    rec.check(
        "key 全部为三段式",
        bool(records) and all(mem.parse_key(item["key"]) is not None for item in records),
        f"keys={[item['key'] for item in records]}",
    )
    keys = [item["key"] for item in records]
    texts = [f"{item['key']} {item.get('summary', '')}" for item in records]
    rec.check("提到过的生日被记住", any("生日" in text for text in texts), f"keys={keys}")
    rec.check("提到过的咖啡被记住", any("咖啡" in text for text in texts), f"keys={keys}")
    rec.check("无价值闲聊不应成条", not any("热" in key or "空调" in key for key in keys), f"keys={keys}")
    duplicates = sum(
        1
        for index, item in enumerate(records)
        for other in records[index + 1 :]
        if mem.find_similar([other], str(item["key"])) is not None
    )
    rec.check("同一事实没有拆成多条近义 key", duplicates == 0, f"重复对={duplicates} keys={keys}")
    state_after_build = json.loads(json.dumps(hub.get_state("long_memory"), ensure_ascii=False))
    rec.check(
        "游标已推进、计数清零",
        state_after_build["groups"][GROUP]["pending_rounds"] == 0
        and state_after_build["groups"][GROUP]["cursor"] == len(rows),
        json.dumps(state_after_build["groups"][GROUP], ensure_ascii=False),
    )

    # 召回阶段：关掉"随手总结"，避免提问被当事实
    params = hub.get_params("long_memory")
    params["summarize_rounds"] = 10_000
    hub.get_state("long_memory")["groups"][GROUP]["pending_rounds"] = 0

    # -- 2. 召回（纯本地匹配，零额外请求） --------------------------------- #
    print("\n--- 2. 召回 ---", flush=True)
    before_calls = _request_count()
    for speaker, question, expect, kind in _RECALL_CASES:
        _say(brain, rec, speaker, question)
        injected = rec.chats[-1]["injected"]
        if kind == "hit":
            ok: bool | None = any(expect in item["line"] for item in injected)
        elif kind == "miss":
            ok = not injected
        else:
            ok = None  # 观察项：本地匹配本来就可能漏召，只记录不判分
        rec.recalls.append(
            {
                "speaker": speaker,
                "question": question,
                "expect": expect,
                "kind": kind,
                "injected": injected,
                "ok": ok,
                "reply": rec.chats[-1]["reply"],
            }
        )
    extra_calls = _request_count() - before_calls
    rec.check(
        "召回阶段请求数 == 提问数（没有额外提炼请求）",
        extra_calls == len(_RECALL_CASES),
        f"{extra_calls} 次请求 / {len(_RECALL_CASES)} 个问题",
    )
    scored = [case for case in rec.recalls if case["ok"] is not None]
    hit_count = sum(1 for case in scored if case["ok"])
    rec.check(
        "召回准确率（应命中 / 不应注入）",
        hit_count == len(scored),
        f"{hit_count}/{len(scored)} 符合预期；观察项见报告",
    )
    if hit_count != len(scored):
        missed = [
            f"{case['question']} → {[item['key'] for item in case['injected']]}"
            for case in scored
            if not case["ok"]
        ]
        rec.note("未符合预期的召回：" + "；".join(missed))
    for case in rec.recalls:
        if case["kind"] == "probe":
            injected_keys = [item["key"] for item in case["injected"]]
            rec.note(
                f"观察项（不计分）：{case['question']} → 注入 {injected_keys or '（无）'}"
                "（问法里没有 key 的字面词，命中与否取决于 summary 用词）"
            )

    # -- 3. 强化 ---------------------------------------------------------- #
    print("\n--- 3. 强化（再次提及） ---", flush=True)
    before = {item["key"]: dict(item) for item in _dump_memory(mem_path)}
    _say(brain, rec, "汪敬杨", f"{AI_NAME}，我早上又说了一遍，我爱喝什么咖啡？")
    after = {item["key"]: dict(item) for item in _dump_memory(mem_path)}
    # key 第三段可能叫「早上喝美式」，所以 key 和 summary 都要看
    key = next(
        (
            name
            for name, item in before.items()
            if "咖啡" in name or "咖啡" in str(item.get("summary") or "")
        ),
        "",
    )
    if key:
        grew = (
            int(after[key]["recalls"]) > int(before[key]["recalls"])
            and float(after[key]["stability_h"]) > float(before[key]["stability_h"])
        )
        rec.check(
            "命中后 recalls / stability 增长",
            grew,
            f"{key}: recalls {before[key]['recalls']}→{after[key]['recalls']}, "
            f"stability {before[key]['stability_h']}→{after[key]['stability_h']}",
        )
        rec.notes.append(
            f"{key} 强化：recalls {before[key]['recalls']}→{after[key]['recalls']}，"
            f"stability {before[key]['stability_h']}→{after[key]['stability_h']}"
        )
    else:
        rec.check("命中后 recalls / stability 增长", False, "记忆库里没有咖啡相关 key")

    # -- 4. 遗忘（时间跳跃 150 天） ---------------------------------------- #
    print("\n--- 4. 遗忘（模拟 +150 天） ---", flush=True)
    # 隔离遗忘扫描：清零待总结计数并关掉时间触发，避免同一个 tick 里"刚遗忘又被总结复活"
    hub.get_state("long_memory")["groups"][GROUP]["pending_rounds"] = 0
    params["summarize_interval_minutes"] = 0
    before_calls = _request_count()
    future = datetime.now() + timedelta(days=150)
    hub.tick(future)
    rec.check(
        "遗忘扫描不产生模型请求",
        _request_count() == before_calls,
        f"{before_calls} -> {_request_count()}",
    )
    records = _dump_memory(mem_path)
    statuses = {item["key"]: (item["status"], float(item["importance"])) for item in records}
    forgotten = [key for key, (status, _) in statuses.items() if status == mem.STATUS_FORGOTTEN]
    kept = [key for key, (status, _) in statuses.items() if status == mem.STATUS_ACTIVE]
    coffee_key = next((key for key in statuses if "咖啡" in key), "")

    rec.check(
        "低重要度记忆被标记 forgotten",
        bool(forgotten) and (not coffee_key or statuses[coffee_key][0] == mem.STATUS_FORGOTTEN),
        f"forgotten={forgotten}",
    )
    rec.check(
        "高重要度记忆免疫遗忘",
        all(
            status == mem.STATUS_ACTIVE
            for key, (status, importance) in statuses.items()
            if importance >= 0.8
        ),
        f"active={kept}",
    )
    rec.check("遗忘记录仍留在记忆库里", len(records) >= len(forgotten), f"records={len(records)}")

    after_jump: list[dict[str, Any]] = []
    for speaker, question, expect, _kind in (_RECALL_CASES[0], _RECALL_CASES[1]):
        _say(brain, rec, speaker, question)
        injected = rec.chats[-1]["injected"]
        case = {
            "speaker": speaker,
            "question": question,
            "expect": expect,
            "kind": "after_forget",
            "injected": injected,
            "ok": None,
            "time_jump": True,
            "reply": rec.chats[-1]["reply"],
        }
        after_jump.append(case)
        rec.recalls.append(case)
        print(f"[遗忘后] {question} -> 注入 {[item['key'] for item in injected] or '（无）'}", flush=True)

    rec.check(
        "已遗忘的记忆不再被检索",
        not (set(forgotten) & {item["key"] for item in after_jump[0]["injected"]}),
        f"遗忘={forgotten} 注入={[item['key'] for item in after_jump[0]['injected']]}",
    )
    birthday_active = any(
        "生日" in key and status == mem.STATUS_ACTIVE for key, (status, _) in statuses.items()
    )
    if birthday_active:
        rec.check(
            "免疫记忆仍可检索",
            any("生日" in item["key"] for item in after_jump[1]["injected"]),
            f"注入={[item['key'] for item in after_jump[1]['injected']]}",
        )
    else:
        rec.note("生日记录重要度未达免疫门槛（<0.8），本次未验证免疫检索")

    # -- 5. 可关闭性 ------------------------------------------------------ #
    print("\n--- 5. 关闭 long_memory ---", flush=True)
    off_switches = {name: True for name in _SWITCHES if name != "long_memory"}
    brain_off, hub_off = _build_brain(off_switches)
    reply = brain_off.chat(
        f"张天宇：{AI_NAME}，我生日是哪天来着", speaker="张天宇", group=GROUP, mentioned=True
    )
    payload = _last_payload()
    rec.check("关掉 long_memory 后仍能正常回复", bool(reply.strip()), reply[:40])
    rec.check("关掉后不再注入长期记忆", "[长期记忆]" not in str(payload), "")
    rec.check("关掉模块后 hub 里没有该模块", hub_off.get_module("long_memory") is None, "")

    # -- 报告 ------------------------------------------------------------- #
    elapsed = round(time.monotonic() - started, 1)
    payload_data = {
        "scenario": "long_memory",
        "mode": "real",
        "started": rec.started,
        "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": elapsed,
        "requests": _request_count(),
        "group": GROUP,
        "log_dir": str(SESSION_DIR),
        "config_dir": str(TEST_CFG_DIR),
        "message_file": str(msg_path),
        "memory_file": str(mem_path),
        "switches": _SWITCHES,
        "build_rounds": _BUILD_ROUNDS,
        "build_chats": rec.chats[: len(_BUILD_LINES)],
        "recalls": rec.recalls,
        "memory_records": _dump_memory(mem_path),
        "checks": rec.checks,
        "notes": rec.notes,
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = SESSION_DIR.name
    (REPORT_DIR / f"long_memory-{stamp}.json").write_text(
        json.dumps(payload_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = REPORT_DIR / f"long_memory-{stamp}.md"
    report_path.write_text(_markdown(payload_data), encoding="utf-8")

    passed = sum(1 for item in rec.checks if item["ok"])
    print(
        f"\n=== 完成: {passed}/{len(rec.checks)} 项通过 | 请求 {payload_data['requests']} 次 | "
        f"耗时 {elapsed}s | 报告 {report_path} ===",
        flush=True,
    )


def _markdown(data: dict[str, Any]) -> str:
    """把运行数据渲染成可读报告（表格 + 结论）。"""
    checks = data["checks"]
    passed = sum(1 for item in checks if item["ok"])
    rows = data["memory_records"]

    lines: list[str] = []
    add = lines.append
    add("# 长期记忆（long_memory）测试报告")
    add("")
    add(f"- 测试群：`{data['group']}`")
    add(f"- 开始 / 结束：{data['started']} / {data['finished']}（耗时 {data['elapsed_seconds']}s）")
    add(f"- 真实模型请求：{data['requests']} 次")
    add(f"- 日志目录：`{data['log_dir']}`")
    add(f"- 测试配置目录：`{data['config_dir']}`（不污染 `configs/modules/`）")
    add(f"- 消息库：`{data['message_file']}`")
    add(f"- 记忆库：`{data['memory_file']}`")
    add(f"- 模块开关：`{json.dumps(data['switches'], ensure_ascii=False)}`（ai_typing_delay 关闭）")
    add(f"- 结论：**{passed}/{len(checks)} 项通过**")
    add("")

    add("## 1. 建库")
    add("")
    add("| 发言人 | 内容 | AI 回复（节选） |")
    add("| --- | --- | --- |")
    for chat in data["build_chats"]:
        reply = str(chat["reply"]).replace("\n", " ")[:60]
        add(f"| {chat['speaker']} | {chat['text']} | {reply} |")
    add("")
    add(f"建库阶段总结门槛：每 {data['build_rounds']} 轮触发一次（线上默认 10）。")
    add("")
    add("### 记忆库快照（最终状态，含 +150 天遗忘结果）")
    add("")
    add("| key | summary | importance | recalls | stability_h | status |")
    add("| --- | --- | --- | --- | --- | --- |")
    for item in sorted(rows, key=lambda rec: str(rec.get("key"))):
        add(
            f"| `{item.get('key')}` | {item.get('summary')} | {item.get('importance')} | "
            f"{item.get('recalls')} | {item.get('stability_h')} | {item.get('status')} |"
        )
    add("")

    add("## 2. 召回（本地匹配，零额外请求）")
    add("")
    add("| 提问人 | 问题 | 期望 | 实际注入 | 结果 |")
    add("| --- | --- | --- | --- | --- |")
    for case in data["recalls"]:
        injected = ", ".join(f"`{item['key']}`" for item in case["injected"]) or "（无）"
        expect = case["expect"] or "不注入"
        if case["ok"] is None:
            mark = "观察" if case.get("kind") == "probe" else "—"
        else:
            mark = "✅" if case["ok"] else "❌"
        suffix = "（+150 天后）" if case.get("time_jump") else ""
        add(f"| {case['speaker']} | {case['question']}{suffix} | {expect} | {injected} | {mark} |")
    add("")
    add("### 回答节选（看是否真的用上了记忆）")
    add("")
    for case in data["recalls"]:
        reply = str(case["reply"]).replace("\n", " ")[:120]
        add(f"- **{case['question']}** → {reply}")
    add("")

    add("## 3. 检查项")
    add("")
    add("| 检查 | 结果 | 证据 |")
    add("| --- | --- | --- |")
    for item in checks:
        detail = str(item["detail"]).replace("|", "\\|")[:160]
        add(f"| {item['name']} | {'PASS' if item['ok'] else 'FAIL'} | {detail} |")
    add("")

    add("## 4. 观察与结论")
    add("")
    for note in data["notes"]:
        add(f"- {note}")
    add("")
    add("- 召回完全走本地匹配（实体名 + 属性别名 + 次级属性双字），提问轮没有额外模型请求。")
    add("- 记忆库 `memory.md` 一条一行、按 key 合并；`forgotten` 记录保留在文件里，只是不再进检索。")
    add("- 本次产物（群数据 / 日志 / 本报告）按要求保留，未删除。")
    add("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
