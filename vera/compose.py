"""compose(category, merchant, trigger, customer) -> message dict.

Pure and deterministic: same inputs -> same output. Used by both the HTTP
server (/v1/tick) and the offline submission.jsonl generator, so they can't drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import facts as F
from . import playbooks as P
from . import validate as V
from . import decide as D
from . import customer as C
from . import normalize as N
import re

MAX_VARIANTS = 2

_USED_LABEL = {
    "digest_item": "the digest item (title, source, trial size)",
    "regulation_item": "the regulation item",
    "cohort_match": "merchant's matching patient cohort",
    "trend_delta": "the search-trend delta",
    "search_count": "the local search count in the trigger",
    "search_demand": "local search demand for a named service",
    "trigger_delta": "the metric delta in the trigger",
    "milestone_value": "the milestone value",
    "festival": "festival timing",
    "weather": "today's temperature",
    "news": "the local headline",
    "competitor": "the new competitor's distance",
    "review_theme": "the review theme and count",
    "dormancy": "days since last conversation",
    "trigger_text": "the trigger headline",
    "active_offer": "merchant's live offer",
    "catalog_offer": "a category catalog offer (merchant has none live)",
    "ctr_gap": "CTR vs peer average",
    "stale_posts": "days since last Google post",
    "lapsed": "lapsed customer count",
    "views": "30-day profile views",
    "calls_down": "weekly call decline",
}


@dataclass
class Trace:
    family: str = ""
    variant: int = -1
    facts_used: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    skipped_reason: str = ""
    meta: dict = field(default_factory=dict)


def compose_with_trace(category: dict, merchant: dict, trigger: dict,
                       customer: dict | None = None,
                       already_sent: set[str] | frozenset = frozenset()) -> tuple[dict | None, Trace]:
    tr = Trace()
    if not merchant:
        tr.skipped_reason = "unknown_merchant"
        return None, tr
    if trigger.get("expires_at"):
        import datetime
        try:
            exp = datetime.datetime.fromisoformat(trigger["expires_at"].replace('Z', '+00:00'))
            if datetime.datetime.now(datetime.timezone.utc) > exp:
                tr.skipped_reason = "expired"
                return None, tr
        except ValueError:
            pass
    if trigger.get("scope") == "customer" or customer:
        if not customer:
            tr.skipped_reason = "customer_context_missing"
            return None, tr
        return _compose_customer(category, merchant, trigger, customer, already_sent, tr)

    for variant in range(MAX_VARIANTS):
        fs = F.build(category or {}, merchant, trigger)
        d = P.draft(fs, variant)
        if d is None:
            tr.skipped_reason = "insufficient_trigger_data"
            return None, tr
        res = V.check(d.body, d.cta, fs, already_sent)
        tr.issues.append({"variant": variant, "hard": res.hard, "soft": res.soft})
        if res.ok:
            tr.family, tr.variant, tr.facts_used = d.family, variant, list(fs.used)
            return _package(d, fs, trigger, merchant), tr

    tr.skipped_reason = "failed_validation"
    return None, tr


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """The §7 contract. Always returns a message: every canonical test pair must get one.

    The live /v1/tick path uses compose_with_trace() instead, where 'send nothing'
    is a legitimate (and scored) decision.
    """
    msg, _ = compose_with_trace(category, merchant, trigger, customer)
    return msg or fallback(category, merchant, trigger, customer)


# ------------------------------------------------------------------ WhatsApp templates
TEMPLATE_BODY = "{{1}}, {{2}} {{3}}"


def templatize(body: str, greeting: str) -> list[str]:
    """Split body into params for TEMPLATE_BODY: greeting, main content, CTA sentence(s).

    Rendering TEMPLATE_BODY with these params reproduces the body exactly.
    """
    if body.startswith(greeting + ", "):
        rest = body[len(greeting) + 2:]
        sents = re.split(r"(?<=[.?!])\s+", rest)
        k = 1
        if len(sents) >= 3 and sents[-2].endswith("?") and re.match(r"(Reply|Bas|YES)", sents[-1]):
            k = 2
        if len(sents) > k:
            params = [greeting, " ".join(sents[:-k]), " ".join(sents[-k:])]
            if render(params) == body:
                return params
    return [body]


def render(params: list[str]) -> str:
    if len(params) == 1:
        return params[0]
    out = TEMPLATE_BODY
    for i, v in enumerate(params, 1):
        out = out.replace("{{" + str(i) + "}}", v)
    return out


# ------------------------------------------------------------------ customer-facing
def _compose_customer(category, merchant, trigger, customer, already_sent, tr: Trace):
    d, fs, reason = C.compose(category, merchant, trigger, customer)
    if d is None:
        tr.skipped_reason = reason
        return None, tr
    res = V.check(d.body, d.cta, fs, already_sent)
    tr.issues.append({"variant": 0, "hard": res.hard, "soft": res.soft})
    if not res.ok:
        tr.skipped_reason = "failed_validation"
        return None, tr
    tr.family = f"customer_{d.family}"
    tr.meta = {"slots": [s.label for s in d.slots], "slot_short": [s.short for s in d.slots],
               "customer_family": d.family, "customer_name": d.params[0].replace("Hi ", "", 1),
               "business_voice": d.params[1]}
    cid = customer.get("customer_id") or trigger.get("customer_id") or ""
    msg = {
        "body": d.body,
        "cta": d.cta,
        "send_as": "merchant_on_behalf",
        "suppression_key": trigger.get("suppression_key") or f"{fs.trigger_kind}:{cid}:{fs.trigger_id}",
        "rationale": (f"{fs.trigger_kind} trigger for customer {cid} ({customer.get('state', 'unknown state')}) "
                      f"-> customer {d.family} message, sent as the merchant. Consent: {d.consent_note}. "
                      f"Anchored on: customer's last visit and service history"
                      f"{', open slots ranked by their stated preference' if d.slots else ''}"
                      f"{', merchant live offer' if fs.top_offer and fs.top_offer in d.body else ''}. "
                      f"Language: {'Hindi-English mix (customer preference)' if fs.is_hi else 'English'}."),
        "template_name": d.template_name,
        "template_params": templatize(d.body, d.params[0]),
    }
    return msg, tr


# ------------------------------------------------------------------ fallback (submission only)
def fallback(category: dict, merchant: dict, trigger: dict, customer: dict | None) -> dict:
    """Grounded minimal message when the trigger lacks the fields its playbook needs."""
    merchant = merchant or {}
    fs = F.build(category or {}, merchant, trigger)
    if customer:
        F._harvest_numbers(customer, fs.allowed_numbers)
    t = (lambda en, hi: hi if fs.is_hi else en)
    if trigger.get("scope") == "customer" and customer:
        # Customer exists but we may not contact them (consent) or lack a usable kind: tell the merchant.
        cname = str(N.get(customer, "identity", "name", default="") or "").split(" ")[0] or "A customer"
        last = C._fmt_date(N.get(customer, "relationship", "last_visit"))
        seen = t(f" hasn't visited since {last}", f" {last} ke baad nahi aaye") if last else t(" is due for a follow-up", " ka follow-up due hai")
        _, reason = C._consent(customer, "winback")
        body = t(f"{fs.sal}, {cname}{seen}. Their consent doesn't cover promotional messages, so I haven't contacted them. "
                 f"Want me to draft a message you can send personally? Reply YES.",
                 f"{fs.sal}, {cname}{seen}. Unki consent promotional messages cover nahi karti, isliye maine unse contact nahi kiya. "
                 f"Aap khud bhej sakein, aisa ek message draft kar doon? Reply YES.")
        why = f"customer trigger blocked by consent ({reason}); informed the merchant instead of messaging the customer"
    elif trigger.get("scope") == "customer" and not customer:
        body = t(f"{fs.sal}, one of your {fs.audience} is due for a follow-up. "
                 f"Want me to send them a reminder from your number? Reply YES.",
                 f"{fs.sal}, aapke ek {fs.audience[:-1] if fs.audience.endswith('s') else fs.audience} ka follow-up due hai. "
                 f"Aapke number se unhe reminder bhej doon? Reply YES.")
        why = "customer-scope trigger without a customer context: asked the merchant before contacting anyone"
    else:
        fact = D.choose_support(fs, "generic")
        if not fact and fs.support:
            fact = fs.support[0]
            fs.use(fact.key)
        lead = (fact.text(fs.lang) + ". ") if fact else ""   # follows "Name, " so stays lower-case
        offer = P._offer_phrase(fs, oblique=True)
        ask = (t(f"Want me to draft a fresh Google post featuring {offer}? Reply YES.",
                 f"{offer} ke saath ek fresh Google post draft kar doon? Reply YES.") if offer
               else t("Want me to draft a fresh Google post for you? Reply YES.",
                      "Aapke liye ek fresh Google post draft kar doon? Reply YES."))
        body = f"{fs.sal}, {lead}{ask}"
        why = "trigger lacked the fields its playbook needs, so the message rests on the strongest grounded merchant fact"
    res = V.check(body, "binary_yes_stop", fs)
    if not res.ok:
        body = t(f"{fs.sal}, want me to draft a fresh Google post for your {fs.business}? Reply YES.",
                 f"{fs.sal}, aapke {fs.business} ke liye ek fresh Google post draft kar doon? Reply YES.")
    mid = merchant.get("merchant_id") or ""
    return {
        "body": body,
        "cta": "binary_yes_stop",
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or f"{fs.trigger_kind}:{mid}:{fs.trigger_id}",
        "rationale": f"{fs.trigger_kind or 'unknown'} trigger -> fallback. {why}. No details invented.",
        "template_name": "vera_fallback_v1",
        "template_params": templatize(body, fs.sal),
    }


def _package(d: P.Draft, fs: F.FactSheet, trigger: dict, merchant: dict) -> dict:
    if not fs.decision:          # trigger-only families never asked for support
        D.choose_support(fs, d.family if d.family in D.PAIRING else "generic")
    mid = merchant.get("merchant_id") or trigger.get("merchant_id") or ""
    anchors = ", ".join(_USED_LABEL.get(k, k.replace("_", " ")) for k in fs.used) or "trigger only"
    rationale = (
        f"{fs.trigger_kind or 'unknown'} trigger ({trigger.get('source', 'n/a')}, urgency {fs.urgency}) "
        f"-> {d.family} playbook. Anchored on: {anchors}. Decision: {fs.decision}. "
        f"Levers: {d.lever.replace('+', ', ').replace('_', ' ')}. "
        f"Language: {'Hindi-English mix' if fs.is_hi else 'English'}."
    )
    return {
        "body": d.body,
        "cta": d.cta,
        "send_as": "vera",
        "suppression_key": trigger.get("suppression_key") or f"{fs.trigger_kind}:{mid}:{fs.trigger_id}",
        "rationale": rationale,
        "template_name": d.template_name,
        "template_params": templatize(d.body, fs.sal),
    }
