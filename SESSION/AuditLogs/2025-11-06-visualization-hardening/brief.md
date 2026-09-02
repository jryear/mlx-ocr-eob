# Brief — 2025-11-06: Visualization Null-Safety Hardening (CP13) — MINIMAL PASS

## Mission

Deploy minimal null-safe pass (6-8 utilities) to handle NEW doc-rollup sidecars (`extraction.map_metrics.v1.json`, `selection.evidence.v1.json`) + known crash points, enabling production use of visualizers.

## Constraints

- **No Behavior Changes**: Existing logic unchanged; only add null/empty checks with graceful fallbacks
- **No Schema Changes**: Page v1.2, run v1.2, DentalEOBSchema v1.2 untouched
- **No Assembler/Solver/Policy Edits**: Orchestrator team does NOT edit `scripts/assemble_eob.py`, `solver_laws.py`, `selection_policy.py`, or any schema files
- **Surgical Changes Only**: No wholesale rewrites; keep changes minimal and targeted
- **No External Dependencies**: No new libraries (use stdlib only)
- **Byte Stability**: Fixed visualizers produce identical output on repeated runs with same inputs
- **Graceful Degradation**: Print "N/A" or "(missing)" instead of crashing; continue processing remaining data
- **Deterministic**: No inference or default values that hide real gaps; document actual state only

## KPIs

1. **Null-Safe Utility Library Deployed**: 6-8 functions in `scripts/null_safe_utils.py` covering critical patterns (dict access, list indexing, max/min, file loading)
2. **visualize_extraction.py Hardened**: Known crash points fixed (line 148: ILP best/runner_up), NEW doc-rollup sidecars handled (map_metrics, selection.evidence)
3. **visualize_table_diagram.py Hardened**: Known crash points fixed (line 64: max(column_map.values()), line 79: dict comprehension), handles missing sidecars with "N/A"
4. **VISUALIZATION_GUIDE.md Updated**: Documents new doc-rollup sidecars (what they show, how to interpret)
5. **Test Suite Passes 100%**: 2-3 crash-test JSON files (null ILP, empty column_map, missing NEW sidecars) run without crashes
6. **Byte Stability Verified**: Reruns produce identical SHA-256 hashes

## Procedure

1) **Deploy Null-Safe Utility Library (6-8 functions)**
   ```bash
   # Create scripts/null_safe_utils.py with 6-8 functions:
   # - safe_get_dict(d, key, default={})  # Handle None dicts, missing keys
   # - safe_get_list(d, key, default=[])  # Handle None/missing lists
   # - safe_get(d, key, default=None)     # General-purpose getter
   # - safe_max(collection, default=0)    # Handle empty collections
   # - safe_load_json(path, default=None) # Handle missing files
   # - safe_invert_dict(d, default={})   # Handle None/empty dicts for column_map inversion
   # - (optional 2 more based on crash analysis)

   # Minimal test coverage (no full pytest suite)
   python3 scripts/null_safe_utils.py  # Run built-in doctests or simple main()
   ```

2) **Harden visualize_extraction.py (Surgical Fixes Only)**
   ```bash
   # Fix known crash points:
   # Line 148: best.get('certified_rows_sample', 0) → safe_get(best, 'certified_rows_sample', 0)
   # Load NEW sidecars: map_metrics, selection.evidence with safe_load_json()
   # Display new metrics: role_coverage, map_completeness, applied_for_extraction, chosen variant

   # Run on real data
   python3 scripts/visualize_extraction.py out/eob > /tmp/viz_test.txt
   # Verify: no crashes, "N/A" for missing sidecars
   ```

3) **Harden visualize_table_diagram.py (Surgical Fixes Only)**
   ```bash
   # Fix known crash points:
   # Line 64: max(column_map.values()) → safe_max(column_map.values(), 0) if column_map else 0
   # Line 79: {v: k for k, v in column_map.items()} → safe_invert_dict(column_map, {})

   # Run on real data
   python3 scripts/visualize_table_diagram.py out/eob
   # Verify: no crashes, "(missing)" for absent data
   ```

4) **Update VISUALIZATION_GUIDE.md**
   ```bash
   # Add section: "New Doc-Level Sidecars (v1.2)"
   # Document: extraction.map_metrics (role_coverage, map_completeness, anchors_per_row)
   #           selection.evidence (chosen variant, mapper_header_match, conservation samples)
   # Explain how to interpret these in visualizer output
   ```

5) **Create Minimal Test Suite (2-3 fixtures)**
   ```bash
   # tests/fixtures/crash_test_null_ilp.json - ILP best/runner_up null
   # tests/fixtures/crash_test_empty_column_map.json - column_map {}
   # tests/fixtures/crash_test_missing_new_sidecars.json - no map_metrics/selection.evidence

   # Simple test runner (not full pytest)
   for fixture in tests/fixtures/crash_test_*.json; do
     python3 scripts/visualize_extraction.py "$(dirname $fixture)" > /dev/null 2>&1 && echo "✓ $fixture" || echo "✗ $fixture"
   done
   ```

6) **Regression Test + Byte Stability**
   ```bash
   # Test on out/eob, out/eremit_doc_final
   python3 scripts/visualize_extraction.py out/eob > /tmp/viz1.txt
   python3 scripts/visualize_extraction.py out/eob > /tmp/viz2.txt
   shasum -a 256 /tmp/viz1.txt /tmp/viz2.txt
   # Expected: no crashes, identical hashes
   ```

## Artifacts

- **`scripts/null_safe_utils.py`**
  - 6-8 null-safe utility functions with docstrings
  - Minimal test coverage (built-in doctests or simple main())
  - Used by both visualization scripts

- **`scripts/visualize_extraction.py` (hardened)**
  - Surgical fixes for known crash points (line 148: ILP best/runner_up)
  - NEW: Handles doc-rollup sidecars (map_metrics, selection.evidence)
  - Graceful degradation: prints "N/A" for missing fields

- **`scripts/visualize_table_diagram.py` (hardened)**
  - Surgical fixes for known crash points (lines 64, 79: column_map operations)
  - Handles missing sidecars with "(missing)" output

- **`VISUALIZATION_GUIDE.md` (updated)**
  - NEW section: "Doc-Level Sidecars (v1.2)"
  - Documents map_metrics (role_coverage, map_completeness, anchors_per_row)
  - Documents selection.evidence (chosen variant, mapper_header_match)
  - Explains how to interpret these in visualizer output

- **`tests/fixtures/crash_test_*.json`**
  - 2-3 synthetic JSON files triggering known failure modes
  - Minimal reproduction cases for regression testing
  - Focus on: null ILP, empty column_map, missing NEW sidecars

- **BuildLog Entry**:
  - Command: hardening commands run, validation results
  - Artifacts: paths to updated scripts and VISUALIZATION_GUIDE.md
  - KPIs: regression test pass rate (100% expected), byte stability hashes

## Notes

**CP13 Context:**
- CP13 (Micro-OCR) requires production-ready visualization tools
- Minimal hardening pass to handle NEW doc-rollup sidecars + known crash points
- Surgical changes only - no wholesale rewrites

**NEW Doc-Rollup Sidecars to Handle:**
1. `extraction.map_metrics.v1.json` - role_coverage, map_completeness, anchors_per_row histogram
2. `selection.evidence.v1.json` - chosen variant (mapper/him/unknown), mapper_header_match, conservation samples
3. Both may be missing or have None values → graceful degradation required

**Known Crash Points to Fix (Minimal List):**
1. `visualize_extraction.py:148` - `best.get()` when `best is None`
2. `visualize_table_diagram.py:64` - `max(column_map.values())` when `column_map = {}`
3. `visualize_table_diagram.py:79` - dict comprehension when `column_map is None`
4. Missing NEW sidecars (map_metrics, selection.evidence) → handle with safe_load_json()

**Null-Safe Strategy (Minimal Pass):**
- **Surgical fixes**: Only fix known crash points, don't refactor
- **Graceful degradation**: Print "N/A" or "(missing)" for absent data
- **No behavior changes**: Visualization logic unchanged, only add null checks
- **VISUALIZATION_GUIDE.md update**: Document new sidecars for users

**Success Criteria:**
- 6-8 null-safe utilities deployed
- Known crash points fixed in both visualizers
- NEW sidecars handled (map_metrics, selection.evidence)
- VISUALIZATION_GUIDE.md updated with new sidecar documentation
- Regression test: 100% crash-free on out/eob, out/eremit_doc_final
- 2-3 crash-test fixtures pass
- Byte stability verified

**Post-Hardening Usage:**
```bash
# Run on any extraction output (including NEW sidecars)
python3 scripts/visualize_extraction.py out/your_extraction/
python3 scripts/visualize_table_diagram.py out/your_extraction/

# Outputs "N/A" for missing map_metrics/selection.evidence gracefully
```
