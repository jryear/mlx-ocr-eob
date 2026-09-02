# ROI Classification Specification v1

## Overview

Deterministic rules for classifying ROIs into **header**, **money**, or **notes** categories. This classification enables scheduler batching and priority routing.

## Input Features

For each ROI, compute the following features from Vision.Text output:

### Geometric Features

- **cy** (normalized centroid Y): `y_center / page_height` where y_center is the ROI's vertical center
  - Range: [0.0, 1.0]
  - 0.0 = top of page, 1.0 = bottom of page

### Lexical Features

- **H_pos**: Header position indicator
  - `1` if `cy ≤ 0.25` (top quarter of page)
  - `0` otherwise

- **H_lex**: Header keyword count
  - Count tokens matching header keywords: `{EOB, EXPLANATION, PROVIDER, SUBSCRIBER, MEMBER, CLAIM, CHECK, ERA}`
  - Case-insensitive matching
  - Example: "EXPLANATION OF BENEFITS" → H_lex = 2

- **M_money**: Money token count
  - Count tokens matching money patterns:
    - Contains `$` symbol, OR
    - Matches `\d{1,3}(,\d{3})*\.\d{2}` (e.g., "1,234.56")
  - Example: "$125.00 allowed, $25.00 paid" → M_money = 2

- **M_codes**: Medical code count
  - Count tokens matching ADA/CPT patterns:
    - `D\d{4}` (ADA codes: D2740, D1110, etc.)
    - `\d{5}` standalone (CPT codes: 99213, 90847, etc.)
  - Example: "D2740 99213 D1110" → M_codes = 3

- **N_foot**: Footer position indicator
  - `1` if `cy ≥ 0.75` (bottom quarter of page)
  - `0` otherwise

## Classification Algorithm

Apply rules in order; first match wins:

```python
def classify_roi(roi: ROI) -> str:
    """
    Classify ROI into header, money, or notes

    Args:
        roi: ROI with computed features {cy, H_pos, H_lex, M_money, M_codes, N_foot}

    Returns:
        "header" | "money" | "notes"
    """
    # Rule 1: Header indicators
    if (roi.H_pos == 1 and roi.H_lex >= 1) or (roi.H_lex >= 2):
        return "header"

    # Rule 2: Money table indicators
    if (roi.M_money >= 6) or (roi.M_money >= 3 and roi.M_codes >= 2):
        return "money"

    # Rule 3: Footer notes
    if roi.N_foot == 1:
        return "notes"

    # Rule 4: Default to money (bias toward tables)
    return "money"
```

## Decision Tree Visualization

```
                         START
                           |
          ┌────────────────┼────────────────┐
          │                                 │
    H_pos=1 && H_lex≥1              H_lex ≥ 2
      OR H_lex≥2                         ?
          │                                 │
         YES ─────→ HEADER          NO ───┘
                                           │
          ┌────────────────────────────────┘
          │
    M_money ≥ 6
      OR (M_money≥3 && M_codes≥2)
          │
         YES ─────→ MONEY
          │
         NO
          │
    N_foot = 1
          │
         YES ─────→ NOTES
          │
         NO ─────→ MONEY (default)
```

## Rationale

- **Header priority**: Headers appear at top and contain meta keywords; classify first to batch separately
- **Money table detection**: Require multiple money tokens OR money+codes to avoid false positives on single-value notes
- **Footer notes**: Low-confidence text at bottom (disclaimers, footnotes); route last
- **Default to money**: Bias toward table data since most ROIs are service line tables

## Logging

For each ROI, log:

```json
{
  "roi_id": "roi-000123",
  "classification": "money",
  "features": {
    "H_pos": 0,
    "H_lex": 0,
    "M_money": 8,
    "M_codes": 4,
    "N_foot": 0,
    "cy": 0.45
  },
  "rule_matched": "Rule 2: M_money >= 6"
}
```

Store in `preproc.page.v1.json` under `roi_classifications[]`.

## Test Cases

### Test Case 1: Header ROI

**Input:**
```json
{
  "roi_id": "roi-000001",
  "cy": 0.12,
  "tokens": ["EXPLANATION", "OF", "BENEFITS", "MEMBER", "ID", "ABC123"]
}
```

**Features:**
- H_pos = 1 (cy ≤ 0.25)
- H_lex = 3 (EXPLANATION, BENEFITS, MEMBER)
- M_money = 0
- M_codes = 0
- N_foot = 0

**Output:** `"header"` (Rule 1: H_pos=1 && H_lex≥1)

---

### Test Case 2: Money Table ROI

**Input:**
```json
{
  "roi_id": "roi-000002",
  "cy": 0.50,
  "tokens": ["D2740", "Crown", "2025-06-01", "$110.00", "$80.00", "$20.00", "$10.00", "D2750", "$120.00", "$90.00"]
}
```

**Features:**
- H_pos = 0
- H_lex = 0
- M_money = 5
- M_codes = 2 (D2740, D2750)
- N_foot = 0

**Output:** `"money"` (Rule 2: M_money≥3 && M_codes≥2)

---

### Test Case 3: Dense Money Table

**Input:**
```json
{
  "roi_id": "roi-000003",
  "cy": 0.40,
  "tokens": ["$100.00", "$80.00", "$20.00", "$0.00", "$110.00", "$90.00", "$20.00", "$5.00"]
}
```

**Features:**
- H_pos = 0
- H_lex = 0
- M_money = 8
- M_codes = 0
- N_foot = 0

**Output:** `"money"` (Rule 2: M_money ≥ 6)

---

### Test Case 4: Footer Notes

**Input:**
```json
{
  "roi_id": "roi-000004",
  "cy": 0.85,
  "tokens": ["Benefits", "paid", "according", "to", "terms", "of", "policy"]
}
```

**Features:**
- H_pos = 0
- H_lex = 0
- M_money = 0
- M_codes = 0
- N_foot = 1 (cy ≥ 0.75)

**Output:** `"notes"` (Rule 3: N_foot=1)

---

### Test Case 5: Ambiguous ROI (Default)

**Input:**
```json
{
  "roi_id": "roi-000005",
  "cy": 0.55,
  "tokens": ["Provider", "Name", "Phone", "Number"]
}
```

**Features:**
- H_pos = 0
- H_lex = 0 (PROVIDER not in keyword list; only matches as exact case-insensitive "PROVIDER")
- M_money = 0
- M_codes = 0
- N_foot = 0

**Output:** `"money"` (Rule 4: default)

---

### Test Case 6: Secondary Header

**Input:**
```json
{
  "roi_id": "roi-000006",
  "cy": 0.35,
  "tokens": ["CLAIM", "DETAILS", "SUBSCRIBER"]
}
```

**Features:**
- H_pos = 0 (cy > 0.25)
- H_lex = 2 (CLAIM, SUBSCRIBER)
- M_money = 0
- M_codes = 0
- N_foot = 0

**Output:** `"header"` (Rule 1: H_lex ≥ 2)

## Scheduler Integration

Scheduler uses classification to batch ROIs:

```python
# Priority order: headers → money tables → notes
header_rois = [r for r in page.roi_classifications if r.classification == "header"]
money_rois = [r for r in page.roi_classifications if r.classification == "money"]
notes_rois = [r for r in page.roi_classifications if r.classification == "notes"]

# Process headers first (lower budget, faster models)
for roi in header_rois:
    schedule_vlm(roi, budget_class="small", model="2B")

# Process money tables (main workload)
for roi in money_rois:
    schedule_vlm(roi, budget_class="typical", model="2B")

# Process notes last (optional, lower priority)
for roi in notes_rois:
    schedule_vlm(roi, budget_class="small", model="2B")
```

## Determinism Guarantees

- **Feature computation**: Tokenization is whitespace-split only; no NLP
- **Regex patterns**: Anchored and explicit; no locale dependencies
- **Rule ordering**: First-match-wins eliminates ambiguity
- **Default rule**: Ensures every ROI gets a classification

## Validation

Test suite at `scripts/test_roi_classification.py`:

```bash
pytest scripts/test_roi_classification.py -v

# Expected: 6/6 test cases pass
```

## Schema Conformance

Output conforms to `preproc.page.v1.json`:

```bash
ajv validate -s schemas/preproc.page.v1.json -d runs/test_page_preproc.json
```

## Future Enhancements

- **ML classifier**: Train on gold set, fall back to rules on low confidence
- **Payer-specific keywords**: Extend H_lex dictionary per payer
- **Column detection**: Use vertical projection to boost M_money confidence
