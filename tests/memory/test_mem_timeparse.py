"""Write-time time normalisation (docs/03 §3.2): relative dates -> absolute ranges, raw phrase kept."""
from datetime import datetime

import pytest

from alive.memory.timeparse import find_time_refs, parse_cn_number, resolve_first
from tests.memory.memhelp import MONDAY, TZ, at, day_of, kernel


def first(text, now=MONDAY):
    r = resolve_first(text, now, TZ)
    assert r is not None, text
    return r


@pytest.mark.parametrize("text,expected_day,raw", [
    ("我今天有点累", "2026-11-09", "今天"),
    ("明天要考试", "2026-11-10", "明天"),
    ("后天回老家", "2026-11-11", "后天"),
    ("大后天去面试", "2026-11-12", "大后天"),
    ("昨天下雨了", "2026-11-08", "昨天"),
    ("前天见过他", "2026-11-07", "前天"),
    ("大前天的事", "2026-11-06", "大前天"),
    ("我下周三面试", "2026-11-18", "下周三"),
    ("这周五一起吃饭", "2026-11-13", "这周五"),
    ("本周日休息", "2026-11-15", "本周日"),
    ("上周二吵了一架", "2026-11-03", "上周二"),
    ("下下周一交报告", "2026-11-23", "下下周一"),
    ("周三见", "2026-11-11", "周三"),
    ("星期天去公园", "2026-11-15", "星期天"),
    ("11月18日生日", "2026-11-18", "11月18日"),
    ("十二月二十五号聚会", "2026-12-25", "十二月二十五号"),
    ("1月3日考试", "2027-01-03", "1月3日"),
    ("15号发工资", "2026-11-15", "15号"),
    ("三天后出发", "2026-11-12", "三天后"),
    ("2天前买的", "2026-11-07", "2天前"),
    ("下个月5号搬家", "2026-12-05", "下个月5号"),
    ("2026年12月1日入职", "2026-12-01", "2026年12月1日"),
    ("tomorrow is my exam", "2026-11-10", "tomorrow"),
    ("see you next Monday", "2026-11-16", "next Monday"),
    ("the day after tomorrow", "2026-11-11", "the day after tomorrow"),
    ("in 3 days", "2026-11-12", "in 3 days"),
    ("I met her 2 weeks ago", "2026-10-26", "2 weeks ago"),
    ("party on Nov 20th", "2026-11-20", "Nov 20th"),
])
def test_day_phrases(text, expected_day, raw):
    r = first(text)
    assert day_of(r.start) == expected_day, (text, day_of(r.start))
    assert r.raw == raw
    assert r.end > r.start


def test_ranges_and_hours():
    r = first("下周要出差")
    assert (day_of(r.start), r.granularity) == ("2026-11-16", "week") and day_of(r.end - 1) == "2026-11-22"
    r = first("周末去爬山")
    assert (day_of(r.start), day_of(r.end - 1), r.granularity) == ("2026-11-14", "2026-11-15", "range")
    r = first("下周末回家")
    assert day_of(r.start) == "2026-11-21"
    r = first("下个月去日本")
    assert (day_of(r.start), day_of(r.end - 1), r.granularity) == ("2026-12-01", "2026-12-31", "month")
    r = first("明天下午3点开会")
    assert r.start == at(2026, 11, 10, 15, 0) and r.granularity == "hour"
    r = first("明晚8点半看电影")
    assert r.start == at(2026, 11, 10, 20, 30)
    r = first("今晚有空吗")
    assert r.start == at(2026, 11, 9, 18) and r.granularity == "part"
    r = first("下午3点半到")
    assert r.start == at(2026, 11, 9, 15, 30)
    r = first("明年想换工作")
    assert r.granularity == "year" and day_of(r.start) == "2027-01-01"


def test_false_positives_are_ignored():
    for t in ("以前天天去跑步", "坐3号线", "一周一次", "日本料理", "马上月底了也没钱", "我有三个建议"):
        assert find_time_refs(t, MONDAY, TZ) == [], t


def test_nearest_year_and_month_rules():
    # month/day without a year: the year that puts the date within ±183 days of now
    assert day_of(first("3月5日那天").start) == "2027-03-05"
    assert day_of(first("9月1日开学的").start) == "2026-09-01"
    assert day_of(first("2月1日").start) == "2027-02-01"
    # bare day of month: the closest month (on Nov 9: 5号 -> Nov 5, 28号 -> Oct 28 which is closer than Nov 28)
    assert day_of(first("5号").start) == "2026-11-05"
    assert day_of(first("28号").start) == "2026-10-28"


def test_cn_numbers():
    assert parse_cn_number("十五") == 15 and parse_cn_number("二十") == 20 and parse_cn_number("两") == 2
    assert parse_cn_number("二十三") == 23 and parse_cn_number("十") == 10 and parse_cn_number("abc") is None


async def test_note_normalizes_time_at_write(tmp_path):
    k = await kernel(tmp_path)
    try:
        # told on Monday, the record must point at next Wednesday and keep the raw phrase
        rid = await k.memory.note(kind="semantic", content="阿杰下周三有一场面试", source="told_by",
                                  told_by="tg:jie", person_keys=["tg:jie"])
        h = await k.memory.get(rid)
        assert day_of(h.refers_to["start"]) == "2026-11-18" and h.refers_to["raw"] == "下周三"
        assert h.observed_at == MONDAY
        # explicit refers_to raw is resolved relative to observed_at, not to "now"
        rid2 = await k.memory.note(kind="semantic", content="小林要去看牙", refers_to={"raw": "明天"},
                                   observed_at=at(2026, 11, 1, 20), source="told_by", told_by="tg:lin")
        h2 = await k.memory.get(rid2)
        assert day_of(h2.refers_to["start"]) == "2026-11-02" and h2.refers_to["raw"] == "明天"
        # format_hits shows the absolute date next to the relative phrase
        txt = k.memory.format_hits([h], k.clock.now())
        assert "11月18日" in txt and "下周三" in txt
        # a record with no date phrase has no refers_to
        h3 = await k.memory.get(await k.memory.note(kind="semantic", content="阿杰喜欢猫", source="told_by",
                                                     told_by="tg:jie"))
        assert h3.refers_to is None
    finally:
        await k.stop()
