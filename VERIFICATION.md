# Verification record

Everything below was re-run on the final build (v1.1.0). Re-run it yourself with:

```
python -m pytest -q                                    # 196 automated tests
uvicorn bot:app --port 8080 &  python verify_requirements.py http://localhost:8080
```

## 1. Requirement-by-requirement live check (`verify_requirements.py`)

It covers 45 checks, each citing its brief section (T = testing brief, B = main brief), run against a live server over HTTP. Result: **45/45 when team details are set; 44/45 before that** (the remaining check is the `team.json` / env var step, which is yours).

| Area | Checks |
|---|---|
| T§2 endpoints | healthz shape; all 7 metadata fields; context 200 / idempotent / 409 exact body / 400 invalid_scope / 400 malformed |
| T§4 Phase 1 | contexts_loaded equals everything pushed |
| T§2.2, T§5, T§10, FAQ, B§5 | empty tick → `{"actions": []}`; all 11 action fields; ≤ 20 actions; no empty bodies; one action per (merchant, conversation); valid send_as; customer sends as merchant_on_behalf; template on first outbound; every number in every body found in the pushed contexts; single CTA; language = identity.languages; expired triggers not sent; suppression across ticks; conversation_id never reused |
| T§2.3, T§4 Phase 4 | valid send/wait/end shapes; no verbatim repeats; 2 qualifying turns then "ok lets do it" → action; auto-reply ×4 → ends by turn 2; abuse then GST → apology, decline, redirect; opt-out → end; defer → wait; unknown conversation still valid |
| T§4 Phase 3 | new digest version used in the next send; customer pushed mid-test + recall_due → customer message; consent scope respected; slot pick → booked |
| T§5, T§11 | slowest call 33 ms (limit 30 s); teardown wipes everything |
| B§7 | compose() contract and determinism; README ≤ 1 page; conversation_handlers.py; generate_submission.py |

## 2. Found by hand (probing the API and reading transcripts), then fixed and locked in by `tests/test_manual_findings.py`

1. Malformed context pushes returned FastAPI's 422, where the brief requires 400 `{accepted:false, reason, details}`; an empty context_id and a negative version were accepted.
2. `null` in `available_triggers`, `message` or `from_role` crashed validation (counted as malformed by the judge). These are now treated as empty.
3. Devanagari replies ("हाँ कर दो", "मैसेज मत भेजो") weren't understood.
4. Hinglish grammar error: "…draft **karna kar** sakti hoon". Fixed by conjugating properly.
5. **Phase 4 intent scenario went silent.** A second qualifying question got `wait`, because the only candidate reply was a verbatim repeat.
6. Confirming a research note said "queued for your Google profile". "Done" messages are now specific to each deliverable.
7. "Is this relevant for kids?" got an incoherent reply. It's now answered strictly from the digest item (the trial covered high-risk adults).
8. "Tell me more" got a restatement; it now delivers the content.
9. A customer asking "who is this?" got "the team will reply"; the bot now names the clinic and her last-visit date.
10. The FAQ says to match `identity.languages`; Hindi merchants were being switched to English mid-conversation.
11. An updated trigger version could regenerate an existing conversation_id (the brief says that's invalid).
12. **An abandoned thread blocked a merchant forever**, so Phase 3 context never reached them. A conversation now stops counting as in flight after 30 simulated minutes of silence.

## 3. Checked against the challenge web page (screenshots of every section)

| Page item | Where it's handled |
|---|---|
| Deterministic `compose(category, merchant, trigger, customer?)` returning message, CTA, send-as, suppression key, rationale | `bot.py` → `vera/compose.py`; determinism checked twice per pair by `generate_submission.py` |
| Four context layers, customer optional (relationship, consent, status, preference) | `vera/facts.py`, `vera/customer.py` |
| Scoring: decision quality, specificity, category fit, merchant fit (incl. prior conversations), engagement | `vera/decide.py` (one driving signal + stated reason, history-aware ranking), validator grounding, category voice/taboos |
| "Strong message" example: local search demand + real offer + single CTA | `search_demand` fact and count-based trend template (T19 in fixtures) |
| Levers: proof, urgency, curiosity, one yes/no action; one CTA, no fake claims | playbook levers; validator (single CTA, grounded numbers) |
| Judges inject digest items, metric shifts, triggers, customer contexts | versioned store; verifier T§4.3 checks |
| Dataset: seeds expanded by `generate_dataset.py` into 50/200/100 + 30 test pairs | `vera/dataset.py` reads either layout; the DEVELOPER.md workflow starts from the page's command |
| Submit one public URL; stateful, fast, grounded; one-page README covering approach, **model choice**, tradeoffs | DEPLOY.md; README.md (750 words, with a model-choice section) |
| Replays test replies, **objections**, auto-replies, intent handoffs | `vera/reply.py`: objection handlers + the three Phase 4 scenarios |
| "The exam is fresh scenarios"; surprise customer scopes | alias-tolerant accessors, generic-with-restraint for unknown kinds, review/promo customer families (consent-gated) |
| Rejected quickly: hallucinated facts, generic templates, unstable responses, broken endpoints | validator; one-signal decision; 45-check live verifier; 585-reply sweep with 0 silent fallbacks |

### Found by hand in this pass (fixed, with regression tests)

13. The page's gold-standard signal (local search demand) wasn't used at all.
14. Objections ("too expensive", "we have an agency", "never works", "no time") got generic restatements.
15. **Merchant "who is this?" silently returned `wait`.** The validator treated "I'm Vera" as a preamble even when it answered the question.
16. A trust question ("is this a scam?") went unanswered when it came after two other objections.
17. "Change the offer to ₹199" was misread as a curious-ask answer and blocked. Edit requests (price, length, language) are now applied, and numbers the merchant says count as grounded.
18. "In Hindi please" changed Vera's words but not the post itself.
19. "What is magicpin?", "email me", "didn't understand", "what's the benefit?" and "which offer?" all got the draft instead of an answer.
20. Unknown customer trigger kinds were dropped. Review-request and promotional families were added, each consent-gated; with no stated reason, the bot still refuses to guess.

A sweep of 13 triggers × 45 realistic replies (English, Hinglish, Devanagari, objections, curveballs) gives 585 replies with **0 silent validation fallbacks and 0 empty sends**. The 52 waits are exactly the deferrals, and the 52 ends are exactly the declines and stop requests.

## 4. What was not verifiable here

- **LLM judge scores.** `judge_simulator.py` needs your LLM API key.
- **The real dataset and the 30 canonical pairs.** Run `audit_dataset.py` and `generate_submission.py` on them.
- **Collapsed sections on the page** ("full testing flow", "technical constraints", "deployment notes", "package layout and case anchors") weren't expanded in the screenshots. Their content is assumed to match the testing brief.
- **`examples/case-studies.md`** (10 judge-scored cases) and `api-call-examples.md` from the challenge zip haven't been reviewed yet.
