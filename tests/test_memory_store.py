"""长期记忆工具层单测：key 规则、存取、遗忘曲线、本地匹配。全部离线，不碰模型。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src" / "ai" / "module" / "lib"))

import memory_store as mem  # noqa: E402  # pyright: ignore[reportMissingImports]
import message_store as store  # noqa: E402  # pyright: ignore[reportMissingImports]

_NOW = datetime(2026, 10, 26, 12, 0, 0)


def make_record(
    key: str = "张天宇.喜好.咖啡",
    summary: str = "喜欢手冲咖啡",
    importance: float = 0.5,
    stability: float = 24.0,
    last: datetime | None = None,
    status: str = mem.STATUS_ACTIVE,
    recalls: int = 0,
) -> dict:
    moment = last or _NOW
    text = store.format_time(moment)
    return {
        "key": key,
        "summary": summary,
        "importance": importance,
        "status": status,
        "created_at": text,
        "updated_at": text,
        "last_recall_at": text,
        "recalls": recalls,
        "stability_h": stability,
    }


class KeyTest(unittest.TestCase):
    def test_parse_valid(self) -> None:
        self.assertEqual(mem.parse_key("张天宇.喜好.咖啡"), ("张天宇", "喜好", "咖啡"))
        self.assertEqual(mem.parse_key("  张天宇 . 喜好 . 咖啡 "), ("张天宇", "喜好", "咖啡"))

    def test_attr_normalized(self) -> None:
        self.assertEqual(mem.parse_key("张天宇.爱好.跑步")[1], "喜好")
        self.assertEqual(mem.parse_key("张天宇.天气.下雨")[1], "其他")

    def test_invalid_rejected(self) -> None:
        for raw in ("", "张天宇.喜好", "a.b.c.d", "张天宇..咖啡", ".喜好.咖啡", None, 42):
            self.assertIsNone(mem.parse_key(raw), raw)
        self.assertIsNone(mem.parse_key("张" * 21 + ".喜好.咖啡"))

    def test_make_key_sanitizes(self) -> None:
        self.assertEqual(mem.make_key("张.天宇", "喜好", "咖.啡"), "张·天宇.喜好.咖·啡")
        self.assertEqual(mem.make_key("张天宇", "喜好", "  "), "")

    def test_memory_file_sits_next_to_messages(self) -> None:
        path = mem.memory_file(Path("data/groups"), "测试群")
        self.assertEqual(path, Path("data/groups/测试群/memory.md"))

    def test_find_similar_merges_rewording(self) -> None:
        records = [
            make_record(key="汪敬杨.习惯.美式咖啡"),
            make_record(key="汪敬杨.约定.图书馆复习"),
        ]
        self.assertIs(
            mem.find_similar(records, "汪敬杨.喜好.咖啡"), records[0]
        )
        self.assertIs(
            mem.find_similar(records, "汪敬杨.约定.周六图书馆"), records[1]
        )

    def test_find_similar_keeps_different_facts_apart(self) -> None:
        records = [make_record(key="张天宇.喜好.篮球")]
        self.assertIsNone(mem.find_similar(records, "张天宇.喜好.咖啡"))
        self.assertIsNone(mem.find_similar(records, "许斌.喜好.篮球"))
        self.assertIsNone(mem.find_similar(records, "张天宇.厌恶.篮球"))


class StoreTest(unittest.TestCase):
    def test_roundtrip_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "群" / "memory.md"
            self.assertEqual(mem.load_memory(path), [])
            records = [make_record(), make_record(key="张天宇.厌恶.猫毛")]
            mem.save_memory(path, records)
            loaded = mem.load_memory(path)
            self.assertEqual([item["key"] for item in loaded], [item["key"] for item in records])
            self.assertEqual(loaded[0]["summary"], "喜欢手冲咖啡")

    def test_bad_line_skipped_and_last_key_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.md"
            path.write_text(
                'not json\n{"key": "张天宇.喜好.咖啡", "summary": "旧"}\n'
                '{"key": "张天宇.喜好.咖啡", "summary": "新"}\n{"summary": "没有key"}\n',
                encoding="utf-8",
            )
            loaded = mem.load_memory(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["summary"], "新")


class CurveTest(unittest.TestCase):
    def test_initial_stability_scales_with_importance(self) -> None:
        self.assertAlmostEqual(mem.initial_stability(0.0), 24.0)
        self.assertAlmostEqual(mem.initial_stability(1.0), 96.0)
        self.assertAlmostEqual(mem.initial_stability(0.5, {"stability_base_hours": 10, "importance_factor": 1}), 15.0)

    def test_retention_decays_monotonically(self) -> None:
        record = make_record(stability=24.0)
        self.assertAlmostEqual(mem.retention(record, _NOW), 1.0, places=6)
        day = mem.retention(record, _NOW + timedelta(days=1))
        week = mem.retention(record, _NOW + timedelta(days=7))
        self.assertGreater(day, week)
        self.assertAlmostEqual(day, 0.3679, places=3)

    def test_reinforce_grows_and_caps(self) -> None:
        record = make_record(stability=100.0)
        mem.reinforce(record, _NOW + timedelta(hours=1))
        self.assertEqual(record["recalls"], 1)
        self.assertAlmostEqual(record["stability_h"], 180.0)
        self.assertEqual(record["last_recall_at"], store.format_time(_NOW + timedelta(hours=1)))
        for _ in range(20):
            mem.reinforce(record, _NOW)
        self.assertAlmostEqual(record["stability_h"], 720.0)

    def test_decay_scan_forgets_low_retention_but_not_immortal(self) -> None:
        low = make_record(importance=0.3, stability=24.0)
        key = make_record(key="张天宇.身份.生日", importance=0.9, stability=24.0)
        later = _NOW + timedelta(days=60)
        marked = mem.decay_scan([low, key], later)
        self.assertEqual(marked, 1)
        self.assertEqual(low["status"], mem.STATUS_FORGOTTEN)
        self.assertEqual(key["status"], mem.STATUS_ACTIVE)

    def test_forgotten_is_not_marked_twice(self) -> None:
        record = make_record(status=mem.STATUS_FORGOTTEN)
        self.assertEqual(mem.decay_scan([record], _NOW + timedelta(days=999)), 0)


class MatchTest(unittest.TestCase):
    def test_sub_only_hit(self) -> None:
        score, hits = mem.score_record(make_record(), "今天想喝咖啡", "", None)
        self.assertGreaterEqual(score, 0.8)
        self.assertEqual(hits["sub"], 1.0)
        self.assertEqual(hits["attr"], 0.0)

    def test_entity_plus_attr_hit(self) -> None:
        score, hits = mem.score_record(make_record(), "张天宇喜欢什么", "张天宇", None)
        self.assertEqual(hits["entity"], 1.0)
        self.assertEqual(hits["attr"], 1.0)
        self.assertGreater(score, 1.0)

    def test_entity_only_is_not_enough(self) -> None:
        score, hits = mem.score_record(make_record(), "张天宇你好", "", None)
        self.assertEqual(score, 0.0)
        self.assertEqual(hits, {})

    def test_alias_extends_attribute(self) -> None:
        aliases = mem._merge_aliases({"喜好": ["上头"]})
        score, hits = mem.score_record(make_record(), "张天宇对咖啡挺上头的", "张天宇", aliases)
        self.assertEqual(hits["attr"], 1.0)
        self.assertGreater(score, 0.0)

    def test_select_filters_forgotten_and_limits(self) -> None:
        fresh = make_record()
        stale = make_record(key="张天宇.喜好.跑步", summary="爱跑步", last=_NOW - timedelta(days=20))
        forgotten = make_record(key="张天宇.喜好.咖啡", status=mem.STATUS_FORGOTTEN)
        picked = mem.select_records(
            [forgotten, stale, fresh], "张天宇喜欢咖啡和跑步", "张天宇", _NOW, {"inject_top_k": 1}
        )
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0][1]["key"], "张天宇.喜好.咖啡")

    def test_select_empty_text(self) -> None:
        self.assertEqual(mem.select_records([make_record()], "  ", "", _NOW, None), [])

    def test_generic_verb_does_not_match_appointment(self) -> None:
        """「你记得…吗」里的"记得"不是约定主题词，不能把约定记忆召回来。"""
        record = make_record(key="汪敬杨.约定.周六图书馆", summary="约好周六去图书馆复习影像诊断")
        picked = mem.select_records([record], "你还记得我爱喝什么咖啡吗", "汪敬杨", _NOW, None)
        self.assertEqual(picked, [])

    def test_summary_words_are_searchable(self) -> None:
        """key 第三段叫「早晨喝美式」时，问「咖啡」也要能召回（摘要里有"咖啡"）。"""
        record = make_record(
            key="汪敬杨.习惯.早晨喝美式",
            summary="每天早上必须喝一杯美式咖啡，否则第一节课撑不住",
        )
        picked = mem.select_records([record], "你还记得我爱喝什么咖啡吗", "汪敬杨", _NOW, None)
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0][2]["sub"], mem._W_SUMMARY)

    def test_stop_bigrams_do_not_trigger(self) -> None:
        record = make_record(key="汪敬杨.习惯.早晨喝美式", summary="每天早上喝一杯，什么也不加")
        picked = mem.select_records([record], "今天什么也没干", "", _NOW, None)
        self.assertEqual(picked, [])

    # -- 语义补召回 --------------------------------------------------------- #

    def _wording_differs(self) -> tuple[dict, str, str]:
        record = make_record(key="汪敬杨.习惯.晨间美式", summary="起床后必须来一杯")
        return record, "汪敬杨，我早上都喝点什么好", "汪敬杨"

    def test_semantic_recall_when_wording_differs(self) -> None:
        record, text, speaker = self._wording_differs()
        self.assertEqual(mem.select_records([record], text, speaker, _NOW, None), [])
        picked = mem.select_records(
            [record], text, speaker, _NOW, None, similarity=lambda rec: 0.62
        )
        self.assertEqual(len(picked), 1)
        self.assertAlmostEqual(picked[0][2]["sim"], 0.62)

    def test_semantic_recall_respects_threshold(self) -> None:
        record, text, speaker = self._wording_differs()
        picked = mem.select_records(
            [record], text, speaker, _NOW, None, similarity=lambda rec: 0.3
        )
        self.assertEqual(picked, [])

    def test_semantic_recall_requires_identity(self) -> None:
        """语义再像也不能跨人召回。"""
        record = make_record(key="汪敬杨.习惯.晨间美式", summary="起床后必须来一杯")
        picked = mem.select_records(
            [record], "早饭吃了什么", "张天宇", _NOW, None, similarity=lambda rec: 0.99
        )
        self.assertEqual(picked, [])

    def test_similarity_failure_does_not_break_recall(self) -> None:
        record, text, speaker = self._wording_differs()

        def boom(rec):
            raise RuntimeError("模型挂了")

        self.assertEqual(mem.select_records([record], text, speaker, _NOW, None, similarity=boom), [])

    def test_semantic_recall_picks_best_within_identity_gate(self) -> None:
        """别人那条更像，也不能把本人这条顶掉（只在过闸门的候选里取最像）。"""
        mine = make_record(key="张天宇.喜好.咖啡", summary="只喝美式，不加糖")
        other = make_record(key="汪敬杨.喜好.咖啡", summary="只喝手冲，自己磨豆子")
        scores = {"张天宇.喜好.咖啡": 0.58, "汪敬杨.喜好.咖啡": 0.95}
        picked = mem.select_records(
            [mine, other],
            "我又想喝那杯黑的了",
            "张天宇",
            _NOW,
            None,
            similarity=lambda rec: scores[str(rec["key"])],
        )
        self.assertEqual([item[1]["key"] for item in picked], ["张天宇.喜好.咖啡"])

    def test_speaker_known_skips_other_person_memory(self) -> None:
        """说话人已知时，属性词命中也不能跨人召回；被点名才放行。"""
        record = make_record(key="张天宇.身份.生日", summary="2005 年 3 月 14 日出生")
        self.assertEqual(mem.select_records([record], "我生日是哪天", "许斌", _NOW, None), [])
        picked = mem.select_records([record], "张天宇的生日是哪天", "许斌", _NOW, None)
        self.assertEqual([item[1]["key"] for item in picked], ["张天宇.身份.生日"])

    def test_time_bigram_does_not_match_summary(self) -> None:
        """「周三」不能命中「每周三次夜跑」这种跨词组合。"""
        record = make_record(key="张天宇.喜好.夜跑", summary="每周三次夜跑，绕操场三圈")
        self.assertEqual(mem.select_records([record], "周三一起吃饭吗", "张天宇", _NOW, None), [])


if __name__ == "__main__":
    unittest.main()
