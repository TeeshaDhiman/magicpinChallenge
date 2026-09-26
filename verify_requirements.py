"""Verify every testable requirement in the two challenge briefs against a live bot.

    python verify_requirements.py http://localhost:8080
    python verify_requirements.py https://your-bot.example.com --dataset path/to/dataset

Each check cites the brief section it comes from and prints the evidence it saw.
Uses the dataset (default: ./fixtures) to push contexts, then calls /v1/teardown at
the end. Don't run it against a bot while the judge's test window is live.
Stdlib only.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import time
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vera import dataset as DS  # noqa: E402

RESULTS: list[tuple[str, str, bool, str]] = []
NOW = "2026-04-26T10:00:00Z"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


class Bot:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.slowest = 0.0

    def call(self, method, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        t0 = time.perf_counter()
        try:
            with request.urlopen(req, timeout=30) as r:
                status, txt = r.status, r.read().decode()
        except error.HTTPError as e:
            status, txt = e.code, e.read().decode()
        self.slowest = max(self.slowest, time.perf_counter() - t0)
        try:
            return status, json.loads(txt)
        except ValueError:
            return status, {"_raw": txt}

    def push(self, scope, cid, version, payload):
        return self.call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                                 "payload": payload, "delivered_at": NOW})

    def tick(self, ids, now=NOW):
        s, p = self.call("POST", "/v1/tick", {"now": now, "available_triggers": ids})
        return s, (p or {}).get("actions") if isinstance(p, dict) else None

    def reply(self, conv, mid, msg, cid=None, turn=2):
        return self.call("POST", "/v1/reply", {"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                               "from_role": "customer" if cid else "merchant", "message": msg,
                                               "received_at": NOW, "turn_number": turn})[1]


def check(section: str, requirement: str, ok: bool, evidence: str = "") -> bool:
    RESULTS.append((section, requirement, bool(ok), evidence))
    print(f"  [{'PASS' if ok else 'FAIL'}] {section:12s} {requirement}" + (f"\n{'':20s}-> {evidence}" if evidence else ""))
    return ok


def nums(obj, into: set):
    if isinstance(obj, dict):
        for v in obj.values():
            nums(v, into)
    elif isinstance(obj, list):
        for v in obj:
            nums(v, into)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        into.add(str(obj).rstrip("0").rstrip(".") if "." in str(obj) else str(obj))
        if isinstance(obj, float) and abs(obj) <= 1.5:
            into.update({f"{abs(obj) * 100:.0f}", f"{abs(obj) * 100:.1f}".rstrip("0").rstrip(".")})
    elif isinstance(obj, str):
        for m in re.findall(r"\d[\d,]*(?:\.\d+)?", obj):
            into.add(m.replace(",", "").lstrip("0") or "0")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bot = Bot(sys.argv[1])
    ds = DS.load(sys.argv[sys.argv.index("--dataset") + 1] if "--dataset" in sys.argv else
                 Path(__file__).resolve().parent / "fixtures")
    print(f"Target {bot.base}; dataset: {len(ds.categories)} categories, {len(ds.merchants)} merchants, "
          f"{len(ds.customers)} customers, {len(ds.triggers)} triggers\n")
    bot.call("POST", "/v1/teardown", {})

    print("TESTING BRIEF §2: endpoints and response schemas")
    s, h = bot.call("GET", "/v1/healthz")
    check("T§2.4", "healthz 200 {status: ok, uptime_seconds, contexts_loaded{4 scopes}}",
          s == 200 and h.get("status") == "ok" and isinstance(h.get("uptime_seconds"), int)
          and set(h.get("contexts_loaded", {})) == {"category", "merchant", "customer", "trigger"}, json.dumps(h))
    s, md = bot.call("GET", "/v1/metadata")
    keys = ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]
    check("T§2.5", "metadata has all 7 fields", s == 200 and all(k in md for k in keys), f"missing: {[k for k in keys if k not in md]}")
    check("T§2.5", "team identity filled in (YOUR ACTION: team.json or env vars)",
          md.get("team_name") not in ("unset", "", None) and md.get("contact_email") not in ("unset", "", None),
          f"team_name={md.get('team_name')!r} contact_email={md.get('contact_email')!r}")

    cat0 = next(iter(ds.categories))
    s, p = bot.push("category", cat0, 1, ds.categories[cat0])
    check("T§2.1", "context 200 {accepted: true, ack_id, stored_at}",
          s == 200 and p.get("accepted") is True and p.get("ack_id") and p.get("stored_at"), json.dumps(p))
    s, p = bot.push("category", cat0, 1, ds.categories[cat0])
    check("T§2.1", "same (scope, id, version) is an idempotent no-op", s == 200 and p.get("accepted") is True, json.dumps(p))
    bot.push("category", cat0, 3, ds.categories[cat0])
    s, p = bot.push("category", cat0, 2, ds.categories[cat0])
    check("T§2.1", "lower version -> 409 {accepted:false, reason:stale_version, current_version}",
          s == 409 and p == {"accepted": False, "reason": "stale_version", "current_version": 3}, json.dumps(p))
    s, p = bot.push("bogus", "x", 1, {})
    check("T§2.1", "bad scope -> 400 {accepted:false, reason:invalid_scope, details}",
          s == 400 and p.get("accepted") is False and p.get("reason") == "invalid_scope" and "details" in p, json.dumps(p))
    s, p = bot.call("POST", "/v1/context", raw=b"{not json")
    check("T§2.1", "malformed body -> 400 {accepted:false, reason, details}",
          s == 400 and p.get("accepted") is False and "reason" in p, json.dumps(p)[:120])

    print("\nTESTING BRIEF §4 Phase 1: warmup")
    for slug, c in ds.categories.items():
        bot.push("category", slug, 5, c)
    for scope, coll in (("merchant", ds.merchants), ("customer", ds.customers)):
        for k, v in coll.items():
            bot.push(scope, k, 1, v)
    s, h = bot.call("GET", "/v1/healthz")
    loaded = h.get("contexts_loaded", {})
    check("T§4.1", "contexts_loaded reflects every pushed base context",
          loaded.get("category") == len(ds.categories) and loaded.get("merchant") == len(ds.merchants)
          and loaded.get("customer") == len(ds.customers), json.dumps(loaded))

    print("\nTESTING BRIEF §2.2 + §4 Phase 2: ticks")
    s, acts = bot.tick([])
    check("T§2.2", "tick with nothing to do -> 200 {actions: []}", s == 200 and acts == [], f"{s} {acts}")
    for k, v in ds.triggers.items():
        bot.push("trigger", k, 1, v)
    grounded = set()
    for coll in (ds.categories, ds.merchants, ds.customers, ds.triggers):
        nums(list(coll.values()), grounded)
    s, acts = bot.tick(list(ds.triggers))
    acts = acts or []
    need = {"conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id", "template_name",
            "template_params", "body", "cta", "suppression_key", "rationale"}
    check("T§2.2", "every action has all 11 fields", acts and all(need <= set(a) for a in acts),
          f"{len(acts)} actions; missing: {[sorted(need - set(a)) for a in acts if not need <= set(a)][:2]}")
    check("T§5", "≤ 20 actions per tick", len(acts) <= 20, f"{len(acts)} actions")
    check("T§10", "no action has an empty body", all(a.get("body", "").strip() for a in acts))
    pairs = [(a["merchant_id"], a["conversation_id"]) for a in acts]
    check("FAQ", "one action per (merchant_id, conversation_id) per tick", len(pairs) == len(set(pairs)))
    check("B§5", "send_as is 'vera' or 'merchant_on_behalf'", all(a["send_as"] in ("vera", "merchant_on_behalf") for a in acts))
    cust_acts = [a for a in acts if a.get("customer_id")]
    check("B§5/App.B", "customer-scope sends go out as merchant_on_behalf",
          cust_acts and all(a["send_as"] == "merchant_on_behalf" for a in cust_acts),
          f"{len(cust_acts)} customer sends")
    check("B§5.1", "first outbound carries a template (template_name + template_params)",
          all(a["template_name"] and a["template_params"] for a in acts))
    ungrounded = []
    for a in acts:
        for m in re.findall(r"\d[\d,]*(?:\.\d+)?", a["body"]):
            n = m.replace(",", "").lstrip("0") or "0"
            n = n.rstrip("0").rstrip(".") if "." in n else n
            if n not in grounded and n not in {"1", "2", "6"}:
                ungrounded.append(f"{a['trigger_id']}: {m}")
    check("B§5.8", "don't fabricate: every number in every body exists in the pushed contexts",
          not ungrounded, "; ".join(ungrounded[:5]) or f"checked {sum(len(re.findall(r'[0-9]', a['body'])) > 0 for a in acts)} bodies")
    check("B§5.3", "single primary CTA (no multi-option replies outside booking)",
          all(not (set(re.findall(r"\b[Rr]eply\s+([A-Z0-9]+)\b", a["body"])) - {"YES", "STOP"})
              or a["cta"] == "multi_choice_slot" for a in acts))
    langs = {m: "hi" in [str(x).lower() for x in (ds.merchants[m].get("identity") or {}).get("languages", [])] for m in ds.merchants}
    hin = re.compile(r"\b(aapke|aapka|hai|hain|kar|doon|bhejiye)\b", re.I)
    wrong = [a["trigger_id"] for a in acts if a["send_as"] == "vera"
             and bool(hin.search(a["body"])) != langs.get(a["merchant_id"], False)]
    check("FAQ", "language matches merchant identity.languages", not wrong, f"mismatch: {wrong}")
    expired = [t for t, v in ds.triggers.items() if v.get("expires_at", "9999") < NOW]
    check("FAQ", "restraint: expired triggers are not sent", not any(a["trigger_id"] in expired for a in acts),
          f"{len(expired)} expired in dataset")
    s, again = bot.tick(list(ds.triggers))
    check("B§4/T§2.2", "suppression: the same triggers are not re-sent next tick",
          not set(a["trigger_id"] for a in again or []) & set(a["trigger_id"] for a in acts))
    all_convs = [a["conversation_id"] for a in acts + (again or [])]
    check("T§2.2", "conversation_id never reused across ticks", len(all_convs) == len(set(all_convs)))

    print("\nTESTING BRIEF §2.3 + §10: replies")
    merchant_act = next((a for a in acts if a["send_as"] == "vera"), None)
    shapes_ok, bodies = True, [merchant_act["body"]] if merchant_act else []
    if merchant_act:
        for i, msg in enumerate(["what exactly will you post?", "and how long does it take?", "ok lets do it"]):
            d = bot.reply(merchant_act["conversation_id"], merchant_act["merchant_id"], msg, turn=i + 2)
            if d.get("action") == "send":
                shapes_ok &= bool(d.get("body", "").strip()) and "rationale" in d
                bodies.append(d["body"])
            elif d.get("action") == "wait":
                shapes_ok &= isinstance(d.get("wait_seconds"), int)
            elif d.get("action") != "end":
                shapes_ok = False
        check("T§2.3", "reply returns valid send|wait|end shapes", shapes_ok)
        check("T§10", "no verbatim repeat within a conversation", len(bodies) == len(set(bodies)))
        check("T§4.4", "intent: 2 qualifying turns then 'ok lets do it' -> action, no qualifying question",
              bodies and not any(q in bodies[-1].lower() for q in QUALIFYING)
              and any(w in bodies[-1].lower() for w in ("done", "ho gaya", "draft", "confirm", "next")), bodies[-1][:100])
    mids = list(ds.merchants)
    outs = [bot.reply(f"conv_auto_{i}", mids[0], "Thank you for contacting us! Our team will respond shortly.", turn=i + 1)
            for i in range(1, 5)]
    first_end = next((i for i, o in enumerate(outs) if o.get("action") == "end"), None)
    check("T§4.4", "auto-reply sent 4x -> bot exits (ends by turn 2)", first_end is not None and first_end <= 1,
          str([o.get("action") for o in outs]))
    m2 = mids[1] if len(mids) > 1 else mids[0]
    d1 = bot.reply("conv_hostile_v", m2, "you people are useless")
    d2 = bot.reply("conv_hostile_v", m2, "can you also help me file my GST?", turn=3)
    check("T§4.4", "hostile then GST -> polite apology, declines off-topic, redirects to the task",
          d1.get("action") == "send" and "sorry" in d1.get("body", "").lower()
          and d2.get("action") == "send" and "outside" in d2.get("body", "").lower(), f"{d1.get('body', '')[:60]} | {d2.get('body', '')[:60]}")
    d = bot.reply("conv_stop_v", m2, "Stop messaging me. This is useless spam.")
    check("T§4.4", "explicit opt-out -> end", d.get("action") == "end")
    d = bot.reply("conv_defer_v", mids[-1], "busy right now, message me tomorrow")
    check("T§2.3", "merchant asks for time -> wait with wait_seconds", d.get("action") == "wait" and d.get("wait_seconds"))
    d = bot.reply("conv_cold_v", "never_seen_merchant", "hello?", turn=1)
    check("FAQ", "reply on an unknown conversation/merchant is still valid", d.get("action") in ("send", "wait", "end"))

    print("\nTESTING BRIEF §4 Phase 3: adaptive context injection")
    research = next((t for t in ds.triggers.values() if t.get("kind") == "research_digest"), None)
    if research:
        m = ds.merchant_for(research)
        cat = copy.deepcopy(ds.category_for(m))
        cat.setdefault("digest", []).append({"id": "d_verify_new", "kind": "research", "source": "VERIFY J 2026",
                                             "title": "Verify study: 4-week recall cut drop-offs 27%", "trial_n": 777})
        bot.push("category", m["category_slug"], 9, cat)
        t2 = {**research, "id": "trg_verify_new", "suppression_key": "verify_new", "payload": {"top_item_id": "d_verify_new"},
              "expires_at": "2099-01-01T00:00:00Z"}
        bot.push("trigger", "trg_verify_new", 1, t2)
        for cv in [a["conversation_id"] for a in acts if a["merchant_id"] == m["merchant_id"] and not a.get("customer_id")]:
            bot.reply(cv, m["merchant_id"], "not interested")
        s, na = bot.tick(["trg_verify_new"], now="2026-04-26T11:00:00Z")
        body = (na or [{}])[0].get("body", "")
        check("T§4.3", "new digest version is used in the next send", "VERIFY J 2026" in body and "777" in body, body[:120])
    cust_trig = next((t for t in ds.triggers.values() if t.get("scope") == "customer" and ds.customer_for(t)), None)
    if cust_trig:
        cu = copy.deepcopy(ds.customer_for(cust_trig))
        cu["customer_id"] = "c_verify_new"
        cu["identity"] = {**(cu.get("identity") or {}), "name": "Verifya"}
        cu["consent"] = {"opted_in_at": "2025-01-01", "scope": ["recall_reminders", "appointment_reminders"]}
        bot.push("customer", "c_verify_new", 1, cu)
        t3 = {**cust_trig, "id": "trg_verify_recall", "kind": "recall_due", "customer_id": "c_verify_new",
              "suppression_key": "verify_recall", "expires_at": "2099-01-01T00:00:00Z"}
        bot.push("trigger", "trg_verify_recall", 1, t3)
        s, ca = bot.tick(["trg_verify_recall"], now="2026-04-26T11:05:00Z")
        a = (ca or [{}])[0]
        check("T§4.3", "customer pushed mid-test + recall_due -> customer message as merchant_on_behalf",
              a.get("customer_id") == "c_verify_new" and a.get("send_as") == "merchant_on_behalf" and "Verifya" in a.get("body", ""),
              a.get("body", "")[:120])
        cu2 = {**cu, "customer_id": "c_verify_noconsent", "consent": {"opted_in_at": "2025-01-01", "scope": ["appointment_reminders"]}}
        bot.push("customer", "c_verify_noconsent", 1, cu2)
        t4 = {**t3, "id": "trg_verify_winback", "kind": "customer_lapsed_hard", "customer_id": "c_verify_noconsent",
              "suppression_key": "verify_winback"}
        bot.push("trigger", "trg_verify_winback", 1, t4)
        s, wa = bot.tick(["trg_verify_winback"], now="2026-04-26T11:10:00Z")
        check("B§4.4", "consent scope is respected (no win-back without promo consent)", wa == [], json.dumps(wa)[:100])
        if a.get("conversation_id"):
            d = bot.reply(a["conversation_id"], a["merchant_id"], "1", cid="c_verify_new")
            check("B App.B", "customer picks slot 1 -> booked and confirmed", d.get("action") == "send"
                  and any(w in d.get("body", "").lower() for w in ("booked", "book ho gaya")), d.get("body", "")[:100])

    print("\nWEBSITE: replays test objections; the exam is fresh scenarios")
    fm = next((a for a in acts if a["send_as"] == "vera" and a is not merchant_act), None)
    if fm:
        d = bot.reply(fm["conversation_id"], fm["merchant_id"], "honestly Google posts never work for us")
        check("WEB", "objection ('never works') -> grounded answer, stays in the conversation",
              d.get("action") == "send" and re.search(r"\d", d.get("body", "")) is not None, d.get("body", "")[:110])
        d = bot.reply(fm["conversation_id"], fm["merchant_id"], "is this a scam? who gave you my number", turn=3)
        check("WEB", "trust question is answered (who + why + STOP)", d.get("action") == "send" and "STOP" in d.get("body", ""),
              d.get("body", "")[:110])
    newm = copy.deepcopy(next(iter(ds.merchants.values())))
    newm["merchant_id"] = "m_verify_fresh"
    newm["identity"] = {**newm.get("identity", {}), "name": "Fresh Verify Clinic", "owner_first_name": "Zoya", "languages": ["en"]}
    newm["conversation_history"] = []
    bot.push("merchant", "m_verify_fresh", 1, newm)
    bot.push("trigger", "trg_verify_unknown", 1, {"id": "trg_verify_unknown", "scope": "merchant", "kind": "brand_new_kind_xyz",
                                                  "merchant_id": "m_verify_fresh", "urgency": 2, "suppression_key": "verify_unknown",
                                                  "payload": {"headline": "Metro line opening next to your street this Friday"},
                                                  "expires_at": "2099-01-01T00:00:00Z"})
    s, fa = bot.tick(["trg_verify_unknown"], now="2026-04-26T12:05:00Z")
    body = (fa or [{}])[0].get("body", "") if fa else ""
    check("WEB/FAQ", "never-seen merchant + never-seen trigger kind mid-test -> valid, grounded send (or restraint)",
          s == 200 and (fa == [] or ("Metro line" in body and "Zoya" in body)), body[:110] or "restrained")

    print("\nTESTING BRIEF §5 + §11: limits, timeouts, teardown")
    check("T§5", "every call answered well inside 30s", bot.slowest < 5, f"slowest {bot.slowest * 1000:.0f}ms")
    s, _ = bot.call("POST", "/v1/teardown", {})
    s2, h = bot.call("GET", "/v1/healthz")
    check("T§11", "teardown wipes all context", s == 200 and sum(h.get("contexts_loaded", {}).values()) == 0,
          json.dumps(h.get("contexts_loaded")))

    root = Path(__file__).resolve().parent
    if (root / "bot.py").exists():
        print("\nMAIN BRIEF §7: submission artifacts (local files)")
        import importlib
        bm = importlib.import_module("bot")
        check("B§7.1", "bot.py exposes compose(category, merchant, trigger, customer)", callable(getattr(bm, "compose", None)))
        t = next(iter(ds.triggers.values()))
        m = ds.merchant_for(t)
        out1 = bm.compose(ds.category_for(m), m, t, ds.customer_for(t))
        out2 = bm.compose(ds.category_for(m), m, t, ds.customer_for(t))
        check("B§7.1", "compose returns body/cta/send_as/suppression_key/rationale, deterministically",
              {"body", "cta", "send_as", "suppression_key", "rationale"} <= set(out1) and out1 == out2)
        words = len((root / "README.md").read_text(encoding="utf-8").split()) if (root / "README.md").exists() else 0
        check("B§7.3", "README.md exists and fits ~1 page", 0 < words <= 2000, f"{words} words")
        check("B§7.4", "conversation_handlers.py present (optional)", (root / "conversation_handlers.py").exists())
        check("B§7.2", "generate_submission.py present (writes submission.jsonl)", (root / "generate_submission.py").exists())

    fails = [r for r in RESULTS if not r[2]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} requirements verified.")
    for sec, req, _, ev in fails:
        print(f"  FAIL {sec}: {req} ({ev})")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
