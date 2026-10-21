"""Ad copy and asset text rules from the brief (section 5.8)."""

import re

from ads_actions.errors import Refused

HEADLINE_MAX = 30
HEADLINES_MIN, HEADLINES_MAX = 3, 15
DESCRIPTION_MAX = 90
DESCRIPTIONS_MIN, DESCRIPTIONS_MAX = 2, 4
SITELINK_TEXT_MAX = 25
SITELINK_DESC_MAX = 35
CALLOUT_MAX = 25
SNIPPET_VALUE_MAX = 25

# Anything that reads like a price: a currency marker next to a number, or a
# number with a money word after it.
_PRICE_PATTERNS = [
    re.compile(r"(rp\.?|idr|usd|us\$|\$|€|£|¥)\s?\d", re.IGNORECASE),
    re.compile(r"\d[\d.,]*\s?(rp|idr|usd|dollars?|rupiah|euros?|rb|ribu|jt|juta)\b", re.IGNORECASE),
    re.compile(r"\$"),
]


def check_text(label: str, text: str, max_len: int, banned: tuple[str, ...]) -> str:
    if not isinstance(text, str):
        raise Refused(f"{label} must be text.")
    t = " ".join(text.split())
    if not t:
        raise Refused(f"{label} is empty.")
    if len(t) > max_len:
        raise Refused(f"{label} {t!r} is {len(t)} characters; the limit is {max_len}.")
    for b in banned:
        if b.lower() in t.lower():
            name = {"—": "an em dash", "–": "an en dash", "!": "an exclamation mark"}.get(b, repr(b))
            raise Refused(f"{label} {t!r} contains {name}, which is not allowed.")
    for p in _PRICE_PATTERNS:
        if p.search(t):
            raise Refused(f"{label} {t!r} looks like it contains a price, which is not allowed.")
    return t


def check_count(label: str, n: int, lo: int, hi: int) -> None:
    if not lo <= n <= hi:
        raise Refused(f"{label}: {n} given, need {lo} to {hi}.")


def check_unique(label: str, texts: list[str]) -> None:
    seen = set()
    for t in texts:
        if t.lower() in seen:
            raise Refused(f"{label}: {t!r} appears twice.")
        seen.add(t.lower())
