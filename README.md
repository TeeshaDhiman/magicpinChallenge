<p align="center">
  <h1 align="center">🤖 Vera Bot</h1>
  <p align="center">
    <strong>Intelligent WhatsApp Business Messaging Engine for magicpin</strong>
  </p>
  <p align="center">
    <a href="#-architecture"><img src="https://img.shields.io/badge/Architecture-Deterministic_Templates-blue?style=flat-square" alt="Architecture"></a>
    <a href="#-quick-start"><img src="https://img.shields.io/badge/Latency-~1ms-green?style=flat-square" alt="Latency"></a>
    <a href="#-test-suite"><img src="https://img.shields.io/badge/Tests-205_passed-brightgreen?style=flat-square" alt="Tests"></a>
    <a href="#-model-choice"><img src="https://img.shields.io/badge/LLM_at_Runtime-None-orange?style=flat-square" alt="No LLM"></a>
  </p>
</p>

---

> **Team Trinetra** · magicpin AI Challenge Submission  
> Built by **Teesha Dhiman** · [teesha14115@gmail.com](mailto:teesha14115@gmail.com)

---

## 📋 Table of Contents

- [Overview](#-overview)
- [Quick Start](#-quick-start)
- [Architecture](#-architecture)
- [Playbook Coverage](#-playbook-coverage)
- [Model Choice](#-model-choice)
- [Project Structure](#-project-structure)
- [Testing](#-test-suite)
- [Deployment](#-deployment)
- [Design Tradeoffs](#-design-tradeoffs)
- [What Additional Context Would Help](#-what-additional-context-would-have-helped)

---

## 🔍 Overview

Vera is a **deterministic, rules-and-templates messaging engine** that composes personalized WhatsApp messages for merchants and their customers on behalf of magicpin. It handles **20+ trigger types** across 5 business categories (dentists, salons, gyms, restaurants, pharmacies) — all without a single LLM call at runtime.

### ✨ Key Highlights

| Feature | Detail |
|---|---|
| ⚡ **~1ms latency** | No LLM calls — pure template composition |
| 🔒 **Zero hallucination** | Every number is validated against pushed context |
| 🎯 **Deterministic** | Same input → same output, every time |
| 🌐 **Bilingual** | English + Hinglish, auto-detected per merchant/customer |
| 📱 **WhatsApp-ready** | Every message is a valid `{{1}}, {{2}} {{3}}` template |
| 🛡️ **Consent-aware** | Strict opt-in/opt-out handling for customer messages |

---

## 🚀 Quick Start

### Prerequisites
- Python 3.10+
- pip

### Install & Run

```bash
# Clone the repository
git clone https://github.com/TeeshaDhiman/magicpinChallenge.git
cd magicpinChallenge

# Install dependencies
pip install -r requirements.txt

# Run tests
pytest

# Start the bot server
uvicorn bot:app --port 8080
```

### Generate Dataset & Submission

```bash
# Generate expanded dataset
python3 dataset/generate_dataset.py --seed-dir dataset --out expanded

# Audit the dataset
python audit_dataset.py expanded

# Generate submission file
python generate_submission.py expanded/
```

### Verify Deployment

```bash
python smoke_test.py http://localhost:8080
python verify_requirements.py http://localhost:8080
```

---

## 🏗️ Architecture

Every message is built in **four deterministic steps**:

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  1. FACTS    │───▶│  2. DECIDE   │───▶│  3. PLAYBOOK │───▶│  4. VALIDATE │
│              │    │              │    │              │    │              │
│ Extract &    │    │ Pick best    │    │ Compose the  │    │ Block if     │
│ normalize    │    │ signal for   │    │ bilingual    │    │ ungrounded   │
│ all context  │    │ this trigger │    │ message      │    │ or unsafe    │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

### Step 1 — Facts Sheet (`vera/facts.py`)
The four contexts (category, merchant, trigger, customer) are reduced to plain-language, merchant-safe facts: the digest item resolved by ID, live offers, CTR against peer average, days since last Google post, lapsed customers, and more. Every number that may appear in a message is registered for grounding.

### Step 2 — Decision Layer (`vera/decide.py`)
Each trigger family has an explicit list of merchant facts that strengthen it, with a reason. For example, a calls dip is paired with below-peer click-through because that's the likeliest cause. Only **one** supporting fact is used per message. The tick policy ranks competing triggers by urgency → merchant-state fit → prior conversation behavior.

### Step 3 — Playbook Templates (`vera/playbooks.py`)
20+ template families compose messages in both English and Hinglish. Service-plus-price offers are preferred over discounts. One CTA per message. Every message is also a WhatsApp template whose parameters render exactly to the body.

### Step 4 — Validator (`vera/validate.py`)
A message is blocked if it contains any number not in the contexts, a category taboo word, internal jargon, preamble, more than one CTA, promotional shouting on clinical categories, or a verbatim repeat.

---

## 📚 Playbook Coverage

### Merchant-Facing (20 families)

| Family | Trigger Kinds | Description |
|---|---|---|
| 🔬 **Research** | `research_digest`, `research_digest_release` | Shares new research with source citation |
| ⚖️ **Regulation** | `regulation_change`, `compliance_update` | Compliance alerts with effective dates |
| 📈 **Trend** | `category_trend_movement`, `trend_signal` | Local search trend data with YoY deltas |
| 📉 **Perf Dip** | `perf_dip`, `seasonal_perf_dip` | Declining metrics + actionable fix |
| 🚀 **Perf Spike** | `perf_spike` | Capitalize on positive momentum |
| 🏆 **Milestone** | `milestone_reached` | Celebrate achievements + thank-you post |
| 🎉 **Festival** | `festival_upcoming`, `festival` | Seasonal event promotions |
| 🌡️ **Weather** | `weather_heatwave`, `weather_alert` | Weather-triggered Google posts |
| 📰 **Local News** | `local_news_event` | Local events affecting business |
| 🏪 **Competitor** | `competitor_opened` | New competitor alerts with comparison |
| ⭐ **Review Theme** | `review_theme_emerged` | Review sentiment analysis + responses |
| 💬 **Curious Ask** | `dormant_with_vera`, `curious_ask_due` | Re-engagement via merchant questions |
| 🔄 **Renewal** | `renewal_due` | Subscription/plan renewal reminders |
| 💊 **Supply Alert** | `supply_alert` | Pharmacy recall/supply chain alerts |
| 🎯 **Winback** | `winback_eligible` | Merchant re-engagement after churn |
| 🏏 **Event Promo** | `ipl_match_today` | Event-based promotions |
| 📋 **Planning** | `active_planning_intent` | Follow-up on merchant's stated plans |
| 🌸 **Seasonal** | `category_seasonal` | Seasonal category trends |
| ✅ **GBP** | `gbp_unverified` | Google Business Profile verification |
| 🎓 **CDE** | `cde_opportunity` | Continuing education opportunities |

### Customer-Facing (5 families)

| Family | Description |
|---|---|
| 🔔 **Recall** | Recall reminders, trial follow-ups, refill reminders |
| 🤝 **Winback** | Lapsed customer re-engagement |
| 📅 **Appointment** | Appointment reminders with slot choices |
| ⭐ **Review** | Post-visit review requests |
| 🎁 **Promo** | Birthdays, offers, new services, festivals |

---

## 🧠 Model Choice

**No LLM at runtime.** Here's why:

The challenge scores *"decisions, not just writing style"*, requires **deterministic output**, and rejects *"hallucinated facts"* and *"unstable responses"*.

A rules-and-templates engine delivers:
- ✅ **Same output for same input**, every time
- ✅ **~1ms response time** (vs 1-3s for LLM)
- ✅ **Zero hallucination risk** — validator refuses any number not in context
- ✅ **Fully auditable** — every decision has a traceable rationale
- ✅ **No API costs** or rate limits at runtime

The facts sheet and validator are designed so that an LLM composer can later slot in behind the same guardrails.

---

## 📁 Project Structure

```
vera-bot/
├── bot.py                    # FastAPI server (uvicorn entrypoint)
├── vera/
│   ├── compose.py            # Main composition pipeline
│   ├── facts.py              # Context → FactSheet extraction
│   ├── decide.py             # Signal selection & trigger ranking
│   ├── playbooks.py          # 20+ bilingual message templates
│   ├── customer.py           # Customer-facing message composition
│   ├── reply.py              # Conversation reply handling
│   ├── validate.py           # Grounding & safety validator
│   ├── normalize.py          # Defensive field access utilities
│   ├── dataset.py            # Dataset loader
│   ├── store.py              # In-memory conversation state
│   └── engine.py             # Tick engine (trigger processing)
├── conversation_handlers.py  # Reply intent classification
├── tests/
│   ├── test_flow.py          # End-to-end flow tests
│   ├── test_reply.py         # Reply handling tests
│   ├── test_customer.py      # Customer message tests
│   └── test_manual_findings.py
├── fixtures/                 # Seed data for testing
├── team.json                 # Team metadata
├── generate_submission.py    # Submission JSONL generator
├── audit_dataset.py          # Dataset coverage auditor
├── smoke_test.py             # Live endpoint smoke test
├── verify_requirements.py    # Requirements verification
├── Dockerfile                # Container deployment
├── render.yaml               # Render deployment config
├── Procfile                  # Heroku/Render process config
└── requirements.txt          # Python dependencies
```

---

## 🧪 Test Suite

```bash
pytest                     # Run all 205 tests
pytest tests/ -v           # Verbose output
pytest tests/ -x           # Stop on first failure
```

The test suite covers:
- ✅ All 20+ merchant trigger families
- ✅ Customer recall, appointment, review, promo, winback flows
- ✅ Auto-reply detection and escalation
- ✅ Hostile message handling
- ✅ Consent enforcement
- ✅ Grounding validation (no invented numbers)
- ✅ Reply intent classification (objections, edits, commitments)
- ✅ Bilingual output (English + Hinglish)
- ✅ WhatsApp template parameter extraction

---

## 🚢 Deployment

### Render (Recommended)

1. Connect this repo on [Render](https://render.com)
2. It auto-detects from `render.yaml`
3. Add environment variable: `VERA_IGNORE_EXPIRY=1`
4. Deploy!

### Docker

```bash
docker build -t vera-bot .
docker run -p 8080:8080 -e VERA_IGNORE_EXPIRY=1 vera-bot
```

### Manual

```bash
pip install -r requirements.txt
export VERA_IGNORE_EXPIRY=1  # or `set VERA_IGNORE_EXPIRY=1` on Windows
uvicorn bot:app --host 0.0.0.0 --port 8080
```

See [`DEPLOY.md`](DEPLOY.md) for detailed deployment instructions.

---

## ⚖️ Design Tradeoffs

| Decision | Benefit | Cost |
|---|---|---|
| **Templates over LLM** | Determinism, ~1ms, zero hallucination | Less phrasing variety |
| **Restraint over coverage** | Fewer, higher-quality messages | May send fewer messages than a spray-everything bot |
| **Strict consent** | Full regulatory compliance | Some customer messages are skipped |
| **One signal per message** | Clear, focused communication | Doesn't dump every stat available |
| **Service+price over discount** | Higher-value offers to merchants | May miss some discount-driven opportunities |

---

## 💡 What Additional Context Would Have Helped

- 🔍 **Search-keyword field** on merchants — the challenge's gold example ("190 people in your locality are searching…") relies on this
- 📋 **Trigger payload schemas** per kind — field names are currently handled with broad aliases
- 📅 **Merchant calendar & open slots** in a documented field for reliable booking flows
- 💬 **Engagement labels** on `conversation_history` for better trigger ranking
- 📊 **Competitor metrics** (rating, review count) for in-thread comparisons

---

<p align="center">
  <sub>Built with ❤️ by Team Trinetra for the magicpin AI Challenge</sub>
</p>
