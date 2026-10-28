"""真实 API 端到端测试：proactive / delay / atom 三项功能。

不使用任何 mock：模型走真实 ``post``，配置读 ``configs/``，数据写 ``data/``。
测试产生的数据（messages.md / memory.md / members.json / memory_state.json /
运行日志 / 报告）全部保留，脚本只追加、不删除。

用法::

    python scripts/real_e2e_test.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, time as dtime
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
from jsonReader import config as app_config  # noqa: E402
from jsonReader import modules as module_switches  # noqa: E402
from module_hub import ModuleHub  # noqa: E402
from post import post  # noqa: E402
import memory_core as core  # noqa: E402

GROUP: Final[str] = "测试专用"
SPEAKER: Final[str] = "林舟"
LOG_PATH: Final[Path] = _ROOT / "data" / "e2e_run.log"

ROUNDS: Final[tuple[str, ...]] = (
    "在吗？我是林舟，最近有点忙",
    "我在杭州做后端，主要写 Python",
    "我养了只猫叫豆豆，三岁了",
    "我平时爱喝冰美式，周末喜欢爬山",
    "这周项目刚上线，连着加了几天班",
    "豆豆最近总半夜跑酷，睡不好",
    "下周末打算去爬北高峰",
    "你觉得我该换个降噪耳机吗",
    "我同事叫阿伟，也爱爬山",
    "对了，我下个月要去成都出差",
)

#: 发送时间线：一条 = 一次 notifier 调用
_timeline: list[dict[str, Any]] = []
#: 断言结果：(名称, 是否通过, 细节)
_checks: list[tuple[str, bool, str]] = []


def log(message: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def check(name: str, ok: bool, detail: str = "") -> None:
    _checks.append((name, bool(ok), detail))
    log(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))


def notifier(text: str, meta: dict[str, Any]) -> None:
    _timeline.append(
        {
            "at": datetime.now(),
            "monotonic": time.monotonic(),
            "text": text,
            "kind": meta.get("kind"),
            "rule_id": meta.get("rule_id"),
        }
    )
    tag = "主动" if meta.get("kind") == "proactive" else "回复"
    log(f"发送[{tag}] {text}")


def build() -> Brain:
    holder: dict[str, Brain] = {}

    def transcript(limit: int) -> list[dict[str, Any]]:
        brain = holder.get("brain")
        return brain.transcript(limit) if brain else []

    hub = ModuleHub(
        dict(module_switches.data),
        model_caller=post,
        transcript_provider=transcript,
    )
    brain = Brain(hub=hub, notifier=notifier)
    holder["brain"] = brain
    return brain


def _in_span(now: datetime, span: list[str]) -> bool:
    start = dtime(*[int(part) for part in span[0].split(":")])
    end = dtime(*[int(part) for part in span[1].split(":")])
    current = now.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def _message_path(group: str) -> Path:
    """本群消息库：``<message_path>/<群>/messages.md``（相对路径按项目根解析）。"""
    raw = str(app_config.get("message_path") or "data/groups")
    root = Path(raw)
    if not root.is_absolute():
        root = _ROOT / root
    return root / group / "messages.md"


def _group_files(group_dir: Path, message_path: Path) -> dict[str, Path]:
    return {
        "messages": message_path,
        "memory": group_dir / "memory.md",
        "members": group_dir / "members.json",
        "state": group_dir / "memory_state.json",
        "dead": group_dir / "dead_letters.jsonl",
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _tmp_leftovers(*dirs: Path) -> list[str]:
    return sorted(
        f"{directory.name}/{path.name}"
        for directory in dirs
        for path in directory.glob("*.tmp")
    )


def main() -> int:
    started_at = datetime.now()
    log("=" * 60)
    log(f"真实端到端测试开始 | 模型={app_config.get('model')} | 群={GROUP}")

    brain = build()
    brain.start()
    memory_params = brain.hub.get_params("atom_memory")
    data_root = _ROOT / str(memory_params.get("data_root") or "data")
    group_dir = data_root / "groups" / GROUP
    message_path = _message_path(GROUP)
    files = _group_files(group_dir, message_path)
    log(f"数据目录: {group_dir}")
    log(f"消息库: {message_path}")
    log(f"数据文件: {', '.join(f'{key}={path}' for key, path in files.items())}")

    proactive_params = brain.hub.get_params("proactive")
    rules = [rule for rule in proactive_params.get("rules") or [] if isinstance(rule, dict)]
    for rule in rules:
        span = rule.get("window")
        log(
            f"规则 {rule.get('id')} 窗口={span} "
            f"命中={_in_span(datetime.now(), span) if isinstance(span, list) and len(span) == 2 else '?'}"
        )
    log(
        f"proactive 参数: quiet_hours={proactive_params.get('quiet_hours')} "
        f"max_per_hour={proactive_params.get('max_per_hour')}"
    )

    # -- 阶段 1：delay --------------------------------------------------- #
    log("-" * 60)
    log("阶段 1/3：delay（分句 + 打字延迟）")
    first = ROUNDS[0]
    round_started = time.monotonic()
    reply = brain.chat(first, speaker=SPEAKER, group=GROUP, mentioned=True)
    log(f"用户: {first}")
    log(f"模型回包({len(reply)} 字): {reply}")

    queued = brain.outbox.snapshot()
    planned = [
        round((item.ready_at - queued[0].ready_at).total_seconds(), 2) for item in queued
    ] if queued else []
    check("回复按模块分句后进入出站队列", bool(queued), f"入队 {len(queued)} 段")
    log(f"队列计划: {[(item.text, offset) for item, offset in zip(queued, planned)]}")

    sent = brain.flush_outbox(timeout=300)
    actual_gaps = [
        round(_timeline[i]["at"].timestamp() - _timeline[i - 1]["at"].timestamp(), 2)
        for i in range(1, len(_timeline))
    ]
    log(
        f"共发出 {len(sent)} 段, 相邻实际间隔={actual_gaps}, "
        f"本轮耗时={time.monotonic() - round_started:.1f}s"
    )
    check("实际发送段数 == 入队段数", len(sent) == len(queued), f"{len(sent)}/{len(queued)}")
    check(
        "发送顺序与入队顺序一致",
        [item["text"] for item in _timeline] == [item.text for item in queued],
        "",
    )
    if len(planned) > 1:
        deltas = [
            round(abs(actual_gaps[i] - (planned[i + 1] - planned[i])), 2)
            for i in range(len(actual_gaps))
        ]
        check("分段间隔贴合计划延迟(±1s)", all(delta <= 1.0 for delta in deltas), f"偏差={deltas}")

    # -- 阶段 2：atom ---------------------------------------------------- #
    log("-" * 60)
    log("阶段 2/3：atom（消息落盘 + 分句发送 + 累计触发总结）")
    messages_path = files["messages"]
    round_stats: list[dict[str, Any]] = []
    for index, text in enumerate(ROUNDS[1:], start=2):
        reply = brain.chat(text, speaker=SPEAKER, group=GROUP, mentioned=True)
        log(f"用户({index}): {text}")
        log(f"模型({index}): {reply}")
        sent = brain.flush_outbox(timeout=300)

        rows = core.read_messages(messages_path)
        expected = index * 2
        check(
            f"第 {index} 轮消息条数精确",
            len(rows) == expected,
            f"{len(rows)}/{expected}",
        )
        check(
            f"第 {index} 轮无残留临时文件",
            not _tmp_leftovers(group_dir, message_path.parent),
            f"{_tmp_leftovers(group_dir, message_path.parent)}",
        )
        round_stats.append({"round": index, "segments": len(sent), "rows": len(rows)})

    rows = core.read_messages(messages_path)
    atom_ctx = dict(brain.hub.get_state("atom_memory"))
    atom_state = _read_json(files["state"])
    atoms = core.load_atoms(files["memory"])
    memory_text = files["memory"].read_text(encoding="utf-8") if files["memory"].is_file() else ""

    log(
        f"消息库条数={len(rows)} | ctx.watermark={atom_ctx.get('watermark')} "
        f"atom_count={atom_ctx.get('atom_count')} stats={atom_ctx.get('stats')}"
    )
    log(
        f"memory_state: watermark={atom_state.get('last_summarized')} "
        f"responded={atom_state.get('last_responded_total')} "
        f"stats={atom_state.get('stats')} failure={atom_state.get('failure')}"
    )
    check(
        "消息库条数 == 2 × 轮数",
        len(rows) == len(ROUNDS) * 2,
        f"{len(rows)}/{len(ROUNDS) * 2}",
    )
    check("memory_state.json 可解析", bool(atom_state), f"{files['state'].name}")
    check(
        "水位不越过消息总数",
        int(atom_state.get("last_summarized") or 0) <= len(rows),
        f"watermark={atom_state.get('last_summarized')} total={len(rows)}",
    )
    check(
        "memory.md 行级可解析且条数一致",
        len(atoms) == len([line for line in memory_text.splitlines() if line.strip()]),
        f"atoms={len(atoms)} lines={len(memory_text.splitlines())}",
    )

    # 维护型钩子：只跑总结/过期，不做主动提名（避免提前消耗 proactive 冷却）
    log("触发一次维护 tick（hub.tick，不含主动提名）")
    brain.hub.tick(datetime.now())
    atom_ctx = dict(brain.hub.get_state("atom_memory"))
    atom_state = _read_json(files["state"])
    atoms_after = core.load_atoms(files["memory"])
    memory_text = files["memory"].read_text(encoding="utf-8") if files["memory"].is_file() else ""
    log(
        f"维护后: watermark={atom_state.get('last_summarized')} "
        f"atoms={len(atoms_after)} stats={atom_state.get('stats')}"
    )
    log(f"memory.md 首行={memory_text.splitlines()[:2]}")
    check(
        "总结已消费积压（水位>0）",
        int(atom_state.get("last_summarized") or 0) > 0,
        f"watermark={atom_state.get('last_summarized')}",
    )
    check(
        "原子已沉淀到 memory.md",
        bool(atoms_after),
        f"atoms={len(atoms_after)}",
    )
    check(
        "原子写入后无临时文件残留",
        not _tmp_leftovers(group_dir, message_path.parent),
        f"{_tmp_leftovers(group_dir, message_path.parent)}",
    )

    # -- 阶段 3：proactive ------------------------------------------------ #
    log("-" * 60)
    log("阶段 3/3：proactive（主动消息）")
    proactive_before = _read_json(_ROOT / "configs" / "modules" / "proactive.json").get("state", {})
    rows_before = len(core.read_messages(messages_path))
    before = len(_timeline)
    tick_started = time.monotonic()
    results = brain.tick(datetime.now())
    brain.flush_outbox(timeout=300)
    after = len(_timeline)
    new_items = _timeline[before:]
    log(f"tick 结果={json.dumps(results, ensure_ascii=False)}")
    log(f"本次新增发送 {after - before} 条, 耗时={time.monotonic() - tick_started:.1f}s")

    proactive_after = _read_json(_ROOT / "configs" / "modules" / "proactive.json").get("state", {})
    log(f"proactive 状态={json.dumps(proactive_after, ensure_ascii=False)}")
    check("主动消息已真实发出", after > before, f"新增 {after - before} 条")
    check(
        "新增消息类型均为 proactive",
        all(item["kind"] == "proactive" for item in new_items) if new_items else False,
        f"{[item['kind'] for item in new_items]}",
    )
    check(
        "主动消息已标记规则",
        bool(new_items) and all(item["rule_id"] for item in new_items),
        f"{[item['rule_id'] for item in new_items]}",
    )
    check(
        "冷却已提交（last_fired_at 非空）",
        any(
            isinstance(entry, dict) and entry.get("last_fired_at")
            for entry in (proactive_after.get("rules") or {}).values()
        ),
        f"{proactive_after.get('sent_log')}",
    )
    check(
        "pending 已清空",
        not (proactive_after.get("pending") or {}),
        f"{proactive_after.get('pending')}",
    )

    rows_after = core.read_messages(messages_path)
    log(f"消息库条数(收尾)={len(rows_after)}（主动前 {rows_before}）")
    log(f"最后三条={[str(row.get('content', ''))[:20] for row in rows_after[-3:]]}")
    check(
        "主动消息写入消息库",
        len(rows_after) == rows_before + 1,
        f"{len(rows_after)}/{rows_before + 1}",
    )

    # 再 tick 一次：应被冷却/限流挡住，不重复刷屏
    repeat = brain.tick(datetime.now())
    brain.flush_outbox(timeout=60)
    check("冷却生效（再次 tick 不重复发送）", len(_timeline) == after, f"{repeat}")

    # -- 报告 ------------------------------------------------------------ #
    log("-" * 60)
    passed = sum(1 for _, ok, _ in _checks if ok)
    log(f"测试结束，耗时 {(datetime.now() - started_at).total_seconds():.1f}s | 断言 {passed}/{len(_checks)} 通过")
    report = _ROOT / "data" / f"e2e_report_{datetime.now():%Y%m%d_%H%M%S}.md"
    gaps = [
        round(_timeline[i]["monotonic"] - _timeline[i - 1]["monotonic"], 2)
        for i in range(1, len(_timeline))
    ]
    report.write_text(
        "\n".join(
            [
                f"# 真实端到端测试报告 {started_at:%Y-%m-%d %H:%M:%S}",
                "",
                f"- 群聊：{GROUP}；发言人：{SPEAKER}",
                f"- 模型：{app_config.get('model')}（base_url={app_config.get('base_url')}）",
                f"- 消息库：{messages_path}（{len(rows_after)} 条）",
                f"- 记忆库：{files['memory']}（{len(atoms_after)} 条原子）",
                f"- 运行日志：{LOG_PATH}",
                f"- 断言：{passed}/{len(_checks)} 通过",
                "",
                "## 断言明细",
                "",
                "| 结果 | 断言 | 细节 |",
                "| --- | --- | --- |",
                *[
                    f"| {'PASS' if ok else 'FAIL'} | {name} | {detail} |"
                    for name, ok, detail in _checks
                ],
                "",
                "## 发送时间线",
                "",
                "| 时间 | 类型 | 规则 | 文本 |",
                "| --- | --- | --- | --- |",
                *[
                    f"| {item['at']:%H:%M:%S} | {item['kind']} | {item['rule_id'] or '-'} | "
                    f"{item['text']} |"
                    for item in _timeline
                ],
                "",
                f"相邻发送间隔：{gaps}",
                "",
                "## 原子状态",
                "",
                "```json",
                json.dumps(atom_state, ensure_ascii=False, indent=2),
                "```",
                "",
                "## proactive 状态",
                "",
                "```json",
                json.dumps(proactive_after, ensure_ascii=False, indent=2),
                "```",
                "",
                "## memory.md",
                "",
                "```",
                memory_text.strip(),
                "```",
            ]
        ),
        encoding="utf-8",
    )
    log(f"报告已写入: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
