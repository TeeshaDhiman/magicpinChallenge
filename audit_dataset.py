"""Audit the real dataset against the bot, before submitting.

    python audit_dataset.py path/to/dataset

Reports, per trigger kind:
  * which kinds route to the generic playbook (no dedicated template)
  * which triggers fall back or are skipped, and why
  * payload fields the templates never read  -> likely an alias to add in vera/playbooks.py
Plus context gaps (merchants without performance/offers/languages, customers without
consent, digest ids referenced by triggers but missing from the category).
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict

from vera import customer as C
from vera import dataset as DS
from vera import facts as F
from vera import playbooks as P
from vera.compose import compose_with_trace

IGNORE_KEYS = {"merchant_id", "customer_id", "category", "trigger_id"}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ds = DS.load(sys.argv[1])
    print(f"Loaded {len(ds.categories)} categories, {len(ds.merchants)} merchants, {len(ds.customers)} customers, "
          f"{len(ds.triggers)} triggers, {len(ds.test_pairs)} test pairs"
          f"{f' ({ds.test_pairs_file.name})' if ds.test_pairs_file else ' (no test-pairs file found)'}\n")

    kinds, outcomes = Counter(), defaultdict(Counter)
    unread: dict[str, Counter] = defaultdict(Counter)
    generic_kinds, missing_digest = set(), []
    for tid, t in ds.triggers.items():
        kind = str(t.get("kind"))
        kinds[kind] += 1
        m = ds.merchant_for(t)
        if not m:
            outcomes[kind]["no merchant in dataset"] += 1
            continue
        cat = ds.category_for(m)
        cust = ds.customer_for(t)
        msg, tr = compose_with_trace(cat, m, t, cust)
        outcomes[kind]["sent" if msg else f"skip: {tr.skipped_reason.split(':')[0]}"] += 1

        is_customer = t.get("scope") == "customer" or cust
        if is_customer:
            if cust:
                _, fs, _ = C.compose(cat, m, t, cust)
            else:
                continue
        else:
            if P.route(kind) == "generic":
                generic_kinds.add(kind)
            fs = F.build(cat, m, t)
            P.draft(fs, 0)
        for k in set(fs.payload) - fs.payload.read - IGNORE_KEYS:
            unread[kind][k] += 1
        ref = (t.get("payload") or {}).get("top_item_id")
        if ref and not fs.digest_item:
            missing_digest.append(f"{tid} -> {ref}")

    print("TRIGGER KINDS")
    for kind, n in kinds.most_common():
        fam = C.CUSTOMER_KINDS.get(kind) and f"customer/{C.CUSTOMER_KINDS[kind]}" or P.route(kind)
        res = ", ".join(f"{v} {k}" for k, v in outcomes[kind].most_common())
        flag = "  <- no dedicated template" if kind in generic_kinds else ""
        print(f"  {kind:34s} x{n:<3d} [{fam}] {res}{flag}")
        if unread[kind]:
            print(f"      unread payload fields: {', '.join(f'{k}({v})' for k, v in unread[kind].most_common())}")

    print("\nCONTEXT GAPS")
    gaps = Counter()
    for m in ds.merchants.values():
        if not m.get("performance"):
            gaps["merchants without performance"] += 1
        if not [o for o in m.get("offers") or [] if str(o.get("status", "active")) == "active"]:
            gaps["merchants without an active offer (catalog fallback used)"] += 1
        if not (m.get("identity") or {}).get("languages"):
            gaps["merchants without identity.languages (English assumed)"] += 1
        if m.get("category_slug") not in ds.categories:
            gaps[f"merchants whose category is missing ({m.get('category_slug')})"] += 1
    for c in ds.customers.values():
        if not isinstance(c.get("consent"), dict):
            gaps["customers without a consent record"] += 1
    for slug, cat in ds.categories.items():
        for key in ("voice", "peer_stats", "offer_catalog", "digest"):
            if not cat.get(key):
                gaps[f"category {slug} missing {key}"] += 1
    for g, n in gaps.most_common():
        print(f"  {n:4d}  {g}")
    for md in missing_digest:
        print(f"  digest item referenced but not found: {md}")
    if not gaps and not missing_digest:
        print("  none")
    print("\nNext: for any 'unread payload fields' that carry the trigger's facts, add the key to the\n"
          "matching N.first(...) alias list in vera/playbooks.py (or vera/customer.py), then re-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
