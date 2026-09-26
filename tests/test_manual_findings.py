"""Regression tests for problems found by reading transcripts and probing the live API by hand.
Each test names the finding it locks in."""
import re

import pytest
from fastapi.testclient import TestClient

import bot
from vera import dataset as DS

ds = DS.load("fixtures")
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]
BAD_HINDI = re.compile(r"\b(karna|bhejna|banana|daalna) (kar|sakti|dungi)\b")


@pytest.fixture
def c():
    bot.store.wipe()
    cl = TestClient(bot.app)
    for s, v in ds.categories.items():
        cl.post("/v1/context", json={"scope": "category", "context_id": s, "version": 1, "payload": v})
    for sc, coll in (("merchant", ds.merchants), ("customer", ds.customers), ("trigger", ds.triggers)):
        for k, v in coll.items():
            cl.post("/v1/context", json={"scope": sc, "context_id": k, "version": 1, "payload": v})
    return cl


def convo(c, tid, msgs, cid=None):
    a = c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": [tid]}).json()["actions"][0]
    out = [a]
    for i, m in enumerate(msgs):
        d = c.post("/v1/reply", json={"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                       "customer_id": cid, "from_role": "customer" if cid else "merchant",
                                       "message": m, "received_at": "2026-04-26T10:05:00Z", "turn_number": i + 2}).json()
        out.append(d)
        if d["action"] == "end":
            break
    return out


def test_finding_phase4_two_qualifying_turns_then_commit_never_silent(c):
    t = convo(c, "trg_002_perfdip_meera", ["what exactly will you post?", "and how long does it take to go live?", "ok lets do it"])
    assert [x.get("action", "send") for x in t[1:]] == ["send", "send", "send"]
    final = t[-1]["body"].lower()
    assert not any(q in final for q in QUALIFYING) and ("done" in final or "ho gaya" in final)
    bodies = [x["body"] for x in t]
    assert len(bodies) == len(set(bodies)), "verbatim repeat in conversation (-2 each)"


def test_finding_research_done_is_not_a_google_post(c):
    t = convo(c, "trg_001_research_meera", ["tell me more", "ok send it"])
    assert "Key point" in t[1]["body"]                      # 'tell me more' gets more
    assert "Google profile" not in t[-1]["body"] and "patient note" in t[-1]["body"]


def test_finding_scope_question_answered_from_digest_only(c):
    t = convo(c, "trg_001_research_meera", ["tell me more", "is this relevant for kids too?"])
    assert "high-risk adults" in t[-1]["body"] and "extend" in t[-1]["body"]


def test_finding_customer_who_is_this(c):
    t = convo(c, "trg_011_recall_priya", ["who is this?"], cid="c_001_priya")
    assert "Dr. Meera's clinic" in t[-1]["body"] and "12 May 2026" in t[-1]["body"]


@pytest.mark.parametrize("tid,msgs", [
    ("trg_002_perfdip_meera", ["hmm", "you are useless", "GST file karoge?"]),
    ("trg_001_research_meera", ["Thank you for contacting us, we will get back to you"]),
    ("trg_012_trend_meera", ["who is this?", "kitna time lagega?", "haan"]),
    ("trg_008_reg_meera", ["price kya hai?", "ok"]),
])
def test_finding_hinglish_conjugation(c, tid, msgs):
    for x in convo(c, tid, msgs):
        assert not BAD_HINDI.search(x.get("body", "")), x.get("body")


def test_finding_malformed_requests_get_400_shapes(c):
    r = c.post("/v1/context", json={"scope": "merchant", "context_id": "x", "payload": {}})
    assert r.status_code == 400 and r.json()["accepted"] is False and r.json()["reason"] == "malformed"
    r = c.post("/v1/context", content=b"garbage", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["accepted"] is False
    assert c.post("/v1/context", json={"scope": "merchant", "context_id": "", "version": 1, "payload": {}}).status_code == 400
    assert c.post("/v1/context", json={"scope": "merchant", "context_id": "z", "version": -1, "payload": {}}).status_code == 400


def test_finding_nulls_are_tolerated(c):
    assert c.post("/v1/tick", json={"now": None, "available_triggers": None}).json() == {"actions": []}
    assert c.post("/v1/tick", json={}).json() == {"actions": []}
    d = c.post("/v1/reply", json={"conversation_id": "n1", "merchant_id": "m", "message": None, "from_role": None}).json()
    assert d["action"] in ("send", "wait", "end")


def test_finding_devanagari_intents(c):
    def act(msg, conv):
        return c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": "m_002_studio11",
                                          "message": msg, "turn_number": 2}).json()
    assert act("मैसेज मत भेजो", "d1")["action"] == "end"
    assert act("हाँ कर दो", "d2")["action"] == "send"
    assert act("बाद में बात करते हैं", "d3")["action"] == "wait"


def test_finding_conversation_id_never_reused(c):
    first = c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_004_diwali_studio11"]}).json()
    conv = first["actions"][0]["conversation_id"]
    c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": "m_002_studio11", "message": "not interested"})
    t = {**ds.triggers["trg_004_diwali_studio11"], "suppression_key": "festival:diwali:m_002:v2"}
    c.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 2, "payload": t})
    bot.store.mem("m_002_studio11").opted_out = False
    again = c.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": [t["id"]]}).json()["actions"]
    assert again and again[0]["conversation_id"] != conv


def test_finding_hindi_merchant_stays_hinglish_when_typing_english(c):
    t = convo(c, "trg_002_perfdip_meera", ["yes please go ahead"])
    assert "Yeh raha draft" in t[-1]["body"]


def test_finding_abandoned_thread_stops_blocking_after_30_sim_minutes(c):
    first = c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ["trg_004_diwali_studio11"]}).json()
    assert first["actions"]
    # merchant never replies; a new non-urgent trigger 10 min later is held (conversation in flight) ...
    assert c.post("/v1/tick", json={"now": "2026-04-26T10:10:00Z", "available_triggers": ["trg_006_reviews_studio11"]}).json()["actions"] == []
    # ... but 45 simulated minutes later the thread is abandoned and new context gets through
    later = c.post("/v1/tick", json={"now": "2026-04-26T10:45:00Z", "available_triggers": ["trg_006_reviews_studio11"]}).json()
    assert later["actions"] and later["actions"][0]["trigger_id"] == "trg_006_reviews_studio11"


def test_finding_merchant_who_is_this_was_silently_blocked(c):
    t = convo(c, "trg_004_diwali_studio11", ["who is this?"])
    assert t[-1]["action"] == "send" and "I'm Vera" in t[-1]["body"]


def test_finding_trust_question_always_answered_even_after_objections(c):
    t = convo(c, "trg_004_diwali_studio11", ["we already have an agency", "I have no time",
                                             "is this a scam? who gave you my number"])
    assert t[-1]["action"] == "send" and "magicpin partner" in t[-1]["body"] and "STOP" in t[-1]["body"]


@pytest.mark.parametrize("msg,expect", [
    ("Google posts never work for us", "2.1%"),          # skeptic -> answered with the merchant's own data
    ("this is too expensive", "costs you nothing"),
    ("we already have an agency for this", "whoever manages your profile"),
    ("I have no time for this", "one YES"),
])
def test_objections_get_grounded_answers(c, msg, expect):
    tid = "trg_002_perfdip_meera" if "2.1%" in expect else "trg_005_spike_studio11"
    t = convo(c, tid, [msg])
    assert t[-1]["action"] == "send" and (expect in t[-1]["body"] or expect == "2.1%" and "2.1%" in t[-1]["body"])


def test_three_objections_step_back(c):
    t = convo(c, "trg_014_dip_smilecare", ["too costly", "didn't work last time", "we have a guy who does this"])
    assert t[-1]["action"] == "end"


def test_search_demand_is_the_driving_signal_when_present():
    from vera.compose import compose
    t = ds.triggers["trg_019_search_smilecare"]
    m = ds.merchant_for(t)
    body = compose(ds.category_for(m), m, t, None)["body"]
    assert '190 people in Rohini are searching for "dental check up"' in body and "₹299" in body
    t2 = ds.triggers["trg_014_dip_smilecare"]
    assert "190 people in Rohini searched" in compose(ds.category_for(m), m, t2, None)["body"]
