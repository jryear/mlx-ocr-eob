#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict, Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def summarize_doc(eob_path: Path) -> Dict[str, Any]:
    eob = load_json(eob_path, {})
    cons = load_json(eob_path.with_suffix(".conservation.json"), {})
    choices = load_json(eob_path.with_suffix(".sem_map.choice.v1.json"), {"choices": []})

    claim = eob.get("claim") or {}
    payer = claim.get("payer_name") or "UNKNOWN"
    rows: List[Dict[str, Any]] = eob.get("services", [])
    money_keys = ("charged_amount","allowed_amount","insurance_paid","patient_responsibility")
    other_keys = ("procedure_code","service_date")

    rows_total = len(rows)
    rows_3 = 0
    empty_rows = 0
    missing_meta = 0
    locked_uncertain = 0
    for r in rows:
        money_count = sum(1 for k in money_keys if isinstance(r.get(k), str) and r.get(k).strip())
        other_count = sum(1 for k in other_keys if isinstance(r.get(k), str) and r.get(k).strip())
        if money_count >= 3:
            rows_3 += 1
        # empty row if no money and no other typed fields
        if money_count == 0 and other_count == 0:
            empty_rows += 1
        if not all(k in r for k in ("page_number","table_id","row_index")):
            missing_meta += 1
        if r.get("_semantic_uncertain") and r.get("_row_locked"):
            locked_uncertain += 1

    variant_counts = Counter()
    for c in (choices.get("choices") or []):
        v = c.get("extraction_variant") or c.get("chosen")
        if v:
            variant_counts[str(v)] += 1

    return {
        "payer": payer,
        "doc": eob_path.stem,
        "services": rows_total,
        "rows_3of4": rows_3,
        "rows_3_rate": (rows_3/rows_total) if rows_total else 0.0,
        "conservation_rate": float(cons.get("conservation_rate") or 0.0),
        "empty_rows": empty_rows,
        "missing_meta": missing_meta,
        "locked_uncertain": locked_uncertain,
        "variants": dict(variant_counts),
    }


def aggregate(docs: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["docs_processed"] = len(docs)
    out["services_total"] = sum(d.get("services", 0) for d in docs)
    rows_total = max(1, sum(d.get("services", 0) for d in docs))
    out["rows_3of4_rate"] = (sum(d.get("rows_3of4", 0) for d in docs) / rows_total)
    out["conservation_mean"] = (sum(float(d.get("conservation_rate") or 0.0) for d in docs) / max(1, len(docs)))
    out["empty_row_rate"] = (sum(d.get("empty_rows", 0) for d in docs) / rows_total)
    out["missing_metadata_rows"] = sum(d.get("missing_meta", 0) for d in docs)
    out["locked_uncertain_rows"] = sum(d.get("locked_uncertain", 0) for d in docs)
    var = Counter()
    for d in docs:
        var.update(d.get("variants", {}))
    out["extraction_variants"] = dict(var)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Holdout KPI report (payer-bucketed)")
    ap.add_argument("--dataset", required=True, help="datasets/<name> directory")
    ap.add_argument("--out-root", default=str(ROOT / "out/holdout"), help="Root of holdout outputs")
    ap.add_argument("--report", default=None, help="Path to write aggregate JSON")
    args = ap.parse_args()

    dataset = Path(args.dataset).name
    out_dir = Path(args.out_root) / dataset
    if not out_dir.exists():
        raise SystemExit(f"Output directory not found: {out_dir}")

    docs: List[Dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json")):
        # Skip sidecars
        if any(s in path.name for s in (".conservation.", ".sem_map.", ".doc_tables.", ".rowize.", ".header_synthesis.", ".coherence.", ".micro_")):
            continue
        docs.append(summarize_doc(path))

    # Overall aggregate
    overall = aggregate(docs)

    # Payer buckets
    by_payer: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for d in docs:
        by_payer[d["payer"]].append(d)
    buckets: Dict[str, Any] = {}
    for payer, dd in sorted(by_payer.items(), key=lambda kv: -len(kv[1])):
        buckets[payer] = aggregate(dd)
        buckets[payer]["docs"] = len(dd)

    report = {
        "dataset": dataset,
        "overall": overall,
        "payers": buckets,
        "docs": docs,
    }

    dest = Path(args.report) if args.report else (ROOT / f"SESSION/AuditLogs/holdout/{dataset}/aggregate.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, indent=2))
    print(json.dumps({
        "dataset": dataset,
        "docs_processed": overall.get("docs_processed"),
        "services_total": overall.get("services_total"),
        "rows_3of4_rate": overall.get("rows_3of4_rate"),
        "conservation_mean": overall.get("conservation_mean"),
        "empty_row_rate": overall.get("empty_row_rate"),
        "missing_metadata_rows": overall.get("missing_metadata_rows"),
        "locked_uncertain_rows": overall.get("locked_uncertain_rows"),
        "extraction_variants": overall.get("extraction_variants"),
        "report": os.fspath(dest)
    }, indent=2))


if __name__ == "__main__":
    main()

