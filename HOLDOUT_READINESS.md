# Holdout Readiness Checklist

**Status:** Ready for holdout analysis once Vision NDJSONs are captured on hardware-enabled Mac.

---

## RCF Freeze Parameters ✅

### Fixed Thresholds (Locked)
- **Latch:** `header_match_min = 0.15` (no dynamic lowering)
- **Mapper Win Criteria:** `mapper.sample_cons > 0` (must show evidence)
- **Δ-Conservation Gate:** `≥ 0.10` (for soft-accept competition)

### Service Predicate (Doc-Mode)
```
money_rows_ratio ≥ 0.30 ∧
code_or_date_rows_ratio ≥ 0.10 ∧
header contains {Code, ADA, CPT, DOS}
```

### Rowize Validator v1.1
```
min_rows ≥ 6 ∧
money_rows_ratio ≥ 0.50 ∧
code_or_date_rows_ratio ≥ 0.10 ∧
fit_probe ≥ 0.00
```

### Learning Isolation
- Rows from `semantic_uncertain` tables **never** feed priors or locks
- Uncertain rows extract via HIM fallback
- `extraction_variant` tracked in sidecars

### Priors Policy
```
n ≥ 5
IQR ≤ 0.25
clamp [0.20, 1.00]
quantize 0.01
```

### Budget & Stability
- Multi-pass: ≤3
- Locks: monotone
- OCR budget: ≤4 per page
- Byte-stable outputs

---

## V6 Verification Complete ✅

### Critical Bug Fixed
- **v5 Issue:** 538 services with only `{"_semantic_uncertain": true}`
- **v6 Fix:** 0/65 empty rows, full metadata on all services
- **Empty Row Rate:** 0.000 (target achieved)

### Policy Correctness
- **C1 (Service Predicate):** All Vision tables rejected (0 accepted, 5 rejected)
- **C2 (Semantic Uncertain):** 37/39 tables flagged, isolated from priors
- **Extraction:** All uncertain tables extract via HIM
- **Priors:** Only 1/65 services locked (from Page 23, non-uncertain mapper win)

### Metrics (v6 Hybrid)
```
Services:        65
Conservation:    3.08% (2/65 rows)
Mapper Wins:     2 (Pages 7, 23 - genuine Δ conservation)
HIM Wins:        37
Uncertain:       37/39 tables (95%)
Locked Rows:     1 (Page 23 only)
```

### Rowize Baseline (Stable)
```
Services:        113
Conservation:    30.1% (34/113 rows)
```

### Sidecars (All Fields Present)
- ✅ `doc_tables.validate.v1`: `service_header`, `codate_relaxed`, dense metrics
- ✅ `header_synthesis.v1`: `decimals_ratio`, `alignment_std`
- ✅ `sem_map.choice.v1`: `extraction_variant`, `semantic_uncertain`

---

## Holdout Infrastructure ✅

### Batch Capture Script
**Location:** `/tmp/batch_capture.py`

**Status:** Ready but **blocked by CVPixelBuffer hardware limitation** on this machine.

**Action Required:** Run Vision captures on Mac with Vision framework support:
```bash
python3 /tmp/batch_capture.py
```

**Expected Output:**
- 37 unique NDJSONs at `bench/holdout/EOB_ZIP_2025Q3/<sha>.jsonl`
- Run logs at `runs/holdout/EOB_ZIP_2025Q3/<sha>.run.json`

### Holdout Analysis Command
Once NDJSONs are in place:
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

**Output Report:**
```
SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
```

---

## QA Helper Scripts ✅

### Empty Row Rate Check
**Location:** `scripts/tools/qa_empty_rows.py`

**Usage:**
```bash
python3 scripts/tools/qa_empty_rows.py out/holdout
python3 scripts/tools/qa_empty_rows.py out/eremit_doc_gated_v6
```

**Output Example:**
```json
{
  "rows": 65,
  "empty_row_rate": 0.0,
  "missing_meta": 0
}
```

**Thresholds:**
- Warn: `empty_row_rate > 0.05`
- Fail: `empty_row_rate > 0.10`

### Extraction Variant Check
```bash
jq -r '.choices[] | [.table_id,.extraction_variant,.semantic_uncertain] | @tsv' \
  out/holdout/**/*.sem_map.choice.v1.json | head -20
```

---

## Top-Line KPI Checks

From `SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json`:

### Required Metrics
- **docs_processed:** Expected 37 (where NDJSONs exist)
- **rows_3of4_rate:** Track completeness
- **conservation_mean:** Target median 0.50–0.65 (payer-agnostic)

### Per-Doc Sidecars
For each processed doc `<sha>`:

1. **Doc Table Gating:**
   - `out/holdout/<sha>.doc_tables.validate.v1.json`
   - Check: `service_header=true` for accepted tables
   - Check: `codate_relaxed` only when `money_rows_ratio≥0.75`

2. **Rowize Fallbacks:**
   - `out/holdout/<sha>.rowize.validate.v1.json`
   - Count: accepted vs rejected
   - Verify: weak fallbacks pruned

3. **Soft-Accept Decisions:**
   - `out/holdout/<sha>.sem_map.choice.v1.json`
   - Verify: `extraction_variant` present
   - Verify: `semantic_uncertain` only where no evidence

4. **Pseudo-Headers:**
   - `out/holdout/<sha>.header_synthesis.v1.json`
   - Verify: `decimals_ratio` and `alignment_std` per band

5. **Output Rows:**
   - `out/holdout/<sha>.json`
   - Verify: No empty rows
   - Verify: Metadata present (`page_number`, `table_id`, `row_index`)
   - Verify: `_row_locked` never on uncertain rows

---

## Stop/Go Criteria

### Go (Proceed to Overlay Evaluation)
- ✅ Overall conservation ≥ 0.50
- ✅ Empty row rate = 0.0
- ✅ Budgets honored (p95 ≤ 3s/page)
- ✅ Byte-stable outputs

### No-Go (Engine Work First)
- ❌ Overall conservation < 0.50
- ❌ Systematic header-poor buckets where:
  - Mapper never wins, AND
  - Pseudo-headers don't improve Δ-conservation

---

## Guardrails to Add

### Acceptance Flow Checks
1. **Empty Row Rate:**
   - Warn: > 0.05
   - Fail: > 0.10

2. **Metadata Completeness:**
   - 100% rows must include `{page_number, table_id, row_index}`

3. **Mode Path Sidecar:**
   - Record `{"capture": "vision|ndjson", "fallback": "rowize|none"}` per doc
   - Fail if missing

4. **Regression Gate:**
   - No payer bucket regresses > 5pp conservation vs prior within budgets

---

## Next Actions

1. **Immediate (Requires Mac with Vision Support):**
   - Run `/tmp/batch_capture.py` to generate 37 NDJSONs
   - Place NDJSONs at `bench/holdout/EOB_ZIP_2025Q3/<sha>.jsonl`

2. **After NDJSONs Available:**
   - Run `python3 scripts/holdout_shadow.py --dataset datasets/EOB_ZIP_2025Q3 --mode document ...`
   - Review `SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json`
   - Run `python3 scripts/tools/qa_empty_rows.py out/holdout`
   - Spot-check per-doc sidecars

3. **Analysis (After Holdout):**
   - Aggregate payer-bucketed KPIs
   - Generate sidecar hash index
   - Identify payers where doc-mode helps vs rowize-dominant
   - Evaluate if latch 0.15→0.12 is broadly beneficial

4. **Only After Holdout Proves Need:**
   - Tune `common.yaml` if multiple payers show consistent header evidence
   - Evaluate payer overlays via promotion gates
   - Consider latch adjustment only if data supports

---

## Files Ready

### QA Scripts
- ✅ `scripts/tools/qa_empty_rows.py`

### Batch Capture
- ✅ `/tmp/batch_capture.py` (needs Vision-enabled Mac)

### Analysis Scripts
- ✅ `scripts/holdout_shadow.py`
- ✅ `scripts/acceptance_doc_only.py`
- ✅ `scripts/assemble_eob.py`

### Test Outputs (Validated)
- ✅ `out/eremit_doc_gated_v6/` (hybrid v6 - verified)
- ✅ `out/eremit_rowize_only_v2/` (baseline - stable)
- ✅ `out/eremit_doc_only_accept/` (service predicate - working)

---

**Blocking Issue:** Vision capture requires hardware support not available on this machine. Transfer batch script to Vision-enabled Mac or provide pre-captured NDJSONs.

**Ready for:** Immediate holdout analysis once NDJSONs are in place.
