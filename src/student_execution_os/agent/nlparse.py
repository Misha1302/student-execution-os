"""Deterministic RU/EN natural-language task capture.

Turns everyday phrases such as

    "В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно"
    "Remind me tomorrow evening to send the report"

into a ``task.create``-shaped payload without any remote model, so capture keeps
working offline and without LLM credentials. The browser/Android client carries a
line-by-line port of this module (``web/static/js/nlparse.js``); both are checked
against the same fixture file (``tests/fixtures/nl_capture_cases.json``).

The parser is intentionally conservative: anything it cannot determine is left
out and reported in ``unresolved`` so the UI can ask a human question instead of
guessing. It never invents identifiers and never touches storage.

Moments (a date and/or a time of day) are assigned a role from the words around
them:

* ``deadline`` – "к", "до", "by", "due", "сдать …"  → ``actual_cutoff`` (KNOWN)
* ``remind``   – "напомни …", "remind me …"        → ``remind_at``
* ``start``    – "начать с", "не раньше", "after"   → ``actionable_from``
* ``when``     – a plain "завтра вечером сделать …" → work window
  (``actionable_from`` = window start, ``target_at`` = soft finish)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---- vocabulary ----------------------------------------------------------------------

_NUM_WORDS = {
    # ru (nominative/accusative/genitive forms used in speech)
    "ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "одной": 1,
    "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4, "четырех": 4,
    "пять": 5, "пяти": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7,
    "восемь": 8, "восьми": 8, "девять": 9, "девяти": 9, "десять": 10, "десяти": 10,
    "одиннадцать": 11, "одиннадцати": 11, "двенадцать": 12, "двенадцати": 12,
    "пятнадцать": 15, "пятнадцати": 15, "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30, "сорок": 40, "сорока": 40, "пятьдесят": 50,
    "полтора": 1.5, "полторы": 1.5, "пару": 2, "пары": 2, "несколько": 3,
    # en
    "zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "couple": 2, "few": 3, "several": 3,
}
_NUM_ALT = "|".join(sorted((re.escape(k) for k in _NUM_WORDS), key=len, reverse=True))
_NUM = rf"(?:\d+(?:[.,]\d+)?|{_NUM_ALT})"

_H_UNIT = r"(?:часик\w*|час(?:а|ов)?|ч\.?|hours?|hrs?|h)"
_M_UNIT = r"(?:минут\w*|мин\.?|minutes?|mins?|m)"

_WEEKDAYS = {
    "понедельник": 0, "понедельника": 0, "пн": 0, "monday": 0, "mon": 0,
    "вторник": 1, "вторника": 1, "вт": 1, "tuesday": 1, "tue": 1, "tues": 1,
    "среда": 2, "среду": 2, "среды": 2, "ср": 2, "wednesday": 2, "wed": 2,
    "четверг": 3, "четверга": 3, "чт": 3, "thursday": 3, "thu": 3, "thurs": 3,
    "пятница": 4, "пятницу": 4, "пятницы": 4, "пт": 4, "friday": 4, "fri": 4,
    "суббота": 5, "субботу": 5, "субботы": 5, "сб": 5, "saturday": 5, "sat": 5,
    "воскресенье": 6, "воскресенья": 6, "вс": 6, "sunday": 6, "sun": 6,
    # dative ("к пятнице") and prepositional ("в среде" is rare, "к среде" is common)
    "понедельнику": 0, "вторнику": 1, "среде": 2, "четвергу": 3, "пятнице": 4, "субботе": 5, "воскресенью": 6,
}
_WD_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
    "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RU_MONTH = r"(январ[ья]|феврал[ья]|марта?|апрел[ья]|ма[йя]|июн[ья]|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья])"
_EN_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

# Day parts: (window start, window end, reminder/deadline anchor)
_PARTS = {
    "morning": (time(8, 0), time(12, 0), time(9, 0)),
    "noon": (time(12, 0), time(12, 0), time(12, 0)),
    "afternoon": (time(12, 0), time(17, 0), time(14, 0)),
    "evening": (time(18, 0), time(22, 0), time(19, 0)),
    "night": (time(21, 0), time(23, 59), time(22, 0)),
    "midnight": (time(23, 59), time(23, 59), time(23, 59)),
}
_DEADLINE_PART = {"morning": time(9, 0), "noon": time(12, 0), "afternoon": time(15, 0),
                  "evening": time(18, 0), "night": time(23, 0), "midnight": time(23, 59)}

_PART_WORDS = [
    (r"с\s+утра|утром|утра|in\s+the\s+morning|this\s+morning|morning", "morning"),
    (r"в\s+полдень|к\s+полудню|до\s+полудня|полдень|at\s+noon|by\s+noon|noon|midday", "noon"),
    (r"к\s+обеду|до\s+обеда|после\s+обеда|днем|днём|in\s+the\s+afternoon|afternoon", "afternoon"),
    (r"к\s+вечеру|до\s+вечера|вечером|вечера|сегодня\s+вечером|tonight|in\s+the\s+evening|evening", "evening"),
    (r"ночью|на\s+ночь|at\s+night|night", "night"),
    (r"в\s+полночь|до\s+полуночи|к\s+полуночи|at\s+midnight|by\s+midnight|midnight", "midnight"),
]

_CATEGORY_RULES = [
    ("EXAM", r"экзамен|зач[её]т|контрольн|коллоквиум|сесси[яию]|\bexam|midterm|\bfinals?\b|\bquiz|\btest\b"),
    ("HOMEWORK", r"домашк|домашн|\bдз\b|д/з|лаб(?:а|у|ы|ораторн)|реферат|курсов|курсач|эссе|доклад|презентаци|слайд|конспект|задачи\s+по|"
                 r"homework|assignment|\blab\b|essay|problem\s+set|pset|coursework|\bthesis"),
    ("ADMIN", r"справк|документ|заявлени|оплат|деканат|стипенди|паспорт|виз[аыу]\b|анкет|"
              r"paperwork|documents?|\bpay\b|\bbill|application|\bform\b|\bvisa\b|register"),
    ("PERSONAL_APPOINTMENT", r"врач|стоматолог|парикмахер|записаться|больниц|поликлиник|"
                             r"doctor|dentist|appointment|haircut|clinic"),
    ("MEETING", r"встреч|созвон|собрани|\bmeeting|\bcall\b|meet\s+with"),
    ("ERRAND", r"купить|магазин|забрать|отнести|почт[аеуы]|аптек|"
               r"\bbuy\b|pick\s+up|groceries|\bshop"),
    ("WORK", r"работ[аеуы]|отч[её]т|проект|клиент|\breport|\bwork\b|project|client"),
    ("LESSON", r"\bпар[аеуы]\b|лекци|семинар|занятие|урок|lecture|\bclass\b|seminar|lesson"),
]

_FILLERS = {
    "надо", "нужно", "необходимо", "мне", "я", "пожалуйста", "плиз", "это", "а", "и", "но", "ещё", "еще",
    "please", "i", "need", "to", "have", "must", "should", "gotta", "it", "and", "also",
    "напомни", "напомнить", "напоминание", "remind", "me", "reminder",
}
# Tokens that are numbers but never a clock time ("в пару кликов", "at a café").
_NOT_CLOCK = {"a", "an", "couple", "few", "several", "пару", "пары", "несколько", "полтора", "полторы", "ноль", "zero"}
# Counted nouns: "в 2 этапа", "at 3 pages" are quantities, not times of day.
_COUNTED = r"задач|этап|раз|страниц|глав|упражнен|пункт|част|шаг|урок|steps?|pages?|chapters?|tasks?|parts?|times?"
_EDGE_PREPS = {"в", "во", "к", "ко", "до", "на", "по", "с", "со", "за", "at", "by", "on", "in", "for", "until", "before", "from"}

_EXAM_DATIVE = {"экзамен": "экзамену", "зачет": "зачёту", "зачёт": "зачёту", "контрольная": "контрольной",
                "контрольную": "контрольной", "коллоквиум": "коллоквиуму", "тест": "тесту", "сессия": "сессии", "сессию": "сессии"}


@dataclass
class _Piece:
    start: int
    end: int
    kind: str                     # "date" | "time" | "part" | "instant" | "range"
    value: object
    cue: str | None = None        # "deadline" | "remind" | "start" | "target" | None
    date_only_relative: bool = False  # "через неделю": a span, not a named day
    same_weekday: bool = False        # "в пятницу" said on a Friday


@dataclass
class _Moment:
    pieces: list[_Piece] = field(default_factory=list)

    @property
    def start(self) -> int:
        return min(p.start for p in self.pieces)

    def get(self, kind: str):
        return next((p for p in self.pieces if p.kind == kind), None)

    @property
    def cue(self) -> str | None:
        cues = [p.cue for p in self.pieces if p.cue]
        for wanted in ("remind", "deadline", "start", "target"):
            if wanted in cues:
                return wanted
        return None


def _num(token: str) -> float | None:
    token = token.strip().lower().replace(",", ".")
    if token in _NUM_WORDS:
        return float(_NUM_WORDS[token])
    try:
        return float(token)
    except ValueError:
        return None


def _normalize(text: str) -> str:
    """Lowercase with ё→е while keeping every index aligned with the original."""
    out = []
    for char in text:
        lower = char.lower()
        if len(lower) != 1:
            lower = char
        out.append("е" if lower == "ё" else lower)
    return "".join(out)


class _Parser:
    def __init__(self, text: str, now: datetime, zone: ZoneInfo) -> None:
        self.raw = text
        self.low = _normalize(text)
        self.now = now.astimezone(zone)
        self.zone = zone
        self.taken = [False] * len(text)
        self.pieces: list[_Piece] = []

    # -- span bookkeeping ---------------------------------------------------------------

    def free(self, start: int, end: int) -> bool:
        return not any(self.taken[start:end])

    def take(self, start: int, end: int) -> None:
        for index in range(start, end):
            self.taken[index] = True

    def scan(self, pattern: str, flags: int = 0):
        for match in re.finditer(pattern, self.low, flags):
            if match.end() > match.start() and self.free(match.start(), match.end()):
                yield match

    # -- recognisers --------------------------------------------------------------------

    def importance(self) -> str | None:
        rules = [
            ("LOW", r"(?:это\s+)?(?:не\s+срочно|не\s+важно|неважно|не\s+горит|когда-нибудь|когда\s+будет\s+время|"
                    r"not\s+urgent|low\s+priority|no\s+rush|not\s+important|whenever|someday)"),
            ("CRITICAL", r"(?:это\s+)?(?:очень|супер|крайне)\s+(?:важно|срочно)|критично|горит|"
                         r"(?:very|super|extremely)\s+(?:important|urgent)|critical"),
            ("HIGH", r"(?:это\s+)?(?:важн(?:о|ая|ый|ое)|срочн(?:о|ая|ый|ое))|(?:it'?s\s+)?(?:important|urgent|asap|high\s+priority)"),
        ]
        found = None
        for level, pattern in rules:
            for match in self.scan(rf"(?<![\w-])(?:{pattern})(?![\w-])\s*!*"):
                self.take(match.start(), match.end())
                found = found or level
        if found is None:
            bangs = re.search(r"!{2,}\s*$", self.low)
            if bangs:
                self.take(bangs.start(), bangs.end())
                found = "HIGH"
        return found

    def no_deadline(self) -> bool:
        hit = False
        for match in self.scan(r"(?<!\w)(?:без\s+(?:дедлайна|срока)|no\s+deadline|without\s+(?:a\s+)?deadline)(?!\w)"):
            self.take(match.start(), match.end())
            hit = True
        return hit

    def relative(self) -> None:
        """"через 2 часа", "через неделю", "in 3 days", "in an hour"."""
        pattern = (rf"(?<!\w)(?:(к|до|by|within)\s+)?(?:через|in)\s+(?:({_NUM})\s+)?"
                   r"(минут\w*|мин|час\w*|дн(?:я|ей)|день|сутки|недел\w*|месяц\w*|minutes?|mins?|hours?|days?|weeks?|months?)(?!\w)")
        for match in self.scan(pattern):
            amount = _num(match.group(2)) if match.group(2) else 1.0
            if amount is None:
                continue
            unit = match.group(3)
            cue = "deadline" if match.group(1) else None
            if unit.startswith(("мин", "min")):
                value = self.now + timedelta(minutes=amount)
                kind, date_only = "instant", False
            elif unit.startswith(("час", "hour")):
                value = self.now + timedelta(minutes=round(amount * 60))
                kind, date_only = "instant", False
            elif unit.startswith(("д", "сут", "day")):
                value, kind, date_only = self.now.date() + timedelta(days=round(amount)), "date", True
            elif unit.startswith(("нед", "week")):
                value, kind, date_only = self.now.date() + timedelta(days=round(amount * 7)), "date", True
            else:
                value, kind, date_only = _add_months(self.now.date(), round(amount)), "date", True
            self.take(match.start(), match.end())
            self.pieces.append(_Piece(match.start(), match.end(), kind, value, cue, date_only))

    def period_ends(self) -> None:
        """"до конца недели", "by the end of the month", "на этой неделе", "next week"."""
        rules = [
            (r"(?:к|до)\s+конца\s+(?:этой\s+)?недели|by\s+the\s+end\s+of\s+(?:the|this)\s+week|by\s+end\s+of\s+week", "week_end", "deadline"),
            (r"(?:к|до)\s+конца\s+(?:этого\s+)?месяца|by\s+the\s+end\s+of\s+(?:the|this)\s+month", "month_end", "deadline"),
            (r"на\s+этой\s+неделе|this\s+week", "week_end", "target"),
            (r"на\s+следующей\s+неделе|next\s+week", "next_week_end", "target"),
        ]
        for pattern, what, cue in rules:
            for match in self.scan(rf"(?<!\w)(?:{pattern})(?!\w)"):
                today = self.now.date()
                if what == "week_end":
                    value = today + timedelta(days=6 - today.weekday())
                elif what == "next_week_end":
                    value = today + timedelta(days=13 - today.weekday())
                else:
                    value = _add_months(today.replace(day=1), 1) - timedelta(days=1)
                self.take(match.start(), match.end())
                self.pieces.append(_Piece(match.start(), match.end(), "date", value, cue, True))

    def dates(self) -> None:
        today = self.now.date()
        prep = r"(?:(к|до|в|во|на|by|on|before|until|from|after|с|со|после)\s+)?"
        for match in self.scan(rf"(?<!\w){prep}(сегодня|завтра|послезавтра|today|tomorrow|day\s+after\s+tomorrow)(?!\w)"):
            word = match.group(2)
            offset = 0 if word in {"сегодня", "today"} else 2 if word in {"послезавтра", "day after tomorrow"} else 1
            self._date_piece(match, today + timedelta(days=offset), match.group(1))
        for match in self.scan(rf"(?<!\w){prep}(?:(следующ\w*|ближайш\w*|этот|эту|это|next|this)\s+)?({_WD_ALT})(?!\w)"):
            target = _WEEKDAYS[match.group(3)]
            days = (target - today.weekday()) % 7
            modifier = match.group(2) or ""
            if modifier.startswith(("следующ", "next")):
                days = days + 7 if days else 7
            self._date_piece(match, today + timedelta(days=days), match.group(1), weekday_same_day=days == 0)
        ru = rf"(?<!\w){prep}(\d{{1,2}})\s+{_RU_MONTH}(?:\s+(\d{{4}}))?(?:\s*г(?:ода|\.)?)?(?!\w)"
        for match in self.scan(ru):
            self._calendar_piece(match, int(match.group(2)), _month(match.group(3)), match.group(4), match.group(1))
        en1 = rf"(?<!\w){prep}{_EN_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?(?!\w)"
        for match in self.scan(en1):
            self._calendar_piece(match, int(match.group(3)), _month(match.group(2)), match.group(4), match.group(1))
        en2 = rf"(?<!\w){prep}(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_EN_MONTH}(?:,?\s+(\d{{4}}))?(?!\w)"
        for match in self.scan(en2):
            self._calendar_piece(match, int(match.group(2)), _month(match.group(3)), match.group(4), match.group(1))
        for match in self.scan(rf"(?<![\w.:]){prep}(\d{{4}})-(\d{{2}})-(\d{{2}})(?![\w:])"):
            self._calendar_piece(match, int(match.group(4)), int(match.group(3)), match.group(2), match.group(1))
        for match in self.scan(rf"(?<![\w.:]){prep}(\d{{1,2}})[./](\d{{1,2}})(?:[./](\d{{2,4}}))?(?![\w:]|\.\d)"):
            day, month = int(match.group(2)), int(match.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                self._calendar_piece(match, day, month, match.group(4), match.group(1))

    def _date_piece(self, match, value: date, preposition: str | None, *, weekday_same_day: bool = False) -> None:
        self.take(match.start(), match.end())
        piece = _Piece(match.start(), match.end(), "date", value, _cue(preposition))
        piece.same_weekday = weekday_same_day
        self.pieces.append(piece)

    def _calendar_piece(self, match, day: int, month: int | None, year: str | None, preposition: str | None) -> None:
        if month is None:
            return
        today = self.now.date()
        try:
            if year:
                value = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
            else:
                value = date(today.year, month, day)
                if value < today:
                    value = date(today.year + 1, month, day)
        except ValueError:
            return
        self.take(match.start(), match.end())
        self.pieces.append(_Piece(match.start(), match.end(), "date", value, _cue(preposition)))

    def times(self) -> None:
        prep = r"(?:(к|до|в|во|около|после|с|со|не\s+позже|не\s+позднее|не\s+раньше|at|by|before|until|after|from|around)\s+)?"
        suffix = r"(?:\s*(утра|дня|вечера|ночи|am|pm|a\.m\.|p\.m\.))?"
        # 18:00, 9.30 pm, в 18:30
        for match in self.scan(rf"(?<![\w.:]){prep}(\d{{1,2}})[:.](\d{{2}})(?![\w.:]){suffix}"):
            hour, minute = int(match.group(2)), int(match.group(3))
            if hour < 24 and minute < 60:
                self._time_piece(match, _hour(hour, match.group(4), explicit=True), minute, match.group(1))
        # 6pm, 6 p.m.
        for match in self.scan(rf"(?<![\w.:]){prep}(\d{{1,2}})\s*(am|pm|a\.m\.|p\.m\.)(?!\w)"):
            self._time_piece(match, _hour(int(match.group(2)), match.group(3), explicit=True), 0, match.group(1))
        # "в 6 вечера", "к шести", "at six", "в 18 часов" — a preposition is required.
        words = rf"(\d{{1,2}}|{_NUM_ALT}|часу)"
        pattern = (rf"(?<!\w)(к|до|в|во|около|после|с|со|не\s+позже|не\s+позднее|at|by|before|until|after|around)\s+{words}"
                   rf"(?:\s*(?:час(?:а|ов|ам|у)?|ч\.?|o'?clock))?{suffix}(?!\s*(?:минут|мин|hours?|mins?|minutes?|дн|недел|week|day|{_COUNTED}))(?!\w)")
        for match in self.scan(pattern):
            token = match.group(2)
            if token in _NOT_CLOCK:
                continue
            if token == "часу":
                hour = 13
            else:
                number = _num(token)
                if number is None or number != int(number) or not 0 <= number <= 23:
                    continue
                hour = _hour(int(number), match.group(3), explicit=bool(match.group(3)))
            self._time_piece(match, hour, 0, match.group(1))
        # day parts
        for pattern, part in _PART_WORDS:
            for match in self.scan(rf"(?<!\w)(?:(к|до|by|before|until|after|после)\s+)?(?:{pattern})(?!\w)"):
                cue = _cue(match.group(1))
                text = match.group(0)
                if cue is None and re.match(r"(?:к|до)\s", text):
                    cue = "deadline"
                if cue is None and re.match(r"(?:после)\s", text):
                    cue = "start"
                self.take(match.start(), match.end())
                self.pieces.append(_Piece(match.start(), match.end(), "part", part, cue))

    def _time_piece(self, match, hour: int, minute: int, preposition: str | None) -> None:
        self.take(match.start(), match.end())
        self.pieces.append(_Piece(match.start(), match.end(), "time", time(hour % 24, minute), _cue(preposition)))

    def effort(self) -> int | None:
        prefix = (r"(?:(?:это\s+)?(?:займ[её]т|займут|потребует(?:ся)?|уйд[её]т|нужно|надо|нужен|нужна)\s+|"
                  r"(?:it\s+)?(?:will\s+)?(?:takes?|need(?:s)?)\s+|на\s+|for\s+)?"
                  r"(?:(?:примерно|около|где-то|где\s+то|приблизительно|about|around|roughly|approx\.?|~)\s*)?")
        tail = r"(?:\s+работы|\s+of\s+work)?"
        found: int | None = None

        def record(match, minutes: float) -> None:
            nonlocal found
            self.take(match.start(), match.end())
            if found is None and minutes > 0:
                found = int(round(minutes))

        for match in self.scan(rf"(?<!\w){prefix}(\d+)\s*{_H_UNIT}\s*(?:и\s+)?(\d+)\s*{_M_UNIT}(?!\w){tail}"):
            record(match, int(match.group(1)) * 60 + int(match.group(2)))
        for match in self.scan(rf"(?<!\w){prefix}(?:полтора\s+часа|час\s+с\s+половиной|(?:an?\s+)?hour\s+and\s+a\s+half|1\.5\s*(?:h|hours?|ч))(?!\w){tail}"):
            record(match, 90)
        for match in self.scan(rf"(?<!\w){prefix}(?:полчаса|пол\s+часа|half\s+an\s+hour|half\s+hour)(?!\w){tail}"):
            record(match, 30)
        for match in self.scan(rf"(?<!\w){prefix}({_NUM})\s*(?:-?\s*)(часик\w*|час(?:а|ов)?|ч\.?|hours?|hrs?|h)(?!\w){tail}"):
            value = _num(match.group(1))
            if value is not None and value <= 24:
                record(match, value * 60)
        for match in self.scan(rf"(?<!\w){prefix}({_NUM})\s*(?:-?\s*)(минут\w*|мин\.?|minutes?|mins?|m)(?!\w){tail}"):
            value = _num(match.group(1))
            if value is not None and value <= 600 and not (match.group(2) == "m" and not match.group(1)[0].isdigit()):
                record(match, value)
        # "часа два", "минут двадцать" — the inverted order means "about".
        for match in self.scan(rf"(?<!\w){prefix}(час(?:а|ов)?|минут)\s+({_NUM_ALT}|\d+)(?!\w){tail}"):
            value = _num(match.group(2))
            if value is not None:
                record(match, value * 60 if match.group(1).startswith("час") else value)
        # "займёт час", "на час", "an hour" with an explicit effort cue.
        cue_prefix = r"(?:(?:это\s+)?(?:займ[её]т|потребует(?:ся)?|уйд[её]т|нужен|нужно|надо)\s+(?:примерно\s+|около\s+)?|на\s+|(?:takes?|for|need)\s+)"
        for match in self.scan(rf"(?<!\w){cue_prefix}(?:(?:целый|один)\s+)?(час|часик|an\s+hour|one\s+hour|a\s+minute)(?!\w)"):
            record(match, 60 if "час" in match.group(1) or "hour" in match.group(1) else 1)
        for match in self.scan(r"(?<!\w)(?:весь\s+день|целый\s+день|all\s+day)(?!\w)"):
            record(match, 360)
        return found

    def chunking(self) -> tuple[bool | None, int | None]:
        whole = list(self.scan(r"(?<!\w)(?:за\s+один\s+раз|одним\s+куском|за\s+раз|in\s+one\s+(?:go|sitting))(?!\w)"))
        for match in whole:
            self.take(match.start(), match.end())
        if whole:
            return False, None
        parts = list(self.scan(rf"(?<!\w)(?:по\s+частям|частями|in\s+(?:chunks|parts))(?:\s*(?:по\s+)?({_NUM})\s*{_M_UNIT})?(?!\w)"))
        parts += list(self.scan(rf"(?<!\w)(?:по|in\s+(?:blocks|chunks)\s+of)\s+(\d+)\s*{_M_UNIT}(?!\w)"))
        size = None
        for match in parts:
            self.take(match.start(), match.end())
            if match.lastindex and match.group(1):
                size = int(_num(match.group(1)) or 0) or None
        return (True, size) if parts else (None, None)

    def reminder_cue(self) -> list[tuple[int, int]]:
        spans = []
        for match in self.scan(r"(?<!\w)(?:напомни(?:те)?(?:\s+мне)?|напомнить(?:\s+мне)?|поставь\s+напоминание|напоминание|remind\s+me(?:\s+to)?|reminder)(?!\w)"):
            self.take(match.start(), match.end())
            spans.append((match.start(), match.end()))
        return spans

    def deadline_words(self) -> bool:
        return bool(re.search(r"(?<!\w)(?:сдать|сдача|сдаю|дедлайн\w*|срок\w*|отправить\s+до|due|deadline|submit|hand\s+in|turn\s+in)(?!\w)", self.low))

    def strip_deadline_labels(self) -> None:
        for match in self.scan(r"(?<!\w)(?:дедлайн|срок(?:\s+сдачи)?|deadline|due(?:\s+date)?)\s*[:\-—]?\s*(?=\S)"):
            self.take(match.start(), match.end())

    def start_words(self) -> None:
        for match in self.scan(r"(?<!\w)(?:начать|начну|не\s+раньше|starting|start|not\s+before)(?=\s+(?:с|со|в|во|on|at|from|$))"):
            self.take(match.start(), match.end())
            self.pieces.append(_Piece(match.start(), match.end(), "cue", None, "start"))


def _cue(preposition: str | None) -> str | None:
    if not preposition:
        return None
    preposition = " ".join(preposition.split())
    if preposition in {"к", "до", "by", "before", "until", "не позже", "не позднее"}:
        return "deadline"
    if preposition in {"после", "after", "from", "с", "со", "не раньше"}:
        return "start"
    return None


def _hour(hour: int, suffix: str | None, *, explicit: bool) -> int:
    suffix = (suffix or "").replace(".", "")
    if suffix in {"pm", "вечера", "дня"} and hour < 12:
        return hour + 12
    if suffix == "ночи":
        return hour + 12 if 9 <= hour <= 11 else (0 if hour == 12 else hour)
    if suffix in {"am", "утра"}:
        return 0 if hour == 12 else hour
    if explicit and suffix == "":
        # "18:00" style: take it literally.
        return hour
    # Bare "к шести", "at 6": students mean the afternoon/evening for 1–7.
    return hour + 12 if 1 <= hour <= 7 else hour


def _month(token: str | None) -> int | None:
    if not token:
        return None
    token = token.lower()
    for prefix, number in sorted(_MONTHS.items(), key=lambda item: -len(item[0])):
        if token.startswith(prefix):
            return number
    return None


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    for day in (value.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def _group(pieces: list[_Piece], low: str) -> list[_Moment]:
    """Adjacent date/time pieces ("в пятницу к шести", "завтра вечером") form one moment."""
    ordered = sorted((p for p in pieces if p.kind != "cue"), key=lambda p: p.start)
    moments: list[_Moment] = []
    for piece in ordered:
        if moments:
            last = moments[-1]
            gap = low[max(p.end for p in last.pieces):piece.start]
            kinds = {p.kind for p in last.pieces}
            compatible = not (piece.kind in kinds or (piece.kind == "instant") or ("instant" in kinds)
                              or (piece.kind in {"time", "part"} and kinds & {"time", "part"}))
            if compatible and re.fullmatch(r"[\s,]*(?:(?:и|в|во|к|до|на|at|on|by)\s*)?[\s,]*", gap):
                last.pieces.append(piece)
                continue
        moments.append(_Moment([piece]))
    for cue in (p for p in pieces if p.kind == "cue"):
        following = [m for m in moments if m.start >= cue.end]
        if following:
            following[0].pieces.append(_Piece(cue.start, cue.end, "cue", None, cue.cue))
    return moments


class NaturalTaskParser:
    """Stateless façade; ``parse`` returns a task.create-compatible proposal."""

    def parse(self, text: str, *, now: datetime, timezone_name: str = "UTC") -> dict[str, object]:
        try:
            zone = ZoneInfo(timezone_name or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("UTC")
        clean_lines = [line.strip() for line in str(text or "").strip().splitlines() if line.strip()]
        if not clean_lines:
            return {"title": "", "unresolved": ["title"]}
        head = " ".join(clean_lines[0].split())
        head = re.sub(r"^(?:задача|задачу|task|todo|to-do)\s*:\s*", "", head, flags=re.IGNORECASE)
        description = "\n".join(clean_lines[1:]) or None
        parser = _Parser(head, now, zone)

        importance = parser.importance()
        absent = parser.no_deadline()
        remind_spans = parser.reminder_cue()
        parser.strip_deadline_labels()
        parser.start_words()
        parser.relative()
        parser.period_ends()
        parser.dates()
        parser.times()
        splittable, chunk = parser.chunking()
        effort = parser.effort()

        category = "GENERAL"
        for name, pattern in _CATEGORY_RULES:
            if re.search(pattern, parser.low):
                category = name
                break

        fields: dict[str, object] = {}
        moments = _group(parser.pieces, parser.low)
        has_deadline_words = parser.deadline_words()
        cutoff_date_only = False
        for moment in moments:
            role = moment.cue
            if role is None and remind_spans and any(end <= moment.start and moment.start - end <= 3 for _, end in remind_spans):
                role = "remind"
            if role is None and remind_spans and "remind_at" not in fields and not has_deadline_words:
                role = "remind"
            if role is None and (has_deadline_words or category == "EXAM") and "actual_cutoff" not in fields:
                role = "deadline"
            if role is None and "actual_cutoff" not in fields and all(p.kind == "date" and p.date_only_relative for p in moment.pieces):
                # "через неделю", "in 3 days": a span to finish within, not a day to start on.
                role = "deadline"
            role = role or "when"
            resolved = self._resolve(moment, role, parser.now, zone, category, effort)
            if resolved is None:
                continue
            key, value, date_only = resolved
            if key == "window":
                start, finish = value
                fields.setdefault("actionable_from", start)
                if finish is not None:
                    fields.setdefault("target_at", finish)
            elif key not in fields:
                fields[key] = value
                if key == "actual_cutoff":
                    cutoff_date_only = date_only

        title = _title(parser, category)
        unresolved: list[str] = []
        if not title:
            unresolved.append("title")
        if effort is None:
            unresolved.append("estimated_total_effort_minutes")
        if "actual_cutoff" in fields:
            cutoff = {"state": "KNOWN", "at": _iso(fields.pop("actual_cutoff")), "boundary": "INCLUSIVE"}
        elif absent:
            cutoff = {"state": "ABSENT"}
        elif "remind_at" in fields or "actionable_from" in fields or "target_at" in fields:
            # A reminder or a planned slot without any deadline words: nothing is due.
            cutoff = {"state": "ABSENT"}
        else:
            cutoff = {"state": "UNKNOWN"}
            unresolved.append("actual_cutoff")
        if splittable is None:
            splittable = bool(effort and effort >= 120 and category in {"EXAM", "HOMEWORK", "WORK", "GENERAL"})
        payload: dict[str, object] = {
            "title": title,
            "description": description,
            "category": category,
            "importance": importance or "NORMAL",
            "estimated_total_effort_minutes": effort,
            "actual_cutoff": cutoff,
            "target_at": _iso(fields.get("target_at")),
            "actionable_from": _iso(fields.get("actionable_from")),
            "remind_at": _iso(fields.get("remind_at")),
            "splittable": bool(splittable),
            "min_chunk_minutes": None,
            "max_chunk_minutes": None,
        }
        if payload["splittable"]:
            payload["min_chunk_minutes"] = min(30, effort or 30)
            payload["max_chunk_minutes"] = chunk or max(payload["min_chunk_minutes"], min(90, effort or 90))
        payload["unresolved"] = unresolved
        payload["cutoff_time_assumed"] = cutoff_date_only
        return payload

    @staticmethod
    def _resolve(moment: _Moment, role: str, now: datetime, zone: ZoneInfo, category: str, effort: int | None):
        instant = moment.get("instant")
        day_piece = moment.get("date")
        clock = moment.get("time")
        part = moment.get("part")
        if instant is not None:
            value = instant.value.replace(second=0, microsecond=0)
            key = {"deadline": "actual_cutoff", "remind": "remind_at", "start": "actionable_from", "target": "target_at"}.get(role)
            if key:
                return key, value, False
            return "window", (value, value + timedelta(minutes=effort) if effort else None), False
        today = now.date()
        day = day_piece.value if day_piece else None
        date_only = clock is None and part is None

        def at(d: date, t: time) -> datetime:
            return datetime.combine(d, t, zone)

        def next_occurrence(t: time) -> datetime:
            candidate = at(day or today, t)
            if day is None and candidate <= now:
                candidate = at(today + timedelta(days=1), t)
            return candidate

        if role == "deadline":
            if clock is not None:
                value = next_occurrence(clock.value)
            elif part is not None:
                value = next_occurrence(_DEADLINE_PART[part.value])
            else:
                value = at(day, time(9, 0) if category == "EXAM" else time(23, 59))
            if day_piece is not None and day_piece.same_weekday and value <= now:
                value += timedelta(days=7)
            return "actual_cutoff", value, date_only
        if role == "remind":
            if clock is not None:
                value = next_occurrence(clock.value)
            elif part is not None:
                value = next_occurrence(_PARTS[part.value][2])
            else:
                value = at(day, time(9, 0))
                if value <= now:
                    value = now.replace(second=0, microsecond=0) + timedelta(minutes=5)
            return "remind_at", value, date_only
        if role in {"start", "target"}:
            key = "actionable_from" if role == "start" else "target_at"
            if clock is not None:
                value = next_occurrence(clock.value)
            elif part is not None:
                window = _PARTS[part.value]
                value = next_occurrence(window[0] if role == "start" else window[1])
            else:
                value = at(day, time(0, 0) if role == "start" else time(23, 59))
            return key, value, date_only
        # "when": a planned work window.
        if clock is not None:
            start = next_occurrence(clock.value)
            return "window", (start, start + timedelta(minutes=effort) if effort else None), False
        if part is not None:
            start_t, end_t, _ = _PARTS[part.value]
            start = next_occurrence(start_t)
            finish = at(start.astimezone(zone).date(), end_t)
            return "window", (start, finish if finish > start else None), False
        if day is None:
            return None
        start = at(day, time(0, 0))
        if start < now:
            start = now.replace(second=0, microsecond=0)
        return "window", (start, at(day, time(23, 59))), True


def _title(parser: _Parser, category: str) -> str:
    kept = "".join(char if not parser.taken[index] else " " for index, char in enumerate(parser.raw))
    words = re.findall(r"[^\s]+", kept)
    # Drop fillers and dangling prepositions at both edges, repeatedly.
    def bare(word: str) -> str:
        return _normalize(word.strip(",.;:!?—-–()\"«»"))
    changed = True
    while words and changed:
        changed = False
        while words and (bare(words[0]) in _FILLERS | _EDGE_PREPS or not bare(words[0])):
            words.pop(0)
            changed = True
        while words and (bare(words[-1]) in _FILLERS | _EDGE_PREPS or not bare(words[-1])):
            words.pop()
            changed = True
    # Fillers in the middle that only separate clauses ("экзамен, надо подготовиться").
    words = [w for i, w in enumerate(words) if not (bare(w) in {"надо", "нужно", "мне", "это", "please"} and 0 < i)]
    title = " ".join(words)
    title = re.sub(r"\s+([,.;:!?])", r"\1", title)
    title = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", title)
    title = title.strip(" ,.;:!?—-–")
    if category == "EXAM":
        title = _exam_title(title)
    if title:
        title = title[0].upper() + title[1:]
    return title[:300]


def _exam_title(title: str) -> str:
    """"экзамен, подготовиться" → "подготовиться к экзамену"."""
    low = _normalize(title)
    match = re.match(r"^(экзамен|зачет|контрольная|контрольную|коллоквиум|тест|сессия|сессию)((?:\s+по\s+[^,]+?)?)\s*,?\s*(подготовиться|готовиться)$", low)
    if match:
        noun = _EXAM_DATIVE.get(match.group(1), match.group(1))
        return f"{match.group(3)} к {noun}{title[match.end(2) - len(match.group(2)):match.end(2)]}".strip()
    match = re.match(r"^(exam|test|quiz|midterm|final)((?:\s+in\s+[^,]+?)?)\s*,?\s*(?:to\s+)?(prepare|study)$", low)
    if match:
        return f"{match.group(3)} for {match.group(1)}{match.group(2)}".strip()
    return title


def _iso(value: object) -> str | None:
    if value is None:
        return None
    assert isinstance(value, datetime)
    return value.astimezone(timezone.utc).isoformat()


def parse_task(text: str, *, now: datetime, timezone_name: str = "UTC") -> dict[str, object]:
    return NaturalTaskParser().parse(text, now=now, timezone_name=timezone_name)
