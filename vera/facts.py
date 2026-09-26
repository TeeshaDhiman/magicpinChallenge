"""Facts sheet: the only material a message is allowed to be built from.

A FactSheet is derived deterministically from (category, merchant, trigger).
It holds:
  * core identity fields (salutation, language, locality, offers, ...)
  * bilingual 'supporting facts' about the merchant's state, already phrased
    in plain language (never raw signal strings)
  * the trigger resolved into named fields (digest item looked up, etc.)
  * the set of numbers that are legitimately groundable, used by the validator
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import normalize as N

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def canon_num(s: str) -> str:
    """'1,24,500' -> '124500', '06' -> '6', '3.0' -> '3', '0.50' -> '0.5'."""
    s = s.replace(",", "")
    if "." in s:
        ip, fp = s.split(".", 1)
        s = f"{ip.lstrip('0') or '0'}.{fp}".rstrip("0").rstrip(".")
    else:
        s = s.lstrip("0") or "0"
    return s


class TrackedPayload(dict):
    """Records which payload keys the templates actually read (for audit_dataset.py)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.read: set[str] = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)


@dataclass
class Fact:
    key: str
    en: str
    hi: str

    def text(self, lang: str) -> str:
        return self.hi if lang == "hinglish" else self.en


@dataclass
class FactSheet:
    slug: str
    lang: str
    sal: str
    name: str
    locality: str
    city: str
    audience: str
    business: str
    active_offers: list[str]
    catalog_offers: list[str]
    taboos: list[str]
    signals: set[str]
    trigger_kind: str
    trigger_id: str
    urgency: int
    payload: dict
    digest_item: dict | None = None
    peer: dict = field(default_factory=dict)
    context_text: str = ""
    decision: str = ""
    support: list[Fact] = field(default_factory=list)
    allowed_numbers: set[str] = field(default_factory=set)
    used: list[str] = field(default_factory=list)

    # ---- grounding -------------------------------------------------------
    def allow(self, *values: Any) -> None:
        for v in values:
            if v is None:
                continue
            for m in _NUM.findall(str(v)):
                self.allowed_numbers.add(canon_num(m))

    def use(self, key: str) -> None:
        if key not in self.used:
            self.used.append(key)

    def pick_support(self, exclude: tuple[str, ...] = (), n: int = 1) -> list[Fact]:
        out = [f for f in self.support if f.key not in exclude][:n]
        for f in out:
            self.use(f.key)
        return out

    @property
    def top_offer(self) -> str | None:
        return self.active_offers[0] if self.active_offers else None

    @property
    def is_hi(self) -> bool:
        return self.lang == "hinglish"


def _harvest_numbers(obj: Any, into: set[str]) -> None:
    """Every number in the raw contexts is groundable, plus percent forms of fractions."""
    if isinstance(obj, dict):
        for v in obj.values():
            _harvest_numbers(v, into)
    elif isinstance(obj, list):
        for v in obj:
            _harvest_numbers(v, into)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        into.add(canon_num(str(obj)))
        into.add(canon_num(N.fmt_int(obj)))
        if isinstance(obj, float) and abs(obj) <= 1.5:
            p = abs(obj) * 100
            into.add(canon_num(f"{p:.0f}"))
            into.add(canon_num(f"{p:.1f}"))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            into.add(canon_num(m))


def _parse_signals(raw: Any) -> dict[str, str]:
    """['stale_posts:22d', 'dormant'] or [{'kind':..,'value':..}] -> {name: value}."""
    out: dict[str, str] = {}
    for s in raw or []:
        if isinstance(s, dict):
            k = str(s.get("kind") or s.get("name") or "")
            if k:
                out[k] = str(s.get("value") or "")
        else:
            k, _, v = str(s).partition(":")
            out[k.strip()] = v.strip()
    return out


def _top_search(merchant: dict) -> tuple[str, float] | None:
    """Highest-volume search term from any of the plausible GBP search-keyword shapes."""
    raw = (N.first(merchant, "search_keywords", "top_searches", "search_terms", "keywords")
           or N.get(merchant, "performance", "search_keywords") or N.get(merchant, "performance", "top_searches")
           or N.get(merchant, "gbp", "search_keywords") or [])
    best = None
    for k in raw if isinstance(raw, list) else []:
        if not isinstance(k, dict):
            continue
        term = N.first(k, "keyword", "term", "query", "search_term")
        n = N.first(k, "count", "searches", "volume", "impressions", "value")
        if term and isinstance(n, (int, float)) and n > 0 and (best is None or n > best[1]):
            best = (str(term), n)
    return best


def _lookup_digest(category: dict, payload: dict) -> dict | None:
    item = payload.get("top_item") or payload.get("item")
    if isinstance(item, dict) and item.get("title"):
        return item
    item_id = N.first(payload, "top_item_id", "digest_item_id", "item_id")
    for d in (category or {}).get("digest") or []:
        if item_id and d.get("id") == item_id:
            return d
    return None


def build(category: dict, merchant: dict, trigger: dict) -> FactSheet:
    category = category or {}
    slug = N.category_slug(merchant, category)
    ident = merchant.get("identity") or {}
    lang = N.lang_mode(ident.get("languages"))
    payload = TrackedPayload(trigger.get("payload") or {})

    offers = merchant.get("offers") or []
    active = [o.get("title") for o in offers
              if isinstance(o, dict) and o.get("title") and str(o.get("status", "active")).lower() == "active"]
    catalog = [o.get("title") if isinstance(o, dict) else str(o)
               for o in category.get("offer_catalog") or []]

    signals = _parse_signals(merchant.get("signals"))

    fs = FactSheet(
        slug=slug, lang=lang,
        sal=N.salutation(merchant, slug, lang),
        name=ident.get("name") or "",
        locality=ident.get("locality") or "",
        city=ident.get("city") or "",
        audience=N.audience(slug),
        business=N.business_noun(slug),
        active_offers=active,
        catalog_offers=[c for c in catalog if c],
        taboos=N.taboos(category),
        signals=set(signals),
        trigger_kind=str(trigger.get("kind") or ""),
        trigger_id=str(trigger.get("id") or ""),
        urgency=int(trigger.get("urgency") or 1),
        payload=payload,
        digest_item=_lookup_digest(category, payload),
        peer=category.get("peer_stats") or {},
    )

    for ctx in (category, merchant, trigger):
        _harvest_numbers(ctx, fs.allowed_numbers)
    import json as _json
    fs.context_text = _json.dumps([category, merchant, trigger], ensure_ascii=False).lower()

    _build_support(fs, category, merchant, signals)
    return fs


def _build_support(fs: FactSheet, category: dict, merchant: dict, signals: dict[str, str]) -> None:
    """Merchant-state facts in priority order (strongest merchant-fit hook first)."""
    perf = merchant.get("performance") or {}
    peer = category.get("peer_stats") or {}
    agg = merchant.get("customer_aggregate") or {}

    ctr, peer_ctr = perf.get("ctr"), peer.get("avg_ctr")
    if isinstance(ctr, (int, float)) and isinstance(peer_ctr, (int, float)) and ctr < peer_ctr:
        a, b = N.fmt_pct(ctr, 1), N.fmt_pct(peer_ctr, 1)
        fs.allow(a, b)
        fs.support.append(Fact(
            "ctr_gap",
            f"your profile click-through is {a}% vs the {b}% peer average",
            f"aapka profile click-through {a}% hai, peers ka average {b}%",
        ))

    stale = signals.get("stale_posts")
    if stale is not None:
        days = re.sub(r"\D", "", stale)
        if days:
            fs.support.append(Fact(
                "stale_posts",
                f"your last Google post was {days} days ago",
                f"aapki last Google post {days} din pehle gayi thi",
            ))
        else:
            fs.support.append(Fact("stale_posts", "your Google posts have gone quiet",
                                   "aapki Google posts kaafi time se nahi gayi"))

    lapsed = N.first(agg, "lapsed_180d_plus", "lapsed_count")
    if isinstance(lapsed, (int, float)) and lapsed > 0:
        n = N.fmt_int(lapsed)
        fs.support.append(Fact(
            "lapsed",
            f"{n} of your {fs.audience} haven't been back in 6+ months",
            f"aapke {n} {fs.audience} 6+ mahine se wapas nahi aaye",
        ))

    # Local search demand: the website's own "high compulsion" example is built on this
    # ("190 people in your locality are searching for 'Dental Check Up'").
    kw = _top_search(merchant)
    if kw:
        term, n = kw
        loc_en = f" in {fs.locality}" if fs.locality else " near you"
        loc_hi = f"{fs.locality} mein " if fs.locality else "aapke area mein "
        fs.support.append(Fact(
            "search_demand",
            f'{N.fmt_int(n)} people{loc_en} searched for "{term}" on Google',
            f'{loc_hi}{N.fmt_int(n)} logon ne Google pe "{term}" search kiya',
        ))

    views = perf.get("views")
    window = perf.get("window_days") or 30
    if isinstance(views, (int, float)) and views > 0:
        v = N.fmt_int(views)
        fs.support.append(Fact(
            "views",
            f"your profile got {v} views in the last {window} days",
            f"pichle {window} din mein aapke profile pe {v} views aaye",
        ))

    calls_d = N.get(perf, "delta_7d", "calls_pct")
    if isinstance(calls_d, (int, float)) and calls_d < 0:
        p = N.fmt_pct(calls_d)
        fs.support.append(Fact(
            "calls_down",
            f"calls are down {p}% this week",
            f"is hafte calls {p}% kam hain",
        ))
