"""Print the composed message for every trigger in a dataset dir.

    python demo.py                 # uses ./fixtures
    python demo.py path/to/dataset # the real dataset, same layout as judge_simulator
"""
import json
import sys
from pathlib import Path

from vera import dataset as DS
from vera.compose import compose_with_trace


def main():
    ds = DS.load(sys.argv[1] if len(sys.argv) > 1 else "fixtures")
    for t in ds.triggers.values():
        m = ds.merchant_for(t)
        msg, tr = compose_with_trace(ds.category_for(m), m, t, ds.customer_for(t))
        print(f"── {t['id']}  [{t.get('kind')}]")
        if msg:
            print(f"   {msg['body']}")
            print(f"   cta={msg['cta']}  template={msg['template_name']}  facts={tr.facts_used}")
            soft = tr.issues[-1]["soft"]
            if soft:
                print(f"   soft issues: {soft}")
        else:
            print(f"   (no send: {tr.skipped_reason}) {tr.issues or ''}")
        print()


if __name__ == "__main__":
    main()
