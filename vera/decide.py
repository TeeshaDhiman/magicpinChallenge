"""Decision layer: choose the ONE merchant signal that should drive the message,
and rank competing triggers for a merchant, before any text is written.

The judge scores "decision quality": does the bot combine trigger + merchant state
+ category fit, and pick the best signal rather than listing every fact?

Each trigger family lists, in order, the merchant facts that genuinely strengthen
it, with the reason. A fact not in its family's list is never used, even if it's
available. Families with an empty list are carried by the trigger alone, because
adding account stats would only dilute the hook.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import normalize as N
from .facts import Fact, FactSheet

PAIRING: dict[str, list[tuple[str, str]]] = {
    "perf_dip": [
        ("search_demand", "people are actively searching for this service nearby, so the dip is lost demand, not missing demand"),
        ("ctr_gap", "a below-peer click-through is the likeliest reason fewer searchers are turning into contacts"),
        ("stale_posts", "a profile with no recent post loses visibility, which fits a falling metric"),
    ],
    "perf_spike": [
        ("search_demand", "the spike has a concrete source: local searches for a named service"),
        ("stale_posts", "the extra traffic is landing on a profile with no recent post, so there is something to convert it with"),
    ],
    "trend": [
        ("search_demand", "shows the trend is already happening in the merchant's own locality"),
        ("ctr_gap", "rising searches only pay off if the listing gets clicked, and click-through is below peers"),
    ],
    "festival": [
        ("search_demand", "local demand for a named service gives the festive post something specific to sell"),
        ("stale_posts", "festive searchers will land on an out-of-date profile unless a post goes up"),
    ],
    "competitor": [
        ("search_demand", "the new competitor is competing for these exact local searches"),
        ("ctr_gap", "a nearby competitor matters most when the merchant already loses clicks to peers"),
        ("stale_posts", "an inactive profile is the easiest place for a new competitor to win"),
    ],
    "curious_ask": [
        ("search_demand", "proof of what locals are searching for makes the ask concrete"),
        ("stale_posts", "the merchant's answer becomes exactly the post the profile is missing"),
        ("views", "shows the audience a post would reach, which makes the ask concrete"),
    ],
    "generic": [
        ("search_demand", "a specific local benchmark: people searching for a named service"),
        ("stale_posts", "gives the event a concrete action: a fresh post"),
        ("views", "shows the audience a post would reach"),
    ],
    # Trigger alone carries these; account stats would dilute the hook.
    "research": [], "regulation": [], "milestone": [], "review_theme": [],
    "weather": [], "local_news": [],
    # New families
    "renewal": [
        ("lapsed", "lapsed customers make the renewal more compelling"),
        ("views", "profile visibility at stake if renewal lapses"),
    ],
    "supply_alert": [],
    "winback": [
        ("lapsed", "shows how many customers moved away"),
        ("ctr_gap", "below-peer CTR shows lost visibility"),
        ("stale_posts", "an inactive profile accelerates churn"),
    ],
    "event_promo": [
        ("search_demand", "local searches validate crowd interest"),
        ("views", "profile views show the audience that would see a post"),
    ],
    "planning": [
        ("search_demand", "local demand validates the planned initiative"),
        ("views", "profile views confirm audience reach"),
    ],
    "seasonal": [
        ("search_demand", "seasonal demand shift visible in local searches"),
        ("stale_posts", "profile needs a seasonal refresh"),
    ],
    "gbp": [
        ("ctr_gap", "unverified profile is one reason for low CTR"),
        ("views", "views without verification means missed trust signals"),
    ],
    "cde": [],
}

TRIGGER_ONLY_REASON = "the trigger itself is the hook; adding account stats would dilute it"


def choose_support(fs: FactSheet, family: str) -> Fact | None:
    """Pick the single best merchant fact for this family and record why."""
    for key, reason in PAIRING.get(family, []):
        fact = next((f for f in fs.support if f.key == key), None)
        if fact:
            fs.use(key)
            fs.decision = f"paired with {key.replace('_', ' ')} because {reason}"
            return fact
    fs.decision = TRIGGER_ONLY_REASON if not PAIRING.get(family) else \
        "no merchant fact strengthens this trigger, so it stands alone"
    return None


# --------------------------------------------------------------------------
# Prior conversation behavior
# --------------------------------------------------------------------------
_IGNORED = {"ignored", "no_reply", "unanswered", "none", "seen_no_reply", "delivered"}
_REPLIED = {"merchant_replied", "replied", "engaged", "accepted", "clicked"}


@dataclass
class History:
    unanswered_streak: int      # trailing Vera messages with no merchant reply
    replied_ever: bool
    ignored_families: dict[str, int]
    replied_families: set[str]
    unsubscribed_families: set[str]
    sent_keys: set[str]         # suppression keys / trigger ids already sent per history


def read_history(merchant: dict, route) -> History:
    """route: kind -> family (passed in to avoid an import cycle)."""
    turns = merchant.get("conversation_history") or []
    if isinstance(turns, dict):
        turns = turns.get("turns") or []
    streak, replied_ever = 0, False
    ignored: dict[str, int] = {}
    replied: set[str] = set()
    unsub: set[str] = set()
    sent: set[str] = set()

    for t in turns:
        if not isinstance(t, dict):
            continue
        eng = str(t.get("engagement") or "").lower()
        who = str(t.get("from") or "").lower()
        kind = N.first(t, "trigger_kind", "kind", "topic")
        fam = route(kind) if kind else None
        for k in ("suppression_key", "trigger_id"):
            if t.get(k):
                sent.add(str(t[k]))
        if who in ("merchant", "mx") or eng in _REPLIED:
            replied_ever = True
            streak = 0
            if fam:
                replied.add(fam)
        elif who == "vera":
            if "unsubscribe" in eng and fam:
                unsub.add(fam)
            if eng in _IGNORED:
                streak += 1
                if fam:
                    ignored[fam] = ignored.get(fam, 0) + 1
    return History(streak, replied_ever, ignored, replied, unsub, sent)


def trigger_score(fs: FactSheet, family: str, hist: History) -> tuple[int, list[str]]:
    """Higher is better. Urgency dominates; state fit and past behavior break ties."""
    notes = []
    score = fs.urgency * 10
    pairs = [k for k, _ in PAIRING.get(family, [])]
    if any(f.key in pairs for f in fs.support):
        score += 4
        notes.append("merchant state reinforces it")
    seg = str((fs.digest_item or {}).get("patient_segment") or "")
    if family == "research" and "high_risk" in seg and any("high_risk" in s for s in fs.signals):
        score += 4
        notes.append("the research matches the merchant's patient cohort")
    if family in hist.replied_families:
        score += 3
        notes.append("merchant replied to this kind before")
    if hist.ignored_families.get(family):
        score -= 6 * hist.ignored_families[family]
        notes.append(f"merchant ignored this kind {hist.ignored_families[family]}x")
    return score, notes
