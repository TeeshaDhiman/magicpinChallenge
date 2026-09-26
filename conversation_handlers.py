"""Optional §7.4 artifact: multi-turn handling without the HTTP server.

    from conversation_handlers import respond
    state = {"merchant": {...}, "category": {...}, "trigger": {...}, "customer": None,
             "turns": [{"from": "vera", "body": "..."}], "stage": "pitch"}
    out = respond(state, "haan kar do")      # -> {"action": "send", "body": ..., ...}
    state = out["state"]                      # carry forward for the next turn

Same engine as POST /v1/reply (vera/reply.py), so behavior is identical.
"""
from __future__ import annotations

from types import SimpleNamespace

from vera.reply import respond as _respond
from vera.store import Store


def respond(state: dict, merchant_message: str, from_role: str | None = None) -> dict:
    store = Store()
    merchant = state.get("merchant") or {}
    mid = merchant.get("merchant_id") or state.get("merchant_id") or "m_unknown"
    customer = state.get("customer")
    cid = (customer or {}).get("customer_id")
    trigger = state.get("trigger") or {}
    tid = trigger.get("id") or "trg_state"
    if merchant:
        store.put("merchant", mid, 1, merchant)
    if state.get("category"):
        store.put("category", merchant.get("category_slug") or "cat", 1, state["category"])
    if customer:
        store.put("customer", cid, 1, customer)
    if trigger:
        store.put("trigger", tid, 1, trigger)
    mem = store.mem(f"customer:{cid}" if cid else mid)
    mem.sent_bodies.update(state.get("sent_bodies") or [])
    mem.merchant_msgs.extend(state.get("prior_replies") or [])
    mem.auto_replies = state.get("auto_replies", 0)

    conv_id = state.get("conversation_id") or "conv_state"
    store.conversations[conv_id] = {
        "merchant_id": mid, "customer_id": cid, "trigger_id": tid if trigger else None,
        "turns": list(state.get("turns") or []), "status": "open",
        "stage": state.get("stage", "pitch"), "abuse": state.get("abuse", 0),
        "other": state.get("other", 0), "meta": state.get("meta") or {},
    }
    body = SimpleNamespace(conversation_id=conv_id, merchant_id=mid, customer_id=cid,
                           from_role=from_role or ("customer" if cid else "merchant"),
                           message=merchant_message, received_at=None,
                           turn_number=len(state.get("turns") or []) + 1)
    out = _respond(store, body)
    conv = store.conversations[conv_id]
    out["state"] = {**state, "turns": conv["turns"], "stage": conv.get("stage"), "abuse": conv.get("abuse", 0),
                    "other": conv.get("other", 0), "status": conv["status"],
                    "sent_bodies": sorted(mem.sent_bodies), "prior_replies": list(mem.merchant_msgs),
                    "auto_replies": mem.auto_replies}
    return out
