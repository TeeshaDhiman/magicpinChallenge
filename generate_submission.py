"""Generate submission.jsonl (brief §7.2): one line per canonical test pair.

    python generate_submission.py path/to/dataset
    python generate_submission.py path/to/dataset --pairs path/to/test_pairs.json --out submission.jsonl
    python generate_submission.py path/to/dataset --all     # every trigger, for self-review only

Each line: {"test_id", "body", "cta", "send_as", "suppression_key", "rationale"}.
Composes every pair twice to prove determinism, and reports per-call latency (limit: 30s).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from vera import dataset as DS
from vera.compose import compose

FIELDS = ("body", "cta", "send_as", "suppression_key", "rationale")


def _pair_ids(p: dict, i: int) -> tuple[str, str | None, str | None, str | None]:
    tid = str(p.get("test_id") or p.get("id") or f"T{i + 1:02d}")
    trg = p.get("trigger_id") or p.get("trigger")
    mer = p.get("merchant_id") or p.get("merchant")
    cus = p.get("customer_id") or p.get("customer")
    return tid, trg, mer, cus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--pairs", help="test-pairs file (auto-detected in the dataset if omitted)")
    ap.add_argument("--out", default="submission.jsonl")
    ap.add_argument("--all", action="store_true", help="compose every trigger (self-review, not the submission)")
    a = ap.parse_args()

    ds = DS.load(a.dataset)
    if a.pairs:
        raw = DS._read(Path(a.pairs))
        pairs = (raw.get("pairs") or raw.get("test_pairs") or raw.get("tests")) if isinstance(raw, dict) else raw
    elif a.all:
        pairs = [{"test_id": f"A{i + 1:03d}", "trigger_id": t} for i, t in enumerate(ds.triggers)]
    else:
        pairs = ds.test_pairs
        if pairs:
            print(f"Using test pairs from {ds.test_pairs_file}")
    if not pairs:
        print("No canonical test-pairs file found. Pass --pairs <file> (the 30 pairs from the challenge),\n"
              "or --all to compose every trigger for review. Not guessing the canonical set.")
        return 2

    print(f"Dataset: {len(ds.categories)} categories, {len(ds.merchants)} merchants, "
          f"{len(ds.customers)} customers, {len(ds.triggers)} triggers; {len(pairs)} pairs\n")
    lines, problems, slowest, fallbacks = [], [], 0.0, 0
    for i, p in enumerate(pairs):
        test_id, trg_id, mer_id, cus_id = _pair_ids(p, i)
        trigger = ds.triggers.get(str(trg_id)) if trg_id else None
        if trigger is None:
            problems.append(f"{test_id}: trigger {trg_id!r} not in dataset")
            continue
        merchant = ds.merchants.get(str(mer_id)) if mer_id else ds.merchant_for(trigger)
        if merchant is None:
            problems.append(f"{test_id}: merchant {mer_id!r} not in dataset")
            continue
        customer = ds.customers.get(str(cus_id)) if cus_id else ds.customer_for(trigger)
        category = ds.category_for(merchant)

        t0 = time.perf_counter()
        msg = compose(category, merchant, trigger, customer)
        dt = time.perf_counter() - t0
        slowest = max(slowest, dt)
        if msg != compose(category, merchant, trigger, customer):
            problems.append(f"{test_id}: NON-DETERMINISTIC output")
        if "fallback" in msg["rationale"]:
            fallbacks += 1
        lines.append({"test_id": test_id, **{k: msg[k] for k in FIELDS}})
        print(f"{test_id} [{trigger.get('kind')}] {msg['send_as']}: {msg['body'][:100]}"
              f"{'…' if len(msg['body']) > 100 else ''}")

    with open(a.out, "w", encoding="utf-8") as f:
        for ln in lines:
            f.write(json.dumps(ln, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(lines)} lines to {a.out}. Slowest compose: {slowest * 1000:.1f}ms (limit 30s). "
          f"Fallbacks: {fallbacks} (run audit_dataset.py to see which trigger fields were missing).")
    for pr in problems:
        print(f"  PROBLEM: {pr}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
