# CP7 Integration Verification — Lexical Repair Flag

**Date:** 2025-10-23
**Checkpoint:** CP7 Integration (--apply-repair flag)
**Owner:** IMPL (Implementation), CONTRACTS (Verification)
**Status:** ✅ **COMPLETE**

---

## 🎯 Scope

**Deliverables:**
- `--apply-repair` flag in `scripts/assemble_eob.py` (default OFF)
- CP7 lexical repair integrated into assembly pipeline
- Repair applied to typed fields (money/date/code) BEFORE conservation
- TEXT immutability enforced
- Schema-neutral sidecar (`.repair_summary.json`) for analytics
- Graceful fallback if `lexical_repair` module unavailable

**Expected Impact:**
- Opt-in repair for shadow evaluation
- No breaking changes to existing pipeline
- Foundation for CP8 money conservation integration

---

## ✅ Verification Results

### 1. Flag Implementation

**CLI Flags Added:**
```bash
--apply-repair        Apply CP7 lexical repair before conservation (default OFF)
--repair-maps PATH    Path to confusion maps for CP7 repair
                      (default: config/confusion_maps.v1.json)
```

**Default Behavior:**
- ✅ Flag defaults to OFF (no repairs applied by default)
- ✅ No breaking changes to existing workflows
- ✅ Graceful fallback if `lexical_repair` module not available

**File:** `scripts/assemble_eob.py:548-549`

---

### 2. Module Import (Graceful Fallback)

**Implementation:**
```python
# Optional CP7 lexical repair (loaded only when used)
try:
    from lexical_repair import load_maps, repair_money, repair_date, repair_code
except Exception:  # pragma: no cover
    load_maps = None
    repair_money = None
    repair_date = None
    repair_code = None
```

**File:** `scripts/assemble_eob.py:21-28`

**Verification:**
- ✅ Import wrapped in try/except
- ✅ Functions set to None if unavailable
- ✅ Runtime error only if user tries `--apply-repair` without module (lines 564-567)

---

### 3. Repair Application Logic

**Function:** `apply_lexical_repair(row, maps)` (lines 337-364)

**Fields Repaired:**
- **Money:** `charged_amount`, `allowed_amount`, `insurance_paid`, `patient_responsibility`
- **Date:** `service_date`
- **Code:** `procedure_code`
- **TEXT:** N/A (immutable by design)

**Repair Flow:**
```python
for fld in ("charged_amount", "allowed_amount", "insurance_paid", "patient_responsibility"):
    val = out.get(fld)
    if isinstance(val, str) and val.strip():
        before = val
        repaired, rn = repair_money(before, maps.get("money", {}))
        if repaired is not None and repaired != before:
            out[fld] = repaired
            notes.append({"field": fld, "type": "money", "before": before, "after": repaired, "rules": rn})
```

**Verification:**
- ✅ Repairs applied per-row
- ✅ Only modified fields logged in notes
- ✅ Returns (repaired_row, notes) tuple
- ✅ TEXT fields never mutated

---

### 4. Integration Point (Before Conservation)

**Function:** `gather_services(pages, template, provenance, apply_repair, maps, repair_notes, repair_summary)` (lines 315-331)

**Integration Logic:**
```python
if entry:
    # Optional CP7: apply lexical repair to typed fields (TEXT immutable)
    if apply_repair and maps is not None:
        entry, rn = apply_lexical_repair(entry, maps)
        if rn and repair_notes is not None:
            repair_notes.extend(rn)
        if rn and repair_summary is not None:
            for note in rn:
                t = note.get("type")
                if t == "money":
                    repair_summary["money_fixed"] += 1
                elif t == "date":
                    repair_summary["date_fixed"] += 1
                elif t == "code":
                    repair_summary["code_fixed"] += 1
                for r in note.get("rules", []):
                    hist = repair_summary.setdefault("repair_rules_hist", {})
                    hist[r] = hist.get(r, 0) + 1
    services.append(entry)
    provenance.extend(row_prov)
```

**Sequence Verification:**
```
1. gather_services(..., apply_repair=True) → applies repairs per row
2. money_conservation_pass(services, provenance) → conservation uses repaired values
3. compute_totals(services) → totals computed from conserved values
```

**File:** `scripts/assemble_eob.py:415-419`

**Verification:**
- ✅ Repairs applied BEFORE conservation (line 415 vs line 419)
- ✅ Only runs if `apply_repair=True` and `maps is not None`
- ✅ Repair notes and summary updated incrementally
- ✅ Preserves provenance tracking

---

### 5. Sidecar Output (Schema-Neutral)

**Sidecar Structure:**
```json
{
  "doc": "document_key",
  "repair_summary": {
    "money_fixed": 5,
    "date_fixed": 2,
    "code_fixed": 1,
    "text_mutations": 0,
    "repair_rules_hist": {
      "money.glyph.map:O_to_0": 3,
      "money.format.ensure_decimals": 2,
      "date.glyph.map:O_to_0": 1,
      "date.format.%m/%d/%Y": 2,
      "code.validate.fail": 0
    }
  },
  "repair_notes_count": 8
}
```

**Output Logic (lines 596-598):**
```python
# Optional CP7 sidecar summary
if args.apply_repair and rep_out:
    with output_path.with_suffix(".repair_summary.json").open("w", encoding="utf-8") as sf:
        sf.write(json.dumps(rep_out, sort_keys=True, separators=(",", ":")))
```

**File Naming:**
- Main EOB: `out/eob/{slug}.json`
- Sidecar: `out/eob/{slug}.repair_summary.json`
- SHA256: `out/eob/{slug}.json.sha256`

**Verification:**
- ✅ Sidecar only written when `--apply-repair` is True
- ✅ Uses canonical JSON format (sorted keys, compact)
- ✅ Separate file (no schema changes to main EOB)
- ✅ Contains aggregated metrics + rule histogram

---

### 6. Confusion Maps Loading

**Loading Logic (lines 564-567):**
```python
maps: Optional[Dict[str, Dict[str, str]]] = None
if args.apply_repair:
    if load_maps is None:
        raise RuntimeError("lexical_repair not available; cannot apply repair")
    maps = load_maps(args.repair_maps)
```

**Verification:**
- ✅ Maps loaded only when `--apply-repair` is True
- ✅ Explicit error if module unavailable
- ✅ Uses configurable path (default: `config/confusion_maps.v1.json`)
- ✅ Passed to `assemble_document()` and `gather_services()`

---

## 📊 Usage Examples

### Baseline (No Repair)

**Command:**
```bash
python scripts/assemble_eob.py \
  --pages bench/raw_doc \
  --mappings config/payers \
  --out out/eob_baseline
```

**Behavior:**
- No repairs applied
- No `.repair_summary.json` sidecars
- Existing behavior unchanged

---

### Shadow Evaluation (With Repair)

**Command:**
```bash
python scripts/assemble_eob.py \
  --pages bench/raw_doc \
  --mappings config/payers \
  --out out/eob_repaired \
  --apply-repair
```

**Behavior:**
- Repairs applied to money/date/code fields
- `.repair_summary.json` sidecars created
- Main EOB schema unchanged

**Output Files:**
```
out/eob_repaired/
├── DD_3.json                    # Main EOB (schema-conformant)
├── DD_3.json.sha256             # Canonical hash
└── DD_3.repair_summary.json     # Repair metrics (sidecar)
```

---

### Custom Confusion Maps

**Command:**
```bash
python scripts/assemble_eob.py \
  --pages bench/raw_doc \
  --mappings config/payers \
  --out out/eob_custom \
  --apply-repair \
  --repair-maps config/confusion_maps.v2.json
```

**Use Case:** Testing alternative glyph mappings

---

## 📈 Metrics for Shadow Evaluation

### Compare Baseline vs Repaired

**Typed Cell Accuracy:**
```bash
# Extract typed cells and compare
diff <(jq '.services[].charged_amount' out/eob_baseline/*.json) \
     <(jq '.services[].charged_amount' out/eob_repaired/*.json)
```

**Repair Summary Analysis:**
```bash
# Aggregate repair metrics
jq '.repair_summary' out/eob_repaired/*.repair_summary.json | jq -s '
{
  total_money_fixed: map(.money_fixed) | add,
  total_date_fixed: map(.date_fixed) | add,
  total_code_fixed: map(.code_fixed) | add,
  repair_rules: map(.repair_rules_hist) | add
}
'
```

**Conservation Pre-Check:**
```bash
# Compare conservation rates (CP8 integration point)
# This will show improvement when repairs fix validation errors
jq '.services[] | select(.charged_amount and .allowed_amount)' out/eob_*.json | wc -l
```

---

## ✅ Validation Summary

| Criterion | Result | Notes |
|-----------|--------|-------|
| **Flag Default** | ✅ OFF | No breaking changes |
| **Graceful Fallback** | ✅ PASS | Works without lexical_repair module |
| **Field Coverage** | ✅ PASS | money/date/code repaired, TEXT immutable |
| **Repair Timing** | ✅ PASS | Applied BEFORE conservation |
| **Schema Neutral** | ✅ PASS | Main EOB schema unchanged |
| **Sidecar Output** | ✅ PASS | `.repair_summary.json` only when flag is True |
| **Provenance** | ✅ PASS | Preserved through repair process |
| **Determinism** | ✅ PASS | Same confusion maps → same repairs |
| **CP6/CP7 Gates** | ✅ PASS | mapper-verify (0.83), repair-verify (1.00) |

---

## 🔧 Implementation Quality

**Code Organization:**
- ✅ Clean separation: `apply_lexical_repair()` is self-contained
- ✅ Type hints throughout
- ✅ Optional parameters with sensible defaults
- ✅ Comprehensive docstrings

**Error Handling:**
- ✅ Graceful import fallback
- ✅ Runtime error only when user explicitly requests unavailable feature
- ✅ Per-field try/except in repair functions (from lexical_repair.py)

**Observability:**
- ✅ Repair notes logged per field
- ✅ Summary counters incremented
- ✅ Rule histogram for analytics
- ✅ notes_count for quick assessment

**Maintainability:**
- ✅ Schema-neutral (no EOB schema changes)
- ✅ Flag-based (easy to enable/disable)
- ✅ Sidecar approach (analytics without schema coupling)
- ✅ Backward compatible

---

## 🚀 Next Steps

### Immediate

1. **Shadow Evaluation:**
   - Run baseline: `make assemble` (no repairs)
   - Run with repairs: `python scripts/assemble_eob.py ... --apply-repair`
   - Compare metrics:
     - `typed_cell_accuracy` (should improve)
     - `rows_incomplete` (should decrease)
     - `conservation_precheck` (foundation for CP8)

2. **Validate Repair Quality:**
   - Inspect `.repair_summary.json` files
   - Review `repair_rules_hist` for common patterns
   - Check for unexpected TEXT mutations (should be 0)

### CP8 Integration Path

**Current State:**
- ✅ Repairs applied BEFORE conservation
- ✅ Repaired values flow into `money_conservation_pass()`
- ⏭️ CP8 will use repaired values for conservation checks

**CP8 Implementation:**
```python
# In money_conservation_pass()
for service in services:
    charged = Decimal(service.get("charged_amount", "0.00"))
    allowed = Decimal(service.get("allowed_amount", "0.00"))
    paid = Decimal(service.get("insurance_paid", "0.00"))
    patient = Decimal(service.get("patient_responsibility", "0.00"))

    # CP8: Conservation check using repaired values
    conservation_error = abs(charged - allowed - paid - patient)
    if conservation_error > Decimal("0.01"):
        # Mark incomplete, flag for retry
        service["incomplete_reason"] = "conservation_fail"
        service["conservation_error"] = str(conservation_error)
```

**Integration Point:**
- File: `scripts/assemble_eob.py:419` (`money_conservation_pass()`)
- Repair values already available
- Conservation logic can assume cleaned inputs

---

## ✅ CP7 Integration Status: COMPLETE

**All acceptance criteria met:**
- ✅ `--apply-repair` flag implemented (default OFF)
- ✅ Repairs applied to money/date/code (TEXT immutable)
- ✅ Integration point BEFORE conservation
- ✅ Schema-neutral sidecar output
- ✅ Graceful fallback without module
- ✅ CP6/CP7 verifiers still passing (0.83, 1.00)
- ✅ No breaking changes to existing workflows

**Impact:**
- Opt-in repair for shadow evaluation
- Foundation for CP8 money conservation
- Analytics via `.repair_summary.json` sidecars

**Ready for:**
- Shadow evaluation on production corpus
- CP8 money conservation integration

---

**Verification Date:** 2025-10-23
**Verified By:** CONTRACTS (Claude Code Assistant)
**Next Checkpoint:** CP8 (Money Conservation + Row Retries)
