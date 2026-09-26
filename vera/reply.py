"""Reply engine for /v1/reply.

Rules first: every replay scenario (auto-reply, intent transition, hostile, off-topic)
is classified deterministically in well under a millisecond. Each intent routes to a
handler; handlers build text only from the FactSheet, and every body goes through the
same validator as proactive sends (grounding, taboos, jargon, repetition).

Conversation stages:  pitch -> draft_shown -> done
  pitch        Vera's opening message is out; waiting for the merchant
  draft_shown  merchant said yes; Vera delivered the concrete draft, asks to confirm
  done         merchant confirmed; Vera reported completion; next turn ends politely
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import facts as F
from . import decide as D
from . import normalize as N
from . import playbooks as P
from . import validate as V
from .store import Store

MAX_VERA_TURNS = 5

# ---------------------------------------------------------------- classification
_AUTO = [re.compile(p, re.I) for p in (
    r"thank(s| you) for (contacting|reaching out|your message|messaging)",
    r"\b(we|our team|team) will (get back|respond|reply|revert|contact)",
    r"\bautomated (assistant|message|reply|response)\b", r"\bauto[- ]?reply\b",
    r"\bcurrently (unavailable|away|closed|not available)\b",
    r"outside (our )?(business|working|office) hours",
    r"we have received your (message|query|request)",
    r"jaankari ke liye.*shukriya", r"sampark karne ke liye (dhanyavaad|dhanyawad|shukriya)",
    r"hamari team (tak|aapse)", r"main ek automated",
)]
_OPT_OUT = re.compile(r"\b(stop|unsubscribe|opt ?out|remove me|spam|band karo|mat bhejo|message mat|"
                      r"don'?t (message|text|contact) me|stop (messaging|texting|sending))\b", re.I)
_ABUSE = re.compile(r"\b(idiot|stupid|useless|nonsense|bakwas|bekaar|bekar|shut up|fraud|scam|"
                    r"bewakoof|pagal|rubbish|pathetic)\b", re.I)
_NEGATIVE = re.compile(r"\b(not interested|no thanks|no thank you|nahi chahiye|interest nahi|"
                       r"don'?t need|not needed|no need|zaroorat nahi)\b", re.I)
_BARE_NO = {"no", "nope", "nah", "nahi", "nahin", "na", "no.", "nhi"}
_DEFER = re.compile(r"\b(later|busy|not now|baad mein|baad me|abhi nahi|call later|"
                    r"some other time|tomorrow|in a meeting)\b", re.I)
_COMMIT = re.compile(r"\b(yes|yeah|yep|yup|haan|han|haa|ji haan|sure|ok|okay|go ahead|let'?s do it|lets do it|"
                     r"do it|kar do|kardo|kar dijiye|karo|send it|please do|proceed|chalega|chalo|theek hai|"
                     r"thik hai|agreed|interested|join|judna|start|confirm|publish|go for it|sounds good)\b", re.I)
_OFF_TOPIC = re.compile(r"\b(gst|income tax|itr|tax filing|file (my )?tax|loan|insurance|visa|passport|aadhaar|"
                        r"pan card|legal notice|lawyer|court case|credit card|electricity bill)\b", re.I)
_PRICE = re.compile(r"\b(price|pricing|cost|charge|charges|fee|fees|kitna|kitne|paisa|paise|how much)\b", re.I)
_MORE = re.compile(r"\b(tell me more|more details?|details|explain|elaborate|more info|samjhao|samjhaiye|"
                   r"batao|bataiye|detail mein|kaise kaam)\b", re.I)
_TIMING = re.compile(r"\b(how long|when will|how soon|kab tak|kab|kitna time|kitne din|how many days|timeline)\b", re.I)
# Objections (the website: replays test "replies, objections, auto-replies, and intent handoffs")
_OBJ_TRUST = re.compile(r"\b(is this (a )?(spam|scam|legit|real|genuine)|who gave (you )?my number|how did you get my number|"
                        r"are you a bot|kaun ho aap|number kahan se)\b", re.I)
_OBJ_COST = re.compile(r"\b(too expensive|expensive|costly|mehenga|mehnga|can'?t afford|no budget|budget nahi|"
                       r"paise nahi|too much money|kharcha)\b", re.I)
_OBJ_HAVE = re.compile(r"\b(already (have|use|doing|do)|we have (an? )?(agency|guy|person|team|marketing)|agency|"
                       r"someone (else )?(handles|does|manages)|pehle se|already koi|khud kar lete|i do it myself|"
                       r"i'?ll do it myself|my (nephew|son|staff) does)\b", re.I)
_OBJ_SKEPTIC = re.compile(r"\b(doesn'?t work|didn'?t work|never works?|no results|waste of (time|money)|pointless|"
                          r"kuch nahi hota|kuch nahi hua|fayda nahi|faayda nahi|not convinced|don'?t believe|prove it|"
                          r"tried (it|this) before)\b", re.I)
_OBJ_TIME = re.compile(r"\b(no time|don'?t have time|time nahi|waqt nahi|too much work|zyada kaam)\b", re.I)
_EDIT = re.compile(r"\b(change|update|edit|make it|shorter|longer|remove|add|replace|instead of|badlo|badal do|"
                   r"chhota|hindi mein|in hindi|in english|english mein)\b", re.I)
_WHO = re.compile(r"\b(who are you|kaun ho|aap kaun|who is this|what is magicpin|magicpin kya|what is vera|vera kaun)\b", re.I)
_CHANNEL = re.compile(r"\b(e-?mail|mail me|call me|phone (me|karo|kijiye)|call (karo|kijiye)|give me a call)\b", re.I)
_CONFUSED = re.compile(r"\b(samajh nahi|samjha nahi|didn'?t understand|don'?t understand|confused|what do you mean|"
                       r"matlab kya|kya matlab)\b", re.I)
_BENEFIT = re.compile(r"\b(kya fayda|kya faayda|fayda kya|what'?s the benefit|why should i|what will i get|"
                      r"how will (this|it) help|what'?s in it for me|kyun karu|kyon karun)\b", re.I)
_OFFER_Q = re.compile(r"\b(which offer|what offer|kaunsa offer|konsa offer|kaun sa offer|my offers?)\b", re.I)
_QWORD = re.compile(r"^(what|how|why|when|where|which|can|could|is|are|will|kya|kaise|kyun|kab|kahan|kaunsa)\b", re.I)
_HI_MARKERS = re.compile(r"\b(hai|hain|kya|nahi|nahin|haan|karo|kar|mujhe|aap|aapka|ji|bhai|theek|thik|chahiye|"
                         r"kaise|kitna|mein|hum|hamara|batao|dijiye|kijiye|accha|acha)\b", re.I)
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
# Devanagari has combining vowel signs, so \b word boundaries don't work: match substrings.
_DEV_OPT_OUT = re.compile(r"(बंद करो|बन्द करो|मत भेजो|मैसेज मत|परेशान मत)")
_DEV_NEGATIVE = re.compile(r"(नहीं चाहिए|ज़रूरत नहीं|जरूरत नहीं|इंटरेस्ट नहीं|^\s*(नहीं|ना)\s*[।.!]?\s*$)")
_DEV_DEFER = re.compile(r"(बाद में|अभी नहीं|कल बात|व्यस्त)")
_DEV_COMMIT = re.compile(r"(हाँ|हां|जी हाँ|ठीक है|कर दो|कर दीजिए|करो|चलो|बिल्कुल|भेज दो|ज़रूर|जरूर)")


@dataclass
class Intent:
    kind: str           # opt_out | auto_reply | abuse | negative | off_topic | defer | commit | price |
                        # who | question | other | empty
    off_topic: bool = False


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower()).strip(" .!?")


def classify(message: str, prior_merchant_msgs: list[str]) -> Intent:
    text = (message or "").strip()
    low = _norm(text)
    if not low:
        return Intent("empty")
    if _OBJ_TRUST.search(text):
        return Intent("objection_trust")
    if _OPT_OUT.search(text) or _DEV_OPT_OUT.search(text):
        return Intent("opt_out")
    if any(p.search(text) for p in _AUTO) or (len(low) > 15 and low in prior_merchant_msgs):
        return Intent("auto_reply")
    off = bool(_OFF_TOPIC.search(text))
    if _ABUSE.search(text):
        return Intent("abuse", off_topic=off)
    if _NEGATIVE.search(text) or low in _BARE_NO or _DEV_NEGATIVE.search(text):
        return Intent("negative")
    if off:
        return Intent("off_topic", off_topic=True)
    if _EDIT.search(text) and not _COMMIT.search(text.replace("ok", "")):
        return Intent("edit")
    for kind, rx in (("objection_cost", _OBJ_COST), ("objection_have_help", _OBJ_HAVE),
                     ("objection_skeptic", _OBJ_SKEPTIC), ("objection_time", _OBJ_TIME)):
        if rx.search(text):
            return Intent(kind)
    if (_DEFER.search(text) or _DEV_DEFER.search(text)) and not (_COMMIT.search(text) or _DEV_COMMIT.search(text)):
        return Intent("defer")
    if _WHO.search(text):
        return Intent("who")
    for kind, rx in (("channel", _CHANNEL), ("confused", _CONFUSED), ("benefit", _BENEFIT), ("offer_q", _OFFER_Q)):
        if rx.search(text):
            return Intent(kind)
    if _PRICE.search(text):
        return Intent("price")
    if _MORE.search(text) and not re.search(r"\b(do it|go ahead|kar do|send it|publish)\b", low):
        return Intent("question")
    if _COMMIT.search(text) or _DEV_COMMIT.search(text) or "👍" in text or "👌" in text:
        return Intent("commit")
    if "?" in text or _QWORD.search(low):
        return Intent("question")
    return Intent("other")


def detect_lang(message: str, default: str) -> str:
    """FAQ: match the merchant's identity.languages. A Hindi-speaking merchant stays in
    Hinglish even when they type English; an English-profile merchant who writes Hindi
    gets Hinglish back (they've shown they use it)."""
    if default == "hinglish":
        return "hinglish"
    if _DEVANAGARI.search(message or "") or len(_HI_MARKERS.findall(message or "")) >= 1:
        return "hinglish"
    return default


# ---------------------------------------------------------------- content helpers
BOOK_LINE = {
    "dentists": "Message us on WhatsApp or call to book your appointment.",
    "salons": "Message us on WhatsApp to book your slot.",
    "restaurants": "Order on WhatsApp or drop by.",
    "gyms": "Message us on WhatsApp to plan your visit.",
    "pharmacies": "Order on WhatsApp for quick pickup.",
}

DEFAULT_HEADLINE = {
    "dentists": "Due for a dental check-up",
    "salons": "Fresh look this week",
    "restaurants": "Craving something good tonight",
    "gyms": "Make this the week you start",
    "pharmacies": "Your medicines, sorted",
}

PENDING = {  # what "yes" would unlock, per family: (english, (hinglish object, hinglish verb stem))
    "research": ("send the key points and a patient-friendly WhatsApp note",
                 ("key points aur ek patient-friendly WhatsApp note", "bhej")),
    "regulation": ("send a 1-page summary of the change", ("change ka 1-page summary", "bhej")),
    "review_theme": ("draft replies to those reviews", ("un reviews ke replies", "draft kar")),
    "competitor": ("put together the side-by-side", ("side-by-side comparison", "bana")),
    "milestone": ("post a thank-you on your Google profile", ("Google profile pe ek thank-you post", "daal")),
}
_POST_PENDING = ("draft a fresh Google post", ("ek fresh Google post", "draft kar"))


DONE = {  # what "confirmed" means, per deliverable
    "post": ("Done. It's queued for your Google profile, and I'll share the live link here once it's up.",
             "Ho gaya. Aapke Google profile ke liye queue kar diya hai; live hote hi link yahin bhej dungi."),
    "research": ("Done. The patient note above is final, so you can forward it to your patients as it is.",
                 "Ho gaya. Upar wala patient note final hai; aap ise seedha apne patients ko forward kar sakte hain."),
    "regulation": ("Done. I'll check your listing against the change and flag anything that needs updating here.",
                   "Ho gaya. Aapki listing ko is change ke saath check karke jo update chahiye woh yahin flag karungi."),
    "review_theme": ("Done. The replies are queued on those reviews, and I'll confirm here once they're live.",
                     "Ho gaya. Replies un reviews pe queue ho gaye hain; live hote hi yahin confirm karungi."),
    "competitor": ("Done. I'll send the side-by-side here next.",
                   "Ho gaya. Side-by-side agla message mein yahin bhejti hoon."),
}


def _t(lang: str, en: str, hi: str) -> str:
    return hi if lang == "hinglish" else en


def _pending(fs: F.FactSheet | None, family: str, lang: str, form: str = "can") -> str:
    """English: bare verb phrase ("draft a fresh Google post").
    Hinglish: conjugated, form="can" -> "ek fresh Google post draft kar sakti hoon",
                          form="will" -> "ek fresh Google post draft kar dungi".
    """
    en, (obj, verb) = PENDING.get(family, _POST_PENDING)
    fest = N.first(fs.payload, "festival", "festival_name") if (fs and family == "festival") else None
    if fest:
        en, obj = f"draft the {fest} Google post", f"{fest} ki Google post"
    if family not in PENDING and fs and fs.top_offer:
        en, obj = f'{en} featuring "{fs.top_offer}"', f'"{fs.top_offer}" ke saath {obj}'
    if lang != "hinglish":
        return en
    return f"{obj} {verb} {'sakti hoon' if form == 'can' else 'dungi'}"


BOOK_LINE_HI = {
    "dentists": "Appointment ke liye WhatsApp ya call kijiye.",
    "salons": "Slot book karne ke liye WhatsApp kijiye.",
    "restaurants": "WhatsApp pe order kijiye ya aa jaiye.",
    "gyms": "Visit plan karne ke liye WhatsApp kijiye.",
    "pharmacies": "Jaldi pickup ke liye WhatsApp pe order kijiye.",
}
DEFAULT_HEADLINE_HI = {
    "dentists": "Dental check-up due hai?", "salons": "Is hafte naya look", "restaurants": "Aaj kuch accha khaane ka mann?",
    "gyms": "Is hafte shuru kijiye", "pharmacies": "Aapki dawaiyan, bina tension",
}


def _post_text(fs: F.FactSheet, headline: str) -> str:
    offer = fs.top_offer or (fs.catalog_offers[0] if fs.catalog_offers else None)
    where = f"{fs.name}, {fs.locality}" if fs.locality else fs.name
    if getattr(fs, "post_hindi", False):
        head = DEFAULT_HEADLINE_HI.get(fs.slug, headline) if headline in DEFAULT_HEADLINE.values() else headline
        parts = [head.rstrip("."), f"{where} mein {offer}." if offer else f"{where} mein aaiye."]
        parts.append(BOOK_LINE_HI.get(fs.slug, "WhatsApp kijiye."))
        return " ".join(x if x.endswith((".", "?")) else x + "." for x in parts)
    parts = [headline.rstrip("."), f"{offer} at {where}." if offer else f"At {where}."]
    parts.append(BOOK_LINE.get(fs.slug, "Message us on WhatsApp."))
    return " ".join(p if p.endswith(".") else p + "." for p in parts)


def _draft(fs: F.FactSheet, family: str, trigger: dict, lang: str, acting: bool = True) -> str:
    """The concrete deliverable behind the 'yes'. Grounded in facts only."""
    item = fs.digest_item or {}
    confirm = _t(lang, "Reply YES to go ahead, or send me the changes.",
                 "Theek lage to YES bhejiye, ya changes bata dijiye.")
    here = _t(lang, "Here's the draft", "Yeh raha draft")

    if family == "research" and item.get("title"):
        src = item.get("source") or "the journal"
        n = item.get("trial_n")
        trial = f", {N.fmt_int(n)} participants" if n else ""
        patient = (f'"From {fs.name}: new research in {src} found that {item["title"][0].lower() + item["title"][1:]}. '
                   f'Ask us at your next visit whether this applies to you."')
        done = _t(lang, "Done. ", "Ho gaya. ") if acting else ""
        return _t(lang,
                  f"{done}Key point: {item['title']} ({src}{trial}). {here} of the patient WhatsApp note: {patient} {confirm}",
                  f"{done}Key point: {item['title']} ({src}{trial}). {here}, patient WhatsApp note: {patient} {confirm}")
    if family == "regulation":
        title = item.get("title") or N.first(fs.payload, "title", "headline", default="the new rule")
        src = item.get("source") or N.first(fs.payload, "source", default="")
        eff = item.get("effective_date") or N.first(fs.payload, "effective_date", default="")
        eff_txt = _t(lang, f" It takes effect {eff}.", f" Yeh {eff} se laagu hai.") if eff else ""
        src_txt = f" ({src})" if src else ""
        return _t(lang,
                  f"Here's the summary: {title}{src_txt}.{eff_txt} Next, I'll flag anything on your listing that needs updating. {confirm}",
                  f"Summary yeh hai: {title}{src_txt}.{eff_txt} Next, aapki listing mein jo update chahiye woh main flag kar dungi. {confirm}")
    if family == "review_theme":
        theme = N.first(fs.payload, "theme", "topic", default="your service")
        count = N.first(fs.payload, "count", "review_count")
        reply = (f'"Thank you for the feedback about {theme}. We hear you and we\'re working on it. '
                 f'Please message us directly so we can make your next visit better."')
        n_txt = _t(lang, f" for all {count}", f", sabhi {count} ke liye") if count else ""
        return _t(lang, f"{here} reply{n_txt}: {reply} {confirm}", f"{here}{n_txt}: {reply} {confirm}")
    if family == "competitor":
        mine = [f for f in fs.support if f.key in ("ctr_gap", "views")][:2]
        mine_txt = "; ".join(f.text(lang) for f in mine)
        side = _t(lang, f" Your side: {mine_txt}." if mine_txt else "", f" Aapki side: {mine_txt}." if mine_txt else "")
        return _t(lang,
                  f"On it. I'm pulling their public Google listing and will send the side-by-side here next.{side} "
                  f"Reply YES if you also want me to draft a Google post to answer them.",
                  f"Kar rahi hoon. Unki public Google listing se side-by-side bana ke yahin bhejti hoon.{side} "
                  f"Jawab mein ek Google post bhi draft kar doon to YES bhejiye.")
    if family == "milestone":
        value = N.first(fs.payload, "value", "threshold", "count")
        label = N.metric_label(str(N.first(fs.payload, "metric", default="reviews")))
        head = f"Thank you for {N.fmt_int(value)} {label}!" if value else "Thank you to everyone who visited us!"
        return f"{here}: \"{_post_text(fs, head)}\" {confirm}"
    if family == "festival":
        name = N.first(fs.payload, "festival", "festival_name", "name", default="the festival")
        head = f"{name} ki shubhkamnayein, hamare saath manaiye" if getattr(fs, "post_hindi", False) else f"Celebrate {name} with us"
        return f"{here}: \"{_post_text(fs, head)}\" {confirm}"
    if family == "trend":
        q = N.first(fs.payload, "query", "search_term", default="")
        head = f"Looking for {q}? We can help" if q else "Now available"
        return f"{here}: \"{_post_text(fs, head)}\" {confirm}"
    if family in ("weather", "local_news"):
        return f"{here}: \"{_post_text(fs, 'We are open today')}\" {confirm}"
    # perf_dip, perf_spike, curious_ask, generic, unknown
    return f"{here}: \"{_post_text(fs, DEFAULT_HEADLINE.get(fs.slug, 'This week at ' + fs.name))}\" {confirm}"


# ---------------------------------------------------------------- engine
def _conv(store: Store, body) -> dict:
    conv = store.conversations.get(body.conversation_id)
    if conv is None:
        conv = {"merchant_id": body.merchant_id, "trigger_id": None, "turns": [], "status": "open"}
        store.conversations[body.conversation_id] = conv
    conv.setdefault("stage", "pitch")
    conv.setdefault("abuse", 0)
    conv.setdefault("other", 0)
    return conv


def _vera_turns(conv: dict) -> int:
    return sum(1 for t in conv["turns"] if t.get("from") == "vera")


def respond(store: Store, body) -> dict:
    conv = _conv(store, body)
    if conv.get("customer_id") or body.from_role == "customer":
        conv.setdefault("customer_id", body.customer_id)
        return respond_customer(store, body, conv)
    mid = body.merchant_id or conv.get("merchant_id")
    mem = store.mem(mid) if mid else None
    prior = list(mem.merchant_msgs) if mem else []
    intent = classify(body.message, prior)
    conv["turns"].append({"from": body.from_role, "body": body.message, "at": body.received_at, "intent": intent.kind})
    if mem and intent.kind != "empty":
        mem.merchant_msgs.append(_norm(body.message))

    merchant = store.get("merchant", mid)
    trigger = store.get("trigger", conv.get("trigger_id")) or {}
    category = store.get("category", (merchant or {}).get("category_slug")) or {}
    family = P.route(trigger.get("kind")) if trigger else "generic"
    fs = F.build(category, merchant, trigger) if merchant else None
    lang = conv.get("lang") or detect_lang(body.message, fs.lang if fs else "en")
    if fs:
        for t in conv["turns"]:
            if t.get("from") != "vera":
                fs.allow(t.get("body"))           # what the merchant said is grounded
        fs.lang = lang
        fs.post_hindi = bool(conv.get("post_hindi"))
        fs.sal = N.salutation(merchant, fs.slug, lang)

    def send(text: str, why: str, cta: str = "open_ended", stage: str | None = None) -> dict:
        sent = mem.sent_bodies if mem else set()
        if fs:
            res = V.check(text, cta, fs, sent, allow_intro=intent.kind in ("who", "objection_trust"))
            if not res.ok:
                return wait(1800, f"Composed reply failed validation ({res.hard}); backing off instead of sending it.")
        elif text in sent:
            return wait(1800, "Would have repeated an earlier message; backing off.")
        if mem:
            mem.sent_bodies.add(text)
        conv["turns"].append({"from": "vera", "body": text, "at": body.received_at})
        if stage:
            conv["stage"] = stage
            if stage == "done":
                conv["status"] = "closed"   # task complete: free the merchant for future proactive sends
        return {"action": "send", "body": text, "cta": cta,
                "rationale": f"Intent={intent.kind}, stage={conv['stage']}, language={lang}. {why}"}

    def end(why: str) -> dict:
        conv["status"] = "closed"
        return {"action": "end", "rationale": f"Intent={intent.kind}. {why}"}

    def wait(sec: int, why: str) -> dict:
        return {"action": "wait", "wait_seconds": sec, "rationale": f"Intent={intent.kind}. {why}"}

    k = intent.kind
    if k == "opt_out":
        if mem:
            mem.opted_out = True
        return end("Merchant asked to stop. Closing and suppressing all future proactive sends.")
    if k == "empty":
        return wait(1800, "Empty message; nothing to act on.")

    if k == "auto_reply":
        mem.auto_replies = (mem.auto_replies + 1) if mem else 1
        if mem and mem.auto_replies >= 2:
            return end(f"Auto-reply #{mem.auto_replies} for this merchant (canned text / verbatim repeat). "
                       "Exiting instead of burning more turns; the owner can reply any time.")
        name = fs.sal if fs else "there"
        return send(_t(lang,
                       f"Looks like an auto-reply, so this one's for the owner: {name}, one reply from you and I'll "
                       f"{_pending(fs, family, 'en')}. Just send YES.",
                       f"Lagta hai yeh auto-reply hai, isliye owner ke liye: {name}, aapka ek reply aur main "
                       f"{_pending(fs, family, 'hinglish', 'will')}. Bas YES bhej dijiye."),
                    "First auto-reply detected: one owner-directed probe, then exit on the next.",
                    cta="binary_yes_stop")

    if k == "abuse":
        conv["abuse"] += 1
        if conv["abuse"] >= 2:
            return end("Repeated hostility; exiting politely rather than pushing.")
        extra = ""
        if intent.off_topic:
            extra = _t(lang, " GST and tax filing are outside what I can do; a CA is the right person for that.",
                       " GST aur tax filing mere scope ke bahar hai; uske liye CA sahi rahenge.")
        return send(_t(lang,
                       f"Sorry if my messages felt pushy; I won't spam you.{extra} If it helps, the one thing I can do "
                       f"today is {_pending(fs, family, 'en')}. Reply YES for that, or STOP and I'll stop.",
                       f"Sorry agar mere messages zyada lage; main spam nahi karungi.{extra} Agar kaam aaye to aaj main "
                       f"{_pending(fs, family, 'hinglish')}. YES bhejiye, ya STOP aur main ruk jaungi."),
                    "Hostile reply: apologize once, offer the single most useful action, give a clean exit.",
                    cta="binary_yes_stop")

    if k == "negative":
        return end("Merchant declined. Graceful exit, no follow-up push.")

    if k == "off_topic":
        return send(_t(lang,
                       f"That's outside what I can help with; a CA or the right specialist is best for it. What I can do "
                       f"right now is {_pending(fs, family, 'en')}. Reply YES and it's done.",
                       f"Yeh mere scope ke bahar hai; iske liye CA ya sahi specialist best rahenge. Abhi main "
                       f"{_pending(fs, family, 'hinglish')}. YES bhejiye, ho jayega."),
                    "Off-topic request: decline honestly, don't fake help, redirect to the pending action.",
                    cta="binary_yes_stop")

    if k.startswith("objection_"):
        if k != "objection_trust":             # a trust question is always answered, never counted
            conv["objections"] = conv.get("objections", 0) + 1
        if conv.get("objections", 0) >= 3 and k != "objection_trust":
            return end("Third objection in this conversation: stepping back instead of pushing.")
        pend_en, pend_hi = _pending(fs, family, "en"), _pending(fs, family, "hinglish", "will")
        yes_en, yes_hi = "Reply YES and I'll share it here.", "YES bhejiye, main yahin share kar dungi."
        if k == "objection_trust":
            biz = fs.name if fs else _t(lang, "your business", "aapke business")
            return send(_t(lang, f"Fair question. I'm Vera, magicpin's assistant. You're hearing from me because {biz} "
                                 f"is a magicpin partner and I help with its Google profile. Reply STOP any time and I'll stop.",
                           f"Sahi sawaal. Main Vera hoon, magicpin ki assistant. Aapse isliye baat kar rahi hoon kyunki {biz} "
                           f"magicpin partner hai aur main uske Google profile mein madad karti hoon. Kabhi bhi STOP bhejiye, main ruk jaungi."),
                        "Trust objection: say plainly who is messaging and offer an exit.", cta="binary_yes_stop")
        if k == "objection_cost":
            return send(_t(lang, f"Fair. Reviewing the draft costs you nothing, and nothing goes live without your YES. {yes_en}",
                           f"Sahi baat. Draft dekhne ka koi kharcha nahi, aur aapke YES ke bina kuch live nahi hoga. {yes_hi}"),
                        "Cost objection: no invented prices; removes risk from the next step.", cta="binary_yes_stop")
        if k == "objection_have_help":
            return send(_t(lang, f"Makes sense. I can hand the draft to whoever manages your profile, so it's one quick review "
                                 f"for them, not extra work. {yes_en}",
                           f"Theek hai. Jo aapka profile sambhalte hain, draft unko de sakti hoon; unke liye bas ek quick review. {yes_hi}"),
                        "Already-have-help objection: position as input to their existing person, not a replacement.",
                        cta="binary_yes_stop")
        if k == "objection_skeptic":
            fact = D.choose_support(fs, family if family in D.PAIRING else "generic") if fs else None
            if fs and not fact and fs.support:
                fact = fs.support[0]
            proof_en = f" Here's what I'm going on: {fact.text('en')}." if fact else ""
            proof_hi = f" Main is basis pe keh rahi hoon: {fact.text('hinglish')}." if fact else ""
            return send(_t(lang, f"Fair to be skeptical.{proof_en} Start with one post and judge it by your own numbers. "
                                 f"Reply YES and I'll {pend_en}.",
                           f"Shaq karna sahi hai.{proof_hi} Ek post se shuru karte hain, aapke apne numbers se judge kijiye. "
                           f"YES bhejiye, main {pend_hi}."),
                        "Skeptic objection: answered with the merchant's own data point, small first step.",
                        cta="binary_yes_stop")
        if k == "objection_time":
            return send(_t(lang, f"That's exactly why I'd do it for you: one YES and I'll {pend_en}. Nothing else needed from you.",
                           f"Isiliye toh main kar deti hoon: bas ek YES aur main {pend_hi}. Aapko aur kuch nahi karna."),
                        "Time objection: effort externalization, one-tap next step.", cta="binary_yes_stop")

    if k == "edit":
        msg = body.message or ""
        if re.search(r"\b(in hindi|hindi mein|hinglish)\b", msg, re.I):
            conv["lang"] = lang = "hinglish"
            conv["post_hindi"] = True
            if fs:
                fs.lang = "hinglish"
                fs.post_hindi = True
        elif re.search(r"\b(in english|english mein)\b", msg, re.I):
            conv["lang"] = lang = "en"
            if fs:
                fs.lang = "en"
        if fs is None:
            return send(_t(lang, "Noted. I'll make that change and share the revised draft here for your confirmation.",
                           "Note kar liya. Change karke revised draft yahin bhejti hoon, aap confirm kar dijiye."),
                        "Edit request without merchant context: acknowledged.", cta="none")
        price = re.search(r"₹\s?(\d[\d,]*)|\b(\d{2,5})\s*(rs|rupees|rupay)\b", msg, re.I)
        if price and (fs.top_offer or fs.catalog_offers):
            new = (price.group(1) or price.group(2)).replace(",", "")
            base = fs.top_offer or fs.catalog_offers[0]
            edited = re.sub(r"₹\s?\d[\d,]*", f"₹{new}", base) if "₹" in base else f"{base} @ ₹{new}"
            fs.active_offers = [edited] + [o for o in fs.active_offers if o != fs.top_offer]
            fs.allow(new)
        draft = _draft(fs, family, trigger, lang, acting=False)
        if re.search(r"\b(shorter|short|chhota)\b", msg, re.I) and family not in PENDING:
            offer = fs.top_offer or (fs.catalog_offers[0] if fs.catalog_offers else None)
            short = f"{offer} at {fs.name}. {BOOK_LINE.get(fs.slug, 'Message us on WhatsApp.')}" if offer \
                else f"{fs.name}. {BOOK_LINE.get(fs.slug, 'Message us on WhatsApp.')}"
            draft = _t(lang, f"Shorter version: \"{short}\" Reply YES to go ahead, or send more changes.",
                       f"Chhota version: \"{short}\" Theek lage to YES bhejiye, ya aur changes bataiye.")
        return send(_t(lang, "Updated. ", "Update kar diya. ") + draft,
                    "Edit request: applied the merchant's change (price / length / language) and re-showed the draft.",
                    cta="binary_yes_stop", stage="draft_shown")

    if k == "defer":
        tomorrow = "tomorrow" in body.message.lower() or " kal" in f" {body.message.lower()}"
        return wait(86400 if tomorrow else 14400, "Merchant asked for time; backing off without another nudge.")

    if _vera_turns(conv) >= MAX_VERA_TURNS:
        return end("Turn budget reached for this conversation; closing cleanly.")

    if k in ("channel", "confused", "benefit", "offer_q") and fs is not None:
        fact = D.choose_support(fs, family if family in D.PAIRING else "generic") or (fs.support[0] if fs.support else None)
        yes = _t(lang, "Reply YES and I'll go ahead.", "YES bhejiye, main aage badhti hoon.")
        if k == "channel":
            lead = _t(lang, "I can only reach you here on WhatsApp. ", "Main sirf yahin WhatsApp pe baat kar sakti hoon. ")
            return send(lead + _draft(fs, family, trigger, lang, acting=False),
                        "Asked for email/call: honest about the channel, delivered the content here.",
                        cta="binary_yes_stop", stage="draft_shown")
        if k == "offer_q":
            if len(fs.active_offers) == 1:
                o = fs.active_offers[0]
                txt = _t(lang, f'Your live offer is "{o}", so that\'s the one I\'d feature. {yes}',
                         f'Aapka live offer "{o}" hai, wahi feature karungi. {yes}')
            elif fs.active_offers:
                listed = ", ".join(f'"{o}"' for o in fs.active_offers[:3])
                txt = _t(lang, f'Your live offers are {listed}. I\'d lead with "{fs.active_offers[0]}". {yes}',
                         f'Aapke live offers {listed} hain. Main "{fs.active_offers[0]}" se shuru karungi. {yes}')
            elif fs.catalog_offers:
                txt = _t(lang, f"You don't have a live offer right now. A service+price offer like \"{fs.catalog_offers[0]}\" "
                               f"is a common one for {fs.business}s. {yes}",
                         f"Abhi aapka koi live offer nahi hai. \"{fs.catalog_offers[0]}\" jaisa service+price offer "
                         f"{fs.business}s ke liye common hai. {yes}")
            else:
                txt = _t(lang, f"You don't have a live offer right now, so the post would go out without one. {yes}",
                         f"Abhi aapka koi live offer nahi hai, toh post bina offer ke jayegi. {yes}")
            return send(txt, "Offer question: answered from the merchant's live offers / category catalog.", cta="binary_yes_stop")
        why = fact.text(lang) if fact else None
        if k == "confused":
            txt = _t(lang, f"Simply put: {why + '. ' if why else ''}The fix I'm suggesting is to {_pending(fs, family, 'en')}. {yes}",
                     f"Seedhi baat: {why + '. ' if why else ''}Mera suggestion: main {_pending(fs, family, 'hinglish')}. {yes}")
            return send(txt, "Merchant didn't understand: one fact, one action, in plain words.", cta="binary_yes_stop")
        txt = _t(lang, f"Here's why it's worth it: {why + '. ' if why else ''}A fresh, specific update gives the people "
                       f"looking at your profile a current reason to choose you. {yes}",
                 f"Fayda yeh hai: {why + '. ' if why else ''}Ek fresh, specific update se aapka profile dekhne walon ko "
                 f"aapko chunne ki ek taaza wajah milti hai. {yes}")
        return send(txt, "Benefit question: answered with the merchant's own data point, no promised results.", cta="binary_yes_stop")

    if k == "who" and re.search(r"magicpin", body.message or "", re.I):
        biz = fs.name if fs else _t(lang, "your business", "aapka business")
        return send(_t(lang,
                       f"magicpin is a local-commerce platform: customers discover businesses like {biz}, buy from them and "
                       f"earn cashback. I'm Vera, magicpin's assistant for your Google profile. Right now I can "
                       f"{_pending(fs, family, 'en')}. Reply YES and I'll send it.",
                       f"magicpin ek local-commerce platform hai: customers {biz} jaise businesses dhoondhte hain, kharidte hain "
                       f"aur cashback kamaate hain. Main Vera hoon, magicpin ki assistant, aapke Google profile ke liye. Abhi main "
                       f"{_pending(fs, family, 'hinglish')}. YES bhejiye."),
                    "Asked what magicpin is: answered plainly, then back to the single next step.", cta="binary_yes_stop")
    if k == "who":
        biz = fs.name if fs else _t(lang, "your business", "aapke business")
        return send(_t(lang,
                       f"I'm Vera, magicpin's assistant for {biz}'s Google profile and customer messages. Right now I can "
                       f"{_pending(fs, family, 'en')}. Reply YES and I'll send it.",
                       f"Main Vera hoon, magicpin ki assistant, {biz} ke Google profile aur customer messages ke liye. Abhi main "
                       f"{_pending(fs, family, 'hinglish')}. YES bhejiye."),
                    "Identity question answered plainly, then back to the single next step.", cta="binary_yes_stop")

    if k == "price":
        return send(_t(lang,
                       "I don't have pricing in front of me, so I won't guess; I'll have the magicpin team confirm it for you. "
                       "Reviewing the draft costs nothing. Reply YES and I'll send it.",
                       "Pricing abhi mere paas nahi hai, isliye guess nahi karungi; magicpin team aapko confirm kar degi. "
                       "Draft dekhne ka koi charge nahi. YES bhejiye, main bhej deti hoon."),
                    "Price question: no invented numbers; keep the low-friction next step.", cta="binary_yes_stop")

    if k == "commit":
        if conv["stage"] == "draft_shown":
            en, hi = DONE.get(family, DONE["post"])
            return send(_t(lang, en, hi), "Merchant confirmed the draft: execute and report back.", cta="none", stage="done")
        if conv["stage"] == "done":
            return end("Task already completed and reported; closing instead of re-pitching.")
        if fs is None:
            return send(_t(lang,
                           "Great, starting now. I'm preparing the draft and will share it here next for your confirmation.",
                           "Badhiya, abhi shuru kar rahi hoon. Draft bana ke yahin bhejti hoon, aap confirm kar dijiye."),
                        "Commitment on an unknown merchant: act immediately, no qualifying questions.",
                        cta="none", stage="draft_shown")
        return send(_draft(fs, family, trigger, lang),
                    "Explicit commitment: switched to action mode and delivered the concrete draft, no re-qualifying.",
                    cta="binary_yes_stop", stage="draft_shown")

    if k == "question":
        if fs is None:
            return send(_t(lang,
                           "Good question. I'll get you a precise answer from the magicpin team and send it here.",
                           "Accha sawaal hai. magicpin team se sahi jawab le ke yahin bhejti hoon."),
                        "Question without merchant context: don't guess.", cta="none")
        msg = body.message or ""
        next_step = _t(lang, "Reply YES and I'll go ahead.", "YES bhejiye, main aage badhti hoon.")
        if _TIMING.search(msg) and conv["stage"] == "draft_shown" and family not in PENDING:
            return send(_t(lang,
                           "As soon as you reply YES, the post above goes into your Google profile queue, and I'll share the "
                           "live link here the moment it's up. Nothing is published without your YES.",
                           "Aapke YES ke turant baad upar wali post Google profile ki queue mein chali jayegi, aur live hote hi "
                           "link yahin bhej dungi. Aapke YES ke bina kuch publish nahi hoga."),
                        "Timing question after the draft: honest answer (no invented durations), merchant keeps control.",
                        cta="binary_yes_stop")
        if _TIMING.search(msg):
            return send(_t(lang,
                           f"As soon as you reply YES, I'll {_pending(fs, family, 'en')}, and I'll confirm here the moment it's done. "
                           f"Nothing goes live without your YES.",
                           f"Aapke YES ke turant baad main {_pending(fs, family, 'hinglish', 'will')}, aur hote hi yahin confirm karungi. "
                           f"Aapke YES ke bina kuch live nahi hoga."),
                        "Timing question: honest answer (no invented durations), control stays with the merchant.",
                        cta="binary_yes_stop")
        item = fs.digest_item or {}
        if family == "research" and item.get("title") and conv["stage"] != "pitch":
            seg = N.humanize_token(item["patient_segment"]) if item.get("patient_segment") else None
            n = item.get("trial_n")
            scope_en = f"The {item.get('source', 'study')} trial" + (f" ({N.fmt_int(n)} participants)" if n else "")
            scope_en += f" was in {seg}; it doesn't say anything beyond that group, so I wouldn't extend it." if seg else \
                " is the only source I have; I won't go beyond what it says."
            scope_hi = f"{item.get('source', 'Study')} trial" + (f" ({N.fmt_int(n)} participants)" if n else "")
            scope_hi += f" {seg} pe tha; usse aage ke liye kuch nahi kehta, isliye main extend nahi karungi." if seg else \
                " hi mera source hai; usse aage main kuch nahi kahungi."
            return send(_t(lang, f"{scope_en} {next_step}", f"{scope_hi} {next_step}"),
                        "Scope question on research: answered strictly from the digest item (segment, trial size).",
                        cta="binary_yes_stop")
        if conv["stage"] == "pitch":
            if family == "research" and item.get("title") and item.get("patient_segment"):
                pass  # the draft below states source and trial; scope follow-ups are handled above
            return send(_draft(fs, family, trigger, lang, acting=False),
                        "Merchant asked what/how: answered with the concrete draft instead of more description.",
                        cta="binary_yes_stop", stage="draft_shown")
        return send(_t(lang,
                       f"Fair question. I don't have more detail than what's in the draft above, so I won't guess. {next_step}",
                       f"Sahi sawaal. Upar wale draft se zyada detail mere paas nahi hai, isliye guess nahi karungi. {next_step}"),
                    "Follow-up question after the draft: honest about limits, no repeat, one next step.",
                    cta="binary_yes_stop")

    # other: free text. In a curious-ask thread, that text is the answer we asked for.
    if family == "curious_ask" and fs and conv["stage"] == "pitch":
        answer = re.sub(r"\s+", " ", body.message).strip().strip(".")[:60]
        post = _post_text(fs, f"Most asked this week: {answer}")
        return send(_t(lang, f"Perfect. Here's the post: \"{post}\" Reply YES to publish, or send changes.",
                       f"Perfect. Yeh raha post: \"{post}\" YES bhejiye to publish kar dungi, ya changes bata dijiye."),
                    "Merchant answered the curious ask: turned their answer into a ready post.",
                    cta="binary_yes_stop", stage="draft_shown")
    conv["other"] += 1
    if conv["other"] >= 2:
        return wait(14400, "Two unclear replies in a row; pausing instead of guessing.")
    return send(_t(lang,
                   f"Got it. To keep it simple: I can {_pending(fs, family, 'en')}. Reply YES and I'll send it.",
                   f"Samajh gayi. Simple rakhte hain: main {_pending(fs, family, 'hinglish')}. YES bhejiye."),
                "Unclear reply: restate the single next step once.", cta="binary_yes_stop")


# ================================================================ customer replies
_SLOT_PICK = re.compile(r"^\s*(1|2)\b|\b(option|slot)\s*(1|2)\b", re.I)
_TIME_WORDS = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b\d{1,2}\s*(am|pm|baje)\b|"
                         r"\b(morning|evening|afternoon|subah|shaam|sham|weekend|tomorrow|kal)\b", re.I)


def respond_customer(store: Store, body, conv: dict) -> dict:
    from .store import customer_key
    cid = conv.get("customer_id") or body.customer_id
    mid = conv.get("merchant_id") or body.merchant_id
    customer = store.get("customer", cid) or {}
    merchant = store.get("merchant", mid) or {}
    trigger = store.get("trigger", conv.get("trigger_id")) or {}
    category = store.get("category", merchant.get("category_slug")) or {}
    meta = conv.get("meta") or {}
    slots, shorts = meta.get("slots") or [], meta.get("slot_short") or []
    fam = meta.get("customer_family", "recall")
    name = meta.get("customer_name") or str(N.get(customer, "identity", "name", default="") or "").split(" ")[0]
    biz = meta.get("business_voice") or N.get(merchant, "identity", "name", default="the team")
    default_lang = N.lang_mode(N.get(customer, "identity", "language_pref"))
    lang = detect_lang(body.message, default_lang)
    mem = store.mem(customer_key(cid)) if cid else None

    fs = F.build(category, merchant, trigger) if merchant else None
    if fs:
        F._harvest_numbers(customer, fs.allowed_numbers)
        fs.allow(*slots)

    intent = classify(body.message, list(mem.merchant_msgs) if mem else [])
    conv["turns"].append({"from": "customer", "body": body.message, "at": body.received_at, "intent": intent.kind})
    if mem and intent.kind != "empty":
        mem.merchant_msgs.append(_norm(body.message))

    def send(text: str, why: str, done: bool = False) -> dict:
        if fs and not V.check(text, "none", fs, mem.sent_bodies if mem else set()).ok:
            return {"action": "wait", "wait_seconds": 3600, "rationale": "Reply failed validation; the merchant's team will follow up."}
        if mem:
            mem.sent_bodies.add(text)
        conv["turns"].append({"from": "vera", "body": text, "at": body.received_at})
        if done:
            conv["status"] = "closed"
        return {"action": "send", "body": text, "cta": "none",
                "rationale": f"Customer reply, intent={intent.kind}, language={lang}, sent as the merchant. {why}"}

    def end(why: str) -> dict:
        conv["status"] = "closed"
        return {"action": "end", "rationale": f"Customer reply, intent={intent.kind}. {why}"}

    t = (lambda en, hi: hi if lang == "hinglish" else en)
    hello = f", {name}" if name else ""
    k, msg = intent.kind, body.message or ""

    if k == "opt_out":
        if mem:
            mem.opted_out = True
        return end("Customer opted out; no further messages on the merchant's behalf.")
    if k in ("empty",):
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Empty customer message."}
    if k in ("auto_reply", "abuse", "negative"):
        return end("Customer declined or isn't engaging; closing without pushing.")
    if k == "defer":
        return {"action": "wait", "wait_seconds": 86400, "rationale": "Customer asked for time; no nudge today."}

    pick = None
    m = _SLOT_PICK.search(msg)
    if m:
        pick = int(next(g for g in m.groups() if g in ("1", "2"))) - 1
    else:
        for i, s in enumerate(shorts):
            if s and re.search(rf"\b{re.escape(s.lower())}", msg.lower()):
                pick = i

    if fam == "review" and k in ("commit", "other"):
        return send(t(f"Thank you{hello}! {biz} will send the review link here shortly.",
                      f"Shukriya{hello}! {biz} review link yahin jaldi bhej dega."),
                    "Customer agreed to review: thanked; the merchant sends the real link (none in context to invent).",
                    done=True)

    if fam == "appointment":
        when = slots[0] if slots else t("tomorrow", "kal")
        if pick == 1 or re.search(r"\b(reschedule|change|another|different|badal)\b", msg, re.I):
            return send(t(f"No problem{hello}. Tell us a day and time that suits you and we'll find the nearest open slot.",
                          f"Koi baat nahi{hello}. Apna convenient din aur time bataiye, hum nearest slot dhoondh denge."),
                        "Reschedule requested: ask for their time, don't guess availability.")
        if pick == 0 or k == "commit":
            return send(t(f"Thanks{hello}, you're confirmed for {when}. See you at {biz}!",
                          f"Shukriya{hello}, aapka appointment {when} ke liye confirm hai. {biz} mein milte hain!"),
                        "Appointment confirmed.", done=True)

    if pick is not None and pick < len(slots):
        return send(t(f"Done{hello}! You're booked for {slots[pick]} at {biz}. See you then.",
                      f"Ho gaya{hello}! Aapka slot {slots[pick]} ke liye book ho gaya hai, {biz} mein. Milte hain!"),
                    f"Customer picked slot {pick + 1}: booked and confirmed in one message.", done=True)
    if k == "commit":
        if len(slots) == 1:
            return send(t(f"Done{hello}! You're booked for {slots[0]} at {biz}. See you then.",
                          f"Ho gaya{hello}! Aapka slot {slots[0]} ke liye book ho gaya hai, {biz} mein. Milte hain!"),
                        "Single offered slot accepted.", done=True)
        if len(slots) >= 2:
            return send(t(f"Great{hello}! Reply 1 for {slots[0]} or 2 for {slots[1]} and it's booked.",
                          f"Badhiya{hello}! {slots[0]} ke liye 1, {slots[1]} ke liye 2 bhejiye, book ho jayega."),
                        "Yes without a slot: one short prompt to pick, nothing assumed.")
    if _TIME_WORDS.search(msg) or k == "commit":
        return send(t(f"Thanks{hello}, noted. The {biz} team will confirm your time shortly.",
                      f"Shukriya{hello}, note kar liya. {biz} team jald hi aapka time confirm karegi."),
                    "Customer proposed their own time: acknowledged; availability confirmed by the merchant, not assumed.")
    if k == "who":
        last = None
        if customer:
            from .customer import _fmt_date
            last = _fmt_date(N.get(customer, "relationship", "last_visit"))
        seen = t(f", where you last visited on {last}", f", jahan aap last {last} ko aaye the") if last else ""
        opts = t(" To book, reply 1 or 2.", " Book karne ke liye 1 ya 2 bhejiye.") if slots else ""
        return send(t(f"This is {biz}{seen}.{opts}", f"Yeh {biz} hai{seen}.{opts}"),
                    "Customer asked who is messaging: identified the merchant plainly.")
    if k in ("price", "question"):
        offer = fs.top_offer if fs else None
        price = t(f" The current offer is {offer}.", f" Abhi {offer} chal raha hai.") if offer and k == "price" else ""
        return send(t(f"Good question{hello}; the {biz} team will reply to it shortly.{price}",
                      f"Accha sawaal{hello}; {biz} team jald hi jawab degi.{price}"),
                    "Customer question: routed to the merchant's team rather than answered with guesses.")
    conv["other"] = conv.get("other", 0) + 1
    if conv["other"] >= 2:
        return {"action": "wait", "wait_seconds": 14400, "rationale": "Two unclear customer replies; pausing."}
    opts = t(f"reply 1 or 2 to pick a slot, or tell us a time that suits you", "slot ke liye 1 ya 2 bhejiye, ya apna time bataiye") if slots \
        else t("tell us a day and time that suits you", "apna convenient din aur time bataiye")
    return send(t(f"Thanks{hello}! To book, {opts}.", f"Shukriya{hello}! Book karne ke liye {opts}."),
                "Unclear reply: one short restatement of how to book.")
