# Vera bot — developer guide

Deterministic, grounded merchant-engagement bot for the magicpin AI challenge. Proactive path: trigger + merchant + category → one message. Reactive path: rule-based reply engine. No LLM, so it's fully deterministic and fast.

Deploying: see **DEPLOY.md**, then verify with `python smoke_test.py https://your-url`.

```
pip install -r requirements-dev.txt
uvicorn bot:app --port 8080                          # real harness
VERA_IGNORE_EXPIRY=1 uvicorn bot:app --port 8080     # local judge_simulator runs (see below)
python demo.py [dataset_dir]                         # print a message for every trigger
python -m pytest -q                                  # 196 tests
```

## Pipeline

| Module | Job |
|---|---|
| `vera/normalize.py` | Defensive reads. Absorbs brief-vs-simulator drift (`taboos`/`vocab_taboo`, `owner_first_name`), salutation (`Dr.` for dentists, `ji` for Hinglish), language mode, Indian number format. |
| `vera/facts.py` | Builds the **FactSheet**: resolved digest item, live offers, catalog offers, and merchant-state facts pre-phrased in English and Hinglish (CTR vs peer, days since last post, lapsed count…). Also collects every number that is legitimately groundable. |
| `vera/playbooks.py` | Routes `trigger.kind` → 12 families (research, regulation, trend, perf dip/spike, milestone, festival, weather, local news, competitor, review theme, curious ask, generic). A template returns `None` when the trigger lacks what it needs; the bot doesn't pad. |
| `vera/validate.py` | Hard blocks: ungrounded numbers, taboos, snake_case jargon, preambles, multi-choice CTAs, promo shouting on clinical categories, verbatim repeats. |
| `vera/decide.py` | The decision step: picks the ONE merchant fact that strengthens each trigger family (with the reason, which goes into the rationale), reads `conversation_history` (3 unanswered → stop non-urgent nudges, ignored topics demoted, replied topics promoted, already-sent topics skipped), and scores competing triggers. |
| `vera/compose.py` | Pure `compose(category, merchant, trigger, customer)` — the §7 contract. Tries phrasing variants until the validator passes. |
| `vera/engine.py` | Tick policy: drop expired / suppressed / opted-out / in-flight (unless urgency ≥ 4), one action per merchant, highest urgency first. |
| `vera/reply.py` | `/v1/reply`: classifies each merchant turn (opt-out, auto-reply incl. verbatim repeats across conversations, abuse, off-topic, decline, defer, commitment, price, question), detects language per turn, and runs pitch → draft → done. A yes delivers the actual draft, never another qualifying question. Every reply body passes the same validator. |
| `vera/customer.py` | Customer-facing composer (`send_as: merchant_on_behalf`): consent-scope check, last-visit date and service history, open slots ranked by the customer's stated preference, numbered slot choice. |
| `vera/dataset.py` | Loads the brief's per-file layout or the simulator's `*_seed.json` layout; finds the test-pairs file. |
| `vera/store.py` | Versioned contexts (equal version = accepted no-op, lower = 409), suppression ledger, per-merchant memory that outlives conversations. |

## Things found in `judge_simulator.py` that this handles

- Re-posting the same version is accepted, so the simulator can be re-run against a live bot.
- The simulator's tick uses the **wall clock**, while dataset triggers carry fixed 2026 expiries. Locally everything looks expired → set `VERA_IGNORE_EXPIRY=1`. Never set it for the real harness.
- The rubric penalises internal jargon; signals never reach the body raw.

## Workflow on the real dataset

```
python3 dataset/generate_dataset.py --seed-dir dataset --out expanded   # from the challenge zip
python audit_dataset.py expanded          # unread fields, kinds without templates, context gaps
python generate_submission.py expanded    # submission.jsonl for the 30 canonical pairs
python demo.py expanded                   # read every message yourself
```

## Tools

| Script | Use |
|---|---|
| `generate_submission.py DATASET [--pairs FILE]` | Writes `submission.jsonl` (§7.2). Composes each pair twice to prove determinism. |
| `audit_dataset.py DATASET` | Run first on the real dataset: lists unread payload fields (aliases to add), kinds without a dedicated template, skip reasons, context gaps. |
| `demo.py [DATASET]` | Prints every composed message with the facts used. |
| `smoke_test.py URL` | Quick check of a deployed bot (see DEPLOY.md). |
| `verify_requirements.py URL` | Full requirement-by-requirement check with brief section references (see VERIFICATION.md). |
| `conversation_handlers.py` | Optional §7.4 `respond(state, message)`, same engine as `/v1/reply`. |

Team metadata: env vars `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, `SUBMITTED_AT`, or fill in `team.json`.
