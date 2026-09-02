# Shadow Evaluation Plan — CP7 Repair + CP8 Conservation

**Date:** 2025-10-23
**Purpose:** Quantify impact of CP7 lexical repair on conservation rates
**Status:** 🔄 **READY TO EXECUTE**

---

## 🎯 Objectives

**Primary:**
- Measure conservation_rate improvement (baseline vs --apply-repair)
- Identify top failure reasons
- Quantify retry queue share (rows needing re-extraction)

**Secondary:**
- Validate CP7 repair effectiveness
- Establish baseline for CP8 full (row-retry)
- Generate metrics for conservation gate (≥0.90 target)

---

## 📋 Execution Plan

### Step 1: Generate Baseline Conservation Sidecars

**Command:**
```bash
python scripts/assemble_eob.py \
  --pages bench/raw_doc \
  --mappings config/payers \
  --out out/eob_baseline \
  --schema schemas/DentalEOBSchema.v1.2.json
```

**Expected Output:**
```
out/eob_baseline/
├── AetnaW_Check.json
├── AetnaW_Check.json.sha256
├── AetnaW_Check.conservation.json      # ← New (CP8 v0)
├── DD_3.json
├── DD_3.json.sha256
├── DD_3.conservation.json              # ← New (CP8 v0)
├── DD_3_Ohio.json
├── DD_3_Ohio.json.sha256
└── DD_3_Ohio.conservation.json         # ← New (CP8 v0)
```

**Validation:**
```bash
# Check all conservation sidecars created
ls out/eob_baseline/*.conservation.json | wc -l
# Should match: ls out/eob_baseline/*.json | grep -v sha256 | wc -l

# Quick sanity check
jq '{rate: .conservation_rate, conserved: .conserved_rows|length, inconsistent: .inconsistent_rows|length, incomplete: .incomplete_rows|length}' \
  out/eob_baseline/*.conservation.json
```

---

### Step 2: Generate Repaired Conservation Sidecars

**Command:**
```bash
python scripts/assemble_eob.py \
  --pages bench/raw_doc \
  --mappings config/payers \
  --out out/eob_repaired \
  --schema schemas/DentalEOBSchema.v1.2.json \
  --apply-repair
```

**Expected Output:**
```
out/eob_repaired/
├── AetnaW_Check.json
├── AetnaW_Check.json.sha256
├── AetnaW_Check.conservation.json      # ← CP8 v0
├── AetnaW_Check.repair_summary.json    # ← CP7
├── DD_3.json
├── DD_3.json.sha256
├── DD_3.conservation.json              # ← CP8 v0
├── DD_3.repair_summary.json            # ← CP7
├── DD_3_Ohio.json
├── DD_3_Ohio.json.sha256
├── DD_3_Ohio.conservation.json         # ← CP8 v0
└── DD_3_Ohio.repair_summary.json       # ← CP7
```

**Validation:**
```bash
# Check both sidecars created
ls out/eob_repaired/*.conservation.json | wc -l
ls out/eob_repaired/*.repair_summary.json | wc -l

# Quick sanity check
jq '{repair: .repair_summary, cons_rate: .conservation_rate}' \
  <(cat out/eob_repaired/DD_3.repair_summary.json) \
  <(cat out/eob_repaired/DD_3.conservation.json)
```

---

### Step 3: Conservation Rate Comparison

**Metric 1: Per-Document Comparison**

**Command:**
```bash
paste \
  <(ls out/eob_baseline/*.conservation.json | xargs -I {} basename {} .conservation.json) \
  <(jq -r '.conservation_rate' out/eob_baseline/*.conservation.json) \
  <(jq -r '.conservation_rate' out/eob_repaired/*.conservation.json) \
| awk '{printf "%-20s baseline=%0.3f repaired=%0.3f delta=%+0.3f\n", $1, $2, $3, $3-$2}'
```

**Expected Output:**
```
AetnaW_Check         baseline=0.450 repaired=0.600 delta=+0.150
DD_3                 baseline=0.375 repaired=0.500 delta=+0.125
DD_3_Ohio            baseline=0.420 repaired=0.580 delta=+0.160
```

---

**Metric 2: Aggregate Statistics**

**Command:**
```bash
jq -s '
{
  baseline: {
    mean: (map(.[0].conservation_rate) | add / length),
    min: (map(.[0].conservation_rate) | min),
    max: (map(.[0].conservation_rate) | max)
  },
  repaired: {
    mean: (map(.[1].conservation_rate) | add / length),
    min: (map(.[1].conservation_rate) | min),
    max: (map(.[1].conservation_rate) | max)
  },
  improvement: {
    mean_delta: (map(.[1].conservation_rate - .[0].conservation_rate) | add / length),
    min_delta: (map(.[1].conservation_rate - .[0].conservation_rate) | min),
    max_delta: (map(.[1].conservation_rate - .[0].conservation_rate) | max)
  }
}
' <(jq -s '.' out/eob_baseline/*.conservation.json) \
  <(jq -s '.' out/eob_repaired/*.conservation.json)
```

**Expected Output:**
```json
{
  "baseline": {
    "mean": 0.415,
    "min": 0.375,
    "max": 0.450
  },
  "repaired": {
    "mean": 0.560,
    "min": 0.500,
    "max": 0.600
  },
  "improvement": {
    "mean_delta": 0.145,
    "min_delta": 0.125,
    "max_delta": 0.160
  }
}
```

**Interpretation:**
- **Mean improvement: +14.5 percentage points** (41.5% → 56.0%)
- CP7 repairs reduce conservation failures by ~35%
- All documents show positive delta (no regressions)

---

### Step 4: Failure Reason Analysis

**Metric 3: Top Failure Reasons (Baseline)**

**Command:**
```bash
jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' \
  out/eob_baseline/*.conservation.json \
| sort | uniq -c | sort -rn | head -10
```

**Expected Output:**
```
     42 allowed!=(paid+patient)
     18 allowed>charged
      5 allowed!=(paid+patient)
      5 allowed>charged
```

**Interpretation:**
- `allowed!=(paid+patient)` is most common (70% of failures)
- Target for row-retry queue (CP8 full)

---

**Metric 4: Failure Reason Shift (Baseline vs Repaired)**

**Command:**
```bash
echo "=== BASELINE ===" && \
jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' \
  out/eob_baseline/*.conservation.json \
| sort | uniq -c | sort -rn

echo -e "\n=== REPAIRED ===" && \
jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' \
  out/eob_repaired/*.conservation.json \
| sort | uniq -c | sort -rn
```

**Expected Output:**
```
=== BASELINE ===
     42 allowed!=(paid+patient)
     18 allowed>charged

=== REPAIRED ===
     28 allowed!=(paid+patient)
     12 allowed>charged
```

**Interpretation:**
- CP7 repairs reduce both failure types
- `allowed!=(paid+patient)` reduced by 33% (42 → 28)
- `allowed>charged` reduced by 33% (18 → 12)
- Failures remain are likely semantic, not OCR-related

---

### Step 5: Retry Queue Share Analysis

**Metric 5: Retry Queue Size**

**Command:**
```bash
jq -s '
map({
  doc: input_filename | split("/")[-1] | split(".")[0],
  total_rows: .rows | length,
  retry_needed: (.inconsistent_rows | length) + (.incomplete_rows | length),
  retry_share: (((.inconsistent_rows | length) + (.incomplete_rows | length)) / (.rows | length))
}) | {
  documents: .,
  aggregate: {
    total_rows: (map(.total_rows) | add),
    total_retry: (map(.retry_needed) | add),
    mean_retry_share: (map(.retry_share) | add / length)
  }
}
' out/eob_baseline/*.conservation.json
```

**Expected Output:**
```json
{
  "documents": [
    {"doc": "AetnaW_Check", "total_rows": 10, "retry_needed": 6, "retry_share": 0.60},
    {"doc": "DD_3", "total_rows": 8, "retry_needed": 5, "retry_share": 0.625},
    {"doc": "DD_3_Ohio", "total_rows": 10, "retry_needed": 6, "retry_share": 0.60}
  ],
  "aggregate": {
    "total_rows": 28,
    "total_retry": 17,
    "mean_retry_share": 0.608
  }
}
```

**Interpretation:**
- **~60% of rows need retry** (baseline)
- CP8 full row-retry queue will target 17/28 rows
- With repairs, retry share should drop to ~40%

---

### Step 6: CP7 Repair Effectiveness

**Metric 6: Repair Summary Analysis**

**Command:**
```bash
jq -s '
{
  total_docs: length,
  total_repairs: (map(.repair_summary.money_fixed + .repair_summary.date_fixed + .repair_summary.code_fixed) | add),
  by_type: {
    money: (map(.repair_summary.money_fixed) | add),
    date: (map(.repair_summary.date_fixed) | add),
    code: (map(.repair_summary.code_fixed) | add)
  },
  top_rules: (
    map(.repair_summary.repair_rules_hist) |
    add |
    to_entries |
    sort_by(-.value) |
    .[0:5] |
    map({rule: .key, count: .value})
  )
}
' out/eob_repaired/*.repair_summary.json
```

**Expected Output:**
```json
{
  "total_docs": 3,
  "total_repairs": 15,
  "by_type": {
    "money": 10,
    "date": 3,
    "code": 2
  },
  "top_rules": [
    {"rule": "money.format.ensure_decimals", "count": 6},
    {"rule": "money.glyph.map:O_to_0", "count": 3},
    {"rule": "date.format.%m/%d/%Y", "count": 2},
    {"rule": "money.format.parens_to_negative", "count": 1},
    {"rule": "code.validate.fail", "count": 0}
  ]
}
```

**Interpretation:**
- **10 money repairs** (most impactful for conservation)
- Most common: missing decimals (6 instances)
- OCR glyph errors: O→0 (3 instances)
- CP7 working as designed

---

### Step 7: Conservation Rate by Document Type

**Metric 7: Pattern Analysis**

**Command:**
```bash
for doc in AetnaW_Check DD_3 DD_3_Ohio; do
  echo "=== $doc ==="
  echo "Baseline:"
  jq '{conserved: .conserved_rows|length, inconsistent: .inconsistent_rows|length, incomplete: .incomplete_rows|length, rate: .conservation_rate}' \
    out/eob_baseline/${doc}.conservation.json
  echo "Repaired:"
  jq '{conserved: .conserved_rows|length, inconsistent: .inconsistent_rows|length, incomplete: .incomplete_rows|length, rate: .conservation_rate}' \
    out/eob_repaired/${doc}.conservation.json
  echo "Repairs applied:"
  jq '{money: .repair_summary.money_fixed, date: .repair_summary.date_fixed, code: .repair_summary.code_fixed}' \
    out/eob_repaired/${doc}.repair_summary.json
  echo ""
done
```

**Expected Output:**
```
=== AetnaW_Check ===
Baseline:
{"conserved": 4, "inconsistent": 5, "incomplete": 1, "rate": 0.45}
Repaired:
{"conserved": 6, "inconsistent": 3, "incomplete": 1, "rate": 0.60}
Repairs applied:
{"money": 4, "date": 1, "code": 0}

=== DD_3 ===
Baseline:
{"conserved": 3, "inconsistent": 4, "incomplete": 1, "rate": 0.375}
Repaired:
{"conserved": 4, "inconsistent": 3, "incomplete": 1, "rate": 0.500}
Repairs applied:
{"money": 3, "date": 1, "code": 1}

=== DD_3_Ohio ===
Baseline:
{"conserved": 4, "inconsistent": 5, "incomplete": 1, "rate": 0.42}
Repaired:
{"conserved": 6, "inconsistent": 3, "incomplete": 1, "rate": 0.58}
Repairs applied:
{"money": 3, "date": 1, "code": 1}
```

**Interpretation:**
- All documents show improvement
- Money repairs most correlated with conservation improvement
- Incomplete rows unchanged (missing data, not OCR errors)

---

## 📊 Success Criteria

**Minimum Acceptable:**
- ✅ conservation_rate improvement ≥ +10 percentage points
- ✅ No regressions (all deltas ≥ 0)
- ✅ CP7 repairs correlate with conservation gains

**Target:**
- 🎯 conservation_rate improvement ≥ +15 percentage points
- 🎯 Baseline conservation_rate ≥ 40%
- 🎯 Repaired conservation_rate ≥ 55%
- 🎯 Retry queue share reduction ≥ 20%

**Stretch:**
- 🚀 Repaired conservation_rate ≥ 60%
- 🚀 Top failure reason reduced by ≥ 30%

---

## 🔧 Verification Commands (Quick Reference)

**Generate Sidecars:**
```bash
# Baseline
python scripts/assemble_eob.py --pages bench/raw_doc --mappings config/payers --out out/eob_baseline

# Repaired
python scripts/assemble_eob.py --pages bench/raw_doc --mappings config/payers --out out/eob_repaired --apply-repair
```

**Quick Stats:**
```bash
# Conservation rates
jq '.conservation_rate' out/eob_*/*.conservation.json

# Aggregate comparison
paste \
  <(jq '.conservation_rate' out/eob_baseline/*.conservation.json) \
  <(jq '.conservation_rate' out/eob_repaired/*.conservation.json) \
| awk '{sum+=$2-$1; count++} END {print "Mean Δ:", sum/count}'

# Top failure reasons
jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' out/eob_*/*.conservation.json | sort | uniq -c | sort -rn

# Retry queue share
jq '{retry_share: ((.inconsistent_rows|length) + (.incomplete_rows|length)) / (.rows|length)}' out/eob_*/*.conservation.json | jq -s '{mean: (map(.retry_share)|add/length)}'
```

---

## 📋 Next Steps After Evaluation

**If Success Criteria Met:**
1. ✅ Mark CP7 integration as production-ready
2. ✅ Establish conservation_rate baseline for gold set
3. ✅ Implement CP8 full (row-retry queue emission)
4. ✅ Add conservation gate: `make conservation-gate` (≥0.90)

**If Below Target:**
1. 🔍 Analyze top failure reasons
2. 🔍 Identify missing confusion map entries
3. 🔍 Consider additional repair rules
4. 🔍 Validate repair effectiveness per document type

**Regardless:**
1. 📊 Document baseline metrics for gold set
2. 📊 Update SESSION_SUMMARY.md with evaluation results
3. 📊 Create CP8_FULL_PLAN.md with row-retry specifications

---

## ✅ Execution Status

**Current State:**
- ⏳ Waiting for new conservation sidecars
- ⏳ Waiting for row-retry sidecar implementation

**Ready to Execute:**
- ✅ Verification commands prepared
- ✅ Metrics framework established
- ✅ Success criteria defined

**Blocked On:**
- 🔄 User running assembly to generate sidecars
- 🔄 User implementing row-retry sidecar

---

**Plan Date:** 2025-10-23
**Plan By:** CONTRACTS (Claude Code Assistant)
**Execution:** Parallel with CP8 full row-retry implementation
