import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bot
from vera.reply import classify, detect_lang

FX = Path(__file__).resolve().parents[1] / "fixtures"
QUALIFYING = ["would you", "do you", "can you tell", "what if", "how about"]


@pytest.fixture
def c():
    bot.store.wipe()
    cl = TestClient(bot.app)
    for f in (FX / "categories").glob("*.json"):
        cl.post("/v1/context", json={"scope": "category", "context_id": f.stem, "version": 1, "payload": json.load(open(f, encoding="utf-8"))})
    for m in json.load(open(FX / "merchants_seed.json", encoding="utf-8"))["merchants"]:
        cl.post("/v1/context", json={"scope": "merchant", "context_id": m["merchant_id"], "version": 1, "payload": m})
    for t in json.load(open(FX / "triggers_seed.json", encoding="utf-8"))["triggers"]:
        cl.post("/v1/context", json={"scope": "trigger", "context_id": t["id"], "version": 1, "payload": t})
    return cl


def start(c, trigger_id):
    acts = c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": [trigger_id]}).json()["actions"]
    assert acts, trigger_id
    return acts[0]


def say(c, conv, mid, msg, turn=2):
    r = c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": None,
                                   "from_role": "merchant", "message": msg,
                                   "received_at": "2026-04-26T10:05:00Z", "turn_number": turn})
    assert r.status_code == 200
    d = r.json()
    assert d["action"] in ("send", "wait", "end") and d.get("rationale")
    if d["action"] == "send":
        assert d["body"].strip()
    return d


# ------------------------------------------------------------ classifier
@pytest.mark.parametrize("msg,kind", [
    ("Thank you for contacting us! Our team will respond shortly.", "auto_reply"),
    ("Aapki jaankari ke liye bahut-bahut shukriya. Main aapki yeh baatein team tak pahuncha deti hoon.", "auto_reply"),
    ("Stop messaging me. This is useless spam.", "opt_out"),
    ("band karo ye sab", "opt_out"),
    ("You people are useless", "abuse"),
    ("can you also help me file my GST?", "off_topic"),
    ("not interested", "negative"),
    ("nahi", "negative"),
    ("busy right now, later", "defer"),
    ("Ok lets do it. Whats next?", "commit"),
    ("haan kar do", "commit"),
    ("Mujhe magicpin judna hai", "commit"),
    ("how much does this cost?", "price"),
    ("what will the post say?", "question"),
    ("who is this?", "who"),
])
def test_classify(msg, kind):
    assert classify(msg, []).kind == kind


def test_verbatim_repeat_is_auto_reply_even_without_canned_phrase():
    msg = "Clinic timings are 10 to 7 Monday to Saturday"
    assert classify(msg, []).kind != "auto_reply"
    assert classify(msg, [msg.lower()]).kind == "auto_reply"


def test_language_follows_identity_languages_faq():
    # FAQ: match the merchant's identity.languages; Hindi merchants stay in Hinglish
    assert detect_lang("yes please send the draft", "hinglish") == "hinglish"
    assert detect_lang("ok", "hinglish") == "hinglish"
    # an English-profile merchant who writes Hindi gets Hinglish back
    assert detect_lang("haan theek hai, kar do", "en") == "hinglish"
    assert detect_lang("हाँ कर दो", "en") == "hinglish"
    assert detect_lang("yes please send the draft", "en") == "en"


# ------------------------------------------------------------ simulator scenarios, verbatim
def test_sim_auto_reply_hell(c):
    msg = "Thank you for contacting us! Our team will respond shortly."
    actions = [say(c, f"conv_auto_{i}", "m_001_drmeera", msg, i + 1)["action"] for i in range(1, 5)]
    assert actions[:2] == ["send", "end"], actions   # one probe, then exit (fewest wasted turns)


def test_sim_intent_transition(c):
    d = say(c, "conv_intent_1", "m_001_drmeera", "Ok lets do it. Whats next?")
    body = d["body"].lower()
    assert d["action"] == "send"
    assert not any(q in body for q in QUALIFYING)
    assert any(w in body for w in ["done", "sending", "draft", "here", "confirm", "proceed", "next"])


def test_sim_hostile(c):
    assert say(c, "conv_hostile", "m_001_drmeera", "Stop messaging me. This is useless spam.")["action"] == "end"


# ------------------------------------------------------------ phase-4 style multi-turn
def test_abuse_then_gst_stays_on_mission(c):
    a = start(c, "trg_004_diwali_studio11")
    d1 = say(c, a["conversation_id"], "m_002_studio11", "you guys are useless")
    assert d1["action"] == "send" and "sorry" in d1["body"].lower() and "STOP" in d1["body"]
    d2 = say(c, a["conversation_id"], "m_002_studio11", "can you also help me file my GST?", 3)
    assert d2["action"] == "send" and "outside what i can help with" in d2["body"].lower()
    assert "Google post" in d2["body"]                       # redirected to the pending action
    d3 = say(c, a["conversation_id"], "m_002_studio11", "this is rubbish", 4)
    assert d3["action"] == "end"


def test_happy_path_pitch_draft_done_end(c):
    a = start(c, "trg_002_perfdip_meera")
    conv, mid = a["conversation_id"], "m_001_drmeera"
    d1 = say(c, conv, mid, "haan kar do")
    assert d1["action"] == "send" and "Dental Cleaning @ ₹299" in d1["body"] and "Yeh raha draft" in d1["body"]
    d2 = say(c, conv, mid, "theek hai, publish karo", 3)
    assert d2["action"] == "send" and d2["body"].startswith("Ho gaya")
    d3 = say(c, conv, mid, "ok", 4)
    assert d3["action"] == "end"


def test_research_yes_delivers_grounded_content(c):
    a = start(c, "trg_001_research_meera")
    d = say(c, a["conversation_id"], "m_001_drmeera", "yes please send")
    for anchor in ("JIDA Oct 2026, p.14", "2,100", "Dr. Meera's Dental Clinic"):
        assert anchor in d["body"]
    assert "guaranteed" not in d["body"].lower() and "cure" not in d["body"].lower()


def test_curious_ask_answer_becomes_post(c):
    a = start(c, "trg_007_dormant_studio11")
    d = say(c, a["conversation_id"], "m_002_studio11", "Keratin treatment")
    assert d["action"] == "send" and "Keratin treatment" in d["body"] and "Haircut @ ₹149" in d["body"]


def test_question_answered_with_the_concrete_draft(c):
    a = start(c, "trg_006_reviews_studio11")
    d = say(c, a["conversation_id"], "m_002_studio11", "what would you write?")
    assert "wait time" in d["body"] and "all 3" in d["body"]


def test_price_question_never_invents_numbers(c):
    a = start(c, "trg_004_diwali_studio11")
    d = say(c, a["conversation_id"], "m_002_studio11", "how much will this cost me?")
    assert d["action"] == "send" and "won't guess" in d["body"]
    assert not any(ch.isdigit() for ch in d["body"])


def test_decline_and_defer(c):
    a = start(c, "trg_004_diwali_studio11")
    assert say(c, a["conversation_id"], "m_002_studio11", "busy, tomorrow")["wait_seconds"] == 86400
    assert say(c, a["conversation_id"], "m_002_studio11", "not interested", 3)["action"] == "end"


def test_opt_out_in_reply_suppresses_future_ticks(c):
    a = start(c, "trg_004_diwali_studio11")
    say(c, a["conversation_id"], "m_002_studio11", "please stop")
    r = c.post("/v1/tick", json={"now": "2026-04-26T11:00:00Z", "available_triggers": ["trg_006_reviews_studio11"]})
    assert r.json()["actions"] == []


def test_never_repeats_itself(c):
    a = start(c, "trg_004_diwali_studio11")
    bodies = []
    for i, m in enumerate(["hmm", "i see", "right", "ok then what"]):
        d = say(c, a["conversation_id"], "m_002_studio11", m, i + 2)
        if d["action"] == "send":
            bodies.append(d["body"])
    assert len(bodies) == len(set(bodies))


def test_unknown_conversation_and_merchant_is_safe(c):
    d = say(c, "conv_never", "m_nobody", "Yes go ahead")
    assert d["action"] == "send" and "draft" in d["body"].lower()
    d = say(c, "conv_never2", None, "what is this?")
    assert d["action"] in ("send", "wait")


def test_done_frees_merchant_for_next_proactive_send(c):
    a = start(c, "trg_002_perfdip_meera")
    say(c, a["conversation_id"], "m_001_drmeera", "yes")
    say(c, a["conversation_id"], "m_001_drmeera", "yes publish", 3)
    nxt = start(c, "trg_001_research_meera")
    assert nxt["trigger_id"] == "trg_001_research_meera"


def test_festival_pending_names_the_festival(c):
    a = start(c, "trg_004_diwali_studio11")
    d = say(c, a["conversation_id"], "m_002_studio11", "hmm")
    assert "Diwali Google post" in d["body"]
