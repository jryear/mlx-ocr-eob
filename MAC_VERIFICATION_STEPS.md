# Mac-Native Vision Verification Steps

**Prerequisites:** Mac with Vision framework support, Swift CLI built

---

## Step 1: Verify Vision Hardware Support

Test that your Mac can create CVPixelBuffers (the previous failure point):

```bash
# Quick smoke test with a single PDF
vision-ocr-cli/.build/release/vision-ocr-cli \
  eobs/tests/DD_3.pdf \
  --dpi 150 \
  --mode document \
  --lang en-US \
  --ndjson /tmp/vision_test.jsonl \
  --log-run /tmp/vision_test_run.json
```

**Expected:** Should complete without CVPixelBuffer errors.

**Check output:**
```bash
# Should show non-zero file
ls -lh /tmp/vision_test.jsonl

# Should contain Vision table data
head -5 /tmp/vision_test.jsonl
```

**If this fails:** Your Mac also lacks Vision support. STOP here.

---

## Step 2: Run Batch Capture (37 PDFs)

```bash
cd /Users/jryear/Desktop/NativeOCR

# Run the batch capture script
python3 /tmp/batch_capture.py
```

**Expected Output:**
```
[1/38] Capturing: Delta taylor.pdf -> c4e411c7...
  SUCCESS: 22.5KB
[2/38] Capturing: DMBA_2.pdf -> <sha>...
  SUCCESS: <size>KB
...
[37/38] SKIP: <duplicate> (already exists)
[38/38] SKIP: <duplicate> (already exists)

Capture Summary:
Total NDJSONs: 37/38
```

**Timing:** Expect ~1-3 minutes total for all captures.

---

## Step 3: Validate Captured NDJSONs

```bash
# Count files
ls -1 bench/holdout/EOB_ZIP_2025Q3/*.jsonl | wc -l
# Expected: 37

# Check file sizes (none should be 0 bytes)
find bench/holdout/EOB_ZIP_2025Q3 -name "*.jsonl" -size 0
# Expected: (no output - no empty files)

# Verify NDJSON format (should show Vision tables)
jq -s 'length' bench/holdout/EOB_ZIP_2025Q3/c4e411c7c8944924a6ba8caf640344aed3923f9846a6f536641c9c653ed8422f.jsonl
# Expected: 1 (one page processed)

jq -s '.[0] | .tables | length' bench/holdout/EOB_ZIP_2025Q3/c4e411c7c8944924a6ba8caf640344aed3923f9846a6f536641c9c653ed8422f.jsonl
# Expected: 1 or more (Vision detected tables)
```

**If any files are 0 bytes:** Re-run batch capture or investigate specific PDF failures.

---

## Step 4: Run Holdout Analysis

```bash
cd /Users/jryear/Desktop/NativeOCR

# Execute full holdout pipeline
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

**Expected Output:**
```
Processing: Delta taylor.pdf
  Doc tables: 0 accepted, 1 rejected
  Rowize fallback: 3 tables extracted
  Services: 12
  Conservation: 0.25
...
[37 documents processed]

Report written to: SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

**Timing:** Expect ~5-15 minutes for 37 documents (depends on page count and table complexity).

**Watch for:**
- No crashes or timeout errors
- Each doc shows "Services: N" (non-zero)
- Conservation rates vary by doc (expected)

---

## Step 5: Check Top-Line KPIs

```bash
# View summary report
jq '{
  docs: .count,
  total_services: .services_total,
  conservation_mean: .conservation_mean,
  rows_3of4_rate: .rows_3of4_rate
}' SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

**Expected Output:**
```json
{
  "docs": 37,
  "total_services": 800-1200,
  "conservation_mean": 0.30-0.65,
  "rows_3of4_rate": 0.60-0.85
}
```

**Target:** Conservation median in 0.50-0.65 range (payer-agnostic).

**If conservation < 0.30:** Header-poor documents dominate; this is informative (not a failure).

---

## Step 6: Run QA Checks

### 6A: Empty Row Rate Check

```bash
python3 scripts/tools/qa_empty_rows.py out/holdout
```

**Expected Output:**
```json
{
  "rows": 800-1200,
  "empty_row_rate": 0.0,
  "missing_meta": 0
}
```

**Pass Criteria:**
- `empty_row_rate = 0.0` ✅
- `missing_meta = 0` ✅

**Warn if:** `empty_row_rate > 0.05`
**Fail if:** `empty_row_rate > 0.10`

### 6B: Extraction Variant Check

```bash
# Sample extraction decisions
find out/holdout -name "*.sem_map.choice.v1.json" -print0 | \
  xargs -0 jq -r '.choices[] | [.table_id,.extraction_variant,.semantic_uncertain] | @tsv' | \
  head -20
```

**Expected Output:**
```
rowize_p0	him	true
rowize_p1	him	true
rowize_p2	mapper	null
vision_p3	him	true
...
```

**Check:**
- `extraction_variant` present on all rows (never null/missing)
- Mix of "mapper" and "him" variants
- `semantic_uncertain=true` dominates (expected for header-poor docs)

### 6C: Service Header Validation

```bash
# Check doc table gating decisions
find out/holdout -name "*.doc_tables.validate.v1.json" -print0 | \
  xargs -0 jq -s '[.[] | .tables[] | {page_index, accepted, service_header, reason}] | .[0:10]'
```

**Expected Output:**
```json
[
  {
    "page_index": 0,
    "accepted": false,
    "service_header": false,
    "reason": "failed_gates"
  },
  {
    "page_index": 1,
    "accepted": true,
    "service_header": true
  },
  ...
]
```

**Check:**
- Accepted tables have `service_header: true`
- Rejected tables show clear `reason` (e.g., "weak_money_rows", "failed_gates")

### 6D: Locked Rows Isolation

```bash
# Count locked rows from uncertain tables (should be 0)
find out/holdout -name "*.json" ! -name "*.*.*" -print0 | \
  xargs -0 python3 -c "
import json, sys
for f in sys.argv[1:]:
    doc = json.load(open(f))
    bad_locks = [s for s in doc.get('services',[]) if s.get('_row_locked') and s.get('_semantic_uncertain')]
    if bad_locks:
        print(f'{f}: {len(bad_locks)} locked uncertain rows (BAD)')
" --
```

**Expected:** (no output - no locked uncertain rows)

**If output appears:** FAIL - uncertain rows are contaminating priors.

---

## Step 7: Spot-Check Individual Docs

Pick 3 representative docs (e.g., Delta Dental, Aetna, Cigna):

```bash
# Find Delta Dental doc
SHA=$(jq -r 'select(.path | contains("Delta")) | .sha256' datasets/EOB_ZIP_2025Q3/manifest.jsonl | head -1)

echo "Checking doc: $SHA"

# Check all sidecars
ls -lh out/holdout/${SHA}.*
```

**Expected Files:**
```
<sha>.json                       # Main output
<sha>.conservation.json          # Conservation metrics
<sha>.doc_tables.validate.v1.json   # Doc table gating
<sha>.rowize.validate.v1.json      # Rowize fallback gating
<sha>.header_synthesis.v1.json     # Pseudo-headers (if rowize fallback used)
<sha>.sem_map.choice.v1.json       # Soft-accept decisions
```

**Spot-Check Main Output:**
```bash
jq '{
  services: (.services | length),
  locked: ([.services[] | select(._row_locked)] | length),
  uncertain: ([.services[] | select(._semantic_uncertain)] | length),
  conservation: (.services | map(select(.allowed_amount and .insurance_paid and .patient_responsibility)) | length)
}' out/holdout/${SHA}.json
```

**Expected:**
```json
{
  "services": 15-50,
  "locked": 0-5,
  "uncertain": 10-45,
  "conservation": varies
}
```

**Check:**
- `locked < uncertain` (uncertain rows not locked)
- `services > 0` (extraction worked)

---

## Step 8: Compare vs V6 Baseline

Compare holdout results to EREMIT60pg baseline:

```bash
# V6 baseline
jq '{services: (.services | length), conservation: .conservation_rate}' \
  out/eremit_doc_gated_v6/EREMIT60pg.conservation.json

# Holdout average
jq '.conservation_mean' SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

**Expected:**
- Holdout conservation >= v6 baseline (0.0308)
- Mixed payers should show higher average than single test doc

---

## Success Criteria

### ✅ Pass (Ready for Overlay Evaluation)
- [x] All 37 NDJSONs captured (no 0-byte files)
- [x] Empty row rate = 0.0
- [x] Missing metadata = 0
- [x] Conservation mean >= 0.30 (informative even if < 0.50)
- [x] No locked uncertain rows
- [x] All extraction_variant fields present
- [x] Service headers validated correctly

### ⚠️  Warn (Investigate but Continue)
- Empty row rate 0.05-0.10
- Conservation mean 0.30-0.49 (header-poor docs dominate)
- Some missing sidecars (non-critical)

### ❌ Fail (Engine Work Required)
- Empty row rate > 0.10
- Missing metadata > 0
- Locked uncertain rows > 0
- Systematic crashes or timeouts
- Conservation mean < 0.20 (extraction failure)

---

## Quick Reference Commands

```bash
# Full pipeline (after captures complete)
python3 /tmp/batch_capture.py && \
python3 scripts/holdout_shadow.py --dataset datasets/EOB_ZIP_2025Q3 --mode document \
  --doc-validate --doc-fallback-rowize --rowize-strict --rowize-tokens \
  --semantic-map --bootstrap-priors --multi-pass 3 --micro-ocr && \
python3 scripts/tools/qa_empty_rows.py out/holdout

# View summary
jq '{docs, services_total, conservation_mean, rows_3of4_rate}' \
  SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json

# Check for issues
find out/holdout -name "*.json" ! -name "*.*.*" -print0 | \
  xargs -0 python3 scripts/tools/qa_empty_rows.py
```

---

## Troubleshooting

### CVPixelBuffer Errors Return
**Symptom:** Batch capture fails with Vision framework errors
**Fix:** This Mac also lacks support; transfer to different hardware

### Empty NDJSONs Created
**Symptom:** Files exist but are 0 bytes
**Check:** `vision-ocr-cli` stderr logs in runs/holdout/
**Fix:** Re-run captures with `--debug` flag, check PDF validity

### Low Conservation Rates
**Symptom:** All docs show < 0.20 conservation
**Analysis:** Not necessarily a failure - may indicate header-poor payer mix
**Action:** Review semantic decisions (mapper vs HIM wins)

### Missing Sidecars
**Symptom:** Some `.validate.v1.json` files missing
**Check:** Logs in SESSION/AuditLogs/
**Likely:** Doc had no tables or all rejected (expected for some docs)

### Timeouts
**Symptom:** Script hangs on specific PDFs
**Fix:** Add `--timeout 180` to holdout_shadow.py call
**Investigate:** Check page count of problem PDFs

---

**Estimated Total Time:** 20-30 minutes
**Critical Path:** Steps 2, 4, 6A
**Documentation:** Save all outputs for payer analysis

**Next:** Once verified, aggregate payer-bucketed KPIs and evaluate overlay needs.
