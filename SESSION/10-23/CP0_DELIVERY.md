# CP0 Delivery Summary

**Date:** 2025-10-23
**Status:** ✅ COMPLETE
**Owner:** CONTRACTS

---

## Deliverables

### ✅ Schemas (4)

All schemas use `draft/2020-12`, `additionalProperties: false`, and strict required fields.

1. **schemas/budget_policy.v1.json**
   - Budget classes: small (<36 cells), typical (≤120), large (>120)
   - Estimation sources: vision_lines, proj_peaks, default
   - Token budget calculation with payer/retry multipliers

2. **schemas/preproc.page.v1.json**
   - Rasterization metadata (DPI, colorspace, interpolation, cropbox)
   - Preprocessing ops (CLAHE, deskew, dewarp, padding, resize)
   - Vision gating medians and route decisions
   - ROI classifications (header, money, notes)

3. **schemas/vlm/header_pass.v1.json**
   - Request/response for header-only VLM pass
   - Hungarian matching with cost terms telemetry
   - Header lock with SHA256 binding to image

4. **schemas/vlm/data_pass.v1.json**
   - Request/response for data extraction with locked headers
   - Row-targeted retry support
   - Failed/incomplete row tracking

### ✅ Sample Payloads (6)

All samples validate successfully against their schemas:

- `samples/budget_policy.v1.sample.json`
- `samples/preproc.page.v1.sample.json`
- `samples/vlm.header_pass.v1.request.sample.json`
- `samples/vlm.header_pass.v1.response.sample.json`
- `samples/vlm.data_pass.v1.request.sample.json`
- `samples/vlm.data_pass.v1.response.sample.json`

### ✅ Makefile Target

**make schema-check-cp0**

Validates all 6 samples against their schemas. Exit code 0 on success.

```bash
$ make schema-check-cp0
=== CP0 Schema Validation ===
Validating budget_policy.v1 sample...
samples/budget_policy.v1.sample.json valid
Validating preproc.page.v1 sample...
samples/preproc.page.v1.sample.json valid
Validating vlm.header_pass.v1 request sample...
samples/vlm.header_pass.v1.request.sample.json valid
Validating vlm.header_pass.v1 response sample...
samples/vlm.header_pass.v1.response.sample.json valid
Validating vlm.data_pass.v1 request sample...
samples/vlm.data_pass.v1.request.sample.json valid
Validating vlm.data_pass.v1 response sample...
samples/vlm.data_pass.v1.response.sample.json valid
✅ All CP0 schemas valid
```

### ✅ Specification Documents (3)

**docs/hungarian_matching.md**
- Complete algorithm specification with normalization, cost function, and tie-breaks
- 6 test fixtures with expected outputs:
  1. Perfect match
  2. Synonym match
  3. Column order swapped
  4. Spelling error
  5. Extra detected header
  6. Dense table (10 columns)
- Pseudocode and scipy integration example
- Validation instructions

**docs/roi_classification.md**
- Deterministic rules: header → money → notes → default
- Feature computation: H_pos, H_lex, M_money, M_codes, N_foot, cy
- 6 test cases with computed features and expected classifications
- Decision tree visualization
- Scheduler integration example

**docs/confusion_maps.md**
- Per-type confusion maps: CODE, MONEY, DATE, ID, TEXT
- Application algorithm with post-repair validation
- Example end-to-end repair with logging
- 6 test cases (repair, preservation, rejection)
- Normalization pre-step (uppercase for CODE/ID)

### ✅ Gold Set Scaffold

**bench/gold_set_v1/**

- `manifest.json`: 10-doc structure with strata (3 simple, 4 typical, 2 complex, 1 fax)
- `README.md`: Acquisition instructions, annotation procedure, CI integration
- `eob_0001/truth.json`: Template ground truth format

**Strata definitions:**
- **simple**: Single-page, <20 rows, high DPI, SLO: conservation ≥99%, TEDS ≥98%
- **typical**: Multi-page, 20-60 rows, SLO: conservation ≥99%, TEDS ≥96%
- **complex**: >60 rows, dense, SLO: conservation ≥99%, TEDS ≥94%
- **fax**: ≤150 DPI, dithered, SLO: conservation ≥85%, TEDS ≥90%

**Status:** Awaiting PDF acquisition and truth annotation (deferred to IMPL owner).

---

## Acceptance Criteria

- [x] AJV validates 1-2 example payloads per schema
- [x] Samples checked into repo under `samples/`
- [x] All schemas use `additionalProperties: false`
- [x] Required fields explicitly listed per contract
- [x] Makefile target `schema-check-cp0` passes
- [x] Specs include test fixtures and validation instructions
- [x] Gold set has manifest, README, and truth template

---

## Verification

Run the following to verify CP0 delivery:

```bash
# Schema validation (exit 0 = success)
make schema-check-cp0

# Check artifacts exist
ls -1 schemas/{budget_policy.v1.json,preproc.page.v1.json,vlm/{header_pass,data_pass}.v1.json}
ls -1 samples/*.sample.json
ls -1 docs/{hungarian_matching,roi_classification,confusion_maps}.md
ls -1 bench/gold_set_v1/{manifest.json,README.md,eob_0001/truth.json}
```

---

## Next Steps (Awaiting CP1-CP3)

After IMPL completes CP1 (Preproc), CP2 (ROI Export), CP3 (BudgetPolicy):

1. **Validate sidecars:** Run `ajv validate -s schemas/preproc.page.v1.json -d runs/*/preproc.page.*.json`
2. **Check budget fields:** Inspect `runs/vlm/*_request.json` for `budget.{class,est_*}`
3. **Verify determinism:** Run twice, diff outputs → must be identical
4. **Check hash binding:** Verify `header_lock_sha256` in data response matches header response

---

## Files Modified/Created

**Created (16 files):**
```
schemas/budget_policy.v1.json
schemas/preproc.page.v1.json
schemas/vlm/header_pass.v1.json
schemas/vlm/data_pass.v1.json

samples/budget_policy.v1.sample.json
samples/preproc.page.v1.sample.json
samples/vlm.header_pass.v1.request.sample.json
samples/vlm.header_pass.v1.response.sample.json
samples/vlm.data_pass.v1.request.sample.json
samples/vlm.data_pass.v1.response.sample.json

docs/hungarian_matching.md
docs/roi_classification.md
docs/confusion_maps.md

bench/gold_set_v1/manifest.json
bench/gold_set_v1/README.md
bench/gold_set_v1/eob_0001/truth.json
```

**Modified (2 files):**
```
Makefile (added schema-check-cp0 target)
SESSION/10-23/Acceptance.md (CP0 status → complete)
```

---

## Sign-off

**Deliverable quality:**
- All schemas validate with AJV
- Sample payloads are realistic and comprehensive
- Specs include executable test cases
- Gold set scaffold ready for PDF acquisition

**Blockers removed:**
- ✅ Schema contracts locked
- ✅ Dual-pass message flow defined
- ✅ Hungarian matching fully specified
- ✅ ROI classification deterministic
- ✅ Confusion maps scoped per-type

**Ready for:** IMPL to proceed with CP1-CP3 implementation and validation.

---

**CP0 COMPLETE. Standing by for CP1-CP3 verification.**
