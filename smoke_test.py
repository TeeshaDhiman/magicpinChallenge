"""Verify a deployed Vera bot before submitting its URL.

    python smoke_test.py https://your-bot.example.com
    python smoke_test.py https://your-bot.example.com --keep   # don't wipe state afterwards

Stdlib only. Pushes a throwaway merchant/category/trigger (ids prefixed smoke_),
exercises all 5 endpoints, then calls /v1/teardown so the smoke data doesn't
inflate healthz counts. Do NOT run this while the judge's test window is live.
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib import error, request

BUDGET_S = 10.0  # judge allows 30s (the local simulator 15s); stay well under
failures: list[str] = []


def call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        with request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read()
    except error.HTTPError as e:
        status, raw = e.code, e.read()
    except (error.URLError, TimeoutError, ConnectionError) as e:
        print(f"  [FAIL] {method} {path} unreachable: {getattr(e, 'reason', e)}")
        return 0, None, time.perf_counter() - t0
    dt = time.perf_counter() - t0
    try:
        payload = json.loads(raw.decode() or "null")
    except ValueError:
        payload = None
    return status, payload, dt


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def timed(label: str, dt: float) -> None:
    check(dt < BUDGET_S, f"{label} latency {dt * 1000:.0f}ms", f"over {BUDGET_S}s budget")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    base = sys.argv[1].rstrip("/")
    keep = "--keep" in sys.argv
    tag = f"smoke_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat().replace("+00:00", "Z")  # noqa: E731

    print(f"Target: {base}")
    if not base.startswith("https://"):
        print("  [WARN] not HTTPS — fine for local testing, the judge expects https for submission")

    print("\nGET /v1/healthz")
    s, p, dt = call(base, "GET", "/v1/healthz")
    if s == 0:
        print("\nBot unreachable. Check the URL, that the service is running, and that it listens on 0.0.0.0:$PORT.")
        return 1
    check(s == 200, "status 200", f"got {s}")
    check(isinstance(p, dict) and p.get("status") == "ok", "status == ok")
    check(isinstance(p, dict) and isinstance(p.get("contexts_loaded"), dict), "contexts_loaded present")
    timed("healthz", dt)

    print("\nGET /v1/metadata")
    s, p, dt = call(base, "GET", "/v1/metadata")
    check(s == 200, "status 200", f"got {s}")
    keys = {"team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"}
    check(isinstance(p, dict) and keys <= set(p), "all metadata keys", f"missing {keys - set(p or {})}")
    if isinstance(p, dict) and p.get("team_name") in ("unset", "TODO", ""):
        print("  [WARN] team_name not set — set TEAM_NAME / TEAM_MEMBERS / CONTACT_EMAIL env vars on the host")

    print("\nPOST /v1/context")
    cat = {"slug": f"{tag}_dentists", "offer_catalog": [{"title": "Dental Cleaning @ ₹299"}],
           "voice": {"tone": "peer_clinical", "taboos": ["guaranteed"]},
           "peer_stats": {"avg_ctr": 0.03}, "digest": []}
    mer = {"merchant_id": f"{tag}_m", "category_slug": cat["slug"],
           "identity": {"name": "Dr. Test's Clinic", "city": "Delhi", "locality": "Saket", "languages": ["en"]},
           "performance": {"views": 1000, "calls": 10, "ctr": 0.02},
           "offers": [{"title": "Dental Cleaning @ ₹299", "status": "active"}], "signals": ["stale_posts:15d"]}
    trg = {"id": f"{tag}_t", "scope": "merchant", "kind": "perf_dip", "source": "internal",
           "merchant_id": mer["merchant_id"], "payload": {"metric": "calls", "delta_pct": -0.3, "window": "7d"},
           "urgency": 3, "suppression_key": f"{tag}_sk", "expires_at": iso(now + timedelta(days=1))}

    def push(scope, cid, version, payload):
        return call(base, "POST", "/v1/context", {"scope": scope, "context_id": cid, "version": version,
                                                   "payload": payload, "delivered_at": iso(now)})

    s, p, dt = push("category", cat["slug"], 2, cat)
    check(s == 200 and p.get("accepted") is True and "ack_id" in p, "new context accepted", f"{s} {p}")
    timed("context", dt)
    s, p, _ = push("category", cat["slug"], 2, cat)
    check(s == 200 and p.get("accepted") is True, "same version is idempotent", f"{s} {p}")
    s, p, _ = push("category", cat["slug"], 1, cat)
    check(s == 409 and p.get("reason") == "stale_version", "lower version -> 409 stale_version", f"{s} {p}")
    s, p, _ = call(base, "POST", "/v1/context", {"scope": "nope", "context_id": "x", "version": 1, "payload": {}})
    check(s == 400 and p.get("reason") == "invalid_scope", "bad scope -> 400 invalid_scope", f"{s} {p}")
    push("merchant", mer["merchant_id"], 1, mer)
    push("trigger", trg["id"], 1, trg)

    print("\nPOST /v1/tick")
    s, p, dt = call(base, "POST", "/v1/tick", {"now": iso(now), "available_triggers": []})
    check(s == 200 and p == {"actions": []}, "empty tick -> no actions", f"{s} {p}")
    s, p, dt = call(base, "POST", "/v1/tick", {"now": iso(now), "available_triggers": [trg["id"]]})
    acts = (p or {}).get("actions") or []
    check(s == 200 and len(acts) == 1, "tick produces one action", f"{s} {p}")
    timed("tick", dt)
    conv_id = None
    if acts:
        a = acts[0]
        need = {"conversation_id", "merchant_id", "send_as", "trigger_id", "template_name",
                "template_params", "body", "cta", "suppression_key", "rationale"}
        check(need <= set(a), "action has all fields", f"missing {need - set(a)}")
        check(bool(a.get("body", "").strip()), "action body non-empty")
        conv_id = a.get("conversation_id")
        print(f"    body: {a.get('body', '')[:140]}")
    s, p, _ = call(base, "POST", "/v1/tick", {"now": iso(now), "available_triggers": [trg["id"]]})
    check(s == 200 and (p or {}).get("actions") == [], "same trigger not re-sent (suppression)", f"{p}")

    print("\nPOST /v1/reply")
    s, p, dt = call(base, "POST", "/v1/reply", {
        "conversation_id": conv_id or f"{tag}_conv", "merchant_id": mer["merchant_id"], "customer_id": None,
        "from_role": "merchant", "message": "Yes please, go ahead", "received_at": iso(now), "turn_number": 2})
    ok_shape = isinstance(p, dict) and p.get("action") in ("send", "wait", "end")
    check(s == 200 and ok_shape, "reply returns send|wait|end", f"{s} {p}")
    if ok_shape and p["action"] == "send":
        check(bool((p.get("body") or "").strip()), "send has non-empty body")
    timed("reply", dt)
    s, p, _ = call(base, "POST", "/v1/reply", {
        "conversation_id": f"{tag}_cold", "merchant_id": "never_seen", "from_role": "merchant",
        "message": "hello?", "received_at": iso(now), "turn_number": 1})
    check(s == 200 and isinstance(p, dict) and p.get("action") in ("send", "wait", "end"),
          "reply on unknown conversation is still valid", f"{s} {p}")

    if not keep:
        print("\nPOST /v1/teardown")
        s, _, _ = call(base, "POST", "/v1/teardown", {})
        check(s == 200, "teardown wiped smoke data", f"got {s}")

    print("\n" + ("ALL CHECKS PASSED — safe to submit this URL." if not failures
                  else f"{len(failures)} FAILED: {', '.join(failures)}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
