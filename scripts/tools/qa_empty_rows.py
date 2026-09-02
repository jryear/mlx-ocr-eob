#!/usr/bin/env python3
"""
QA helper: Check empty row rate and metadata completeness across holdout outputs.

Usage:
  python3 scripts/tools/qa_empty_rows.py out/holdout
  python3 scripts/tools/qa_empty_rows.py out/eremit_doc_gated_v6
"""
import json
import sys
import os
import glob

root = sys.argv[1] if len(sys.argv) > 1 else "out/holdout"

# Data fields that constitute a "non-empty" row
data_fields = (
    "charged_amount",
    "allowed_amount",
    "insurance_paid",
    "patient_responsibility",
    "procedure_code",
    "service_date"
)

# Required metadata fields
meta_fields = ("page_number", "table_id", "row_index")

tot_rows = 0
empty = 0
meta_miss = 0

for f in glob.glob(os.path.join(root, "**", "*.json"), recursive=True):
    # Skip sidecar files
    if ".conservation." in f or ".validate." in f or ".choice." in f or ".synthesis." in f:
        continue

    try:
        doc = json.load(open(f))
    except Exception:
        continue

    rows = doc.get("services", [])
    for r in rows:
        tot_rows += 1

        # Check for data fields
        has_data = any(
            (isinstance(r.get(k), str) and r.get(k).strip()) or
            isinstance(r.get(k), (int, float))
            for k in data_fields
        )
        if not has_data:
            empty += 1

        # Check for metadata
        if not all(k in r for k in meta_fields):
            meta_miss += 1

result = {
    "rows": tot_rows,
    "empty_row_rate": (empty / tot_rows) if tot_rows else None,
    "missing_meta": meta_miss
}

print(json.dumps(result, indent=2))
