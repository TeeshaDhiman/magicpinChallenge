"""Post-composition checks. Deterministic, cheap, run on every outgoing body.

Hard issues block the send (the caller tries another variant or drops it).
Soft issues are reported in the trace but don't block.
Built now for the templates; the same checks will gate LLM output later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .facts import FactSheet, canon_num

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_JARGON = re.compile(r"\b[a-z]+_[a-z0-9_]+\b")
_REPLY_OPT = re.compile(r"\b[Rr]eply\s+([A-Z0-9]+)\b")
_CAPS = re.compile(r"\b[A-Z]{4,}\b")
_CAPS_OK = {"YES", "STOP", "JIDA", "WHATSAPP", "IPL"}
_PREAMBLES = ("hope you", "hope this", "i am reaching out", "i'm reaching out", "i'm vera",
              "this is vera", "vera here", "i am vera", "greetings")
# Small numbers used in fixed template phrasing ("2 minutes", "1-page", "6+ months").
_PHRASE_NUMBERS = {"1", "2", "6"}
CLINICAL = {"dentists", "pharmacies"}
_PROMO_WORDS = ("amazing", "hurry", "limited time", "best deal", "don't miss", "unbelievable")
SOFT_LEN = 700


@dataclass
class Result:
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard


def check(body: str, cta: str, fs: FactSheet, already_sent: set[str] | frozenset = frozenset(),
          allow_intro: bool = False) -> Result:
    """allow_intro: the merchant asked who is writing, so "I'm Vera" is the answer, not a preamble."""
    r = Result()
    text = (body or "").strip()
    if not text:
        r.hard.append("empty_body")
        return r
    low = text.lower()

    # 1. grounding: every number must come from the contexts (or fixed phrasing)
    for m in _NUM.findall(text):
        n = canon_num(m)
        if n not in fs.allowed_numbers and n not in _PHRASE_NUMBERS:
            r.hard.append(f"ungrounded_number:{m}")

    # 2. category taboos
    for t in fs.taboos:
        if re.search(rf"\b{re.escape(t.lower())}\b", low):
            r.hard.append(f"taboo:{t}")

    # 3. internal jargon leaking (snake_case identifiers)
    for j in _JARGON.findall(text):
        r.hard.append(f"jargon:{j}")

    # 4. preamble / re-introduction
    for p in _PREAMBLES:
        if allow_intro and "vera" in p:
            continue
        if p in low:
            r.hard.append(f"preamble:{p}")

    # 5. single primary CTA: YES (+ optional STOP) is one binary choice; anything else is multi-choice
    opts = {o.upper() for o in _REPLY_OPT.findall(text)} - {"YES", "STOP"}
    if cta == "multi_choice_slot":   # numbered slot choice is allowed for booking flows
        opts = {o for o in opts if not o.isdigit()}
    if opts:
        r.hard.append(f"multi_cta:{sorted(opts)}")
    if text.count("?") > 2:
        r.soft.append("many_questions")

    # 6. CTA should land in the last sentence
    if cta != "none":
        last = re.split(r"(?<=[.!?])\s+", text)[-1].lower()
        if "?" not in last and "reply" not in last and "bhej" not in last:
            r.soft.append("cta_not_last")

    # 7. promotional tone for clinical categories
    if fs.slug in CLINICAL:
        shouty = [w for w in _CAPS.findall(text)
                  if w not in _CAPS_OK and w.lower() not in fs.context_text]
        shouty += [w for w in _PROMO_WORDS if re.search(rf"\b{w}\b", low)]
        if shouty or "!!" in text:
            r.hard.append(f"promo_tone:{shouty}")

    # 8. anti-repetition
    if text in already_sent:
        r.hard.append("verbatim_repeat")

    if len(text) > SOFT_LEN:
        r.soft.append(f"long:{len(text)}")
    return r
