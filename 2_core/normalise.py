"""Normalisation for the master: PLZ, and street/city keys used for matching only.

The master shows the cleaned original text; these keys only decide which rows are the same address.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_PAREN_TAIL = re.compile(r"\(.*$")  # an unclosed "(bei ..." is a note, not address
_PAREN_CLOSED = re.compile(r"\([^)]*\)")  # "(bei Fa. X)" anywhere in the line is a note
_NR = re.compile(r"\bnr\.?\s*(?=\d)")
_NUMBER_AT_END = re.compile(r"^(?P<name>.*?)[\s,]*(?P<num>\d+\s*[a-z]?(?:\s*[-/]\s*\d+\s*[a-z]?)?)\s*$")


def fold(text) -> str:
    """Lower case, ß/umlauts to ss/ae/oe/ue, other accents removed. None/NaN -> ''."""
    if text is None or (isinstance(text, float) and text != text):
        return ""
    s = str(text).casefold().translate(_UMLAUTS)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def plz_clean(raw, slug: str = "", city: str = "", corrections: dict | None = None) -> tuple[str, str]:
    """(plz, how): strip; 4 digits are zero-padded to 5; an explicit correction (slug, raw, city) replaces the value.

    `how` is "" (unchanged), "padded" or "corrected". Nothing else is ever repaired (no regex guessing).
    """
    s = "" if raw is None else str(raw).strip()
    if corrections:
        fixed = corrections.get((slug, s, city_key(city)))
        if fixed is not None:
            return fixed, "corrected"
    if s.isdigit() and len(s) == 4:
        return s.zfill(5), "padded"
    return s, ""


def is_valid_plz(plz: str, known: set[str] | None) -> bool:
    """5 digits and (if a GeoNames set is given) known to GeoNames."""
    return len(plz) == 5 and plz.isdigit() and (known is None or plz in known)


def street_parts(street) -> tuple[str, str]:
    """(name_key, number_key) of a street line. '' for missing parts."""
    s = _PAREN_TAIL.sub("", _PAREN_CLOSED.sub(" ", fold(street))).replace("–", "-").replace("—", "-").strip(" ,.;")
    s = _NR.sub("", s)
    m = _NUMBER_AT_END.match(s)
    if m:
        name, num = m.group("name"), re.sub(r"\s+", "", m.group("num"))
    else:
        name, num = s, ""
    tokens = []
    for t in re.split(r"[\s.,;:'’\-/_]+", name):
        if not t:
            continue
        if t == "st":
            t = "sankt"
        elif t == "strasse":
            t = "str"
        elif t.endswith("strasse"):
            t = t[: -len("strasse")] + "str"
        tokens.append(t)
    return "".join(re.sub(r"[^0-9a-z]", "", t) for t in tokens), num


def street_key(street) -> str:
    """'<name>|<number>': Str./Straße/strasse (also as a suffix), St./Sankt, umlauts, hyphen/space and 16A = 16 a alike."""
    name, num = street_parts(street)
    return f"{name}|{num}"


def address_key(street, plz) -> str:
    return f"{street_key(street)}@{str(plz).strip()}"


def has_street(street) -> bool:
    return bool(street_parts(street)[0])


def city_keys(city) -> set[str]:
    """The city and its base (before '-', '/', '('), folded: 'Bielefeld-Brackwede' matches 'Bielefeld'."""
    s = _PAREN_TAIL.sub("", fold(city)).strip()
    if not s:
        return set()
    full = re.sub(r"[^0-9a-z]", "", s)
    base = re.sub(r"[^0-9a-z]", "", re.split(r"[-/]", s)[0])
    return {k for k in (full, base) if k}


def city_key(city) -> str:
    return re.sub(r"[^0-9a-z]", "", _PAREN_TAIL.sub("", fold(city)))


_SIMPLE_NUMBER = re.compile(r"^(\d+)([a-z]?)$")


def numbers_compatible(a: str, b: str) -> bool:
    """Same house, written differently: equal, or one side lacks the letter ('7' ~ '7c', '1a' ~ '1'; '5a' != '5b')."""
    if not a or not b:
        return False
    if a == b:
        return True
    ma, mb = _SIMPLE_NUMBER.match(a), _SIMPLE_NUMBER.match(b)
    return bool(ma and mb and ma.group(1) == mb.group(1) and (not ma.group(2) or not mb.group(2)))


def numbers_related(a: str, b: str) -> bool:
    """Loose: one side has no number, or the two share a number ('26' ~ '26-28', '5a' ~ '5b'). For proposals only."""
    if not a or not b:
        return True
    return bool(set(re.findall(r"\d+", a)) & set(re.findall(r"\d+", b)))


def name_similarity(na: str, nb: str, contain: bool = False) -> float:
    """Ratio of two street-name keys. With `contain`, a name inside the other (min. 5 characters) counts as 0.9;
    only for loose proposals, never for merges ('Neue Industriestr.' would equal 'Industriestr.')."""
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    short, long_ = sorted((na, nb), key=len)
    if contain and len(short) >= 5 and short in long_:
        return 0.9
    return SequenceMatcher(None, na, nb).ratio()


def street_similarity(a, b) -> float:
    """0..1 similarity of two street lines: 0 unless the house numbers are compatible, then the name ratio.

    Used to merge leftovers of one family in the same PLZ (typos, a house-number letter), never across PLZ.
    """
    na, nua = street_parts(a)
    nb, nub = street_parts(b)
    if not na or not nb or not numbers_compatible(nua, nub):
        return 0.0
    return name_similarity(na, nb)


def street_related(a, b, min_name: float = 0.85) -> bool:
    """Loose check for coordinate candidates: similar street name (containment allowed) and a related or missing number."""
    na, nua = street_parts(a)
    nb, nub = street_parts(b)
    return name_similarity(na, nb, contain=True) >= min_name and numbers_related(nua, nub)


def has_number(street) -> bool:
    return bool(street_parts(street)[1])
