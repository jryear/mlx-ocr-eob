# Row-Retry Sidecar Verification Checklist

**Date:** 2025-10-23
**Checkpoint:** CP8 Full — Row-Retry Queue Emission
**Status:** ⏳ **AWAITING IMPLEMENTATION**

---

## 🎯 Verification Checklist

### 1. NDJSON File Creation

**Check:**
```bash
# Verify row-retry files exist
ls -la runs/row_retry/*.ndjson

# Count files (should match number of EOBs)
ls runs/row_retry/*.ndjson | wc -l
ls out/eob/*.json | grep -v sha256 | wc -l
```

**Expected:**
```
runs/row_retry/
├── AetnaW_Check.ndjson
├── DD_3.ndjson
└── DD_3_Ohio.ndjson
```

**✅ PASS:** One NDJSON file per EOB
**❌ FAIL:** Missing files or count mismatch

---

### 2. NDJSON Structure Validation

**Check:**
```bash
# Inspect first retry entry
head -1 runs/row_retry/DD_3.ndjson | jq .

# Validate required fields
head -1 runs/row_retry/DD_3.ndjson | jq 'has("row") and has("failing_fields") and has("reasons") and has("suggested_budget_multiplier")'
```

**Expected Structure:**
```json
{
  "row": 1,
  "failing_fields": ["allowed_amount", "insurance_paid", "patient_responsibility"],
  "reasons": ["allowed!=(paid+patient)"],
  "header_lock_sha256": "aee39fd3be45193894ba914d6613b4d27b9532c848a09f674c0d6fef41a15215",
  "suggested_budget_multiplier": 1.25
}
```

**✅ PASS:** All required fields present
**❌ FAIL:** Missing fields or incorrect structure

---

### 3. Reason Code Alignment

**Check:**
```bash
# Extract reasons from conservation sidecars
jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' \
  out/eob/DD_3.conservation.json | sort -u > /tmp/cons_reasons.txt

# Extract reasons from row-retry NDJSON
jq -r '.reasons[]' runs/row_retry/DD_3.ndjson | sort -u > /tmp/retry_reasons.txt

# Compare
echo "=== Conservation Reasons ===" && cat /tmp/cons_reasons.txt
echo -e "\n=== Row-Retry Reasons ===" && cat /tmp/retry_reasons.txt
echo -e "\n=== Differences ===" && diff /tmp/cons_reasons.txt /tmp/retry_reasons.txt
```

**Expected:**
```
=== Conservation Reasons ===
allowed!=(paid+patient)
allowed>charged

=== Row-Retry Reasons ===
allowed!=(paid+patient)
allowed>charged

=== Differences ===
(no output - identical)
```

**✅ PASS:** Reason codes match exactly
**❌ FAIL:** Mismatched or missing reasons

---

### 4. Row Index Consistency

**Check:**
```bash
# Extract inconsistent row indices from conservation sidecar
jq '.inconsistent_rows[]' out/eob/DD_3.conservation.json | sort -n > /tmp/cons_rows.txt

# Extract row indices from row-retry NDJSON
jq -r '.row' runs/row_retry/DD_3.ndjson | sort -n > /tmp/retry_rows.txt

# Compare
echo "=== Conservation Inconsistent Rows ===" && cat /tmp/cons_rows.txt
echo -e "\n=== Row-Retry Queue Rows ===" && cat /tmp/retry_rows.txt
echo -e "\n=== Differences ===" && diff /tmp/cons_rows.txt /tmp/retry_rows.txt
```

**Expected:**
```
=== Conservation Inconsistent Rows ===
1
2
4
6

=== Row-Retry Queue Rows ===
1
2
4
6

=== Differences ===
(no output - identical)
```

**✅ PASS:** Row indices match exactly
**❌ FAIL:** Mismatched or missing rows

---

### 5. Budget Multiplier Validation

**Check:**
```bash
# Verify all entries have 1.25 multiplier
jq -r '.suggested_budget_multiplier' runs/row_retry/*.ndjson | sort -u
```

**Expected:**
```
1.25
```

**✅ PASS:** All entries have 1.25 multiplier
**❌ FAIL:** Different or missing multipliers

---

### 6. header_lock_sha256 Binding (If Available)

**Check:**
```bash
# Check if dual-pass artifacts exist
ls runs/vlm/*_headers_response.json 2>/dev/null

# If they exist, verify SHA binding
if [ -f "runs/vlm/4098b6818705feb049d4a1c8ee3beaec4afba1c494cf4a200c034be4673326eb_p0_roi0_headers_response.json" ]; then
  header_sha=$(jq -r '.header_lock.lock_sha256' runs/vlm/*_headers_response.json | head -1)
  retry_sha=$(jq -r '.header_lock_sha256 // "null"' runs/row_retry/*.ndjson | head -1)
  echo "Header SHA: $header_sha"
  echo "Retry SHA:  $retry_sha"
  [ "$header_sha" = "$retry_sha" ] && echo "✅ MATCH" || echo "❌ MISMATCH"
else
  echo "⚠️  No dual-pass artifacts - header_lock_sha256 should be null"
  jq -r '.header_lock_sha256 // "null"' runs/row_retry/*.ndjson | sort -u
fi
```

**Expected (if dual-pass exists):**
```
Header SHA: aee39fd3be45193894ba914d6613b4d27b9532c848a09f674c0d6fef41a15215
Retry SHA:  aee39fd3be45193894ba914d6613b4d27b9532c848a09f674c0d6fef41a15215
✅ MATCH
```

**Expected (if dual-pass not available):**
```
⚠️  No dual-pass artifacts - header_lock_sha256 should be null
null
```

**✅ PASS:** SHA matches or gracefully null
**❌ FAIL:** SHA mismatch or unexpected value

---

### 7. Failing Fields Extraction

**Check:**
```bash
# Verify failing_fields populated
jq -c '{row: .row, failing_fields: .failing_fields, reasons: .reasons}' runs/row_retry/DD_3.ndjson | head -5
```

**Expected:**
```json
{"row":1,"failing_fields":["allowed_amount","insurance_paid","patient_responsibility"],"reasons":["allowed!=(paid+patient)"]}
{"row":2,"failing_fields":["allowed_amount","charged_amount"],"reasons":["allowed>charged"]}
{"row":4,"failing_fields":["allowed_amount","insurance_paid","patient_responsibility"],"reasons":["allowed!=(paid+patient)"]}
```

**Logic Verification:**
```python
# Reason → Failing Fields mapping
"allowed!=(paid+patient)"  → ["allowed_amount", "insurance_paid", "patient_responsibility"]
"allowed>charged"          → ["allowed_amount", "charged_amount"]
"missing:paid,patient"     → ["insurance_paid", "patient_responsibility"]
```

**✅ PASS:** failing_fields correctly derived from reasons
**❌ FAIL:** Incorrect or missing fields

---

### 8. Make Target Execution

**Check:**
```bash
# Run make target
make row-retry-emit

# Verify outputs
ls runs/row_retry/*.ndjson
```

**Expected:**
```
Assembling EOBs...
Wrote out/eob/AetnaW_Check.json
Wrote out/eob/DD_3.json
Wrote out/eob/DD_3_Ohio.json
Emitting row-retry queue...
Wrote runs/row_retry/AetnaW_Check.ndjson (6 rows)
Wrote runs/row_retry/DD_3.ndjson (5 rows)
Wrote runs/row_retry/DD_3_Ohio.ndjson (6 rows)
```

**✅ PASS:** Make target runs successfully
**❌ FAIL:** Errors or missing outputs

---

### 9. Anti-Repetition Guard (Optional)

**If implemented, check:**
```bash
# Look for repetition_guard reason
jq -r '.reasons[]' runs/row_retry/*.ndjson | grep -c "repetition_guard" || echo "0 instances"

# If found, inspect details
jq 'select(.reasons | contains(["repetition_guard"]))' runs/row_retry/*.ndjson
```

**Expected (if implemented):**
```json
{
  "row": 7,
  "failing_fields": ["service_description"],
  "reasons": ["repetition_guard"],
  "header_lock_sha256": null,
  "suggested_budget_multiplier": 1.25,
  "repetition_details": {
    "repeated_token": "DENTAL SERVICES",
    "repetition_count": 8,
    "threshold": 5
  }
}
```

**✅ PASS:** Repetition detected and flagged
**⚠️ SKIP:** Not implemented yet

---

### 10. Aggregate Metrics

**Check:**
```bash
# Count total retry rows across corpus
jq -s 'map(length) | {
  total_docs: length,
  total_retry_rows: add,
  mean_retry_per_doc: (add / length)
}' runs/row_retry/*.ndjson
```

**Expected:**
```json
{
  "total_docs": 3,
  "total_retry_rows": 17,
  "mean_retry_per_doc": 5.67
}
```

**Validation:**
```bash
# Compare to conservation sidecars
jq -s 'map(.inconsistent_rows | length) | add' out/eob/*.conservation.json
# Should match total_retry_rows (if only inconsistent rows in queue)
```

**✅ PASS:** Counts align with conservation sidecars
**❌ FAIL:** Count mismatch

---

## 📊 Quick Verification Script

**All-in-One Check:**
```bash
#!/bin/bash
set -e

echo "=== Row-Retry Sidecar Verification ==="
echo ""

# 1. File existence
echo "1. Checking file creation..."
retry_count=$(ls runs/row_retry/*.ndjson 2>/dev/null | wc -l | tr -d ' ')
eob_count=$(ls out/eob/*.json 2>/dev/null | grep -v sha256 | wc -l | tr -d ' ')
echo "   Retry files: $retry_count"
echo "   EOB files: $eob_count"
[ "$retry_count" -eq "$eob_count" ] && echo "   ✅ PASS" || echo "   ❌ FAIL"
echo ""

# 2. Structure validation
echo "2. Checking NDJSON structure..."
head -1 runs/row_retry/*.ndjson | jq 'has("row") and has("failing_fields") and has("reasons") and has("suggested_budget_multiplier")' \
  && echo "   ✅ PASS: All required fields present" \
  || echo "   ❌ FAIL: Missing required fields"
echo ""

# 3. Reason code alignment
echo "3. Checking reason code alignment..."
cons_reasons=$(jq -r '.rows[] | select(.status=="inconsistent") | .reasons[]' out/eob/*.conservation.json | sort -u)
retry_reasons=$(jq -r '.reasons[]' runs/row_retry/*.ndjson | sort -u)
if [ "$cons_reasons" = "$retry_reasons" ]; then
  echo "   ✅ PASS: Reason codes match"
else
  echo "   ❌ FAIL: Reason code mismatch"
  echo "   Conservation: $cons_reasons"
  echo "   Retry: $retry_reasons"
fi
echo ""

# 4. Budget multiplier
echo "4. Checking budget multipliers..."
multipliers=$(jq -r '.suggested_budget_multiplier' runs/row_retry/*.ndjson | sort -u)
if [ "$multipliers" = "1.25" ]; then
  echo "   ✅ PASS: All multipliers are 1.25"
else
  echo "   ⚠️  WARN: Non-standard multipliers: $multipliers"
fi
echo ""

# 5. Aggregate metrics
echo "5. Aggregate metrics..."
total_retry=$(jq -s 'map(length) | add' runs/row_retry/*.ndjson)
total_inconsistent=$(jq -s 'map(.inconsistent_rows | length) | add' out/eob/*.conservation.json)
echo "   Total retry rows: $total_retry"
echo "   Total inconsistent: $total_inconsistent"
[ "$total_retry" -eq "$total_inconsistent" ] && echo "   ✅ PASS" || echo "   ⚠️  WARN: Count mismatch"
echo ""

echo "=== Verification Complete ==="
```

**Save as:** `scripts/verify_row_retry.sh`

**Run:**
```bash
chmod +x scripts/verify_row_retry.sh
./scripts/verify_row_retry.sh
```

---

## ✅ Acceptance Criteria

**Must Have:**
- ✅ One NDJSON file per EOB
- ✅ All required fields present
- ✅ Reason codes match conservation sidecars
- ✅ Row indices match conservation sidecars
- ✅ Budget multiplier = 1.25

**Should Have:**
- ✅ header_lock_sha256 binding (if available)
- ✅ Correct failing_fields extraction
- ✅ Make target functional

**Nice to Have:**
- ⚠️ Anti-repetition guard (optional)
- ⚠️ Repetition details metadata

---

## 🚀 Ready for Verification

**Status:** ⏳ Awaiting implementation

**Next Steps:**
1. User implements row-retry sidecar
2. Run verification checklist
3. Fix any issues
4. Mark CP8 Full complete

---

**Checklist Date:** 2025-10-23
**Checklist By:** CONTRACTS (Claude Code Assistant)
**Status:** Ready for execution when implementation complete
