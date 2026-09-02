# 📊 Extraction Visualization Guide

Units: All residuals/penalties are in cents; service values are in dollars.

## Quick Start

```bash
# Run extraction with all features
python3 scripts/assemble_eob.py \
  --pages bench/your_data \
  --mappings config/payers \
  --out out/demo \
  --solve-roles \
  --solver-locks-strict \
  --emit-evidence \
  --summary-reconcile \
  --budget-stability

# Visualize results
python3 scripts/visualize_extraction.py out/demo
python3 scripts/visualize_table_diagram.py out/demo
```

## 📖 Understanding the Output

### 1. Document Information
Shows metadata about the EOB being processed:
- **Document ID**: SHA-256 hash of canonical content
- **Claim #**: Identifier for this claim
- **Payer**: Insurance company name
- **Patient**: Patient identifier
- **Status**: Processing status (Pending/Complete)

### 2. Extracted Services Table
The core data extracted from the EOB:

```
Row  Code     Date         Charged      Allowed      Paid         Patient      Cert
────────────────────────────────────────────────────────────────────────────────────
1    D1110    01/20/2025   $  150.00    $  120.00    $  100.00    $   20.00    ✓
```

- **Row**: Service line number
- **Code**: Dental procedure code
- **Date**: Service date
- **Charged**: Amount billed by provider
- **Allowed**: Amount allowed by insurance
- **Paid**: Amount insurance paid
- **Patient**: Patient responsibility amount
- **Cert**: ✓ if row is certified and locked

### 3. Column Assignment Analysis
Shows how raw table columns map to semantic roles:

**CHOSEN MAPPING**: The extraction method's column-to-role assignments
```
Column 0 → procedure_code
Column 1 → service_date
Column 2 → charged_amount
Column 3 → allowed_amount
Column 4 → insurance_paid
Column 5 → patient_responsibility
```

**ILP ANALYSIS**: Alternative mappings discovered by solver
- **BEST**: Highest certified row count
- **RUNNER-UP**: Second-best alternative
- **Comparison**: Shows if ILP found better mapping than chosen extraction

### 4. Evidence Plane
Provides audit trail and quality metrics:

**Column Typedness**: Confidence that column contains specific data type
```
code:  col0=0.50
date:  col1=0.50
money: col2=0.50, col3=0.50, col4=0.50, col5=0.50
```

**Swap Energy**: Impact of swapping adjacent money columns
```
Swap columns 2 ↔ 3:  = 0 certified rows  (no change)
Swap columns 3 ↔ 4:  + 2 certified rows  (improvement)
```
- `=` means swap makes no difference
- `+` means swap would improve certification
- `-` means swap would reduce certification

**Quantization Hash**: Fingerprint for byte-stability verification

### 5. Solver Certification
Shows which rows pass law-based validation:

**Table-Level Metrics**:
- **Certified Rows**: Rows that satisfy a law with 0 violations
- **Row Locked %**: Percent of rows locked (when --solver-locks-strict)
- **Conservation**: Percent of rows with all 4 money fields populated
- **Inequality Violations**: Count of rows failing inequality checks

**Law Distribution**:
- **in_network**: Paid + Patient = Allowed (most common)
- **noncovered**: Paid = 0, Patient = Allowed (no coverage)
- **cob_secondary**: Patient = 0 (coordination of benefits)

**Violation Energy**: Distance from satisfying law (0 = perfect)

### 6. Performance Metrics
**Budget**:
- **OCR Calls per Page**: Number of Vision/VLM calls made
- **P95 Seconds/Page**: 95th percentile processing time

**Byte Stability**: Whether re-running produces identical output

### 7. Extraction Map Metrics (NEW in v1.2)
Document-level rollup showing extraction coverage and quality:

**Role Coverage**: Fraction of target semantic roles successfully mapped (0.0-1.0)
```
Role Coverage:       65.0% (fraction of target roles mapped)
```
- Higher is better (100% = all expected roles found)
- Low coverage may indicate missing columns or weak typedness

**Map Completeness**: Count of semantic roles successfully mapped per document
```
Map Completeness:    4 roles mapped
```
- Typical range: 3-6 roles (code, date, 4 money fields)
- Lower than expected = incomplete extraction

**Anchors Per Row**: Histogram showing anchor distribution across rows
```
Anchors Per Row (histogram): [0, 2, 5, 3, 1]
Mode:                2 anchors/row (most common)
```
- Index 0 = count of rows with 0 anchors
- Index 1 = count of rows with 1 anchor, etc.
- Mode indicates typical anchor density (higher = more structured table)

**When to use**: Check map_completeness and role_coverage when diagnosing low certification rates. Missing roles → incomplete conservation → failed certification.

### 8. Selection Evidence (NEW in v1.2)
Document-level record of extraction variant selection:

**Chosen Variant**: Which extraction method was selected
```
Chosen Variant:      MAPPER
```
- **MAPPER**: Header-matched extraction (structured table with headers)
- **HIM**: Header-Independent Method (headerless or low header match)
- **UNKNOWN**: Fallback (no extraction succeeded)

**Mapper Header Match**: Confidence in header alignment (if mapper chosen)
```
Mapper Header Match: 72.5%
```
- Only shown when variant = MAPPER
- Higher is better (>60% = strong header alignment)
- Low match may trigger HIM precedence via delta threshold

**Conservation Samples**: Per-table conservation rates used in selection
```
Conservation Samples (3 tables):
     table_0: 85.0%
     table_1: 90.0%
     table_2: 80.0%
```
- Shows conservation rate (% rows with all 4 money fields) for each table
- Used in selection policy: HIM needs conservation advantage (delta=0.10) over mapper

**When to use**: If extraction quality is poor, check if wrong variant was chosen. Low mapper_header_match → should use HIM. HIM chosen but low conservation → may need VLM fallback.

Policy note: `--summary-penalty` is telemetry-only; only the canary apply preset (`--summary-penalty-apply`) affects energy/selection.

---

## 🎯 Key Concepts Explained

### What is "Certification"?
A row is **certified** when it satisfies one of three laws with zero violation energy:

1. **in_network**: `paid + patient = allowed` (within rounding tolerance)
2. **noncovered**: `paid = 0 AND patient = allowed`
3. **cob_secondary**: `patient = 0`

Plus all these inequalities must hold:
- `allowed ≤ charged`
- `paid ≤ allowed`
- `patient ≤ allowed`

### What is "Locking"?
With `--solver-locks-strict`, certified rows get `_row_locked: true`. This signals:
- High confidence in the data
- Row can be used for training/priors
- Extraction quality is production-ready

### What is "ILP-lite"?
ILP (Integer Linear Programming) enumerates alternative column assignments:
- Tries up to 360 permutations of top money-typed columns
- Evaluates each mapping using solver on sample rows
- Records **best** and **runner_up** alternatives
- Advisory only - doesn't change extraction (yet)

### What is "Swap Energy"?
Real solver-based measurement of column assignment quality:
- Swaps adjacent money columns (e.g., columns 2 ↔ 3)
- Re-runs solver on sample rows
- Records delta in certified row count
- Positive delta = swap improves quality

---

## 🔍 Reading the Table Diagram

```
Raw OCR Table:
┌──────────────────────────────────────────────┐
│ Col 0  │ Col 1  │ Col 2  │ Col 3  │ Col 4  │
├──────────────────────────────────────────────┤
│ D1110  │ 01/20  │ 150.00 │ 120.00 │ 100.00 │
└──────────────────────────────────────────────┘

Column → Role Mapping:
    ▲
  │ procedure_code
            ▲
          │ service_date
                    ▲
                  │ charged_amount
```

The arrows show how raw columns (numbered 0-N) map to semantic roles.

---

## 🚀 Usage Examples

### Example 1: Check Extraction Quality
```bash
python3 scripts/visualize_extraction.py out/my_extraction

# Look for:
# - Certified Rows > 0 (good quality)
# - Conservation = 100% (complete data)
# - Inequality Violations = 0 (valid data)
```

### Example 2: Compare Column Assignments
```bash
python3 scripts/visualize_table_diagram.py out/my_extraction

# Look for:
# - ILP BEST vs CHOSEN differences
# - Positive swap energy (improvement opportunities)
# - Column mapping alignment with expected layout
```

### Example 3: Debug Failed Certification
```bash
python3 scripts/visualize_extraction.py out/failed_case

# Check:
# - Which law was chosen (in_network, noncovered, cob_secondary)
# - Violation energy value (how far from passing)
# - Inequality check failures (which constraint violated)
```

---

## 🎨 Color Legend (Terminal Output)

- **✓**: Success / Certified / Locked
- **✗**: Failure / Not certified / Unlocked
- **⚠**: Warning / Runner-up / Alternative
- **🔒**: Locked row (--solver-locks-strict)
- **🔓**: Unlocked row
- **═**: Major section separator
- **─**: Minor section separator

---

## 📝 Files Generated

After running extraction with all flags:

```
out/demo/
├── document.json                              # Main EOB data
├── document.extraction.map_metrics.v1.json    # NEW: Role coverage, map completeness
├── document.selection.evidence.v1.json        # NEW: Variant selection evidence
├── document.assign.proof.v1.json              # Column assignment proof
├── document.evidence.v1.json                  # Evidence plane with swap energy
├── document.row.ccert.v1.json                 # Row-level certifications
├── document.table.ccert.v1.json               # Table-level metrics
├── document.budget.v1.json                    # Performance metrics
├── document.stability.v1.json                 # Byte-stability contract
└── document.summary_box.v1.json               # Summary totals reconciliation
```

---

## 🔧 Troubleshooting

### "No services extracted"
- Check input NDJSON has tables with cells
- Verify mappings YAML exists for payer
- Try --him-aggressive flag for headerless tables

### "0 certified rows"
- Check conservation % (need all 4 money fields)
- Look at violation energy (how close to passing)
- Inspect inequality checks (which constraint fails)

### "ILP shows better mapping"
- ILP found alternative with more certified rows
- Consider using --prefer-ilp-best (future flag)
- May indicate header matching failed

### "Swap energy shows improvements"
- Adjacent columns may be misaligned
- Run ILP-lite to find better permutation
- Check typedness scores for column confusion

---

## 🎓 Next Steps

1. **Understand your data**: Run visualizer on sample documents
2. **Tune selection policy**: Adjust latch/delta if needed
3. **Improve coverage**: Add missing fields to increase conservation
4. **Verify quality**: Ensure certified_rows > 0 on production data
5. **Monitor performance**: Track P95 times and budget costs

Happy extracting! 🚀
