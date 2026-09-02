# Brief — 2025-11-06: Diagnostic Quality Survey (CP12)

## Mission

Complete quality landscape analysis from extraction sidecars to establish baseline metrics and inform CP12-14 optimization:
- Inventory all extraction artifacts in `out/` directories (doc-level rollup sidecars only)
- Measure schema compliance across doc-level sidecars: `extraction.map_metrics.v1.json`, `assign.proof.v1.json`, `selection.evidence.v1.json`, `table.ccert.v1.json`, `evidence.v1.json`
- Compute conservation, certification, map_completeness, role_coverage baselines per payer/document
- Analyze applied_for_extraction rates, selection chosen rates, anchors_per_row mode
- Catalog failure patterns and prioritize gaps by impact × feasibility
- **Extend** existing `scripts/tools/payer_quality_map.py` with new metrics (do not create new tool)

## Invariants (never break)

- **Elegance > Expediency**: Deterministic measurement only; no inference or estimation
- **No Schema Changes**: `page v1.2`, `run v1.2`, `DentalEOBSchema.v1.2` remain untouched. Analysis via sidecars only
- **No Assembler/Solver/Policy Edits**: Orchestrator team does NOT edit `scripts/assemble_eob.py`, `solver_laws.py`, `selection_policy.py`, or any schema files
- **Idempotency**: Survey produces `*_sha256` hash fingerprints; repeat runs yield identical metrics
- **All Rejections Logged**: Missing files, malformed JSON, incomplete sidecars logged with reason codes
- **Byte Stability**: Re-running survey commands produces identical SHA-256 hashes of report outputs
- **Contracts AJV-gated**: Reports validated against existing schemas; no new schemas created

## State to inspect before coding

**Out directories (extraction outputs with doc-level sidecars):**
- `out/eob/` - baseline 3-document test set
- `out/eob_accuracy_spatial/`, `out/eob_accuracy_spatial_cp12/` - CP12 spatial projection results
- `out/eremit_doc*/` - EREMIT 60pg document-mode extractions (multiple variants)
- `out/holdout/` - held-out validation set
- `out/vlm_e2e*/`, `out/vlm_h*/` - VLM-enhanced extractions

**Required doc-level sidecars per document:**
- `<doc>.json` - Main EOB with services[] array
- `<doc>.extraction.map_metrics.v1.json` - Column assignment metrics (role_coverage, map_completeness, anchors_per_row)
- `<doc>.assign.proof.v1.json` - Column assignment proof (applied_for_extraction, extraction_variant, column_map, ILP alternatives)
- `<doc>.selection.evidence.v1.json` - Selection policy decision (chosen variant, mapper_header_match, conservation samples)
- `<doc>.table.ccert.v1.json` - Table-level certification (certified_rows, conservation, row_locked_pct)
- `<doc>.evidence.v1.json` - Evidence plane (typedness, swap_energy, quantization.hash)

**What to verify in advance:**
1. At least 3 complete extraction runs exist (main EOB JSON + all 5 sidecars above)
2. Schemas exist for validation: `schemas/extraction.map_metrics.v1.json`, `schemas/assign.proof.v1.json`, `schemas/selection.evidence.v1.json`, `schemas/table.ccert.v1.json`, `schemas/evidence.v1.json`
3. `scripts/verify_checkpoint.py` passes on existing outputs
4. `scripts/tools/payer_quality_map.py` exists and runs successfully on holdout aggregate

## Expected KPIs & Validation

**1. Extend payer_quality_map.py with New Metrics**
```bash
# Extend existing tool (do NOT create new script)
# Add to PayerRow dataclass:
#   - map_completeness_mean: float
#   - map_completeness_distribution: List[int]  # histogram [0,1,2,3,4,5,6+]
#   - anchors_per_row_mode: float
#   - role_coverage_mean: float  # fraction of target roles successfully mapped
#   - applied_for_extraction_rate: float  # % tables where apply_evidence.gate.passed=true
#   - selection_chosen_mapper_rate: float  # % docs where selection chose "mapper"
#   - selection_chosen_him_rate: float  # % docs where selection chose "him"
#   - certified_rows_mean: float  # already have certified_rows from table.ccert
#   - summary_residual_mean: float  # if summary_box.v1.json exists, residuals

# Run extended tool
python3 scripts/tools/payer_quality_map.py \
  --out-dirs out/eob out/eremit_doc_final out/holdout \
  --report SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json

# Verify new metrics present
jq '.payers[] | {payer, map_completeness_mean, role_coverage_mean, applied_for_extraction_rate}' \
  SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json
```

**2. Schema Compliance Rate (Doc-Level Sidecars)**
```bash
# Check sidecar validation pass rate
jq '.schema_compliance.pass_rate' SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json
# Expected: ≥0.95 (95% of sidecars validate against schemas)
# Failure modes: missing sidecars, malformed JSON, schema violations
```

**3. Conservation & Certification Baselines Per Payer**
```bash
# Inspect conservation + certified_rows metrics
jq '.payers[] | {payer, conservation_mean, certified_rows_mean, docs_analyzed}' \
  SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json
# Expected:
#   - EREMIT: conservation_mean ∈ [0.15, 0.25], certified_rows_mean ≥ 5
#   - DMBA: conservation_mean ∈ [0.35, 0.50], certified_rows_mean ≥ 10
#   - common: conservation_mean ∈ [0.20, 0.40], certified_rows_mean ≥ 8
```

**4. Map Completeness & Role Coverage**
```bash
# Check column assignment quality
jq '.payers[] | {payer, map_completeness_mean, role_coverage_mean}' \
  SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json
# Expected:
#   - map_completeness_mean ≥ 3.0 (average roles mapped per document)
#   - role_coverage_mean ≥ 0.60 (60%+ of target roles successfully assigned)
```

**5. Applied For Extraction & Selection Rates**
```bash
# Check gate pass rates and selection decisions
jq '.payers[] | {payer, applied_for_extraction_rate, selection_chosen_mapper_rate, selection_chosen_him_rate}' \
  SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json
# Expected:
#   - applied_for_extraction_rate ≥ 0.70 (70%+ tables pass admission gate)
#   - selection rates sum to ~1.0 (mapper + him + unknown)
```

**6. Byte Stability of Survey Output**
```bash
# Run extended tool twice, compare hashes
python3 scripts/tools/payer_quality_map.py [same args] \
  --report /tmp/survey_rerun.json

shasum -a 256 SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json /tmp/survey_rerun.json
# Expected: identical SHA-256 hashes
```

## Procedure

### Phase 1: Extend payer_quality_map.py Data Model
1. Add new fields to `PayerRow` dataclass:
   - `map_completeness_mean`, `map_completeness_distribution`
   - `anchors_per_row_mode`, `role_coverage_mean`
   - `applied_for_extraction_rate`
   - `selection_chosen_mapper_rate`, `selection_chosen_him_rate`
   - `certified_rows_mean` (enhance existing)
   - `summary_residual_mean` (if summary_box sidecars exist)
2. Update `compute_roi()` to incorporate new metrics (optional ROI reweight)

### Phase 2: Scan Out Directories for Sidecars
1. Recursively scan specified `--out-dirs` for main EOB JSON files
2. For each `<doc>.json`, check for presence of 5 required sidecars:
   - `<doc>.extraction.map_metrics.v1.json`
   - `<doc>.assign.proof.v1.json`
   - `<doc>.selection.evidence.v1.json`
   - `<doc>.table.ccert.v1.json`
   - `<doc>.evidence.v1.json`
3. Log missing sidecars as "incomplete extraction"
4. Group documents by payer_name from main JSON

### Phase 3: Extract Metrics from Sidecars (Per Document)
For each complete extraction (main + 5 sidecars):
1. **From map_metrics.v1.json**: `map_completeness`, `role_coverage`, `anchors_per_row` histogram
2. **From assign.proof.v1.json**: `applied_for_extraction` (gate.passed), `extraction_variant`, `column_map` size
3. **From selection.evidence.v1.json**: `chosen` variant (mapper/him/unknown), `mapper_header_match`, conservation samples
4. **From table.ccert.v1.json**: `certified_rows`, `conservation`, `row_locked_pct`
5. **From evidence.v1.json**: `quantization.hash`, `swap_energy` (for audit trail only)
6. **From main JSON**: `services` count, payer_name, document_id

### Phase 4: Aggregate Per-Payer Metrics
1. Group extracted doc-level metrics by payer
2. Compute means: `conservation_mean`, `certified_rows_mean`, `map_completeness_mean`, `role_coverage_mean`
3. Compute rates: `applied_for_extraction_rate`, `selection_chosen_mapper_rate`, `selection_chosen_him_rate`
4. Compute distributions: `map_completeness_distribution` histogram [0,1,2,3,4,5,6+]
5. Compute mode: `anchors_per_row_mode` (most common anchors count)

### Phase 5: Schema Validation & Failure Logging
1. Use AJV to validate each sidecar against its schema
2. Track pass/fail rates per schema type
3. Log validation errors with file path, schema name, error message
4. Classify failure modes: missing_sidecars, schema_invalid, ilp_gaps, empty_services

### Phase 6: Report Generation
1. Write `survey_report.json` with payer-level rows (extend existing payer_quality_map format)
2. Generate `quality_landscape.md` with insights: top/bottom payers, gap analysis
3. Produce `gap_matrix.csv` with impact × feasibility prioritization
4. Append to `SESSION/BuildLog.md` with command, artifacts, KPIs, byte-stability hash

## Artifacts

**Extended payer_quality_map.py Tool:**
- `scripts/tools/payer_quality_map.py` (extended, not new file)
  - Updated `PayerRow` dataclass with 8+ new metrics
  - Updated `compute_roi()` if needed
  - Backward compatible with existing holdout aggregate inputs

**Machine-Readable Report:**
- `SESSION/AuditLogs/2025-11-06-diagnostic-survey/survey_report.json`
  - Extends existing payer_quality_map.v1 format
  - Contains: per-payer rows with new metrics (map_completeness, role_coverage, applied_for_extraction, selection rates, certified_rows, anchors mode)
  - Schema compliance stats, failure pattern counts

**Human-Readable Summary:**
- `SESSION/AuditLogs/2025-11-06-diagnostic-survey/quality_landscape.md`
  - Executive summary with key findings from new metrics
  - Top 3 / bottom 3 payers by ROI
  - Payer-specific conservation, map_completeness, role_coverage baselines
  - Correlation insights (e.g., applied_for_extraction vs conservation)

**Gap Prioritization Matrix:**
- `SESSION/AuditLogs/2025-11-06-diagnostic-survey/gap_matrix.csv`
  - Impact × Feasibility grid for improvement roadmap
  - Columns: gap_type, impact_score, feasibility_score, roi_rank
  - Examples: "low_map_completeness", "missing_role_coverage", "failed_admission_gate"

**BuildLog Entry:**
- Append to `SESSION/BuildLog.md`:
  - Command: `python3 scripts/tools/payer_quality_map.py --out-dirs ... --report ...`
  - Artifacts: paths to survey_report.json, quality_landscape.md, gap_matrix.csv
  - KPIs: schema compliance %, conservation_mean, map_completeness_mean, applied_for_extraction_rate
  - Byte stability: SHA-256 hashes of survey_report.json (2 runs)

## CP12 Progress — 2025-11-06

**Checkpoint Context:**
- **CP12: Spatial Projection & Selector** - Deterministic spatial-only cell merging with roi_type='table' gate
- This diagnostic survey leverages NEW doc-level rollup sidecars to establish baseline quality across CP12+ outputs

**Current State:**
- Multiple extraction runs exist across `out/eob*`, `out/eremit*`, `out/vlm*`, `out/holdout` directories
- New sidecars emitted: `extraction.map_metrics.v1.json`, `assign.proof.v1.json`, `selection.evidence.v1.json`, `table.ccert.v1.json`, `evidence.v1.json`
- Existing `payer_quality_map.py` tool reads holdout aggregate but lacks new metrics
- No systematic analysis of map_completeness, role_coverage, applied_for_extraction rates across payers

**Survey Goals:**
1. **Extend payer_quality_map.py** to consume new doc-level sidecars (not just aggregate)
2. Measure map_completeness (mean, distribution), role_coverage, anchors_per_row mode across payers
3. Quantify applied_for_extraction rates (gate pass rates) and selection chosen rates (mapper vs HIM)
4. Correlate new metrics with existing: conservation, certified_rows, p95 latency
5. Feed findings into CP13 (Micro-OCR) and CP14 (Payer Profiles) design decisions

**Success Criteria for This Brief:**
- `payer_quality_map.py` extended with 8+ new metrics (backward compatible)
- Tool runs successfully on `out/eob`, `out/eremit_doc_final`, `out/holdout` directories
- `survey_report.json` produced with payer-level rows including all new metrics
- Schema compliance ≥95% across all sidecars
- `quality_landscape.md` identifies top 3 / bottom 3 payers by ROI
- `gap_matrix.csv` ranks improvement opportunities by impact × feasibility
- Byte-stable reruns (identical SHA-256 hashes)
- BuildLog entry appended with command, artifacts, KPIs

**Next Steps After Survey:**
1. Review quality_landscape.md for payer-specific insights (which payers benefit most from mapper-first?)
2. Use gap_matrix.csv to prioritize fixes (low map_completeness? failed admission gate?)
3. Update CP13/CP14 acceptance criteria based on measured baselines (e.g., require map_completeness ≥ 3)
4. Feed correlation insights into selection policy tuning (e.g., applied_for_extraction vs conservation)
