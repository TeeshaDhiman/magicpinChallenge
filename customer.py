"""Customer-facing composition (send_as = merchant_on_behalf).

Vera drafts a message from the merchant to one of the merchant's own customers.
Built from Appendix B of the brief: customer's name and language, real last-visit
date and service history, open slots ranked by the customer's stated preference,
the merchant's live offer, and a numbered slot choice (allowed for booking flows).

Hard rules:
  * consent: if the customer's consent scope doesn't cover this kind of message, don't send
  * never invent a slot, an interval ("6-month"), an offer, or a medical claim
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import facts as F
from . import normalize as N

CUSTOMER_KINDS = {
    "recall_due": "recall", "recall_reminder": "recall", "customer_lapsed_soft": "recall",
    "customer_lapsed_hard": "winback", "customer_winback": "winback", "customer_churned": "winback",
    "appointment_tomorrow": "appointment", "appointment_reminder": "appointment",
    "review_request": "review", "post_visit_review": "review", "feedback_request": "review", "post_visit_followup": "review",
    "customer_birthday": "promo", "birthday": "promo", "offer_launch": "promo", "new_service_launch": "promo",
    "loyalty_milestone": "promo", "customer_festival": "promo",
}
CONSENT_NEEDED = {
    "recall": {"recall_reminders", "reminders", "all"},
    "appointment": {"appointment_reminders", "reminders", "all"},
    "winback": {"promotions", "offers", "marketing", "recall_reminders", "all"},
    "review": {"feedback", "review_requests", "reviews", "appointment_reminders", "recall_reminders", "reminders", "all"},
    "promo": {"promotions", "offers", "marketing", "all"},
}
SERVICE_WORD = {"dentists": "check-up", "salons": "appointment", "gyms": "session",
                "restaurants": "visit", "pharmacies": "refill"}
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class Slot:
    label: str               # "Wed 6 Nov, 6pm"
    short: str               # "Wed"
    dt: datetime | None


@dataclass
class CustomerDraft:
    family: str
    body: str
    cta: str
    lever: str
    params: list[str]
    slots: list[Slot] = field(default_factory=list)
    consent_note: str = ""

    @property
    def template_name(self) -> str:
        return f"merchant_customer_{self.family}_v1"


# ---------------------------------------------------------------- helpers
def _fmt_date(s) -> str | None:
    dt = N.parse_dt(s)
    return f"{dt.day} {_MONTHS[dt.month - 1]} {dt.year}" if dt else None


def _fmt_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if m == 0 else f"{h12}:{m:02d}{suffix}"


def _parse_slot(raw) -> Slot | None:
    if isinstance(raw, dict):
        label = raw.get("label") or raw.get("display")
        raw_dt = raw.get("start") or raw.get("datetime") or raw.get("time") or raw.get("slot")
        if label and not raw_dt:
            return Slot(str(label), str(label).split()[0], None)
        raw = raw_dt
    if raw is None:
        return None
    dt = N.parse_dt(raw) if re.match(r"^\d{4}-\d{2}-\d{2}", str(raw)) else None
    if dt:
        return Slot(f"{_DAYS[dt.weekday()]} {dt.day} {_MONTHS[dt.month - 1]}, {_fmt_time(dt)}",
                    _DAYS[dt.weekday()], dt)
    txt = str(raw).strip()
    return Slot(txt, txt.split()[0], None) if txt else None


def _matches(pref: str, s: Slot) -> bool:
    if not s.dt or not pref:
        lab = s.label.lower()
        return ("evening" in pref and re.search(r"\b([5-9]|1[0-1])\s*pm\b", lab) is not None) or \
               ("morning" in pref and "am" in lab)
    wd, h = s.dt.weekday() < 5, s.dt.hour
    ok_day = ("weekday" not in pref or wd) and ("weekend" not in pref or not wd)
    ok_time = (("evening" not in pref or h >= 17) and ("morning" not in pref or h < 12)
               and ("afternoon" not in pref or 12 <= h < 17))
    return ok_day and ok_time


def _slots(merchant: dict, p: dict, pref: str) -> list[Slot]:
    raw = (N.first(p, "available_slots", "open_slots", "slots", "slot_options")
           or N.first(merchant, "available_slots", "open_slots", "slots")
           or N.get(merchant, "availability", "slots") or N.get(merchant, "calendar", "open_slots") or [])
    slots = [s for s in (_parse_slot(r) for r in raw if r) if s]
    slots.sort(key=lambda s: (not _matches(pref, s), s.dt.isoformat() if s.dt else s.label))
    return slots[:2]


def _consent(customer: dict, family: str) -> tuple[bool, str]:
    c = customer.get("consent")
    if not isinstance(c, dict):
        return True, "no consent record in context; message kept strictly transactional"
    if c.get("opted_out") or c.get("revoked_at") or c.get("status") in ("revoked", "opted_out"):
        return False, "customer revoked consent"
    scope = {str(s).lower() for s in (c.get("scope") or [])}
    if scope and not (scope & CONSENT_NEEDED[family]):
        return False, f"consent scope {sorted(scope)} does not cover {family} messages"
    return True, f"opted in {c.get('opted_in_at', '')} with scope {sorted(scope) or 'unspecified'}".strip()


def _business_voice(merchant: dict, slug: str) -> str:
    """How the merchant signs its own message: "Dr. Meera's clinic", "Studio11 Family Salon"."""
    sal = N.salutation(merchant, slug, "en")
    if sal.startswith("Dr. "):
        return f"{sal}'s {N.business_noun(slug)}"
    return N.get(merchant, "identity", "name") or f"your {N.business_noun(slug)}"


def _service(customer: dict, p: dict) -> str | None:
    s = N.first(p, "service", "service_due", "treatment", "service_name")
    if s:
        return str(s)
    hist = N.get(customer, "relationship", "services_received") or []
    names = [str(x.get("name") if isinstance(x, dict) else x) for x in hist if x]
    return Counter(names).most_common(1)[0][0] if names else None


def _offer_for(fs: F.FactSheet, service: str | None) -> str | None:
    if service:
        for o in fs.active_offers:
            if service.lower() in o.lower():
                return o
    return fs.top_offer


def _months_between(a, b) -> int | None:
    da, db = N.parse_dt(a), N.parse_dt(b)
    if not da or not db or db <= da:
        return None
    return (db.year - da.year) * 12 + db.month - da.month - (1 if db.day < da.day else 0)


# ---------------------------------------------------------------- compose
def compose(category: dict, merchant: dict, trigger: dict, customer: dict) -> tuple[CustomerDraft | None, F.FactSheet, str]:
    """Returns (draft or None, factsheet for validation, skip_reason)."""
    fs = F.build(category or {}, merchant, trigger)
    F._harvest_numbers(customer, fs.allowed_numbers)
    fs.context_text += " " + str(customer).lower()

    kind = str(trigger.get("kind") or "").lower()
    family = CUSTOMER_KINDS.get(kind) or next(
        (fam for words, fam in ((("recall",), "recall"), (("appointment", "booking"), "appointment"),
                                (("lapsed", "winback", "churn"), "winback"), (("review", "feedback"), "review"),
                                (("birthday", "offer", "launch", "festival", "loyalty", "promo"), "promo"))
         if any(w in kind for w in words)), "")
    if not family and N.first(trigger.get("payload") or {}, "headline", "message", "occasion"):
        family = "promo"          # unknown customer kind with a stated reason: treat as promotional (strictest consent)
    if not family:
        return None, fs, "unknown_customer_trigger_kind"
    ok, consent_note = _consent(customer, family)
    if not ok:
        return None, fs, f"no_consent: {consent_note}"

    ident = customer.get("identity") or {}
    name = str(ident.get("name") or "").split()[0] if ident.get("name") else ""
    lang = N.lang_mode(ident.get("language_pref") or ident.get("languages")
                       or N.get(merchant, "identity", "languages"))
    fs.lang = lang
    hi = lang == "hinglish"
    t = (lambda en, h: h if hi else en)
    hello = f"Hi {name}" if name else t("Hi", "Namaste")
    biz = _business_voice(merchant, fs.slug)
    pref = str(N.get(customer, "preferences", "preferred_slots", default="") or "").lower()
    rel = customer.get("relationship") or {}
    last = rel.get("last_visit")
    last_txt = _fmt_date(last)
    p = fs.payload          # tracked, so audit_dataset.py can see which fields were read
    service = _service(customer, p)
    svc_word = service or SERVICE_WORD.get(fs.slug, "visit")
    offer = _offer_for(fs, service)

    if family == "appointment":
        appt = _parse_slot(N.first(p, "appointment_time", "appointment_at", "slot", "time", "start"))
        if appt:
            fs.allow(appt.label)
        when = t(f"tomorrow at {_fmt_time(appt.dt)}" if appt and appt.dt else "tomorrow",
                 f"kal {_fmt_time(appt.dt)} baje" if appt and appt.dt else "kal")
        if appt and appt.dt:
            fs.allow(_fmt_time(appt.dt))
        body = t(f"{hello}, a reminder from {biz}: your {svc_word} appointment is {when}. "
                 f"Reply 1 to confirm or 2 to reschedule.",
                 f"{hello}, {biz} se reminder: aapka {svc_word} appointment {when} hai. "
                 f"Confirm karne ke liye 1, reschedule ke liye 2 reply kijiye.")
        return CustomerDraft("appointment", body, "multi_choice_slot", "timeliness+low_effort_reply",
                             [hello, biz, when], [appt] if appt else [], consent_note), fs, ""

    if family == "review":
        visit = _fmt_date(N.first(p, "visit_date", "visited_on", "last_visit")) or last_txt
        last_txt = visit
        on = t(f" on {last_txt}", f" {last_txt} ko") if last_txt else ""
        body = t(f"{hello}, thank you for visiting {biz}{on}. If you have a minute, a short Google review helps other "
                 f"{fs.audience} find us. Reply YES and we'll send you the link.",
                 f"{hello}, {biz}{on} aane ke liye shukriya. Agar ek minute ho, toh ek chhota Google review doosre "
                 f"{fs.audience} ko humein dhoondhne mein madad karta hai. YES bhejiye, hum link bhej denge.")
        return CustomerDraft("review", body, "binary_yes_stop", "reciprocity+low_effort_reply",
                             [hello, biz, on], [], consent_note), fs, ""
    if family == "promo":
        occasion = N.first(p, "headline", "message", "occasion", "service", "new_service")
        is_bday = "birthday" in kind
        if not occasion and not offer and not is_bday:
            return None, fs, "insufficient_trigger_data"
        lead = t("Happy birthday from all of us!", "Aapko janmadin ki bahut shubhkamnayein!") if is_bday else \
            (f"{str(occasion).rstrip('.')}." if occasion else "")
        offer_line2 = t(f" {offer} is on for you.", f" Aapke liye {offer} chal raha hai.") if offer else ""
        body = t(f"{hello}, {biz} here. {lead}{offer_line2} Reply YES to book, or STOP to opt out.",
                 f"{hello}, {biz} here. {lead}{offer_line2} Book karne ke liye YES, messages band karne ke liye STOP bhejiye.")
        body = re.sub(r"\s+", " ", body).strip()
        return CustomerDraft("promo", body, "binary_yes_stop", "personal_occasion+real_offer+single_cta",
                             [hello, biz, lead], [], consent_note), fs, ""

    slots = _slots(merchant, p, pref)
    for s in slots:
        fs.allow(s.label)

    months = N.first(p, "months_since_last_visit", "months_since_visit")
    if months is None:
        months = _months_between(last, N.first(p, "as_of", "due_date", "recall_due_date", "date"))
    if months is not None:
        fs.allow(months)
    interval = N.first(p, "recall_interval_months", "interval_months", "recall_months")
    recall = (t(f"your {interval}-month {svc_word} recall", f"aapka {interval}-month {svc_word} recall") if interval
              else t(f"your next {svc_word}", f"aapka next {svc_word}"))

    if family == "recall":
        if months:
            why = t(f"It's been {months} months since your last visit, so {recall} is due.",
                    f"Aapki last visit ko {months} mahine ho gaye, {recall} due hai.")
        elif last_txt:
            why = t(f"Your last visit was on {last_txt}, so {recall} is due.",
                    f"Aapki last visit {last_txt} ko thi, {recall} due hai.")
        else:
            why = t(f"{recall[0].upper() + recall[1:]} is due.", f"{recall[0].upper() + recall[1:]} due hai.")
    else:  # winback: warm, no guilt
        why = t(f"It's been a while since your last visit{f' on {last_txt}' if last_txt else ''}, and we'd love to see you again.",
                f"Aapki last visit{f' ({last_txt})' if last_txt else ''} ko kaafi time ho gaya, aapse phir milna accha lagega.")

    offer_line = t(f"{offer} is on right now. ", f"Abhi {offer} chal raha hai. ") if offer else ""

    if len(slots) >= 2:
        a, b = slots[0], slots[1]
        slot_line = t(f"We have 2 slots open for you: {a.label} or {b.label}. ",
                      f"Aapke liye 2 slots ready hain: {a.label} ya {b.label}. ")
        cta_line = t(f"Reply 1 for {a.short}, 2 for {b.short}, or tell us a time that suits you.",
                     f"{a.short} ke liye 1, {b.short} ke liye 2 reply kijiye, ya apna time bataiye.")
        cta = "multi_choice_slot"
    elif len(slots) == 1:
        slot_line = t(f"One slot is open for you: {slots[0].label}. ", f"Aapke liye ek slot ready hai: {slots[0].label}. ")
        cta_line = t("Reply YES to book it, or tell us a time that suits you.",
                     "Book karne ke liye YES bhejiye, ya apna time bataiye.")
        cta = "binary_yes_stop"
    else:
        slot_line = ""
        cta_line = t("Reply with a day and time that suits you and we'll book it.",
                     "Apna convenient din aur time reply kijiye, hum book kar denge.")
        cta = "open_ended"
    if family == "winback":
        cta_line += t(" Reply STOP to opt out.", " Messages band karne ke liye STOP bhejiye.")

    body = f"{hello}, {biz} here. {why} {slot_line}{offer_line}{cta_line}"
    body = re.sub(r"\s+", " ", body).strip()
    return CustomerDraft(family, body, cta, "specificity+personal_history+low_effort_reply",
                         [hello, biz, why], slots, consent_note), fs, ""
