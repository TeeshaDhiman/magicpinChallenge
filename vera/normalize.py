"""Defensive accessors over raw context dicts.

The challenge brief and judge_simulator.py disagree on some field names
(e.g. voice.taboos vs voice.vocab_taboo, identity.owner_first_name exists only
in the simulator). Every read of a raw context goes through this module so the
rest of the code never has to care.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

AUDIENCE_NOUN = {
    "dentists": ("patients", "patients"),
    "salons": ("clients", "clients"),
    "gyms": ("members", "members"),
    "restaurants": ("customers", "customers"),
    "pharmacies": ("customers", "customers"),
}

BUSINESS_NOUN = {
    "dentists": "clinic",
    "salons": "salon",
    "gyms": "gym",
    "restaurants": "restaurant",
    "pharmacies": "pharmacy",
}

PEER_NOUN = {  # "a new ___ opened nearby"
    "dentists": "dental clinic",
    "salons": "salon",
    "gyms": "gym",
    "restaurants": "restaurant",
    "pharmacies": "pharmacy",
}

METRIC_LABEL = {
    "views": ("profile views", "profile views"),
    "calls": ("calls", "calls"),
    "directions": ("direction requests", "direction requests"),
    "ctr": ("click-through rate", "click-through rate"),
    "leads": ("leads", "leads"),
    "reviews": ("Google reviews", "Google reviews"),
    "rating": ("rating", "rating"),
}


def get(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
        if cur is None:
            return default
    return cur


def first(d: dict, *keys: str, default: Any = None) -> Any:
    """First present (non-None, non-empty) value among keys."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def category_slug(merchant: dict, category: dict | None) -> str:
    return str(merchant.get("category_slug") or get(category, "slug") or "").lower()


def taboos(category: dict | None) -> list[str]:
    voice = get(category, "voice", default={}) or {}
    return [str(t) for t in (voice.get("taboos") or voice.get("vocab_taboo") or [])]


def voice_tone(category: dict | None) -> str:
    return str(get(category, "voice", "tone", default="") or "")


def audience(slug: str) -> str:
    return AUDIENCE_NOUN.get(slug, ("customers", "customers"))[0]


def business_noun(slug: str) -> str:
    return BUSINESS_NOUN.get(slug, "business")


def peer_noun(slug: str) -> str:
    return PEER_NOUN.get(slug, "business")


def metric_label(metric: str) -> str:
    return METRIC_LABEL.get(metric.lower(), (metric.replace("_", " "),))[0]


_DR_NAME = re.compile(r"^\s*Dr\.?\s*([A-Z][a-zA-Z]+)")


def salutation(merchant: dict, slug: str, lang: str) -> str:
    """How we address the merchant at the start of a message.

    Dentists always get the Dr. prefix (the simulator's rubric checks this).
    Hinglish merchants who aren't doctors get the respectful 'ji'.
    """
    ident = merchant.get("identity") or {}
    owner = (ident.get("owner_first_name") or "").strip()
    name = (ident.get("name") or "").strip()

    if not owner:
        m = _DR_NAME.match(name)
        if m:
            owner = m.group(1)
    if owner:
        owner = re.sub(r"^Dr\.?\s*", "", owner)
        if slug == "dentists" or _DR_NAME.match(name):
            return f"Dr. {owner}"
        return f"{owner} ji" if lang == "hinglish" else owner
    return f"{name} team" if name else "Hi there"


def lang_mode(languages: Any) -> str:
    """'hinglish' if Hindi is in the merchant's languages, else 'en'."""
    if isinstance(languages, str):
        languages = [languages]
    langs = {str(x).lower() for x in (languages or [])}
    if langs & {"hi", "hi-en", "hinglish", "hi-en mix", "hindi"}:
        return "hinglish"
    return "en"


def as_pct(x: Any) -> float | None:
    """Accept 0.18 (fraction) or 18 (percent) and return percent."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v * 100 if abs(v) <= 1.5 else v


def fmt_int(n: Any) -> str:
    """Indian digit grouping: 124500 -> 1,24,500."""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        return str(n)
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return ("-" if n < 0 else "") + s


def fmt_pct(x: Any, decimals: int = 0) -> str | None:
    p = as_pct(x)
    if p is None:
        return None
    return f"{abs(p):.{decimals}f}" if decimals else str(int(round(abs(p))))


def parse_dt(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def humanize_token(s: str) -> str:
    """'high_risk_adults' -> 'high-risk adults'. Used only for known-safe fields."""
    s = str(s).replace("_", " ").strip()
    return re.sub(r"\bhigh risk\b", "high-risk", s)
