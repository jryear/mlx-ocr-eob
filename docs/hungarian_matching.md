# Hungarian Mapping (CP6)

This document specifies the deterministic header→schema mapping used to lock headers against canonical columns.

Cost Model (frozen for CP6)
- Normalization: NFC, uppercase, collapse multi-space, strip punctuation except `/` and `-`.
- Terms per (detected header h_i, canonical c_j):
  - lev_norm = Levenshtein(h_i, c_j) / max(|h_i|, |c_j|)
  - jacc_sim = Jaccard over whitespace tokens (bigrams if single-token)
  - pos_pen  = |i − j| / max(n−1, 1)
  - syn_bonus = 1.0 if h_i ∈ synonyms[c_j] (case-insensitive), else 0.0
- Total cost: 0.60·lev_norm + 0.20·(1 − jacc_sim) + 0.15·pos_pen − 0.25·syn_bonus
- Reject if cost ≥ 0.50; send canonical to `unmapped`.
- Tie-break: lower total → longer canonical → lex canonical → lex detected_norm.

Outputs
- mapping[]: {detected, canonical, detected_idx, expected_idx, cost, terms{lev_norm,jaccard,pos_pen,syn_bonus}, mapping_source, rank}
- unmapped[]: {canonical, expected_idx, reject_reason}

Fixtures (JSON)
- Place under `samples/mapper/` and evaluate with `scripts/header_mapper.py`.

## Fixtures

### 1. Perfect match + synonym
- canonical.json
```json
["dos","allowed","paid","patient"]
```
- detected.json
```json
["DATE OF SERVICE","ALLOWED","PAID","PATIENT"]
```
- synonyms.json
```json
{"DOS":["DATE OF SERVICE","DOS"]}
```

### 2. Swapped order
- canonical.json `["code","desc","dos","billed"]`
- detected.json `["BILLED","DATE OF SERVICE","DESC","CODE"]`

### 3. Spelling error
- canonical.json `["allowed","paid"]`
- detected.json `["ALLOWD","PAID"]`

### 4. Extra detected header
- canonical.json `["allowed","paid","patient"]`
- detected.json `["ALLOWED","PAID","FOOTER"]`

### 5. Mixed case + punctuation
- canonical.json `["proc_cd"]`
- detected.json `["Procedure / Code"]`

### 6. Dense 10-column header
- canonical.json `["col1","col2","col3","col4","col5","col6","col7","col8","col9","col10"]`
- detected.json `["COL10","COL9","COL8","COL7","COL6","COL5","COL4","COL3","COL2","COL1"]`

Run
```
python3 scripts/header_mapper.py --detected samples/mapper/<case>/detected.json \
  --canonical samples/mapper/<case>/canonical.json --synonyms samples/mapper/<case>/synonyms.json
```
