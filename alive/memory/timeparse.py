"""Relative / absolute date normalisation for memory writes (docs/03 §3.2).

``find_time_refs(text, now, tz)`` scans Chinese and English text for date phrases and resolves each
to an absolute ``[start, end)`` range in the persona timezone, keeping the raw phrase. Supported:

Chinese: 今天/今晚/今早 · 明天/明早/明晚 · 后天 · 大后天 · 昨天/昨晚 · 前天 · 大前天 · N天后/N天前 ·
N周后/N个月后 · 下周三 / 这周五 / 本周日 / 上周二 / 下下周一 · 周X/星期X/礼拜X · 周末/下周末/上周末 ·
下周/这周/上周 · 下个月/这个月/上个月 · 明年/今年/去年/后年/前年 · X月X日/X月X号/X月X ·
YYYY年M月D日 · X号/X日 · optional part of day (早上/上午/中午/下午/晚上) and clock time (3点/3点半/15:30).
English: today/tonight/tomorrow/yesterday · the day after tomorrow / the day before yesterday ·
next/this/last <weekday> · (on) <weekday> · next/this/last week|month|year|weekend · in N days/weeks ·
N days/weeks ago · <Month> <day> / <day> <Month> · ISO dates (2026-11-18).

Ambiguity rules (documented so they can be tuned):
- A bare weekday (周三 / Monday) means the nearest *upcoming* occurrence (today counts).
- A month/day without a year picks the year that puts the date within ±183 days of now.
- A bare day of month (15号) picks the month (previous/current/next) closest to now; ties go to the future.
- Weeks start on Monday. 下周三 = Wednesday of next calendar week.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Optional

__all__ = ["TimeRef", "find_time_refs", "resolve_first", "parse_cn_number"]


@dataclass
class TimeRef:
    start: float
    end: float
    granularity: str      # hour | part | day | range | week | month | year
    raw: str
    pos: int = 0

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "granularity": self.granularity, "raw": self.raw}


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
              "八": 8, "九": 9}
_CN_NUM = "零〇一二两三四五六七八九十"
_NUM = rf"(?:\d{{1,4}}|[{_CN_NUM}]{{1,4}})"


def parse_cn_number(s: str) -> Optional[int]:
    """'3' -> 3, '十五' -> 15, '二十' -> 20, '两' -> 2, '二十三' -> 23. None if unparsable (<100)."""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if any(ch not in _CN_DIGITS and ch != "十" for ch in s):
        return None
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGITS.get(a, None) if a else 1
        if tens is None:
            return None
        ones = 0
        if b:
            if len(b) != 1 or b not in _CN_DIGITS:
                return None
            ones = _CN_DIGITS[b]
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    # e.g. '二三' is not a number we accept
    return None


_WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "七": 6, "末": -1}
_WEEKDAY_EN = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
               "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5,
               "sat": 5, "sunday": 6, "sun": 6}
_MONTH_EN = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
             "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9,
             "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12,
             "dec": 12}

_PARTS = {  # part of day -> (start hour, end hour)
    "凌晨": (0, 6), "清晨": (5, 8), "早上": (6, 11), "早晨": (6, 11), "上午": (8, 12), "中午": (11, 14),
    "下午": (13, 18), "傍晚": (17, 19), "晚上": (18, 24), "夜里": (21, 24), "半夜": (0, 3),
    "晚": (18, 24), "早": (6, 11),
}

# ---------------------------------------------------------------------------
# patterns (ordered: most specific first; compiled into one alternation)
# ---------------------------------------------------------------------------
_PART_RE = r"(?P<part>凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)?"
_CLOCK_RE = (rf"(?:(?P<hour>\d{{1,2}}|[{_CN_NUM}]{{1,3}})(?:点|時|时)(?P<half>半|一刻|三刻)?"
             rf"(?:(?P<minute>\d{{1,2}}|[{_CN_NUM}]{{1,3}})分?)?|(?P<hh>\d{{1,2}}):(?P<mm>\d{{2}}))?")
_TAIL = _PART_RE + _CLOCK_RE

_CN_PATTERNS = [
    # absolute: 2026年11月18日 / 2026-11-18 / 2026/11/18
    ("ymd", rf"(?P<y>\d{{4}})\s*(?:年|-|/|\.)\s*(?P<m>\d{{1,2}})\s*(?:月|-|/|\.)\s*(?P<d>\d{{1,2}})\s*(?:日|号)?" + _TAIL),
    # 11月18日 / 十一月十八号 / 3月5
    ("md", rf"(?P<m>\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*月\s*(?P<d>\d{{1,2}}|[{_CN_NUM}]{{1,3}})\s*(?:日|号)?" + _TAIL),
    # 下下周三 / 下周三 / 这周五 / 本周日 / 上周二 / 下星期一 / 下礼拜天 / 这个周末
    ("week_day", r"(?P<rel>下下|下个?|这个?|本|上上|上个?)\s*(?:周|星期|礼拜|禮拜)(?P<wd>[一二三四五六日天七末])" + _TAIL),
    # N天后 / N天以后 / N天之后 / N天前 / N天以前
    ("n_days", rf"(?P<n>{_NUM})\s*(?:天|日)(?P<dir>后|以后|之后|過後|前|以前|之前)"),
    ("n_weeks", rf"(?P<n>{_NUM})\s*(?:个)?(?:周|星期|礼拜)(?P<dir>后|以后|之后|前|以前|之前)"),
    ("n_months", rf"(?P<n>{_NUM})\s*个月(?P<dir>后|以后|之后|前|以前|之前)"),
    # 大后天 / 大前天 / 后天 / 前天 / 明天 / 昨天 / 今天 (+ part/clock)
    ("rel_day", r"(?<![以之提])(?P<w>大后天|大前天|后天|前天|明天|明日|明儿|明早|明晚|昨天|昨日|昨儿|昨晚|今天|今日|今儿|今早|今晚)" + _TAIL),
    # 下周 / 这周 / 本周 / 上周 / 下个星期 (whole week)
    ("week", r"(?P<rel>下下|下个?|这个?|本|上上|上个?)\s*(?:周|星期|礼拜)(?![一二三四五六日天七末年])"),
    # 周末 (bare)
    ("weekend", r"(?<![下这本上个])(?:周末|週末)"),
    # 周三 / 星期三 / 礼拜三 (bare weekday)
    ("bare_wd", r"(?<![下这本上个每\d一二三四五六七八九十两几])(?:周|星期|礼拜|禮拜)(?P<wd>[一二三四五六日天])" + _TAIL),
    # 下个月 / 下月 / 这个月 / 本月 / 上个月 / 上月
    ("month", r"(?<![马早晚])(?P<rel>下个|下|这个|本|上个|上)月(?:(?P<d>\d{1,2}|[" + _CN_NUM + r"]{1,3})(?:号|日))?"),
    # 明年 / 今年 / 去年 / 后年 / 前年
    ("year", r"(?P<w>明年|今年|去年|后年|前年)"),
    # 15号 / 15日 / 十五号
    ("dom", rf"(?<![\d月周期拜生节])(?P<d>\d{{1,2}}|[{_CN_NUM}]{{1,3}})(?:号)(?!码|楼|线|房|位|口|门)" + _TAIL),
    # 下午3点半 / 晚上8点 (part of day required, means today)
    ("clock", r"(?P<part>凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜)(?=\d|[" + _CN_NUM + r"])" + _CLOCK_RE),
    ("dom2", r"(?<![\d月周期拜生节一二三四五六七八九十])(?P<d>\d{1,2})日(?![本元子常记])" + _TAIL),
]

_EN_PATTERNS = [
    ("en_iso", r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"),
    ("en_dayafter", r"\bthe day after tomorrow\b"),
    ("en_daybefore", r"\bthe day before yesterday\b"),
    ("en_rel_day", r"\b(?P<w>today|tonight|tomorrow|tmr|tmrw|yesterday)\b"),
    ("en_rel_wd", r"\b(?P<rel>next|this|last|coming)\s+(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b"),
    ("en_rel_unit", r"\b(?P<rel>next|this|last)\s+(?P<unit>week|weekend|month|year)\b"),
    ("en_in_n", r"\bin\s+(?P<n>\d{1,3}|a|an|one|two|three|four|five|six|seven)\s+(?P<unit>days?|weeks?|months?)\b"),
    ("en_n_ago", r"\b(?P<n>\d{1,3}|a|an|one|two|three|four|five|six|seven)\s+(?P<unit>days?|weeks?|months?)\s+ago\b"),
    ("en_md", r"\b(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b"),
    ("en_dm", r"\b(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"),
    ("en_on_wd", r"\b(?:on\s+)?(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"),
]
_EN_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7}


def _compile(patterns):
    parts = []
    for name, pat in patterns:
        # rename groups per alternative to keep group names unique
        pat2 = re.sub(r"\(\?P<(\w+)>", lambda m: f"(?P<{name}__{m.group(1)}>", pat)
        parts.append(f"(?P<{name}>{pat2})")
    return re.compile("|".join(parts), re.I)


_CN_RE = _compile(_CN_PATTERNS)
_EN_RE = _compile(_EN_PATTERNS)


# ---------------------------------------------------------------------------
# date helpers
# ---------------------------------------------------------------------------
def _midnight(d: date, tz: tzinfo) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _day_range(d: date, tz: tzinfo) -> tuple:
    s = _midnight(d, tz)
    return s.timestamp(), _midnight(d + timedelta(days=1), tz).timestamp()


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, 1)


def _safe_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _nearest_year(today: date, m: int, d: int) -> Optional[date]:
    best = None
    for y in (today.year - 1, today.year, today.year + 1):
        c = _safe_date(y, m, d)
        if c is None:
            continue
        diff = (c - today).days
        if -183 <= diff <= 183:
            if best is None or abs(diff) < abs((best - today).days):
                best = c
    return best


def _nearest_month(today: date, d: int) -> Optional[date]:
    cands = []
    for k in (-1, 0, 1):
        base = _add_months(date(today.year, today.month, 1), k)
        c = _safe_date(base.year, base.month, d)
        if c is not None:
            cands.append(c)
    if not cands:
        return None
    # closest; ties -> future
    return min(cands, key=lambda c: (abs((c - today).days), -(c - today).days))


def _g(m: re.Match, name: str, key: str) -> Optional[str]:
    try:
        return m.group(f"{name}__{key}")
    except IndexError:
        return None


def _apply_tail(m: re.Match, name: str, d: date, tz: tzinfo, base_gran: str = "day",
                implicit_part: Optional[str] = None) -> tuple:
    """Refine a day with an optional part-of-day and clock time. Returns (start, end, granularity)."""
    s, e = _day_range(d, tz)
    part = _g(m, name, "part") or implicit_part
    hour_s = _g(m, name, "hour")
    hh = _g(m, name, "hh")
    if hour_s is None and hh is None:
        if part and part in _PARTS:
            h0, h1 = _PARTS[part]
            return s + h0 * 3600, s + h1 * 3600, "part"
        return s, e, base_gran
    if hh is not None:
        hour, minute = int(hh), int(_g(m, name, "mm") or 0)
    else:
        hour = parse_cn_number(hour_s) or 0
        half = _g(m, name, "half")
        minute = {"半": 30, "一刻": 15, "三刻": 45}.get(half or "", 0)
        mn = _g(m, name, "minute")
        if mn is not None:
            minute = parse_cn_number(mn) or 0
    if part in ("下午", "晚上", "傍晚", "夜里", "晚") and hour < 12:
        hour += 12
    if part == "中午" and hour < 6:
        hour += 12
    if hour > 23 or minute > 59:
        return s, e, base_gran
    st = _midnight(d, tz) + timedelta(hours=hour, minutes=minute)
    return st.timestamp(), st.timestamp() + 3600, "hour"


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


_REL_WEEK = {"下下": 2, "下": 1, "下个": 1, "这": 0, "这个": 0, "本": 0, "上": -1, "上个": -1, "上上": -2}


def _resolve_cn(m: re.Match, today: date, tz: tzinfo) -> Optional[tuple]:
    name = m.lastgroup
    if name is None:
        return None
    if name == "ymd":
        d = _safe_date(int(_g(m, name, "y")), int(_g(m, name, "m")), int(_g(m, name, "d")))
        return _apply_tail(m, name, d, tz) if d else None
    if name == "md":
        mo, dd = parse_cn_number(_g(m, name, "m")), parse_cn_number(_g(m, name, "d"))
        if not mo or not dd or mo > 12 or dd > 31:
            return None
        d = _nearest_year(today, mo, dd)
        return _apply_tail(m, name, d, tz) if d else None
    if name == "week_day":
        rel = _g(m, name, "rel")
        off = _REL_WEEK.get(rel, 0)
        wd = _WEEKDAY_CN[_g(m, name, "wd")]
        ws = _week_start(today) + timedelta(weeks=off)
        if wd == -1:  # 下周末
            sat = ws + timedelta(days=5)
            return _day_range(sat, tz)[0], _day_range(sat + timedelta(days=1), tz)[1], "range"
        return _apply_tail(m, name, ws + timedelta(days=wd), tz)
    if name in ("n_days", "n_weeks", "n_months"):
        n = parse_cn_number(_g(m, name, "n"))
        if n is None or n > 400:
            return None
        sign = -1 if (_g(m, name, "dir") or "").endswith("前") else 1
        if name == "n_days":
            d = today + timedelta(days=sign * n)
            return (*_day_range(d, tz), "day")
        if name == "n_weeks":
            d = today + timedelta(weeks=sign * n)
            return (*_day_range(d, tz), "day")
        first = _add_months(date(today.year, today.month, 1), sign * n)
        d = _safe_date(first.year, first.month, min(today.day, 28)) or first
        return (*_day_range(d, tz), "day")
    if name == "rel_day":
        w = _g(m, name, "w")
        off = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明儿": 1, "明早": 1, "明晚": 1, "今天": 0, "今日": 0,
               "今儿": 0, "今早": 0, "今晚": 0, "昨天": -1, "昨日": -1, "昨儿": -1, "昨晚": -1, "前天": -2,
               "大前天": -3}[w]
        implicit = "早上" if w.endswith("早") else ("晚上" if w.endswith("晚") else None)
        return _apply_tail(m, name, today + timedelta(days=off), tz, implicit_part=implicit)
    if name == "week":
        off = _REL_WEEK.get(_g(m, name, "rel"), 0)
        ws = _week_start(today) + timedelta(weeks=off)
        return _day_range(ws, tz)[0], _day_range(ws + timedelta(days=6), tz)[1], "week"
    if name == "weekend":
        ws = _week_start(today)
        sat = ws + timedelta(days=5)
        return _day_range(sat, tz)[0], _day_range(sat + timedelta(days=1), tz)[1], "range"
    if name == "bare_wd":
        wd = _WEEKDAY_CN[_g(m, name, "wd")]
        delta = (wd - today.weekday()) % 7
        return _apply_tail(m, name, today + timedelta(days=delta), tz)
    if name == "month":
        rel = _g(m, name, "rel")
        off = {"下个": 1, "下": 1, "这个": 0, "本": 0, "上个": -1, "上": -1}[rel]
        first = _add_months(date(today.year, today.month, 1), off)
        dd = _g(m, name, "d")
        if dd:
            dn = parse_cn_number(dd)
            d = _safe_date(first.year, first.month, dn or 0) if dn else None
            if d:
                return (*_day_range(d, tz), "day")
        nxt = _add_months(first, 1)
        return _midnight(first, tz).timestamp(), _midnight(nxt, tz).timestamp(), "month"
    if name == "year":
        off = {"后年": 2, "明年": 1, "今年": 0, "去年": -1, "前年": -2}[_g(m, name, "w")]
        y = today.year + off
        return _midnight(date(y, 1, 1), tz).timestamp(), _midnight(date(y + 1, 1, 1), tz).timestamp(), "year"
    if name == "clock":
        return _apply_tail(m, name, today, tz)
    if name in ("dom", "dom2"):
        dn = parse_cn_number(_g(m, name, "d"))
        if not dn or dn > 31:
            return None
        d = _nearest_month(today, dn)
        return _apply_tail(m, name, d, tz) if d else None
    return None


def _en_n(s: str) -> Optional[int]:
    s = s.lower()
    if s.isdigit():
        return int(s)
    return _EN_NUM.get(s)


def _resolve_en(m: re.Match, today: date, tz: tzinfo) -> Optional[tuple]:
    name = m.lastgroup
    if name == "en_iso":
        d = _safe_date(int(_g(m, name, "y")), int(_g(m, name, "m")), int(_g(m, name, "d")))
        return (*_day_range(d, tz), "day") if d else None
    if name == "en_dayafter":
        return (*_day_range(today + timedelta(days=2), tz), "day")
    if name == "en_daybefore":
        return (*_day_range(today - timedelta(days=2), tz), "day")
    if name == "en_rel_day":
        w = _g(m, name, "w").lower()
        if w == "tonight":
            s, _ = _day_range(today, tz)
            return s + 18 * 3600, s + 24 * 3600, "part"
        off = {"today": 0, "tomorrow": 1, "tmr": 1, "tmrw": 1, "yesterday": -1}[w]
        return (*_day_range(today + timedelta(days=off), tz), "day")
    if name == "en_rel_wd":
        rel = _g(m, name, "rel").lower()
        wd = _WEEKDAY_EN[_g(m, name, "wd").lower()]
        if rel in ("this", "coming"):
            delta = (wd - today.weekday()) % 7
            return (*_day_range(today + timedelta(days=delta), tz), "day")
        off = 1 if rel == "next" else -1
        d = _week_start(today) + timedelta(weeks=off, days=wd)
        return (*_day_range(d, tz), "day")
    if name == "en_rel_unit":
        rel, unit = _g(m, name, "rel").lower(), _g(m, name, "unit").lower()
        off = {"next": 1, "this": 0, "last": -1}[rel]
        if unit in ("week", "weekend"):
            ws = _week_start(today) + timedelta(weeks=off)
            if unit == "weekend":
                sat = ws + timedelta(days=5)
                return _day_range(sat, tz)[0], _day_range(sat + timedelta(days=1), tz)[1], "range"
            return _day_range(ws, tz)[0], _day_range(ws + timedelta(days=6), tz)[1], "week"
        if unit == "month":
            first = _add_months(date(today.year, today.month, 1), off)
            return _midnight(first, tz).timestamp(), _midnight(_add_months(first, 1), tz).timestamp(), "month"
        y = today.year + off
        return _midnight(date(y, 1, 1), tz).timestamp(), _midnight(date(y + 1, 1, 1), tz).timestamp(), "year"
    if name in ("en_in_n", "en_n_ago"):
        n = _en_n(_g(m, name, "n"))
        if n is None:
            return None
        sign = 1 if name == "en_in_n" else -1
        unit = _g(m, name, "unit").lower()
        if unit.startswith("day"):
            d = today + timedelta(days=sign * n)
        elif unit.startswith("week"):
            d = today + timedelta(weeks=sign * n)
        else:
            first = _add_months(date(today.year, today.month, 1), sign * n)
            d = _safe_date(first.year, first.month, min(today.day, 28)) or first
        return (*_day_range(d, tz), "day")
    if name in ("en_md", "en_dm"):
        mon = _MONTH_EN.get(_g(m, name, "mon").lower().rstrip("."))
        dd = int(_g(m, name, "d"))
        if not mon:
            return None
        d = _nearest_year(today, mon, dd)
        return (*_day_range(d, tz), "day") if d else None
    if name == "en_on_wd":
        wd = _WEEKDAY_EN[_g(m, name, "wd").lower()]
        delta = (wd - today.weekday()) % 7
        return (*_day_range(today + timedelta(days=delta), tz), "day")
    return None


def find_time_refs(text: str, now: float, tz: tzinfo, limit: int = 8) -> list:
    """All date phrases in ``text`` resolved relative to ``now`` (epoch seconds) in ``tz``."""
    if not text:
        return []
    today = datetime.fromtimestamp(now, tz).date()
    out: list = []
    taken: list = []
    for rx, resolver in ((_CN_RE, _resolve_cn), (_EN_RE, _resolve_en)):
        for m in rx.finditer(text):
            if not m.group(0).strip():
                continue
            a, b = m.span()
            if any(a < tb and ta < b for ta, tb in taken):
                continue
            try:
                r = resolver(m, today, tz)
            except Exception:  # pragma: no cover - defensive: never fail a write on a date
                r = None
            if r is None:
                continue
            start, end, gran = r
            out.append(TimeRef(float(start), float(end), gran, m.group(0).strip(), a))
            taken.append((a, b))
            if len(out) >= limit:
                break
    out.sort(key=lambda t: t.pos)
    return out


def resolve_first(text: str, now: float, tz: tzinfo) -> Optional[TimeRef]:
    refs = find_time_refs(text, now, tz, limit=4)
    return refs[0] if refs else None
