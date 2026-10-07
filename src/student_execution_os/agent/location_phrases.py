"""«Когда приду домой, напомни разобрать вещи» → a typed place-enter trigger (ADR 0035).

Deterministic RU/EN reading of the few phrasings that tie a reminder to arriving at
or leaving one of the user's places. Mirrored on the device (``js/location-phrases.js``,
same fixture). The place is matched against the user's own places by name; when none
fits, the words are kept and the user picks or creates the place — no place, address
or position is ever invented.
"""
from __future__ import annotations

import re

L = r"(?<![0-9a-zа-я])"
R = r"(?![0-9a-zа-я])"
# (pattern, transition). Group "place" is the user's words for the place.
_PATTERNS = [
    (rf"{L}когда\s+(?:я\s+)?(?:приду|вернусь|буду|окажусь|доберусь|приеду|зайду)\s+(?:домой|(?:в|во|на|к|у)\s+(?P<place>[^,.!?]+?)){R}"
     rf"(?=\s*(?:,|напомн|скажи|$))", "ENTER"),
    (rf"{L}когда\s+(?:я\s+)?(?:уйду|выйду|уеду|выеду)\s+(?:из|с|со|от)\s+(?P<place>[^,.!?]+?){R}(?=\s*(?:,|напомн|скажи|$))",
     "EXIT"),
    (rf"{L}when\s+i\s+(?:get|arrive|am|come)\s+(?:home|(?:to|at|back\s+to|in)\s+(?:the\s+)?(?P<place>[^,.!?]+?)){R}"
     rf"(?=\s*(?:,|remind|$))", "ENTER"),
    (rf"{L}when\s+i\s+leave\s+(?:the\s+)?(?P<place>[^,.!?]+?){R}(?=\s*(?:,|remind|$))", "EXIT"),
]
_HOME_WORDS = {"домой", "home"}
_HOME_NAMES = ("дом", "home")
_REMIND = rf"{L}(?:напомни(?:те)?|напоминай|скажи|remind\s+me(?:\s+to)?|remind){R}(?:\s+(?:мне|me|to|о|про))?"


def _low(text: str) -> str:
    return text.lower().replace("ё", "е")


def _stem_match(a: str, b: str) -> bool:
    if a == b:
        return True
    shortest = min(len(a), len(b))
    if shortest < 3:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, shortest - 2)


def match_place(words: str, places: list[dict]) -> list[dict]:
    """The user's places whose alias or name the words name (all of the name's words)."""
    said = re.findall(r"[0-9a-zа-я]+", _low(words))
    if not said:
        return []
    found = []
    for place in places:
        for name in (place.get("alias"), place.get("display_name"), place.get("name")):
            tokens = re.findall(r"[0-9a-zа-я]+", _low(name or ""))
            if tokens and all(any(_stem_match(t, s) for s in said) for t in tokens):
                found.append(place)
                break
    return found


def parse_location_trigger(text: str, places: list[dict]) -> dict[str, object] | None:
    """A typed reading, or None when the text does not tie anything to a place transition."""
    raw = " ".join(str(text or "").split())
    if not raw or len(raw) > 300:
        return None
    low = _low(raw)
    if len(low) != len(raw):
        return None
    for pattern, transition in _PATTERNS:
        hit = re.search(pattern, low)
        if hit is None:
            continue
        place_words = (hit.group("place") or "").strip()
        home = not place_words or place_words in _HOME_WORDS
        if home:
            candidates = [p for p in places if any(_low(p.get(k) or "") in _HOME_NAMES
                                                  for k in ("alias", "display_name", "name"))]
            place_words = raw[hit.start():hit.end()].split()[-1] if not place_words else place_words
            place_words = "дом" if any("Ѐ" <= c <= "ӿ" for c in low) else "home"
        else:
            candidates = match_place(place_words, places)
        rest = raw[:hit.start()] + " " + raw[hit.end():]
        rest_low = _low(rest)
        remind = re.search(_REMIND, rest_low)
        if remind is not None:
            rest = rest[:remind.start()] + " " + rest[remind.end():]
        title = re.sub(r"\s+", " ", rest).strip(" ,.;:—-!")
        title = re.sub(r"^(?:и|and|что|to)\s+", "", title, flags=re.IGNORECASE).strip(" ,.;:—-!")
        if not title:
            return None
        result: dict[str, object] = {"kind": "LOCATION_TRIGGER", "transition": transition,
                                     "title": title[:1].upper() + title[1:], "place_text": place_words}
        if len(candidates) == 1:
            result["place_id"] = candidates[0]["id"]
            result["unresolved"] = []
        else:
            result["unresolved"] = ["place_id"]
        return result
    return None


_ADD_PLACE = re.compile(
    rf"^(?:добавь|создай|сохрани|запомни)\s+(?:новое\s+)?(?:место|адрес)\s+(?P<name>[^,:]+?)"
    rf"(?:\s*[,:]\s*(?:адрес\s+)?(?P<address>.+))?$|^(?:add|save|create)\s+(?:a\s+)?place\s+(?P<en_name>[^,:]+?)"
    rf"(?:\s*[,:]\s*(?:address\s+)?(?P<en_address>.+))?$")


def parse_place_create(text: str) -> dict[str, object] | None:
    """«Добавь место Спортзал», «добавь место Дом: Ленина 5» → CREATE_PLACE (the user's words only)."""
    raw = " ".join(str(text or "").split()).strip(" .!")
    low = _low(raw)
    if not raw or len(raw) > 300 or len(low) != len(raw):
        return None
    hit = _ADD_PLACE.match(low)
    if hit is None:
        return None
    span = "name" if hit.group("name") else "en_name"
    name = raw[hit.start(span):hit.end(span)].strip()
    if not name:
        return None
    result: dict[str, object] = {"display_name": name[:1].upper() + name[1:]}
    address_span = "address" if span == "name" else "en_address"
    if hit.group(address_span):
        result["address"] = raw[hit.start(address_span):hit.end(address_span)].strip()
    return result
