"""Tick policy: decide which triggers are worth a message right now.

Restraint is scored, so the policy filters aggressively before composing:
  expired -> already sent (suppression key) -> opted-out merchant ->
  merchant already in an open conversation (unless the new trigger is urgent) ->
  one action per merchant per tick (highest urgency, then soonest expiry).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import normalize as N
from .compose import compose_with_trace
from . import decide as D
from . import facts as F
from . import playbooks as P
from .store import Store, customer_key

MAX_ACTIONS_PER_TICK = 20
URGENT = 4
# The local judge_simulator stamps ticks with the wall clock, while dataset triggers carry
# fixed 2026 expiries, so everything looks expired locally. Set VERA_IGNORE_EXPIRY=1 for
# local simulator runs only; the real harness uses simulated time and expiry must hold.
IGNORE_EXPIRY = os.environ.get("VERA_IGNORE_EXPIRY") == "1"


def _unique_conv_id(store: Store, base: str) -> str:
    """Brief §2.2: reusing a conversation_id on /tick is invalid, even for an updated trigger."""
    cid, n = base, 1
    while cid in store.conversations:
        n += 1
        cid = f"{base}_{n}"
    return cid


def plan_tick(store: Store, now_iso: str, available_triggers: list[str]) -> tuple[list[dict], list[dict]]:
    """Returns (actions, decisions). decisions explains every trigger considered (for logs/tests)."""
    now = N.parse_dt(now_iso) or datetime.now(timezone.utc)
    decisions: list[dict] = []
    candidates: list = []
    customer_candidates: list = []

    for tid in available_triggers:
        trg = store.get("trigger", tid)
        if not trg:
            decisions.append({"trigger_id": tid, "skip": "unknown_trigger"})
            continue
        trg = {**trg, "id": trg.get("id") or tid}
        mid = trg.get("merchant_id") or N.get(trg, "payload", "merchant_id")
        merchant = store.get("merchant", mid)
        if not merchant:
            decisions.append({"trigger_id": tid, "skip": "unknown_merchant"})
            continue
        exp = N.parse_dt(trg.get("expires_at"))
        if exp and exp <= now and not IGNORE_EXPIRY:
            decisions.append({"trigger_id": tid, "skip": "expired"})
            continue
        if trg.get("suppression_key") and trg["suppression_key"] in store.suppressed:
            decisions.append({"trigger_id": tid, "skip": "suppressed"})
            continue
        if store.mem(mid).opted_out:
            decisions.append({"trigger_id": tid, "skip": "merchant_opted_out"})
            continue
        category = store.get("category", merchant.get("category_slug")) or {"slug": merchant.get("category_slug")}

        # ---- customer-facing: restraint is per customer, and never blocked by Vera<->merchant threads
        if trg.get("scope") == "customer" or trg.get("customer_id"):
            cid = trg.get("customer_id") or N.get(trg, "payload", "customer_id")
            customer = store.get("customer", cid)
            if not customer:
                decisions.append({"trigger_id": tid, "skip": "customer_context_missing"})
                continue
            if store.mem(customer_key(cid)).opted_out:
                decisions.append({"trigger_id": tid, "skip": "customer_opted_out"})
                continue
            if store.has_open_customer_conversation(cid, now):
                decisions.append({"trigger_id": tid, "skip": "customer_conversation_in_flight"})
                continue
            customer_candidates.append((trg, merchant, category, customer, cid))
            continue

        if store.has_open_conversation(mid, now) and int(trg.get("urgency") or 1) < URGENT:
            decisions.append({"trigger_id": tid, "skip": "conversation_in_flight"})
            continue

        # prior conversation behavior (from the pushed merchant context)
        family = P.route(trg.get("kind"))
        hist = D.read_history(merchant, P.route)
        urgency = int(trg.get("urgency") or 1)
        if trg["id"] in hist.sent_keys or trg.get("suppression_key") in hist.sent_keys:
            decisions.append({"trigger_id": tid, "skip": "already_sent_per_history"})
            continue
        if family in hist.unsubscribed_families:
            decisions.append({"trigger_id": tid, "skip": "merchant_unsubscribed_from_topic"})
            continue
        if hist.unanswered_streak >= 3 and urgency < URGENT:
            decisions.append({"trigger_id": tid, "skip": "three_unanswered_nudges"})
            continue
        score, notes = D.trigger_score(F.build(category, merchant, trg), family, hist)
        candidates.append((trg, merchant, category, score, notes))

    # one per merchant: best decision score, then soonest expiry, then id (deterministic)
    far = datetime.max.replace(tzinfo=timezone.utc)
    candidates.sort(key=lambda c: (-c[3], N.parse_dt(c[0].get("expires_at")) or far, c[0]["id"]))
    ranked: dict[str, list[tuple[dict, dict, dict, list[str]]]] = {}
    for trg, merchant, category, _score, notes in candidates:
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        ranked.setdefault(mid, []).append((trg, merchant, category, notes))

    actions: list[dict] = []
    for mid, options in ranked.items():
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            decisions.append({"merchant_id": mid, "skip": "tick_cap"})
            continue
        for i, (trg, merchant, category, notes) in enumerate(options):
            msg, trace = compose_with_trace(category, merchant, trg, None,
                                            already_sent=store.mem(mid).sent_bodies)
            if msg is None:
                decisions.append({"trigger_id": trg["id"], "skip": trace.skipped_reason, "issues": trace.issues})
                continue
            if notes:
                msg["rationale"] += f" Ranked first because: {'; '.join(notes)}."
            held = [f"{str(t.get('kind', '?')).replace('_', ' ')} (urgency {t.get('urgency', '?')})"
                    for t, *_ in options[i + 1:]]
            if held:
                more = f" +{len(held) - 3} more" if len(held) > 3 else ""
                msg["rationale"] += f" Held back for this merchant: {', '.join(held[:3])}{more}."
            conv_id = _unique_conv_id(store, f"conv_{mid}_{trg['id']}")
            actions.append({"conversation_id": conv_id, "merchant_id": mid, "customer_id": None,
                            "trigger_id": trg["id"], **msg})
            store.record_send(mid, conv_id, msg["suppression_key"], msg["body"], trg["id"], now_iso)
            decisions.append({"trigger_id": trg["id"], "sent": conv_id, "family": trace.family,
                              "facts": trace.facts_used, "soft_issues": trace.issues[-1]["soft"]})
            for t, *_ in options[i + 1:]:
                decisions.append({"trigger_id": t["id"], "skip": "lower_priority_same_merchant"})
            break
    # ---- customer-facing sends: one per customer, most urgent first
    far = datetime.max.replace(tzinfo=timezone.utc)
    customer_candidates.sort(key=lambda c: (-int(c[0].get("urgency") or 1),
                                            N.parse_dt(c[0].get("expires_at")) or far, c[0]["id"]))
    done_customers: set[str] = set()
    for trg, merchant, category, customer, cid in customer_candidates:
        if cid in done_customers:
            decisions.append({"trigger_id": trg["id"], "skip": "lower_priority_same_customer"})
            continue
        if len(actions) >= MAX_ACTIONS_PER_TICK:
            decisions.append({"trigger_id": trg["id"], "skip": "tick_cap"})
            continue
        mid = merchant.get("merchant_id") or trg.get("merchant_id")
        msg, trace = compose_with_trace(category, merchant, trg, customer,
                                        already_sent=store.mem(customer_key(cid)).sent_bodies)
        if msg is None:
            decisions.append({"trigger_id": trg["id"], "skip": trace.skipped_reason})
            continue
        conv_id = _unique_conv_id(store, f"conv_{cid}_{trg['id']}")
        actions.append({"conversation_id": conv_id, "merchant_id": mid, "customer_id": cid,
                        "trigger_id": trg["id"], **msg})
        store.record_send(mid, conv_id, msg["suppression_key"], msg["body"], trg["id"], now_iso,
                          customer_id=cid, meta=trace.meta)
        done_customers.add(cid)
        decisions.append({"trigger_id": trg["id"], "sent": conv_id, "family": trace.family})
    return actions, decisions
