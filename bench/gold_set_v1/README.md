# Gold Set v1

## Overview

Immutable reference dataset for CI gates and non-regression testing. Contains 10 stratified EOB documents with ground truth annotations.

## Structure

```
gold_set_v1/
├── manifest.json              # Metadata and SLO targets
├── README.md                  # This file
├── eob_0001/
│   ├── doc.pdf               # Source PDF
│   └── truth.json            # Ground truth annotations
├── eob_0002/
│   ├── doc.pdf
│   └── truth.json
...
└── eob_0010/
    ├── doc.pdf
    └── truth.json
```

## Strata

- **simple** (3 docs): Single-page, <20 rows, high DPI, clean scans
- **typical** (4 docs): Multi-page, 20-60 rows, standard quality
- **complex** (2 docs): >60 rows, dense tables, small fonts
- **fax** (1 doc): ≤150 DPI, dithered, artifacts

## Ground Truth Format

Each `truth.json` contains:

```json
{
  "patient": "DOE, JANE",
  "provider": "SMITH DENTAL",
  "payer": "ACME PPO",
  "claims": [
    {
      "claim_id": "CLM123456",
      "rows": [
        {
          "proc_cd": "D2740",
          "desc": "Crown",
          "dos": "2025-06-01",
          "billed": 110000,
          "allowed": 80000,
          "paid": 20000,
          "adjust": -10000,
          "patient": 10000
        }
      ],
      "money_conservation": true
    }
  ],
  "totals": {
    "billed": 110000,
    "allowed": 80000,
    "paid": 20000,
    "adjust": -10000,
    "patient": 10000
  }
}
```

**Money format:** All amounts in **cents** (integer). E.g., $120.00 = `120000`.

**Date format:** ISO 8601 (`YYYY-MM-DD`).

**Codes:** Uppercase (ADA: `D####`, CPT: `#####`).

## Acquisition Instructions

### Option 1: Anonymized Real Data

1. Source 10 EOBs from partners/users (with consent)
2. Redact all PHI:
   - Patient names → `DOE, JANE` / `DOE, JOHN`
   - Member IDs → `MEM000001`, `MEM000002`
   - Provider names → `SMITH DENTAL`, `JONES MEDICAL`
3. Rasterize at target DPI, re-scan for fax stratum
4. Manually annotate `truth.json` (double-check math)

### Option 2: Synthetic Generation

1. Use template EOB generator (TBD: `scripts/generate_synthetic_eob.py`)
2. Render to PDF at specified DPI
3. Generate matching `truth.json` from template data
4. Validate: Pipeline processes synthetic → compare to source

### Option 3: Existing Test PDFs

1. Use PDFs from `eobs/tests/` if available
2. Manually create `truth.json` annotations
3. Compute SHA256: `shasum -a 256 doc.pdf > doc.pdf.sha256`
4. Update `manifest.json` with hashes

## Annotation Procedure

For each document:

1. **Visual inspection**: Open PDF, identify all claims and service lines
2. **Manual extraction**: Transcribe into `truth.json` template (see below)
3. **Money conservation check**: Verify `billed = allowed + paid + adjust + patient` (±$0.01) for each row
4. **Double-entry validation**: Two annotators independently create `truth.json`, compare diffs
5. **Hash lock**: Compute SHA256 and add to `manifest.json`

## Truth JSON Template

```json
{
  "patient": "DOE, JANE",
  "provider": "SMITH DENTAL",
  "payer": "ACME PPO",
  "member_id": "MEM123456",
  "claim_date": "2025-06-15",
  "claims": [
    {
      "claim_id": "CLM789012",
      "rows": [
        {
          "proc_cd": "D2740",
          "desc": "Porcelain Crown",
          "dos": "2025-06-01",
          "billed": 110000,
          "allowed": 80000,
          "paid": 20000,
          "adjust": -10000,
          "patient": 10000
        },
        {
          "proc_cd": "D1110",
          "desc": "Prophylaxis Adult",
          "dos": "2025-06-01",
          "billed": 8500,
          "allowed": 8500,
          "paid": 8500,
          "adjust": 0,
          "patient": 0
        }
      ],
      "money_conservation": true
    }
  ],
  "totals": {
    "billed": 118500,
    "allowed": 88500,
    "paid": 28500,
    "adjust": -10000,
    "patient": 10000
  },
  "notes": "Optional: any special considerations (e.g., partial row on page 2)"
}
```

## CI Integration

```bash
# Validate all gold set docs
make validate-gold-set

# Expected output:
# - Schema validation: 10/10 pass
# - Money conservation: ≥99% rows pass (simple/typical/complex), ≥85% (fax)
# - TEDS: ≥0.96 (typical), ≥0.90 (fax)
```

## Immutability

**Gold set v1 is frozen.** To add samples:

1. Create `gold_set_v2/` with new manifest
2. Increment version in manifest.json
3. Update CI to test both v1 (non-regression) and v2 (new features)

## Ownership

- **Acquisition**: TBD (IMPL owner sources PDFs)
- **Annotation**: TBD (manual or semi-automated with scripts/annotate_eob.py)
- **Validation**: CONTRACTS (schema conformance, math checks)

## Status

**Current:** Scaffold complete, awaiting PDF acquisition and truth annotation.

**Next steps:**
1. Source 10 PDFs (prioritize existing `eobs/tests/` if available)
2. Annotate `truth.json` for at least 3 simple strata docs
3. Run `make validate-gold-set` to establish baseline
4. Expand to full 10 docs before CP10 (CI gates)
