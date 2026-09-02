# Quick Verification Checklist

Copy-paste these commands in order on your Vision-enabled Mac.

---

## ☑️ Step 1: Smoke Test Vision

```bash
cd /Users/jryear/Desktop/NativeOCR

vision-ocr-cli/.build/release/vision-ocr-cli \
  eobs/tests/DD_3.pdf \
  --dpi 150 --mode document --lang en-US \
  --ndjson /tmp/vision_test.jsonl \
  --log-run /tmp/vision_test_run.json

# Verify (should show ~20-40KB)
ls -lh /tmp/vision_test.jsonl
```

**If fails:** STOP - hardware issue

---

## ☑️ Step 2: Batch Capture (37 PDFs)

```bash
python3 /tmp/batch_capture.py

# Verify count
ls -1 bench/holdout/EOB_ZIP_2025Q3/*.jsonl | wc -l
# Expected: 37

# Check for empty files (should show nothing)
find bench/holdout/EOB_ZIP_2025Q3 -name "*.jsonl" -size 0
```

---

## ☑️ Step 3: Run Holdout Analysis

```bash
python3 scripts/holdout_shadow.py \
  --dataset datasets/EOB_ZIP_2025Q3 \
  --mode document \
  --doc-validate \
  --doc-fallback-rowize \
  --rowize-strict \
  --rowize-tokens \
  --semantic-map \
  --bootstrap-priors \
  --multi-pass 3 \
  --micro-ocr
```

**Wait:** 5-15 minutes

---

## ☑️ Step 4: Check Top-Line KPIs

```bash
jq '{docs, services_total, conservation_mean, rows_3of4_rate}' \
  SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

**Expected:**
- docs: 37
- services_total: 800-1200
- conservation_mean: 0.30-0.65

---

## ☑️ Step 5: Run QA Checks

```bash
# Empty row check
python3 scripts/tools/qa_empty_rows.py out/holdout

# Expected: {"rows": N, "empty_row_rate": 0.0, "missing_meta": 0}
```

---

## ☑️ Step 6: Verify Extraction Variants

```bash
find out/holdout -name "*.sem_map.choice.v1.json" -print0 | \
  xargs -0 jq -r '.choices[] | [.extraction_variant,.semantic_uncertain] | @tsv' | \
  head -10
```

**Expected:** Mix of "mapper" and "him", no null values

---

## ☑️ Step 7: Check for Locked Uncertain Rows (Should be 0)

```bash
find out/holdout -name "*.json" ! -name "*.*.*" -print0 | \
  xargs -0 python3 -c "
import json, sys
for f in sys.argv[1:]:
    doc = json.load(open(f))
    bad = sum(1 for s in doc.get('services',[]) if s.get('_row_locked') and s.get('_semantic_uncertain'))
    if bad:
        print(f'{f}: {bad} BAD LOCKS')
" --
```

**Expected:** (no output)

---

## Pass Criteria

- [x] 37 NDJSONs captured (no 0-byte files)
- [x] Empty row rate = 0.0
- [x] Missing metadata = 0
- [x] Conservation mean >= 0.30
- [x] No locked uncertain rows
- [x] All extraction_variant present

---

## One-Liner (After Manual Review)

```bash
cd /Users/jryear/Desktop/NativeOCR && \
python3 /tmp/batch_capture.py && \
python3 scripts/holdout_shadow.py --dataset datasets/EOB_ZIP_2025Q3 --mode document \
  --doc-validate --doc-fallback-rowize --rowize-strict --rowize-tokens \
  --semantic-map --bootstrap-priors --multi-pass 3 --micro-ocr && \
python3 scripts/tools/qa_empty_rows.py out/holdout && \
jq '{docs, services, conservation: .conservation_mean}' SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

---

**Total Time:** ~20-30 minutes
**Critical Outputs:**
- `SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json`
- `out/holdout/<sha>.json` (37 files)
- QA results from qa_empty_rows.py
