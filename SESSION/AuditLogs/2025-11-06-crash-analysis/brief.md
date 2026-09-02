# Brief — 2025-11-06: Visualization Crash Analysis (CP13) — OPTIONAL

**STATUS: Run ONLY IF NEEDED after Plan 4 (Visualization Hardening) shows remaining issues**

## Mission

Short pass to identify top 10 remaining failure modes in visualization scripts after initial null-safe hardening, to inform targeted fixes.

## Constraints

- **No Schema Changes**: Page v1.2, run v1.2, DentalEOBSchema v1.2 untouched
- **Null-Safe Only**: Defensive coding patterns; no behavior changes to extraction logic
- **Byte Stability**: Analysis outputs produce identical hashes on reruns
- **Rejection Logging**: All crash points logged with line numbers, data access patterns, and null value sources
- **Deterministic**: No inference or guessing about missing data; document actual state only

## KPIs

1. **Top 10 Crash Points Documented**: Most frequent failure modes after Plan 4 hardening applied
2. **Failure Types Categorized**: Missing sidecars, empty tables[], None values in new doc-rollup schemas (map_metrics, selection.evidence)
3. **Minimal Reproduction**: 2-3 synthetic edge cases that still crash after null-safe hardening
4. **Priority Ranking**: Failures ranked by frequency × impact (not exhaustive taxonomy)

## Procedure

**ONLY RUN IF Plan 4 (Visualization Hardening) reveals remaining crash points**

1) **Quick Regression Test After Plan 4**
   - Run hardened visualizers on `out/eob`, `out/eremit_doc_final`, `out/holdout` directories
   - Log any remaining crashes with stack traces
   - If zero crashes → DONE (skip rest of plan)

2) **Identify Top 10 Remaining Failure Modes**
   - Parse crash logs for most frequent error types
   - Categorize by: missing new sidecars (map_metrics, selection.evidence), None in nested dicts, empty tables[] after hardening
   - Rank by frequency × impact (how many docs affected)

3) **Minimal Edge Case Reproduction**
   - Create 2-3 synthetic JSON files that trigger top failures
   - Focus on new doc-rollup sidecar shape issues (not already handled by Plan 4 utilities)

4) **Document Findings for Targeted Fix**
   - Write `failure_audit.md` with top 10 cases only
   - Include line numbers, error messages, suggested fixes
   - Flag for follow-up hardening pass (if needed)

## Artifacts

- **`SESSION/AuditLogs/2025-11-06-crash-analysis/failure_audit.md`**
  - Top 10 failure cases only (not exhaustive)
  - Line numbers, error messages, frequency counts
  - Suggested fixes for each case

- **`SESSION/AuditLogs/2025-11-06-crash-analysis/edge_cases.json`**
  - 2-3 minimal reproduction cases (not 40+)
  - Focus on new doc-rollup sidecar shape issues
  - N/A placeholders for missing data

- **`SESSION/AuditLogs/2025-11-06-crash-analysis/crash_reproduction.sh`**
  - Shell script with 2-3 reproduction commands
  - Verifies remaining crashes after Plan 4 hardening

## Notes

**CP13 Context:**
- CP13 (Micro-OCR) requires production-ready visualization tools
- Plan 4 (Visualization Hardening) applies initial null-safe pass (6-8 utilities)
- This plan is OPTIONAL - only run if Plan 4 leaves remaining crashes

**Known Crash Points (from previous session) - SHOULD BE FIXED BY PLAN 4:**
- `visualize_extraction.py` line 148: `best.get()` when `best is None`
- `visualize_table_diagram.py` line 64: `max(column_map.values())` when `column_map = {}`
- `visualize_table_diagram.py` line 79: dict comprehension when `column_map is None`
- Missing sidecars (`.assign.proof`, `.evidence`)

**New Failure Modes This Plan Targets (if needed):**
- Missing NEW sidecars: `extraction.map_metrics.v1.json`, `selection.evidence.v1.json`
- None values in nested dicts from new doc-rollup schemas
- Empty tables[] arrays after hardening applied

**Success Criteria:**
- Regression test after Plan 4: if zero crashes → DONE
- If crashes remain: top 10 documented with line numbers and suggested fixes
- 2-3 minimal reproduction cases for remaining issues
- Findings inform targeted follow-up hardening (if needed)
