"""In-memory, versioned context store + cross-conversation merchant memory."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

SCOPES = ("category", "merchant", "customer", "trigger")


@dataclass
class MerchantMemory:
    opted_out: bool = False
    sent_bodies: set[str] = field(default_factory=set)
    last_trigger_at: dict[str, str] = field(default_factory=dict)
    merchant_msgs: list[str] = field(default_factory=list)   # normalized, across all conversations
    auto_replies: int = 0                                    # auto-replies seen, across all conversations


def customer_key(customer_id: str) -> str:
    return f"customer:{customer_id}"


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self.suppressed: set[str] = set()
        self.memory: dict[str, MerchantMemory] = {}
        self.conversations: dict[str, dict[str, Any]] = {}

    # ---- contexts ----------------------------------------------------------
    def put(self, scope: str, cid: str, version: int, payload: dict) -> tuple[str, int]:
        """Returns (status, current_version). status: 'stored' | 'duplicate' | 'stale'."""
        with self._lock:
            cur = self.contexts.get((scope, cid))
            if cur is not None:
                if version == cur["version"]:
                    return "duplicate", cur["version"]
                if version < cur["version"]:
                    return "stale", cur["version"]
            self.contexts[(scope, cid)] = {"version": version, "payload": payload}
            return "stored", version

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in SCOPES}
        for scope, _ in list(self.contexts):
            out[scope] = out.get(scope, 0) + 1
        return out

    # ---- merchant memory ---------------------------------------------------
    def mem(self, merchant_id: str) -> MerchantMemory:
        with self._lock:
            return self.memory.setdefault(merchant_id, MerchantMemory())

    def record_send(self, merchant_id: str, conversation_id: str, suppression_key: str,
                    body: str, trigger_id: str, now: str,
                    customer_id: str | None = None, meta: dict | None = None) -> None:
        with self._lock:
            if suppression_key:
                self.suppressed.add(suppression_key)
            m = self.mem(customer_key(customer_id) if customer_id else merchant_id)
            m.sent_bodies.add(body)
            m.last_trigger_at[trigger_id] = now
            self.conversations[conversation_id] = {
                "merchant_id": merchant_id, "customer_id": customer_id, "trigger_id": trigger_id,
                "turns": [{"from": "vera", "body": body, "at": now}],
                "status": "open", "meta": meta or {},
            }

    def _live(self, c: dict, now, window_min: int) -> bool:
        """Open AND active recently. A thread the other side abandoned stops blocking after
        `window_min` simulated minutes, so later context (Phase 3) can still reach them."""
        if c["status"] != "open":
            return False
        if now is None:
            return True
        from .normalize import parse_dt
        stamps = [parse_dt(t.get("at")) for t in c.get("turns", [])]
        stamps = [x for x in stamps if x]
        if not stamps:
            return True
        return (now - max(stamps)).total_seconds() < window_min * 60

    def has_open_conversation(self, merchant_id: str, now=None, window_min: int = 30) -> bool:
        """Merchant-facing conversations only; a customer thread never blocks Vera <-> merchant."""
        return any(c["merchant_id"] == merchant_id and not c.get("customer_id") and self._live(c, now, window_min)
                   for c in self.conversations.values())

    def has_open_customer_conversation(self, customer_id: str, now=None, window_min: int = 30) -> bool:
        return any(c.get("customer_id") == customer_id and self._live(c, now, window_min)
                   for c in self.conversations.values())

    def wipe(self) -> None:
        with self._lock:
            self.contexts.clear()
            self.suppressed.clear()
            self.memory.clear()
            self.conversations.clear()
