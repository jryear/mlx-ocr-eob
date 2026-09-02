# Brief — 2025-11-06: Field-Level Quality Analysis (CP14)

## Mission

Measure field-level extraction baseline from doc-level sidecars to establish discovery-mode metrics and inform CP14 payer profile design:
- Use `extraction.map_metrics.v1.json` (role_coverage, map_completeness, anchors histogram), `table.ccert.v1.json` (certified_rows, conservation), `selection.evidence.v1.json` (chosen variant)
- Quantify field population rates for money, procedure, date, provider, metadata categories
- Identify co-occurrence patterns and correlations (role_coverage vs conservation)
- Generate payer-specific profiles: applied_for_extraction rate, selection chosen rates, anchors_per_row mode
- Produce evidence-based improvement roadmap ranked by impact × feasibility

## Invariants (never break)

- **Elegance > Expediency**: Discovery mode only; measure baseline without imposing thresholds or failing on low coverage
- **No Schema Changes**: DentalEOBSchema.v1.2 untouched; analysis reads existing services[] arrays and sidecars without modification
- **No Assembler/Solver/Policy Edits**: Orchestrator team does NOT edit `scripts/assemble_eob.py`, `solver_laws.py`, `selection_policy.py`, or any schema files
- **Idempotency**: Field coverage reports produce `*_sha256` hash fingerprints; repeat runs yield identical metrics
- **All Gaps Logged**: Missing fields logged with document_id, payer, reason (no OCR, typedness too low, column not mapped, etc.)
- **Byte Stability**: Re-running analysis commands produces identical SHA-256 hashes of report outputs
- **Contracts AJV-gated**: Reports validated against existing schemas; no new schemas created

## State to inspect before coding

**Primary Analysis Targets (with NEW doc-level sidecars):**
- `out/eob/*.json` - Baseline 3-document test set
- `out/eremit_doc_final/*.json` - EREMIT 60pg final extraction
- `out/holdout/*.json` - Held-out validation set
- `out/eob_accuracy_spatial_cp12/*.json` - CP12 spatial projection results

**Required NEW sidecars per document:**
- `<doc>.extraction.map_metrics.v1.json` - role_coverage (which roles successfully mapped), map_completeness (count), anchors_per_row histogram
- `<doc>.assign.proof.v1.json` - applied_for_extraction (gate.passed), extraction_variant, column_map
- `<doc>.selection.evidence.v1.json` - chosen variant (mapper/him/unknown), mapper_header_match, conservation samples
- `<doc>.table.ccert.v1.json` - certified_rows, conservation, row_locked_pct
- `<doc>.evidence.v1.json` - typedness scores, swap_energy (for audit trail)

**What to verify in advance:**
1. At least 20 complete extraction runs exist (main EOB JSON + 5 sidecars above)
2. Services arrays contain mix of complete/incomplete rows
3. Payer distribution includes EREMIT, DMBA, common
4. New map_metrics sidecars exist (if missing, cannot measure role_coverage)

## Expected KPIs & Validation

**1. Role Coverage Baseline (from map_metrics)**
```bash
# Run field quality analysis (to be created)
python3 scripts/measure_field_coverage.py \
  --out-dirs out/eob out/eremit_doc_final out/holdout \
  --report SESSION/AuditLogs/2025-11-06-field-quality-analysis/field_coverage_report.csv

# Inspect role_coverage rates per payer
jq '.payers[] | {payer, role_coverage_mean, map_completeness_mean}' \
  SESSION/AuditLogs/2025-11-06-field-quality-analysis/baseline_summary.json
# Expected (discovery mode - no hard thresholds):
#   role_coverage_mean: 0.40-0.70 (fraction of target roles successfully mapped)
#   map_completeness_mean: 3.0-5.0 (average roles mapped per document)
```

**2. Conservation vs Role Coverage Correlation**
```bash
# Check if role_coverage predicts conservation success
jq '.correlations | {role_coverage_vs_conservation, role_coverage_vs_certified_rows}' \
  SESSION/AuditLogs/2025-11-06-field-quality-analysis/baseline_summary.json
# Expected:
#   role_coverage_vs_conservation: 0.60-0.80 (positive correlation)
#   role_coverage_vs_certified_rows: 0.50-0.75 (positive correlation)
```

**3. Payer-Specific Profiles (applied_for_extraction, selection rates, anchors mode)**
```bash
# Review per-payer metrics from map_metrics + selection.evidence + table.ccert
jq '.payers[] | {payer, applied_for_extraction_rate, selection_chosen_mapper_rate, anchors_per_row_mode}' \
  SESSION/AuditLogs/2025-11-06-field-quality-analysis/baseline_summary.json
# Expected variance:
#   - EREMIT: applied_for_extraction_rate ≥ 0.70, selection_chosen_mapper_rate ~0.50
#   - DMBA: applied_for_extraction_rate ≥ 0.80, selection_chosen_mapper_rate ~0.70
#   - common: Dependent on source document quality
```

**4. Gap Prioritization Matrix (impact × feasibility)**
```bash
# Inspect ranked improvement opportunities
cat SESSION/AuditLogs/2025-11-06-field-quality-analysis/gap_matrix.csv
# Expected format:
#   gap_type,impact_score,feasibility_score,roi_rank
#   low_role_coverage,85,7,1  # High impact, high feasibility
#   missing_map_completeness,72,6,2
#   failed_admission_gate,65,5,3
```

**5. Field Population Rates (from services[] arrays)**
```bash
# Check actual field presence in services
jq '.field_population | {money_fields, procedure_code, service_date}' \
  SESSION/AuditLogs/2025-11-06-field-quality-analysis/baseline_summary.json
# Expected (discovery mode):
#   money_fields: 0.40-0.60 (40-60% of services have all 4 money fields)
#   procedure_code: 0.50-0.70
#   service_date: 0.30-0.50
```

**6. Byte Stability of Analysis Output**
```bash
# Run analysis twice, compare hashes
python3 scripts/measure_field_coverage.py [same args] \
  --report /tmp/field_coverage_rerun.csv

shasum -a 256 SESSION/AuditLogs/2025-11-06-field-quality-analysis/field_coverage_report.csv \
  /tmp/field_coverage_rerun.csv
# Expected: identical SHA-256 hashes
```

## Procedure

### Phase 1: Load Doc-Level Sidecars
For each document in target directories:
1. Load `<doc>.extraction.map_metrics.v1.json` → extract `role_coverage`, `map_completeness`, `anchors_per_row` histogram
2. Load `<doc>.assign.proof.v1.json` → extract `applied_for_extraction` (gate.passed), `extraction_variant`
3. Load `<doc>.selection.evidence.v1.json` → extract `chosen` variant (mapper/him/unknown)
4. Load `<doc>.table.ccert.v1.json` → extract `certified_rows`, `conservation`
5. Load main `<doc>.json` → extract `services[]` for field population rates, payer_name

### Phase 2: Aggregate Per-Payer Metrics
Group documents by payer_name, compute:
1. **From map_metrics**: `role_coverage_mean`, `map_completeness_mean`, `anchors_per_row_mode` (most common)
2. **From assign.proof**: `applied_for_extraction_rate` (% tables with gate.passed=true)
3. **From selection.evidence**: `selection_chosen_mapper_rate`, `selection_chosen_him_rate`
4. **From table.ccert**: `certified_rows_mean`, `conservation_mean`
5. **From services[]**: Field population rates (money, procedure, date, provider, metadata)

### Phase 3: Correlation Analysis
Compute correlations between key metrics:
1. `role_coverage` vs `conservation` (does better role mapping → higher conservation?)
2. `role_coverage` vs `certified_rows` (does better role mapping → more certified rows?)
3. `applied_for_extraction` vs `conservation` (does passing admission gate predict quality?)
4. `map_completeness` vs `certified_rows` (more roles → more certification?)

### Phase 4: Gap Prioritization & Report Generation
**CSV Report (`field_coverage_report.csv`):**
- Row per document with columns: doc_id, payer, money_coverage, procedure_coverage, date_coverage, provider_coverage, metadata_coverage, certified_rows

**JSON Summary (`baseline_summary.json`):**
- Overall coverage rates per category
- Co-occurrence matrix
- Payer-specific profiles
- Certification correlation stats

**Markdown Roadmap (`improvement_roadmap.md`):**
- Executive summary
- Top 10 highest-ROI gaps with actionable recommendations
- Payer-specific optimization strategies

**Gap Matrix (`gap_matrix.csv`):**
- Field, impact_score, feasibility_score, roi_rank
- Sortable for priority queue

## Artifacts

**Machine-Readable Report:**
- `SESSION/AuditLogs/2025-11-06-field-quality-analysis/field_coverage_report.csv`
  - One row per document with coverage rates for each field category
  - Columns: doc_id, payer, money_coverage, procedure_coverage, date_coverage, provider_coverage, metadata_coverage, certified_rows, row_count

**Baseline Summary:**
- `SESSION/AuditLogs/2025-11-06-field-quality-analysis/baseline_summary.json`
  - Contains: role_coverage trends, map_completeness distributions, applied_for_extraction correlations with conservation, payer profiles (selection rates, anchors mode), field population rates

**Gap Prioritization Matrix:**
- `SESSION/AuditLogs/2025-11-06-field-quality-analysis/gap_matrix.csv`
  - Ranked list of improvement opportunities
  - Columns: field, impact_score, feasibility_score, roi_rank
  - Sortable by ROI for priority queue

**Improvement Roadmap:**
- `SESSION/AuditLogs/2025-11-06-field-quality-analysis/improvement_roadmap.md`
  - Human-readable executive summary
  - Top 10 gaps with actionable recommendations
  - Payer-specific optimization strategies (e.g., "EREMIT: prioritize procedure code extraction via VLM")

## CP14 Progress — 2025-11-06

**Checkpoint Context:**
- **CP14: Payer Profiles** - Payer-specific configuration files informed by role_coverage, map_completeness, applied_for_extraction rates
- This field quality analysis leverages NEW doc-level sidecars to measure baseline metrics

**Current State:**
- Universal payer config exists (`config/payers/common.yaml`), EREMIT-specific config (`config/payers/EREMIT.yaml`)
- NEW sidecars available: `extraction.map_metrics.v1.json`, `selection.evidence.v1.json`
- Unknown: payer-specific role_coverage trends, applied_for_extraction rates, selection chosen rates

**Analysis Goals:**
1. Measure role_coverage (which roles successfully mapped) and map_completeness (count) from map_metrics
2. Quantify applied_for_extraction rates (admission gate pass rates) per payer
3. Analyze selection chosen rates (mapper vs HIM precedence) per payer
4. Correlate role_coverage with conservation and certified_rows (does better mapping predict quality?)
5. Feed findings into CP14 payer profile design (which payers need custom header mappings?)

**Success Criteria for This Brief:**
- Field quality analysis completes on ≥20 documents with map_metrics sidecars
- role_coverage_mean and map_completeness_mean measured per payer (discovery mode)
- applied_for_extraction_rate and selection_chosen_mapper_rate computed per payer
- Correlations measured: role_coverage vs conservation, role_coverage vs certified_rows
- Gap matrix produced with impact × feasibility ranking
- Byte-stable reruns (identical SHA-256 hashes)

**Expected Insights (Discovery Mode - No Hard Thresholds):**
- role_coverage_mean likely 0.40-0.70 (40-70% of target roles successfully mapped)
- map_completeness_mean likely 3.0-5.0 (average roles mapped per document)
- applied_for_extraction_rate likely 0.70-0.90 (admission gate passes most tables)
- Payer variance: EREMIT may have lower role_coverage (headerless tables harder), DMBA higher (cleaner structure)
- Positive correlation expected: higher role_coverage → higher conservation, more certified_rows

**Next Steps After Analysis:**
1. Review baseline_summary.json for payer-specific insights (which payers benefit from custom profiles?)
2. Use gap_matrix.csv to prioritize fixes (low role_coverage? failed admission gate? missing map_completeness?)
3. Update CP14 acceptance criteria with measured baselines (e.g., require role_coverage ≥ 0.60 for production)
4. Feed correlation insights into selection policy tuning (e.g., if role_coverage < 0.40, prefer HIM over mapper?)
5. Design payer-specific header mappings to improve role_coverage on weak payers
