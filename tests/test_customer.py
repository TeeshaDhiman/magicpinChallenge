import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import bot
import conversation_handlers as CH
from vera import dataset as DS
from vera import facts as F
from vera import validate as V
from vera.compose import compose, compose_with_trace, render

ROOT = Path(__file__).resolve().parents[1]
FX = ROOT / "fixtures"
ds = DS.load(FX)


def parts(tid):
    t = ds.triggers[tid]
    m = ds.merchant_for(t)
    return ds.category_for(m), m, t, ds.customer_for(t)


# ------------------------------------------------------------ composition
def test_priya_matches_appendix_b():
    msg = compose(*parts("trg_011_recall_priya"))
    b = msg["body"]
    assert msg["send_as"] == "merchant_on_behalf" and msg["cta"] == "multi_choice_slot"
    for anchor in ("Hi Priya", "Dr. Meera's clinic here", "5 mahine", "6-month cleaning recall",
                   "Wed 4 Nov, 6pm", "Thu 5 Nov, 5pm", "Dental Cleaning @ ₹299", "1", "2"):
        assert anchor in b, anchor
    assert "fluoride" not in b.lower()           # appendix mentions it, but it isn't in our data: don't invent


def test_slots_ranked_by_customer_preference():
    b = compose(*parts("trg_011_recall_priya"))["body"]
    assert "11am" not in b and "Sat" not in b    # weekday_evening preference beats earlier slots


def test_consent_scope_blocks_customer_message():
    msg, tr = compose_with_trace(*parts("trg_016_winback_rohit"))
    assert msg is None and tr.skipped_reason.startswith("no_consent")
    # submission path: tell the merchant instead of messaging the customer
    out = compose(*parts("trg_016_winback_rohit"))
    assert out["send_as"] == "vera" and "Rohit" in out["body"] and "3 Aug 2025" in out["body"]


def test_appointment_reminder():
    msg = compose(*parts("trg_017_appt_ananya"))
    assert "hair spa appointment is tomorrow at 11:30am" in msg["body"] and "Reply 1 to confirm" in msg["body"]


def test_no_slots_means_no_invented_slot():
    msg = compose(*parts("trg_018_recall_karan"))
    assert msg["cta"] == "open_ended" and "slot" not in msg["body"].lower()
    assert "2 Mar 2026" in msg["body"] and "no consent record" in msg["rationale"]


@pytest.mark.parametrize("tid", list(ds.triggers))
def test_compose_always_returns_valid_message(tid):
    cat, m, t, c = parts(tid)
    msg = compose(cat, m, t, c)
    assert msg and msg["body"].strip()
    assert set(msg) >= {"body", "cta", "send_as", "suppression_key", "rationale"}
    fs = F.build(cat, m, t)
    if c:
        F._harvest_numbers(c, fs.allowed_numbers)
    for s in ("Wed 4 Nov, 6pm", "Thu 5 Nov, 5pm", "11:30am"):
        fs.allow(s)
    assert V.check(msg["body"], msg["cta"], fs).ok, V.check(msg["body"], msg["cta"], fs).hard


@pytest.mark.parametrize("tid", list(ds.triggers))
def test_template_params_render_exactly_to_body(tid):
    msg = compose(*parts(tid))
    assert render(msg["template_params"]) == msg["body"]
    if len(msg["template_params"]) == 3:
        assert msg["template_params"][2]           # the CTA is its own parameter


# ------------------------------------------------------------ live tick + replies
@pytest.fixture
def c():
    bot.store.wipe()
    cl = TestClient(bot.app)
    for slug, cat in ds.categories.items():
        cl.post("/v1/context", json={"scope": "category", "context_id": slug, "version": 1, "payload": cat})
    for scope, coll in (("merchant", ds.merchants), ("customer", ds.customers), ("trigger", ds.triggers)):
        for k, v in coll.items():
            cl.post("/v1/context", json={"scope": scope, "context_id": k, "version": 1, "payload": v})
    return cl


def tick(c, ids):
    return c.post("/v1/tick", json={"now": "2026-04-26T10:00:00Z", "available_triggers": ids}).json()["actions"]


def say(c, conv, msg, cid=None, mid="m_001_drmeera"):
    d = c.post("/v1/reply", json={"conversation_id": conv, "merchant_id": mid, "customer_id": cid,
                                   "from_role": "customer" if cid else "merchant", "message": msg,
                                   "received_at": "2026-04-26T10:05:00Z", "turn_number": 2}).json()
    assert d["action"] in ("send", "wait", "end")
    return d


def test_tick_sends_customer_and_merchant_messages_independently(c):
    acts = tick(c, ["trg_011_recall_priya", "trg_001_research_meera"])
    by = {a["trigger_id"]: a for a in acts}
    assert by["trg_011_recall_priya"]["customer_id"] == "c_001_priya"
    assert by["trg_011_recall_priya"]["send_as"] == "merchant_on_behalf"
    assert by["trg_001_research_meera"]["send_as"] == "vera"


def test_tick_skips_consent_blocked_customer(c):
    assert tick(c, ["trg_016_winback_rohit"]) == []


def test_customer_books_slot_by_number_and_by_day(c):
    a = tick(c, ["trg_011_recall_priya"])[0]
    d = say(c, a["conversation_id"], "1", cid="c_001_priya")
    assert d["action"] == "send" and "Wed 4 Nov, 6pm" in d["body"] and d["body"].startswith("Ho gaya, Priya")
    bot.store.wipe()


def test_customer_books_by_day_name(c):
    a = tick(c, ["trg_011_recall_priya"])[0]
    d = say(c, a["conversation_id"], "thursday works", cid="c_001_priya")
    assert "Thu 5 Nov, 5pm" in d["body"]


def test_customer_yes_with_two_slots_asks_to_pick(c):
    a = tick(c, ["trg_011_recall_priya"])[0]
    d = say(c, a["conversation_id"], "yes please", cid="c_001_priya")
    assert "1" in d["body"] and "2" in d["body"] and "booked" not in d["body"].lower()


def test_customer_own_time_is_noted_not_promised(c):
    a = tick(c, ["trg_011_recall_priya"])[0]
    d = say(c, a["conversation_id"], "can I come saturday morning instead?", cid="c_001_priya")
    assert "confirm" in d["body"] and "booked" not in d["body"].lower()


def test_appointment_confirm_and_reschedule(c):
    a = tick(c, ["trg_017_appt_ananya"])[0]
    d = say(c, a["conversation_id"], "2", cid="c_003_ananya", mid="m_002_studio11")
    assert "day and time" in d["body"]
    d = say(c, a["conversation_id"], "1", cid="c_003_ananya", mid="m_002_studio11")
    assert "confirmed" in d["body"]


def test_customer_stop_blocks_future_customer_sends_only(c):
    a = tick(c, ["trg_011_recall_priya"])[0]
    assert say(c, a["conversation_id"], "STOP", cid="c_001_priya")["action"] == "end"
    t = {**ds.triggers["trg_011_recall_priya"], "id": "trg_again", "suppression_key": "again"}
    c.post("/v1/context", json={"scope": "trigger", "context_id": "trg_again", "version": 1, "payload": t})
    assert tick(c, ["trg_again"]) == []
    assert tick(c, ["trg_001_research_meera"])[0]["send_as"] == "vera"   # merchant unaffected


def test_merchant_opt_out_also_stops_sends_on_their_behalf(c):
    say(c, "conv_m", "Stop messaging me", mid="m_001_drmeera")
    assert tick(c, ["trg_011_recall_priya"]) == []


def test_customer_thread_does_not_block_merchant_thread(c):
    tick(c, ["trg_011_recall_priya"])
    assert tick(c, ["trg_001_research_meera"])


# ------------------------------------------------------------ artifacts
def test_generate_submission(tmp_path):
    out = tmp_path / "submission.jsonl"
    r = subprocess.run([sys.executable, "generate_submission.py", str(FX), "--out", str(out)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == len(ds.test_pairs)
    for ln in lines:
        assert list(ln) == ["test_id", "body", "cta", "send_as", "suppression_key", "rationale"]
    assert {ln["send_as"] for ln in lines} == {"vera", "merchant_on_behalf"}


def test_generate_submission_refuses_to_guess_pairs(tmp_path):
    d = tmp_path / "ds"
    (d / "categories").mkdir(parents=True)
    r = subprocess.run([sys.executable, "generate_submission.py", str(d)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 2 and "Not guessing" in r.stdout


def test_loader_reads_brief_per_file_layout(tmp_path):
    d = tmp_path / "dataset"
    for sub in ("categories", "merchants", "customers", "triggers"):
        (d / sub).mkdir(parents=True)
    json.dump(ds.categories["dentists"], open(d / "categories" / "dentists.json", "w", encoding="utf-8"))
    json.dump(ds.merchants["m_001_drmeera"], open(d / "merchants" / "m_001_drmeera_dentist_delhi.json", "w", encoding="utf-8"))
    json.dump(ds.customers["c_001_priya"], open(d / "customers" / "c_001_priya_for_m001.json", "w", encoding="utf-8"))
    t = {k: v for k, v in ds.triggers["trg_011_recall_priya"].items() if k not in ("merchant_id", "customer_id")}
    t["payload"] = {**t["payload"], "merchant_id": "m_001_drmeera", "customer_id": "c_001_priya"}
    json.dump(t, open(d / "triggers" / "trg_internal_001_recall_due_priya.json", "w", encoding="utf-8"))
    loaded = DS.load(d)
    trig = loaded.triggers["trg_011_recall_priya"]
    assert loaded.merchant_for(trig) and loaded.customer_for(trig)       # ids via payload, per the brief
    assert compose(loaded.category_for(loaded.merchant_for(trig)), loaded.merchant_for(trig), trig,
                   loaded.customer_for(trig))["send_as"] == "merchant_on_behalf"


def test_audit_flags_unread_payload_fields(tmp_path):
    d = tmp_path / "ds"
    (d / "categories").mkdir(parents=True)
    json.dump(ds.categories["salons"], open(d / "categories" / "salons.json", "w", encoding="utf-8"))
    json.dump({"merchants": [ds.merchants["m_002_studio11"]]}, open(d / "merchants_seed.json", "w", encoding="utf-8"))
    t = {**ds.triggers["trg_004_diwali_studio11"], "payload": {"festival": "Diwali", "countdown_days": 4}}
    json.dump({"triggers": [t]}, open(d / "triggers_seed.json", "w", encoding="utf-8"))
    r = subprocess.run([sys.executable, "audit_dataset.py", str(d)], cwd=ROOT, capture_output=True, text=True)
    assert "unread payload fields: countdown_days(1)" in r.stdout


def test_conversation_handlers_multi_turn():
    t = ds.triggers["trg_002_perfdip_meera"]
    m = ds.merchant_for(t)
    s = {"merchant": m, "category": ds.category_for(m), "trigger": t, "turns": []}
    o1 = CH.respond(s, "haan kar do")
    assert o1["action"] == "send" and "draft" in o1["body"]
    o2 = CH.respond(o1["state"], "publish karo")
    assert o2["body"].startswith("Ho gaya")
    assert CH.respond(o2["state"], "ok")["action"] == "end"


def test_metadata_env_overrides_team_json(monkeypatch):
    monkeypatch.setenv("TEAM_NAME", "Env Team")
    monkeypatch.setenv("TEAM_MEMBERS", "A, B")
    md = TestClient(bot.app).get("/v1/metadata").json()
    assert md["team_name"] == "Env Team" and md["team_members"] == ["A", "B"]


# ------------------------------------------------------------ fresh-scenario customer kinds (website: "surprise customer scopes")
def _cust_trigger(kind, cust, payload=None):
    return {"id": "t_x", "scope": "customer", "kind": kind, "merchant_id": "m_001_drmeera",
            "customer_id": cust["customer_id"], "payload": payload or {}}


def test_review_request_is_consent_gated_and_grounded():
    cat, m = ds.categories["dentists"], ds.merchants["m_001_drmeera"]
    msg, _ = compose_with_trace(cat, m, _cust_trigger("review_request", ds.customers["c_001_priya"]), ds.customers["c_001_priya"])
    assert msg["send_as"] == "merchant_on_behalf" and "12 May 2026" in msg["body"] and "http" not in msg["body"]


def test_promo_needs_promotional_consent():
    cat, m = ds.categories["dentists"], ds.merchants["m_001_drmeera"]
    pri = ds.customers["c_001_priya"]
    msg, tr = compose_with_trace(cat, m, _cust_trigger("customer_birthday", pri), pri)
    assert msg is None and tr.skipped_reason.startswith("no_consent")
    ok = {**pri, "consent": {"opted_in_at": "2025-11-04", "scope": ["promotions"]}}
    msg, _ = compose_with_trace(cat, m, _cust_trigger("customer_birthday", ok), ok)
    assert "janmadin" in msg["body"] and "STOP" in msg["body"]


def test_unknown_customer_kind_without_reason_is_not_guessed():
    cat, m = ds.categories["dentists"], ds.merchants["m_001_drmeera"]
    ok = {**ds.customers["c_001_priya"], "consent": {"scope": ["promotions"]}}
    msg, tr = compose_with_trace(cat, m, _cust_trigger("vip_upgrade", ok), ok)
    assert msg is None
    msg, _ = compose_with_trace(cat, m, _cust_trigger("vip_upgrade", ok, {"headline": "You've unlocked priority booking"}), ok)
    assert "priority booking" in msg["body"]
