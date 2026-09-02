
## 2025-11-06: Plan 1 (Diagnostic Survey) + Plan 4 (Visualization Hardening) - COMPLETE

**Context:** Completed Plan 1 (Diagnostic Survey) and Plan 4 (Visualization Hardening)

**Commands:**
```bash
# Assembler with production preset
python3 scripts/assemble_eob.py \
  --pages bench/holdout/EOB_ZIP_2025Q3 \
  --glob '*.jsonl' \
  --mappings config/payers \
  --out out/eob \
  --doc-validate \
  --doc-fallback-rowize \
  --rowize-tokens \
  --rowize-strict \
  --solve-roles \
  --emit-evidence \
  --summary-reconcile \
  --ilp-assign-extract

# Survey tool (extended with NEW metrics)
python3 scripts/tools/payer_quality_map.py \
  --root out/eob \
  --out out/reports/payer_quality_map.v2.json \
  --emit-md out/reports/quality_landscape.md \
  --emit-gaps out/reports/gap_matrix.csv

# Visualizer smoke tests
python3 scripts/visualize_extraction.py out/eob
python3 scripts/visualize_table_diagram.py out/eob
```

**Artifacts:**
- out/eob/* (37 EOBs with new sidecars; 25 map_metrics present)
- out/reports/payer_quality_map.v2.json
- out/reports/quality_landscape.md
- out/reports/gap_matrix.csv

**KPIs:**
- Extraction doc coverage: 37/140 (26.4%)
- Applied_for_extraction rate (overall): 7.9% (140 docs, 4 payers in survey)
- Map completeness: median=2.0, p95=4.0; per payer:
  - common: map_comp=2.13, role_cov=0.232, applied=29.0%, ROI=0.162
  - Aetna: map_comp=1.75, role_cov=0.173, applied=50.0%, ROI=0.161

**Byte-stability:**
- JSON hash: 50dd76a7a7812b8aa5a3f318982e0842f4e7bdee9330a97ce4c529f7a1e96750
- MD hash: 627751d1547e25d6102185f7b355543d72b76fab057cd8e69837d7ce776efe0c
- CSV hash: d475feb24323836c7cc412b80625a4589b7375d9b2914707c9887d77509d5102

**Status:** PASS; verifier pending on Plan 3 outputs


## 2025-11-06: Plan 3 (Field Quality Analysis) - COMPLETE

**Context:** Completed Plan 3 (Field Quality Analysis) with correlation analysis

**Commands:**
```bash
# Field quality report tool (NEW)
python3 scripts/tools/field_quality_report.py \
  --root out/eob \
  --out-csv out/reports/field_coverage_report.csv \
  --out-json out/reports/baseline_summary.json \
  --out-md out/reports/improvement_roadmap.md
```

**Artifacts:**
- scripts/tools/field_quality_report.py (NEW - 456 lines)
- out/reports/field_coverage_report.csv (140 doc-level metrics)
- out/reports/baseline_summary.json (global metrics + correlations)
- out/reports/improvement_roadmap.md (ranked recommendations)

**KPIs:**
- Total documents: 140; Extracted: 37 (26.4%)
- Global metrics:
  - applied_for_extraction_rate: 7.86%
  - map_completeness_mean: 1.37
  - role_coverage_mean: 14.4%
  - conservation_mean: 11.6%
  - certified_rows_mean: 1.65
- Correlations (Pearson):
  - role_coverage vs conservation: 0.723 (strong positive)
  - applied_for_extraction vs conservation: 0.675 (strong positive)
  - role_coverage vs certified_rows: 0.535 (moderate positive)
  - map_completeness vs certified_rows: 0.509 (moderate positive)
- Top gaps identified: Low map_completeness (75.7% impact), Low conservation (97.3% impact), Low role_coverage (81.1% impact)

**Byte-stability:**
- CSV hash: 230ccd328247b6c672e57876f6e8b8e1375bc4fdec40174567c36b9a4e1ebf6c
- JSON hash: 2a111175c47b832221310305d511284437064498113ae1655e0390cd8da0b8e0
- MD hash: 116197623a2a04dd3ac1e5716adb00a0c026a54c980a7ada99f0770e32c5422d

**Status:** PASS; all outputs byte-stable, non-zero metrics, ready for reviewer validation

