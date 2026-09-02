# Confusion Maps Specification v1

## Overview

Per-column-type character substitution rules for lexical repair of VLM-extracted cells. Prevents OCR-style confusions (O↔0, l↔1, etc.) while preserving human text fidelity.

## Design Principle

**Apply confusion maps ONLY to constrained column types** (codes, money, dates, IDs). **Never mutate text columns** (patient names, provider names, descriptions).

## Confusion Map Definitions

Stored in `confusion_maps.v1.json`:

```json
{
  "CODE": {
    "O": "0",
    "I": "1",
    "L": "1",
    "l": "1",
    "S": "5",
    "B": "8",
    "Z": "2",
    "G": "6",
    "Q": "0",
    "D": "0"
  },
  "MONEY": {
    "O": "0",
    "o": "0",
    "S": "5",
    "l": "1",
    "I": "1"
  },
  "DATE": {},
  "ID": {
    "O": "0",
    "I": "1",
    "l": "1",
    "S": "5",
    "B": "8"
  },
  "TEXT": {}
}
```

### Rationale

- **CODE** (ADA/CPT): Most confusion-prone; codes are alphanumeric but heavily numeric-weighted
- **MONEY**: Numeric-only after parsing; minimal confusion set
- **DATE**: No confusions (strict format validation handles errors)
- **ID**: Similar to CODE but fewer false positives (member IDs, claim IDs)
- **TEXT**: Empty map; preserve as-is

## Column Type Assignment

From schema hint in VLM request:

```json
{
  "schema_hint": {
    "canonical_columns": ["proc_cd", "desc", "dos", "billed", "allowed", "paid", "patient"],
    "types": {
      "proc_cd": "code",
      "desc": "text",
      "dos": "date",
      "billed": "money",
      "allowed": "money",
      "paid": "money",
      "patient": "money"
    }
  }
}
```

If type not specified for a column, default:
- `*_cd`, `*_code`, `procedure`, `diagnosis` → **CODE**
- `*_id`, `member*`, `claim*`, `subscriber*` → **ID**
- `billed`, `allowed`, `paid`, `charged`, `adjust*`, `patient`, `copay`, `deductible` → **MONEY**
- `dos`, `date*`, `*_date` → **DATE**
- Everything else → **TEXT**

## Application Algorithm

```python
def apply_confusion_map(cell_value: str, column_type: str, confusion_maps: dict) -> str:
    """
    Apply confusion map to cell value based on column type

    Args:
        cell_value: Raw extracted cell text
        column_type: One of "CODE", "MONEY", "DATE", "ID", "TEXT"
        confusion_maps: Loaded from confusion_maps.v1.json

    Returns:
        Repaired cell value
    """
    if column_type not in confusion_maps:
        return cell_value  # No map defined, return as-is

    confusion_map = confusion_maps[column_type]

    if not confusion_map:  # Empty map (TEXT, DATE)
        return cell_value

    # Apply character-level substitutions
    repaired = ''.join(confusion_map.get(c, c) for c in cell_value)

    return repaired
```

## Post-Repair Validation

After applying confusion map, validate against column constraints:

### CODE Validation

```python
def validate_code(cell: str) -> bool:
    """Validate ADA/CPT code format"""
    # ADA: D + 4 digits
    if re.match(r'^D\d{4}$', cell):
        return True
    # CPT: 5 digits
    if re.match(r'^\d{5}$', cell):
        return True
    # Allow empty
    if cell == "":
        return True
    return False
```

**On failure:** Return empty string `""` and log `repair_notes: "code_validation_failed"`

### MONEY Validation

```python
def validate_money(cell: str) -> bool:
    """Validate money format (cents as integer string)"""
    # After parsing: should be integer string (cents)
    # e.g., "12000" for $120.00
    if re.match(r'^-?\d+$', cell):
        return True
    if cell == "":
        return True
    return False
```

**On failure:** Return `""` and log `repair_notes: "money_validation_failed"`

### DATE Validation

```python
def validate_date(cell: str) -> bool:
    """Validate ISO date format YYYY-MM-DD"""
    if re.match(r'^\d{4}-\d{2}-\d{2}$', cell):
        # Further validate with datetime.strptime if needed
        return True
    if cell == "":
        return True
    return False
```

**On failure:** Return `""` and log `repair_notes: "date_validation_failed"`

### ID Validation

```python
def validate_id(cell: str) -> bool:
    """Validate ID format (alphanumeric, 4-20 chars)"""
    if re.match(r'^[A-Z0-9]{4,20}$', cell):
        return True
    if cell == "":
        return True
    return False
```

**On failure:** Return `""` and log `repair_notes: "id_validation_failed"`

### TEXT Validation

No validation; preserve as-is. Never reject text cells.

## Pipeline Integration

```python
def repair_and_validate(cells: List[str],
                        canonical_order: List[str],
                        schema_types: dict) -> Tuple[List[str], List[str]]:
    """
    Apply confusion maps and validate all cells

    Returns:
        (repaired_cells, repair_notes)
    """
    confusion_maps = load_json("confusion_maps.v1.json")
    repaired_cells = []
    repair_notes = []

    for i, cell in enumerate(cells):
        col_idx = i % len(canonical_order)
        canonical = canonical_order[col_idx]
        col_type = schema_types.get(canonical, infer_type(canonical))

        # Apply confusion map
        repaired = apply_confusion_map(cell, col_type, confusion_maps)

        # Validate
        valid = validate_cell(repaired, col_type)

        if not valid:
            repair_notes.append(f"Row {i // len(canonical_order)}, col '{canonical}': validation failed")
            repaired = ""  # Reject invalid cell

        repaired_cells.append(repaired)

    return repaired_cells, repair_notes
```

## Example: End-to-End Repair

### Input

**VLM Response:**
```json
{
  "rows": 2,
  "cols": 4,
  "cells": ["D274O", "Crown", "2025-O6-01", "11OOOO", "D2750", "Crown", "2025-06-02", "120000"]
}
```

**Schema:**
```json
{
  "canonical_order": ["proc_cd", "desc", "dos", "billed"],
  "types": {
    "proc_cd": "code",
    "desc": "text",
    "dos": "date",
    "billed": "money"
  }
}
```

### Repair Process

1. **Row 0, Cell 0 (D274O)**: Type=CODE
   - Apply CODE map: `O → 0` → `D2740`
   - Validate: `D\d{4}` → ✅ Pass

2. **Row 0, Cell 1 (Crown)**: Type=TEXT
   - No map → `Crown`
   - No validation → ✅ Pass

3. **Row 0, Cell 2 (2025-O6-01)**: Type=DATE
   - No map (DATE map empty) → `2025-O6-01`
   - Validate: `\d{4}-\d{2}-\d{2}` → ❌ Fail (O not a digit)
   - Reject → `""`

4. **Row 0, Cell 3 (11OOOO)**: Type=MONEY
   - Apply MONEY map: `O → 0, O → 0, O → 0` → `110000`
   - Validate: `\d+` → ✅ Pass

5. **Row 1, Cell 0 (D2750)**: Type=CODE
   - No confusions → `D2750`
   - Validate: `D\d{4}` → ✅ Pass

6. **Row 1, Cell 1-3**: All pass similarly

### Output

```json
{
  "rows": 2,
  "cols": 4,
  "cells": ["D2740", "Crown", "", "110000", "D2750", "Crown", "2025-06-02", "120000"],
  "repair_notes": ["Row 0, col 'dos': date_validation_failed"],
  "incomplete_rows": [0]
}
```

## Test Cases

### Test 1: CODE Repair

**Input:** `"D274O"` (type=CODE)
**Expected:** `"D2740"` (O→0)

### Test 2: TEXT Preservation

**Input:** `"JOHNSON"` (type=TEXT)
**Expected:** `"JOHNSON"` (no change, even though contains O)

### Test 3: MONEY Repair

**Input:** `"12OOO"` (type=MONEY)
**Expected:** `"12000"` (O→0, o→0)

### Test 4: Invalid CODE Rejection

**Input:** `"D27XY"` (type=CODE)
**Repair:** `"D27XY"` (no confusions)
**Validation:** Fail (not `D\d{4}`)
**Output:** `""`

### Test 5: ID Repair

**Input:** `"MB1234O"` (type=ID)
**Expected:** `"MB12340"` (O→0)

### Test 6: Case Sensitivity

**Input:** `"d2740"` (type=CODE)
**Repair:** `"d2740"` (no lowercase D in map)
**Validation:** Fail (not `D\d{4}`)
**Output:** `""`
**Note:** Normalize to uppercase BEFORE applying map

## Normalization Pre-Step

Add uppercase normalization for CODE/ID columns:

```python
if col_type in ("CODE", "ID"):
    cell = cell.upper()  # Normalize before repair
```

## Schema Conformance

Repair notes conform to `vlm/data_pass.v1.json` response:

```json
{
  "failed_rows": [0],
  "incomplete_rows": [0]
}
```

## Test Suite

```bash
pytest scripts/test_lexical_repair.py -v

# Expected: 6/6 test cases pass
```

## Maintenance

When adding new payer-specific confusions:

1. Update `confusion_maps.v1.json`
2. Version bump if changing existing maps
3. Add test case to suite
4. Document in `unknown_lex.csv` if new patterns emerge

## Future Enhancements

- **Confidence-weighted repair**: Only apply if VLM confidence < 0.85
- **Lexicon validation**: Check codes against ADA/CPT master lists
- **Provider-specific rules**: E.g., specific payer uses non-standard code formats
