"""Load the challenge dataset in either layout.

Brief layout (§6):       categories/*.json, merchants/*.json, customers/*.json, triggers/*.json
Simulator layout:        categories/*.json, merchants_seed.json, customers_seed.json, triggers_seed.json

Also finds the canonical test-pairs file if one ships with the dataset.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Dataset:
    root: Path
    categories: dict[str, dict] = field(default_factory=dict)
    merchants: dict[str, dict] = field(default_factory=dict)
    customers: dict[str, dict] = field(default_factory=dict)
    triggers: dict[str, dict] = field(default_factory=dict)
    test_pairs: list[dict] = field(default_factory=list)
    test_pairs_file: Path | None = None

    def merchant_for(self, trigger: dict) -> dict | None:
        return self.merchants.get(trigger_merchant_id(trigger))

    def customer_for(self, trigger: dict) -> dict | None:
        return self.customers.get(trigger_customer_id(trigger))

    def category_for(self, merchant: dict | None) -> dict:
        return self.categories.get((merchant or {}).get("category_slug"), {}) if merchant else {}


def trigger_merchant_id(t: dict) -> str | None:
    return t.get("merchant_id") or (t.get("payload") or {}).get("merchant_id")


def trigger_customer_id(t: dict) -> str | None:
    return t.get("customer_id") or (t.get("payload") or {}).get("customer_id")


def _read(path: Path) -> Any:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def _records(root: Path, name: str, id_key: str) -> dict[str, dict]:
    """Accept dir-of-files, <name>_seed.json, or <name>.json, wrapped or bare lists."""
    out: dict[str, dict] = {}
    d = root / name
    items: list = []
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            obj = _read(f)
            if isinstance(obj, list):
                items.extend(obj)
            elif isinstance(obj, dict) and name in obj and isinstance(obj[name], list):
                items.extend(obj[name])
            elif isinstance(obj, dict):
                obj.setdefault(id_key, f.stem)
                items.append(obj)
    for fname in (f"{name}_seed.json", f"{name}.json"):
        f = root / fname
        if f.exists():
            obj = _read(f)
            items.extend(obj.get(name, []) if isinstance(obj, dict) else obj)
    for it in items:
        if isinstance(it, dict) and it.get(id_key):
            out[str(it[id_key])] = it
    return out


def _find_test_pairs(root: Path) -> tuple[list[dict], Path | None]:
    pats = ("*test*pair*.json", "*test*pair*.jsonl", "*test_set*.json", "*test_set*.jsonl",
            "*submission*test*.json", "*canonical*.json", "*canonical*.jsonl")
    for base in (root, root.parent):
        for pat in pats:
            for f in sorted(base.glob(pat)):
                obj = _read(f)
                if isinstance(obj, dict):
                    obj = obj.get("pairs") or obj.get("test_pairs") or obj.get("tests") or []
                if isinstance(obj, list) and obj:
                    return obj, f
    return [], None


def load(root: str | Path) -> Dataset:
    root = Path(root)
    ds = Dataset(root=root)
    cat_dir = root / "categories"
    if cat_dir.is_dir():
        for f in sorted(cat_dir.glob("*.json")):
            c = _read(f)
            ds.categories[str(c.get("slug") or f.stem)] = c
    ds.merchants = _records(root, "merchants", "merchant_id")
    ds.customers = _records(root, "customers", "customer_id")
    ds.triggers = _records(root, "triggers", "id")
    ds.test_pairs, ds.test_pairs_file = _find_test_pairs(root)
    return ds
