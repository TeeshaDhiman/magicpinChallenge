import copy
import json
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bot
from vera import facts as F
from vera import playbooks as P
from vera import validate as V
from vera.compose import compose, compose_with_trace

FX = Path(__file__).resolve().parents[1] / "fixtures"
CATS = {f.stem: json.load(open(f, encoding="utf-8")) for f in (FX / "categories").glob("*.json")}
MERCH = {m["merchant_id"]: m for m in json.load(open(FX / "merchants_seed.json", encoding="utf-8"))["merchants"]}
TRIG = {t["id"]: t for t in json.load(open(FX / "triggers_seed.json", encoding="utf-8"))["triggers"]}
NOW = "2026-04-26T10:00:00Z"


def ctx(tid):
    t = TRIG[tid]
    m = MERCH[t["merchant_id"]]
    return CATS[m["category_slug"]], m, t


@pytest.fixture
def client():
    bot.store.wipe()
    c = TestClient(bot.app)
    for slug, cat in CATS.items():
        c.post("/v1/context", json={"scope": "category", "context_id": slug, "version": 1, "payload": cat})
    for mid, m in MERCH.items():
        c.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": 1, "payload": m})
    for tid, t in TRIG.items():
        c.post("/v1/context", json={"scope": "trigger", "context_id": tid, "version": 1, "payload": t})
    return c


# ---------------------------------------------------------------- /v1/context
def test_context_idempotent_and_versioned(client):
    body = {"scope": "category", "context_id": "dentists", "version": 1, "payload": CATS["dentists"]}
    r = client.post("/v1/context", json=body)
    assert r.status_code == 200 and r.json()["accepted"] and r.json().get("duplicate")
    r = client.post("/v1/context", json={**body, "version": 3})
    assert r.status_code == 200 and r.json()["accepted"]
    r = client.post("/v1/context", json={**body, "version": 2})
    assert r.status_code == 409 and r.json() == {"accepted": False, "reason": "stale_version", "current_version": 3}


def test_invalid_scope_400(client):
    r = client.post("/v1/context", json={"scope": "bogus", "context_id": "x", "version": 1, "payload": {}})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_scope"


def test_healthz_counts(client):
    counts = client.get("/v1/healthz").json()["contexts_loaded"]
    assert counts == {"category": 2, "merchant": 3, "customer": 0, "trigger": len(TRIG)}


# ---------------------------------------------------------------- composition
MERCHANT_TRIGGERS = [t for t in TRIG if TRIG[t]["scope"] == "merchant" and TRIG[t]["payload"]]


@pytest.mark.parametrize("tid", MERCHANT_TRIGGERS)
def test_every_message_passes_validation(tid):
    cat, m, t = ctx(tid)
    msg, tr = compose_with_trace(cat, m, t)
    assert msg, tr.skipped_reason
    fs = F.build(cat, m, t)
    assert V.check(msg["body"], msg["cta"], fs).ok
    assert not re.search(r"\b[a-z]+_[a-z_]+\b", msg["body"]), "internal jargon leaked"
    assert set(msg) >= {"body", "cta", "send_as", "suppression_key", "rationale"}
    assert msg["send_as"] == "vera"


@pytest.mark.parametrize("tid", MERCHANT_TRIGGERS)
def test_deterministic(tid):
    assert compose(*ctx(tid)) == compose(*ctx(tid))


def test_dentist_gets_dr_prefix_and_hinglish():
    msg = compose(*ctx("trg_001_research_meera"))
    assert msg["body"].startswith("Dr. Meera,")
    assert "Aapke" in msg["body"] or "aapke" in msg["body"]


def test_english_merchant_gets_english():
    body = compose(*ctx("trg_004_diwali_studio11"))["body"]
    assert body.startswith("Ravi,") and "aapke" not in body.lower()


def test_research_anchors_on_digest_item():
    body = compose(*ctx("trg_001_research_meera"))["body"]
    for anchor in ("JIDA Oct 2026, p.14", "2,100", "38%", "high-risk adult"):
        assert anchor in body


def test_prefers_service_price_offer():
    body = compose(*ctx("trg_002_perfdip_meera"))["body"]
    assert "Dental Cleaning @ ₹299" in body and "% off" not in body.lower()
    # merchant with no live offer falls back to a catalog service+price pattern, not a discount
    body = compose(*ctx("trg_014_dip_smilecare"))["body"]
    assert "Dental Cleaning @ ₹299" in body


def test_expired_offer_never_used():
    body = compose(*ctx("trg_002_perfdip_meera"))["body"]
    assert "Deep Cleaning" not in body


def test_thin_trigger_is_not_sent():
    msg, tr = compose_with_trace(*ctx("trg_015_thin_smilecare"))
    assert msg is None and tr.skipped_reason == "insufficient_trigger_data"


def test_customer_trigger_without_customer_context_is_not_sent_live():
    msg, tr = compose_with_trace(*ctx("trg_011_recall_priya"))
    assert msg is None and tr.skipped_reason == "customer_context_missing"


# ---------------------------------------------------------------- validator guards
def _fs(tid):
    return F.build(*ctx(tid))


def test_validator_blocks_fabricated_number():
    r = V.check("Dr. Meera, calls fell 47% this week. Reply YES.", "binary_yes_stop", _fs("trg_002_perfdip_meera"))
    assert any(i.startswith("ungrounded_number:47") for i in r.hard)


def test_validator_blocks_taboo_both_schemas():
    # dentists uses voice.taboos, salons uses voice.vocab_taboo (simulator schema)
    assert "taboo:guaranteed" in V.check("Dr. Meera, guaranteed results. Reply YES.", "x", _fs("trg_001_research_meera")).hard
    assert "taboo:cheapest" in V.check("Ravi, cheapest haircut in town. Reply YES.", "x", _fs("trg_004_diwali_studio11")).hard


def test_validator_blocks_multi_cta_jargon_preamble_shouting():
    fs = _fs("trg_001_research_meera")
    assert any(i.startswith("multi_cta") for i in V.check("Reply YES for A, Reply NO for B", "x", fs).hard)
    assert any(i.startswith("jargon") for i in V.check("Signal ctr_below_peer_median fired. Reply YES", "x", fs).hard)
    assert any(i.startswith("preamble") for i in V.check("Hope you're well! Reply YES", "x", fs).hard)
    assert any(i.startswith("promo_tone") for i in V.check("AMAZING DEAL today. Reply YES", "x", fs).hard)
    assert V.check("Dr. Meera, IOPA X-ray rule changed. Want the summary?", "open_ended",
                   _fs("trg_008_reg_meera")).ok, "acronyms present in context must not be flagged"


def test_validator_blocks_repeat():
    fs = _fs("trg_001_research_meera")
    body = "Dr. Meera, something. Want it?"
    assert "verbatim_repeat" in V.check(body, "open_ended", fs, already_sent={body}).hard


# ---------------------------------------------------------------- tick policy
def tick(client, triggers, now=NOW):
    r = client.post("/v1/tick", json={"now": now, "available_triggers": triggers})
    assert r.status_code == 200
    return r.json()["actions"]


def test_tick_policy_one_per_merchant_highest_urgency(client):
    acts = tick(client, list(TRIG))
    by_m = {}
    for a in acts:
        assert a["merchant_id"] not in by_m, "more than one action for a merchant in a tick"
        by_m[a["merchant_id"]] = a
    assert by_m["m_001_drmeera"]["trigger_id"] == "trg_002_perfdip_meera"      # urgency 4 wins
    assert by_m["m_002_studio11"]["trigger_id"] == "trg_004_diwali_studio11"   # urgency 3 wins
    ids = {a["trigger_id"] for a in acts}
    assert "trg_010_expired_meera" not in ids and "trg_011_recall_priya" not in ids
    for a in acts:
        assert {"conversation_id", "merchant_id", "trigger_id", "template_name",
                "template_params", "body", "cta", "suppression_key", "rationale"} <= set(a)


def test_tick_suppression_and_in_flight(client):
    first = tick(client, ["trg_004_diwali_studio11"])
    assert len(first) == 1
    assert tick(client, ["trg_004_diwali_studio11"]) == []          # suppressed
    assert tick(client, ["trg_006_reviews_studio11"]) == []         # conversation in flight, urgency 2


def test_tick_unknown_ids_and_empty(client):
    assert tick(client, []) == []
    assert tick(client, ["does_not_exist"]) == []


def test_new_merchant_version_changes_message(client):
    """Phase 3: a performance update must flow into the next composition."""
    m = copy.deepcopy(MERCH["m_001_drmeera"])
    m["performance"]["ctr"] = 0.045  # now above peer: the CTR-gap fact must disappear
    client.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 2, "payload": m})
    body = tick(client, ["trg_003_competitor_meera"])[0]["body"]
    assert "2.1%" not in body


def test_new_digest_version_is_used(client):
    cat = copy.deepcopy(CATS["dentists"])
    cat["digest"].append({"id": "d_new", "title": "Silver diamine fluoride arrested 81% of early lesions",
                          "source": "IDJ May 2026", "trial_n": 640})
    client.post("/v1/context", json={"scope": "category", "context_id": "dentists", "version": 2, "payload": cat})
    t = {**TRIG["trg_001_research_meera"], "id": "trg_new", "suppression_key": "new",
         "payload": {"top_item_id": "d_new"}}
    client.post("/v1/context", json={"scope": "trigger", "context_id": "trg_new", "version": 1, "payload": t})
    body = tick(client, ["trg_new"])[0]["body"]
    assert "81%" in body and "IDJ May 2026" in body and "640" in body


def test_opt_out_blocks_future_ticks(client):
    r = client.post("/v1/reply", json={"conversation_id": "conv_x", "merchant_id": "m_002_studio11",
                                        "from_role": "merchant", "message": "Stop messaging me. This is useless spam.",
                                        "received_at": NOW, "turn_number": 2})
    assert r.json()["action"] == "end"
    assert tick(client, ["trg_004_diwali_studio11"]) == []


def test_tick_latency(client):
    t0 = time.perf_counter()
    tick(client, list(TRIG))
    assert time.perf_counter() - t0 < 0.5


# ---------------------------------------------------------------- robustness
@pytest.mark.parametrize("kind", list(P.KIND_TO_FAMILY) + ["totally_new_kind"])
def test_no_crash_on_sparse_inputs(kind):
    """The real dataset's payload shapes are unknown: sparse input must never raise or fabricate."""
    sparse_m = {"merchant_id": "m_x", "category_slug": "gyms", "identity": {"name": "Iron Den"}}
    for payload in ({}, {"metric": "calls"}, {"headline": "Road closed near market"}):
        t = {"id": "t", "kind": kind, "scope": "merchant", "payload": payload}
        msg, _ = compose_with_trace({}, sparse_m, t)
        if msg:
            assert V.check(msg["body"], msg["cta"], F.build({}, sparse_m, t)).ok


# ---------------------------------------------------------------- decision quality
def test_rationale_explains_the_decision():
    r = compose(*ctx("trg_002_perfdip_meera"))["rationale"]
    assert "Decision: paired with ctr gap because" in r
    r = compose(*ctx("trg_001_research_meera"))["rationale"]
    assert "the trigger itself is the hook" in r


def test_one_driving_signal_only():
    """Guidance: don't repeat every available fact. Meera has CTR gap, stale posts, lapsed, views, calls."""
    body = compose(*ctx("trg_002_perfdip_meera"))["body"]
    present = [s for s in ("2.1%", "22 din", "78", "2,410") if s in body]
    assert present == ["2.1%"]


def test_signal_choice_follows_merchant_state():
    cat, m, t = ctx("trg_002_perfdip_meera")
    m = copy.deepcopy(m)
    m["performance"]["ctr"] = 0.05          # no longer below peer -> next best reason: stale posts
    body = compose(cat, m, t)["body"]
    assert "2.1%" not in body and "22 din" in body


def test_tick_rationale_names_what_was_held_back(client):
    acts = tick(client, ["trg_001_research_meera", "trg_002_perfdip_meera", "trg_003_competitor_meera"])
    r = acts[0]["rationale"]
    assert acts[0]["trigger_id"] == "trg_002_perfdip_meera"
    assert "Held back for this merchant:" in r and "competitor opened" in r and "research digest" in r


def _with_history(client, mid, history, version=2):
    m = copy.deepcopy(MERCH[mid])
    m["conversation_history"] = history
    client.post("/v1/context", json={"scope": "merchant", "context_id": mid, "version": version, "payload": m})


def test_history_ignored_topic_is_demoted(client):
    # Studio11: festival (urgency 3) normally beats perf_spike (urgency 2)...
    _with_history(client, "m_002_studio11", [
        {"ts": "2026-03-01T10:00:00Z", "from": "vera", "kind": "festival_upcoming", "engagement": "ignored"},
        {"ts": "2026-03-20T10:00:00Z", "from": "vera", "kind": "festival_upcoming", "engagement": "ignored"},
        {"ts": "2026-04-01T10:00:00Z", "from": "vera", "kind": "perf_spike", "engagement": "merchant_replied"},
    ])
    acts = tick(client, ["trg_004_diwali_studio11", "trg_005_spike_studio11"])
    assert acts[0]["trigger_id"] == "trg_005_spike_studio11"
    assert "ignored this kind" not in acts[0]["rationale"]
    assert "merchant replied to this kind before" in acts[0]["rationale"]


def test_three_unanswered_nudges_stops_non_urgent(client):
    _with_history(client, "m_002_studio11", [
        {"ts": f"2026-04-0{i}T10:00:00Z", "from": "vera", "engagement": "ignored"} for i in (1, 2, 3)])
    assert tick(client, ["trg_004_diwali_studio11", "trg_006_reviews_studio11"]) == []


def test_history_already_sent_is_skipped(client):
    _with_history(client, "m_002_studio11", [
        {"ts": "2026-04-20T10:00:00Z", "from": "vera", "suppression_key": "festival:diwali:m_002",
         "engagement": "merchant_replied"}])
    acts = tick(client, ["trg_004_diwali_studio11"])
    assert acts == []


def test_research_cohort_match_counts_as_merchant_fit(client):
    acts = tick(client, ["trg_001_research_meera", "trg_003_competitor_meera"])
    assert acts[0]["trigger_id"] == "trg_001_research_meera"      # tie at 24, sooner expiry wins
    assert "matches the merchant's patient cohort" in acts[0]["rationale"]


def test_uncomposable_winner_falls_back_to_runner_up(client):
    # SmileCare: the thin festival trigger has no data; the views dip must still go out
    t = {**TRIG["trg_015_thin_smilecare"], "urgency": 5}
    client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 2, "payload": t})
    acts = tick(client, ["trg_015_thin_smilecare", "trg_014_dip_smilecare"])
    assert [a["trigger_id"] for a in acts] == ["trg_014_dip_smilecare"]


def test_customer_scope_never_blocks_merchant_message(client):
    t = {**TRIG["trg_011_recall_priya"], "urgency": 5}
    client.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 2, "payload": t})
    acts = tick(client, ["trg_011_recall_priya", "trg_001_research_meera"])
    assert [a["trigger_id"] for a in acts] == ["trg_001_research_meera"]
    assert "recall" not in acts[0]["rationale"]
