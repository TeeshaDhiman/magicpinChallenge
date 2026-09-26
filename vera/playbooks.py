"""Playbook router + deterministic templates.

Each trigger kind maps to a family. Each family template takes a FactSheet and
returns a Draft, or None if the trigger lacks the fields it needs (in which
case the router falls back to 'generic', and generic itself returns None rather
than send an empty-calorie message).

Rules every template follows:
  * open with the salutation, no preamble, never re-introduce "I'm Vera"
  * one why-now anchor from the trigger, at most one merchant-state fact
  * exactly one ask, in the last sentence
  * only text that came from the FactSheet (so the validator can ground it)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import normalize as N
from .facts import FactSheet
from . import decide as D


@dataclass
class Draft:
    family: str
    body: str
    cta: str             # "binary_yes_stop" | "open_ended" | "none"
    lever: str           # compulsion levers used, for the rationale
    template_params: list[str]

    @property
    def template_name(self) -> str:
        return f"vera_{self.family}_v1"


KIND_TO_FAMILY = {
    "research_digest": "research", "research_digest_release": "research",
    "category_research_digest_release": "research",
    "regulation_change": "regulation", "compliance_update": "regulation",
    "category_trend_movement": "trend", "trend_signal": "trend",
    "perf_dip": "perf_dip", "perf_spike": "perf_spike",
    "seasonal_perf_dip": "perf_dip",
    "milestone_reached": "milestone",
    "festival_upcoming": "festival", "festival": "festival",
    "weather_heatwave": "weather", "weather_alert": "weather",
    "local_news_event": "local_news",
    "competitor_opened": "competitor",
    "review_theme_emerged": "review_theme",
    "dormant_with_vera": "curious_ask", "scheduled_recurring": "curious_ask",
    "curious_ask_due": "curious_ask",
    "renewal_due": "renewal",
    "supply_alert": "supply_alert",
    "winback_eligible": "winback",
    "ipl_match_today": "event_promo",
    "active_planning_intent": "planning",
    "category_seasonal": "seasonal",
    "gbp_unverified": "gbp",
    "cde_opportunity": "cde",
}

SERVICE_NOUN = {"dentists": "treatment", "salons": "service", "gyms": "class",
                "restaurants": "dish", "pharmacies": "product"}

ACTION_CTA = {
    "en": ["Want me to draft it? Reply YES.", "Shall I set it up for you? Reply YES."],
    "hinglish": ["Draft kar doon? Reply YES.", "Main set up kar doon? Bas YES bhej dijiye."],
}


def _t(fs: FactSheet, en: str, hi: str) -> str:
    return hi if fs.is_hi else en


def _cta(fs: FactSheet, variant: int) -> str:
    opts = ACTION_CTA[fs.lang]
    return opts[variant % len(opts)]


def _support(fs: FactSheet, family: str) -> str:
    """The one merchant fact the decision layer picked for this family, as a sentence."""
    fact = D.choose_support(fs, family)
    if not fact:
        return ""
    txt = fact.text(fs.lang)
    return txt[0].upper() + txt[1:] + ". "


def _offer_phrase(fs: FactSheet, oblique: bool = False) -> str | None:
    """Service+price beats discount copy: prefer the merchant's live offer, else a catalog pattern.

    oblique=True gives the Hindi oblique case ('aapke ... offer ke saath').
    """
    if fs.top_offer:
        fs.use("active_offer")
        return _t(fs, f'your "{fs.top_offer}" offer',
                  f'aapke "{fs.top_offer}" offer' if oblique else f'aapka "{fs.top_offer}" offer')
    if fs.catalog_offers:
        fs.use("catalog_offer")
        return _t(fs, f'a service+price offer like "{fs.catalog_offers[0]}"',
                  f'"{fs.catalog_offers[0]}" jaisa service+price offer')
    return None


def _window(fs: FactSheet) -> str:
    w = str(N.first(fs.payload, "window", "period", "compared_to", "comparison", "baseline", default="")).lower()
    table = {
        "yesterday": ("yesterday", "kal"), "1d": ("yesterday", "kal"),
        "7d": ("this week", "is hafte"), "wow": ("this week", "is hafte"), "week": ("this week", "is hafte"),
        "30d": ("this month", "is mahine"), "mom": ("this month", "is mahine"),
    }
    en, hi = table.get(w, ("recently", "haal hi mein"))
    return _t(fs, en, hi)


def _metric_and_delta(fs: FactSheet) -> tuple[str, str] | None:
    p = fs.payload
    metric = N.first(p, "metric", "kpi", "metric_name", "signal_metric")
    delta = N.first(p, "delta_pct", "pct_change", "change_pct", "delta", "change", "pct", "percent_change", "wow_pct", "vs_avg_pct")
    pct = N.fmt_pct(delta) if delta is not None else None
    if not metric or not pct:
        return None
    fs.use("trigger_delta")
    return N.metric_label(str(metric)), pct


# --------------------------------------------------------------------------
# Family templates
# --------------------------------------------------------------------------

def research(fs: FactSheet, v: int) -> Draft | None:
    item = fs.digest_item
    if not item or not item.get("title"):
        return None
    fs.use("digest_item")
    title, source = item["title"], item.get("source") or ""
    n = item.get("trial_n")
    unit = "patient" if fs.slug in ("dentists", "pharmacies") else "person"
    trial = f" ({N.fmt_int(n)}-{unit} trial)" if n else ""

    seg_line = ""
    seg = item.get("patient_segment")
    if seg:
        seg_h = N.humanize_token(seg)
        words = seg_h.split()
        if words[-1].endswith("s") and not words[-1].endswith("ss"):
            words[-1] = words[-1][:-1]
        seg_adj = " ".join(words)
        cohort = any("high_risk" in s for s in fs.signals) and "high-risk" in seg_h
        if cohort:
            fs.use("cohort_match")
            seg_line = _t(fs, f"Directly relevant to your {seg_adj} {fs.audience}. ",
                          f"Aapke {seg_adj} {fs.audience} ke liye seedha relevant hai. ")
        else:
            seg_line = _t(fs, f"Most relevant for {seg_h}. ", f"Khaas taur pe {seg_h} ke liye. ")

    src = f" ({source})" if source else ""
    ask = _t(fs,
             f"Want me to pull the key points and draft a short WhatsApp note your {fs.audience} can read?",
             f"Main key points nikaal ke aapke {fs.audience} ke liye ek short WhatsApp note draft kar doon?")
    lead = _t(fs, "new research worth 2 minutes", "naya research, 2 minute ka read")
    body = f"{fs.sal}, {lead}{src}: {title}{trial}. {seg_line}{ask}"
    return Draft("research", body, "open_ended", "specificity+curiosity+reciprocity",
                 [fs.sal, title, source])


def regulation(fs: FactSheet, v: int) -> Draft | None:
    item = fs.digest_item or {}
    title = item.get("title") or N.first(fs.payload, "title", "headline", "summary", "rule", "change")
    if not title:
        return None
    fs.use("regulation_item")
    source = item.get("source") or N.first(fs.payload, "source", default="")
    eff = N.first(item, "effective_date") or N.first(fs.payload, "effective_date", "effective_from", "effective_on", "deadline")
    src = f" ({source})" if source else ""
    eff_txt = _t(fs, f", effective {eff}", f", {eff} se laagu") if eff else ""
    ask = _t(fs, f"Want a 1-page summary of what changes for your {fs.business}?",
             f"Aapke {fs.business} ke liye kya badlega, uska 1-page summary bhej doon?")
    lead = _t(fs, "compliance heads-up", "compliance update")
    body = f"{fs.sal}, {lead}{src}: {title}{eff_txt}. {ask}"
    return Draft("regulation", body, "open_ended", "specificity+loss_aversion+reciprocity",
                 [fs.sal, title, str(source)])


def trend(fs: FactSheet, v: int) -> Draft | None:
    q = N.first(fs.payload, "query", "search_term", "keyword", "term", "trend", "search_query")
    d = N.first(fs.payload, "delta_yoy", "delta_pct", "change_pct", "yoy_pct", "growth_pct", "delta")
    pct = N.fmt_pct(d) if d is not None else None
    count = N.first(fs.payload, "searches", "search_count", "count", "volume")
    if not q or not (pct or isinstance(count, (int, float))):
        return None
    offer = _offer_phrase(fs)
    if not pct:
        # Absolute local demand: the website's own high-compulsion pattern.
        fs.use("search_count")
        loc = N.first(fs.payload, "locality", "area", default=fs.locality)
        where_en, where_hi = (f" in {loc}" if loc else " near you"), (f"{loc} mein " if loc else "aapke area mein ")
        ask = _t(fs, f"Want me to put up a Google post featuring {offer} for them? Reply YES." if offer
                 else "Want me to put up a Google post aimed at them? Reply YES.",
                 f"Unke liye {offer} ke saath ek Google post daal doon? Reply YES." if offer
                 else "Unke liye ek Google post daal doon? Reply YES.")
        body = _t(fs, f'{fs.sal}, {N.fmt_int(count)} people{where_en} are searching for "{q}" on Google. {ask}',
                  f'{fs.sal}, {where_hi}{N.fmt_int(count)} log Google pe "{q}" search kar rahe hain. {ask}')
        return Draft("trend", body, "binary_yes_stop", "specific_benchmark+real_offer+single_cta",
                     [fs.sal, str(q), N.fmt_int(count)])
    fs.use("trend_delta")
    up = (N.as_pct(d) or 0) >= 0
    dir_en, dir_hi = ("up", "badh gayi hain") if up else ("down", "kam hui hain")
    support = _support(fs, "trend")
    ask = _t(fs, "Want me to draft a Google post and service listing that targets this search? Reply YES.",
             "Is search ke liye ek Google post aur service listing draft kar doon? Reply YES.")
    body = _t(fs,
              f'{fs.sal}, searches for "{q}" are {dir_en} {pct}% year-on-year. {support}{ask}',
              f'{fs.sal}, "{q}" ki searches saal-dar-saal {pct}% {dir_hi}. {support}{ask}')
    return Draft("trend", body, "binary_yes_stop", "specificity+curiosity+effort_externalization",
                 [fs.sal, str(q), f"{pct}%"])


def perf_dip(fs: FactSheet, v: int) -> Draft | None:
    md = _metric_and_delta(fs)
    offer = _offer_phrase(fs, oblique=True)
    if not md:
        # Placeholder fallback: use support facts (CTR gap, calls_down)
        ctr_fact = next((f for f in fs.support if f.key == "ctr_gap"), None)
        calls_fact = next((f for f in fs.support if f.key == "calls_down"), None)
        anchor = ctr_fact or calls_fact
        if not anchor or not offer:
            return None
        fs.use(anchor.key)
        body = _t(fs,
                  f"{fs.sal}, {anchor.en[0].upper() + anchor.en[1:]}. "
                  f"A fresh Google post featuring {offer} is the fastest fix. {_cta(fs, v)}",
                  f"{fs.sal}, {anchor.hi[0].upper() + anchor.hi[1:]}. "
                  f"Sabse jaldi fix: {offer} ke saath ek fresh Google post. {_cta(fs, v)}")
        return Draft("perf_dip", body, "binary_yes_stop", "loss_aversion+specificity+effort_externalization",
                     [fs.sal, anchor.en])
    metric, pct = md
    support = _support(fs, "perf_dip")
    body = _t(fs,
              f"{fs.sal}, your {metric} fell {pct}% {_window(fs)}. {support}"
              f"A fresh Google post featuring {offer} is the fastest fix. {_cta(fs, v)}",
              f"{fs.sal}, aapke {metric} {_window(fs)} {pct}% gir gaye. {support}"
              f"Sabse jaldi fix: {offer} ke saath ek fresh Google post. {_cta(fs, v)}")
    return Draft("perf_dip", body, "binary_yes_stop", "loss_aversion+specificity+effort_externalization",
                 [fs.sal, metric, f"{pct}%"])


def perf_spike(fs: FactSheet, v: int) -> Draft | None:
    md = _metric_and_delta(fs)
    offer = _offer_phrase(fs)
    if not md:
        # Placeholder fallback: use views or search demand
        views_fact = next((f for f in fs.support if f.key == "views"), None)
        demand_fact = next((f for f in fs.support if f.key == "search_demand"), None)
        anchor = views_fact or demand_fact
        if not anchor or not offer:
            return None
        fs.use(anchor.key)
        body = _t(fs,
                  f"{fs.sal}, {anchor.en[0].upper() + anchor.en[1:]}. "
                  f"Pin {offer} on your Google profile to convert that traffic. {_cta(fs, v + 1)}",
                  f"{fs.sal}, {anchor.hi[0].upper() + anchor.hi[1:]}. "
                  f"Is traffic ko convert karne ke liye {offer} Google profile pe pin kar dete hain. {_cta(fs, v + 1)}")
        return Draft("perf_spike", body, "binary_yes_stop", "specificity+momentum+effort_externalization",
                     [fs.sal, anchor.en])
    metric, pct = md
    support = _support(fs, "perf_spike")
    body = _t(fs,
              f"{fs.sal}, your {metric} jumped {pct}% {_window(fs)}. {support}"
              f"Pin {offer} on your Google profile to convert that traffic. {_cta(fs, v + 1)}",
              f"{fs.sal}, aapke {metric} {_window(fs)} {pct}% badh gaye. {support}"
              f"Is traffic ko convert karne ke liye {offer} Google profile pe pin kar dete hain. {_cta(fs, v + 1)}")
    return Draft("perf_spike", body, "binary_yes_stop", "specificity+momentum+effort_externalization",
                 [fs.sal, metric, f"{pct}%"])


def milestone(fs: FactSheet, v: int) -> Draft | None:
    metric = N.first(fs.payload, "metric", default="reviews")
    value = N.first(fs.payload, "value", "threshold", "count", "milestone", "milestone_value", "total")
    if value is None:
        # Placeholder fallback: check merchant performance for review count
        perf_reviews = N.get(dict(fs.payload), "_skip", default=None)  # no-op, just to mark
        # Try to get review count from merchant context
        import json as _j
        ctx = fs.context_text
        import re as _re
        rm = _re.search(r'"reviews?_count"\s*:\s*(\d+)', ctx)
        if rm:
            value = int(rm.group(1))
            fs.allow(value)
        if value is None:
            rm = _re.search(r'"total_reviews"\s*:\s*(\d+)', ctx)
            if rm:
                value = int(rm.group(1))
                fs.allow(value)
        if value is None:
            return None
    fs.use("milestone_value")
    label = N.metric_label(str(metric))
    peer_line = ""
    if "review" in str(metric):
        avg = fs.peer.get("avg_reviews")
        if isinstance(avg, (int, float)) and float(value) > avg:
            peer_line = _t(fs, f"The peer average is {N.fmt_int(avg)}. ",
                           f"Peers ka average {N.fmt_int(avg)} hai. ")
    ask = _t(fs, "Want me to post a thank-you on your Google profile to keep the momentum? Reply YES.",
             "Momentum banaye rakhne ke liye Google profile pe ek thank-you post daal doon? Reply YES.")
    body = _t(fs, f"{fs.sal}, congratulations — your {fs.business} just crossed {N.fmt_int(value)} {label}. {peer_line}{ask}",
              f"{fs.sal}, badhai ho — aapke {fs.business} ne {N.fmt_int(value)} {label} cross kar liye. {peer_line}{ask}")
    return Draft("milestone", body, "binary_yes_stop", "pride+social_proof+effort_externalization",
                 [fs.sal, N.fmt_int(value), label])


def festival(fs: FactSheet, v: int) -> Draft | None:
    name = N.first(fs.payload, "festival", "festival_name", "event_name", "occasion", "name")
    if not name:
        return None
    fs.use("festival")
    days = N.first(fs.payload, "days_until", "days_to_go", "days_left", "in_days", "days_away")
    date = N.first(fs.payload, "date", "festival_date", "event_date", "on")
    if isinstance(days, (int, float)):
        d = int(days)
        when = _t(fs, {0: "today", 1: "tomorrow"}.get(d, f"in {d} days"),
                  {0: "aaj", 1: "kal"}.get(d, f"{d} din mein"))
    elif date:
        when = _t(fs, f"on {date}", f"{date} ko")
    else:
        when = _t(fs, "coming up", "aane wala")
    offer = _offer_phrase(fs, oblique=True)
    support = _support(fs, "festival")
    offer_line = _t(fs, f"I can have a {name} Google post ready around {offer}. ",
                    f"Main {offer} ke saath ek {name} Google post ready kar sakti hoon. ") if offer else ""
    body = _t(fs, f"{fs.sal}, {name} is {when}. {support}{offer_line}{_cta(fs, v)}",
              f"{fs.sal}, {name} {when} hai. {support}{offer_line}{_cta(fs, v)}")
    return Draft("festival", body, "binary_yes_stop", "timeliness+effort_externalization",
                 [fs.sal, str(name), when])


def weather(fs: FactSheet, v: int) -> Draft | None:
    temp = N.first(fs.payload, "temp_c", "temperature_c", "max_temp_c", "temperature", "temp")
    city = N.first(fs.payload, "city", "location", default=fs.city)
    if temp is None or not city:
        return None
    fs.use("weather")
    ask = _t(fs, f"Want me to post a short heat-day update on your Google profile so people know you're open? Reply YES.",
             f"Google profile pe ek short update daal doon ki aap khule hain? Reply YES.")
    body = _t(fs, f"{fs.sal}, it's {temp}°C in {city} today {ask}",
              f"{fs.sal}, aaj {city} mein {temp}°C hai. {ask}")
    return Draft("weather", body, "binary_yes_stop", "timeliness+effort_externalization",
                 [fs.sal, f"{temp}°C", str(city)])


def local_news(fs: FactSheet, v: int) -> Draft | None:
    headline = N.first(fs.payload, "headline", "title", "summary", "event", "description", "news")
    if not headline:
        return None
    fs.use("news")
    ask = _t(fs, f"If it affects your {fs.business}'s hours or access, I can post a Google update in 2 minutes. Reply YES.",
             f"Agar isse aapke {fs.business} ke timings ya raasta affect hota hai, main 2 minute mein Google update daal doon. Reply YES.")
    body = _t(fs, f"{fs.sal}, local update: {headline}. {ask}", f"{fs.sal}, local update: {headline}. {ask}")
    return Draft("local_news", body, "binary_yes_stop", "timeliness+reciprocity",
                 [fs.sal, str(headline)])


def competitor(fs: FactSheet, v: int) -> Draft | None:
    dist = N.first(fs.payload, "distance_km", "distance")
    if dist is None and isinstance(N.first(fs.payload, "distance_m", "distance_meters"), (int, float)):
        dist = round(N.first(fs.payload, "distance_m", "distance_meters") / 1000, 1)
        fs.allow(dist)
    comp = N.first(fs.payload, "competitor_name", "competitor", "business_name", "name")
    comp = comp.get("name") if isinstance(comp, dict) else comp
    if dist is None and not fs.locality:
        return None
    fs.use("competitor")
    what = N.peer_noun(fs.slug)
    named = f' ("{comp}")' if comp else ""
    where_en = f"{dist} km from you" if dist is not None else f"in {fs.locality}"
    where_hi = f"aapse {dist} km door" if dist is not None else f"{fs.locality} mein"
    rating = N.first(fs.payload, "rating", "their_rating", "competitor_rating")
    rating_en = f", starting at {rating}★" if rating else ""
    rating_hi = f", {rating}★ rating ke saath" if rating else ""
    support = _support(fs, "competitor")
    ask = _t(fs, "Want a quick side-by-side of your profile vs theirs?",
             "Aapke profile vs unka ek quick side-by-side bhej doon?")
    body = _t(fs, f"{fs.sal}, a new {what}{named} just listed on Google {where_en}{rating_en}. {support}{ask}",
              f"{fs.sal}, ek naya {what}{named} Google pe list hua hai, {where_hi}{rating_hi}. {support}{ask}")
    return Draft("competitor", body, "open_ended", "curiosity+loss_aversion",
                 [fs.sal, what, where_en])


def review_theme(fs: FactSheet, v: int) -> Draft | None:
    theme = N.first(fs.payload, "theme", "topic", "keyword", "theme_label", "metric_or_topic")
    count = N.first(fs.payload, "count", "review_count", "mentions", "n_reviews", "num_reviews",
                    "occurrences_30d")
    if not theme or theme == fs.trigger_kind:
        # metric_or_topic just echoes the kind name, not a real theme
        return None
    if count is None:
        count = "several"
    fs.use("review_theme")
    positive = str(N.first(fs.payload, "sentiment", default="")).lower() == "positive"
    if positive:
        ask = _t(fs, "That's worth showing off — want me to turn it into a Google post? Reply YES.",
                 "Yeh dikhane layak hai — ise Google post bana doon? Reply YES.")
    else:
        ask = _t(fs, f"Want me to draft calm, public replies to all {count}? Reply YES.",
                 f"Sabhi {count} reviews ke liye shaant, public replies draft kar doon? Reply YES.")
    body = _t(fs, f'{fs.sal}, {count} of your recent Google reviews mention "{theme}". {ask}',
              f'{fs.sal}, aapke {count} recent Google reviews mein "{theme}" ka zikr hai. {ask}')
    return Draft("review_theme", body, "binary_yes_stop", "specificity+reciprocity+effort_externalization",
                 [fs.sal, str(count), str(theme)])


def curious_ask(fs: FactSheet, v: int) -> Draft | None:
    service = SERVICE_NOUN.get(fs.slug, "service")
    days = N.first(fs.payload, "days_since_last_message", "days_silent", "days_since_last_reply", "days_inactive", "days_dormant", "days")
    opener = ""
    if isinstance(days, (int, float)) and fs.trigger_kind == "dormant_with_vera":
        fs.use("dormancy")
        opener = _t(fs, f"it's been {int(days)} days since we last spoke. ",
                    f"humari last baat ko {int(days)} din ho gaye. ")
    support = _support(fs, "curious_ask")
    ask = _t(fs, f"Quick one: which {service} are {fs.audience} asking about most this week? Reply with it and I'll turn it into a Google post.",
             f"Ek quick sawaal: is hafte {fs.audience} sabse zyada kaunsa {service} pooch rahe hain? Reply mein bata dijiye, main use Google post bana dungi.")
    body = f"{fs.sal}, {opener}{support}{ask}"
    return Draft("curious_ask", body, "open_ended", "asking_the_merchant+effort_externalization",
                 [fs.sal, service])


def generic(fs: FactSheet, v: int) -> Draft | None:
    """Unknown trigger kinds: send only if we have both a why-now line and a merchant fact."""
    why = N.first(fs.payload, "headline", "title", "summary", "message", "description", "event")
    if not why or not isinstance(why, str):
        return None
    support = _support(fs, "generic")
    if not support:
        return None
    fs.use("trigger_text")
    offer = _offer_phrase(fs, oblique=True)
    ask = (_t(fs, f"Want me to draft a Google post around {offer}? Reply YES.",
              f"{offer} ke saath ek Google post draft kar doon? Reply YES.") if offer
           else _cta(fs, v))
    body = f"{fs.sal}, {why.rstrip('.')}. {support}{ask}"
    return Draft("generic", body, "binary_yes_stop", "timeliness+effort_externalization",
                 [fs.sal, why])


def renewal(fs: FactSheet, v: int) -> Draft | None:
    """Subscription/plan renewal reminders."""
    days = N.first(fs.payload, "days_remaining", "days_left", "days_until_renewal", "days_to_renewal")
    plan = N.first(fs.payload, "plan", "plan_name", "subscription", "membership")
    amount = N.first(fs.payload, "renewal_amount", "amount", "price", "fee")
    if not days and not plan and not amount:
        # Placeholder: use support facts
        support = _support(fs, "generic")
        if not support and fs.support:
            fact = fs.support[0]
            fs.use(fact.key)
            support = fact.text(fs.lang)
            support = support[0].upper() + support[1:] + ". "
        if not support:
            return None
        ask = _t(fs, "Want me to draft a renewal reminder for your profile? Reply YES.",
                 "Renewal reminder draft kar doon aapke profile ke liye? Reply YES.")
        body = f"{fs.sal}, {support}{ask}"
        return Draft("renewal", body, "binary_yes_stop", "loss_aversion+effort_externalization",
                     [fs.sal])
    fs.use("trigger_text")
    parts = []
    if plan:
        parts.append(_t(fs, f"your {plan} plan", f"aapka {plan} plan"))
    else:
        parts.append(_t(fs, "your subscription", "aapka subscription"))
    if days is not None:
        fs.allow(days)
        parts.append(_t(fs, f"renews in {days} days", f"{days} din mein renew hoga"))
    else:
        parts.append(_t(fs, "is up for renewal", "renewal ke liye aane wala hai"))
    if amount:
        fs.allow(amount)
        parts.append(_t(fs, f"at ₹{N.fmt_int(amount)}", f"₹{N.fmt_int(amount)} mein"))
    support = _support(fs, "generic")
    ask = _t(fs, "Want me to set up an auto-renewal reminder post? Reply YES.",
             "Auto-renewal reminder post set up kar doon? Reply YES.")
    body = f"{fs.sal}, {' '.join(parts)}. {support}{ask}"
    return Draft("renewal", body, "binary_yes_stop", "loss_aversion+timeliness+effort_externalization",
                 [fs.sal, str(plan or 'subscription')])


def supply_alert_tmpl(fs: FactSheet, v: int) -> Draft | None:
    """Supply chain / recall alerts for pharmacies."""
    molecule = N.first(fs.payload, "molecule", "drug", "medicine", "product")
    mfr = N.first(fs.payload, "manufacturer", "mfr", "company", "brand")
    batches = N.first(fs.payload, "affected_batches", "batches", "batch_numbers")
    if not molecule and not mfr:
        return None
    fs.use("trigger_text")
    parts = [_t(fs, "urgent", "urgent")]
    if molecule:
        parts.append(_t(fs, f"voluntary recall on {molecule}", f"{molecule} pe voluntary recall"))
    if mfr:
        parts.append(_t(fs, f"by {mfr}", f"{mfr} dwara"))
    if batches:
        if isinstance(batches, list):
            batches = ", ".join(str(b) for b in batches)
        parts.append(_t(fs, f"(batches: {batches})", f"(batches: {batches})"))
    ask = _t(fs, "Want me to draft the customer notification + replacement workflow?",
             "Customer notification + replacement workflow draft kar doon?")
    body = f"{fs.sal}, {' '.join(parts)}. {ask}"
    return Draft("supply_alert", body, "open_ended", "urgency+specificity+compliance",
                 [fs.sal, str(molecule or '')])


def winback(fs: FactSheet, v: int) -> Draft | None:
    """Merchant winback (re-engagement after expiry)."""
    days = N.first(fs.payload, "days_since_expiry", "days_lapsed", "days_since_last")
    lapsed = N.first(fs.payload, "lapsed_customers_added_since_expiry", "lapsed_count")
    dip = N.first(fs.payload, "perf_dip_pct", "dip_pct")
    support = _support(fs, "generic")
    if not support and fs.support:
        fact = fs.support[0]
        fs.use(fact.key)
        support = fact.text(fs.lang)
        support = support[0].upper() + support[1:] + ". "
    offer = _offer_phrase(fs)
    parts = []
    if days:
        fs.allow(days)
        parts.append(_t(fs, f"it's been {days} days since your plan expired",
                        f"aapke plan ko expire hue {days} din ho gaye"))
    if lapsed:
        fs.allow(lapsed)
        parts.append(_t(fs, f"and {N.fmt_int(lapsed)} customers have gone to competitors since",
                        f"aur tab se {N.fmt_int(lapsed)} customers competitors ke paas gaye"))
    if not parts:
        parts.append(_t(fs, "your profile could use a refresh",
                        "aapke profile ko refresh ki zaroorat hai"))
    offer_line = _t(fs, f" I can highlight {offer} to bring them back.",
                    f" Unhe wapas laane ke liye main {offer} highlight kar sakti hoon.") if offer else ""
    ask = _cta(fs, v)
    body = f"{fs.sal}, {', '.join(parts)}. {support}{offer_line} {ask}"
    return Draft("winback", body, "binary_yes_stop", "loss_aversion+specificity+effort_externalization",
                 [fs.sal])


def event_promo(fs: FactSheet, v: int) -> Draft | None:
    """Event-based promotions (IPL, local events, etc.)."""
    event = N.first(fs.payload, "match", "event", "event_name", "headline", "title")
    city = N.first(fs.payload, "city", "location", default=fs.city)
    venue = N.first(fs.payload, "venue", "stadium")
    if not event and not city:
        return None
    fs.use("trigger_text")
    offer = _offer_phrase(fs, oblique=True)
    support = _support(fs, "generic")
    event_line = str(event) if event else _t(fs, "a big local event", "ek bada local event")
    where = f" in {city}" if city else ""
    offer_line = _t(fs, f" I can put up a Google post around {offer} for the crowd.",
                    f" Crowd ke liye {offer} ke saath ek Google post daal doon.") if offer else ""
    ask = _cta(fs, v)
    body = f"{fs.sal}, {event_line}{where} today. {support}{offer_line} {ask}"
    return Draft("event_promo", body, "binary_yes_stop", "timeliness+effort_externalization",
                 [fs.sal, event_line])


def planning(fs: FactSheet, v: int) -> Draft | None:
    """Active planning intent."""
    topic = N.first(fs.payload, "intent_topic", "topic", "subject", "planning_topic")
    last_msg = N.first(fs.payload, "merchant_last_message", "last_message", "context")
    support = _support(fs, "generic")
    if not topic and not last_msg:
        if not support and fs.support:
            fact = fs.support[0]
            fs.use(fact.key)
            support = fact.text(fs.lang)
            support = support[0].upper() + support[1:] + ". "
        if not support:
            return None
        ask = _cta(fs, v)
        body = f"{fs.sal}, {support}{ask}"
        return Draft("planning", body, "binary_yes_stop", "effort_externalization",
                     [fs.sal])
    fs.use("trigger_text")
    topic_line = _t(fs, f"you mentioned {topic}", f"aapne {topic} ka zikr kiya tha") if topic else ""
    ask = _t(fs, "Want me to draft a plan and timeline for this? Reply YES.",
             "Iska ek plan aur timeline draft kar doon? Reply YES.")
    body = f"{fs.sal}, {topic_line}. {support}{ask}" if topic_line else f"{fs.sal}, {support}{ask}"
    return Draft("planning", body, "binary_yes_stop", "effort_externalization+specificity",
                 [fs.sal, str(topic or '')])


def seasonal(fs: FactSheet, v: int) -> Draft | None:
    """Category seasonal alerts."""
    season = N.first(fs.payload, "season", "season_name", "period")
    trends = N.first(fs.payload, "trends", "trend", "category_trend")
    support = _support(fs, "generic")
    if not season and not trends:
        if not support:
            return None
        ask = _cta(fs, v)
        body = f"{fs.sal}, {support}{ask}"
        return Draft("seasonal", body, "binary_yes_stop", "timeliness+effort_externalization",
                     [fs.sal])
    fs.use("trigger_text")
    season_line = _t(fs, f"{season} season is here", f"{season} ka mausam aa gaya hai") if season else ""
    offer = _offer_phrase(fs, oblique=True)
    ask = (_t(fs, f"Want me to draft a seasonal Google post around {offer}? Reply YES.",
              f"{offer} ke saath ek seasonal Google post draft kar doon? Reply YES.") if offer
           else _cta(fs, v))
    body = f"{fs.sal}, {season_line}. {support}{ask}" if season_line else f"{fs.sal}, {support}{ask}"
    return Draft("seasonal", body, "binary_yes_stop", "timeliness+effort_externalization",
                 [fs.sal, str(season or '')])


def gbp(fs: FactSheet, v: int) -> Draft | None:
    """Google Business Profile verification nudge."""
    uplift = N.first(fs.payload, "estimated_uplift_pct", "uplift_pct", "uplift")
    verified = N.first(fs.payload, "verified", "is_verified")
    if verified is True:
        return None  # Already verified
    fs.use("trigger_text")
    support = _support(fs, "generic")
    uplift_line = _t(fs, f"Verified profiles see up to {uplift}% more engagement. ",
                     f"Verified profiles ko {uplift}% zyada engagement milti hai. ") if uplift else ""
    ask = _t(fs, "Want me to walk you through the verification steps? Reply YES.",
             "Verification steps mein madad kar doon? Reply YES.")
    body = f"{fs.sal}, your Google Business Profile isn't verified yet. {uplift_line}{support}{ask}" if not fs.is_hi else \
           f"{fs.sal}, aapka Google Business Profile abhi verified nahi hai. {uplift_line}{support}{ask}"
    return Draft("gbp", body, "binary_yes_stop", "loss_aversion+effort_externalization",
                 [fs.sal])


def cde(fs: FactSheet, v: int) -> Draft | None:
    """Continuing education opportunity."""
    fee = N.first(fs.payload, "fee", "cost", "price")
    credits = N.first(fs.payload, "credits", "ce_credits", "cde_credits", "points")
    if not fee and not credits:
        return None
    fs.use("trigger_text")
    parts = []
    if credits:
        fs.allow(credits)
        parts.append(_t(fs, f"a CDE webinar worth {credits} credits",
                        f"ek CDE webinar, {credits} credits ke liye"))
    else:
        parts.append(_t(fs, "a CDE webinar", "ek CDE webinar"))
    if fee:
        fs.allow(fee)
        parts.append(_t(fs, f"at ₹{N.fmt_int(fee)}", f"₹{N.fmt_int(fee)} mein"))
    ask = _t(fs, "Want me to share the details? Reply YES.",
             "Details share kar doon? Reply YES.")
    body = f"{fs.sal}, {' '.join(parts)} is coming up. {ask}"
    return Draft("cde", body, "binary_yes_stop", "professional_growth+specificity",
                 [fs.sal])


FAMILIES: dict[str, Callable[[FactSheet, int], Draft | None]] = {
    "research": research, "regulation": regulation, "trend": trend,
    "perf_dip": perf_dip, "perf_spike": perf_spike, "milestone": milestone,
    "festival": festival, "weather": weather, "local_news": local_news,
    "competitor": competitor, "review_theme": review_theme,
    "curious_ask": curious_ask, "generic": generic,
    "renewal": renewal, "supply_alert": supply_alert_tmpl,
    "winback": winback, "event_promo": event_promo,
    "planning": planning, "seasonal": seasonal,
    "gbp": gbp, "cde": cde,
}


def route(kind: str) -> str:
    return KIND_TO_FAMILY.get((kind or "").lower(), "generic")


def draft(fs: FactSheet, variant: int = 0) -> Draft | None:
    family = route(fs.trigger_kind)
    d = FAMILIES[family](fs, variant)
    if d is None and family != "generic":
        d = generic(fs, variant)
    return d
