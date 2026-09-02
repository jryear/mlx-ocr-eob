# Brief — 2025-10-27: Projection vs. Micro-OCR

## Mission
- Validate geometry-first spatial merge on the accuracy bench (DD_3, DD_3_Ohio, AetnaW_Check).
- Implement CP13 micro-OCR per cell to lift `header_echo`/`low_signal` tables without mutating schemas.
- Ensure conservation > baseline and services != D0000 when projection + micro-OCR succeed.

## Invariants (never break)
- Elegance > expediency; deterministic only. Flags default OFF.
- No schema changes: `page v1.2`, `run v1.2` remain untouched. Sidecars only (`.spatial_merge.json`, `.reocr.reason.json`, etc.).
- Idempotency: maintain `prompt_sha256`, `request_sha256`, `response_sha256`; new outputs must include hashes.
- All rejection paths must log reasons (`header_echo`, `rect`, `low_signal`, `micro_ocr_fail`).

## State to inspect before coding
- `runs/vlm/*_headers_response_<sha>.json` and `_data_response_<sha>.json` (ensure hashed filenames present).
- `out/eob_forced_combo/*.vlm_tables.json` and `*.vlm_tables_rejected.json` (header_echo reason). 
- `out/eob_forced_combo/*.spatial_merge.json` (projection stats; expect `accepted:false`).
- Acceptance.md update (2025-10-27 entry).

## Expected KPIs & Validation
1. Re-run forced capture (7B instruct) with hashed outputs:
   ```bash
   ALLOW_FORCE_VLM=1 VLM_MODEL=mlx-community/Qwen2-VL-7B-Instruct-4bit \
     VLM_MAX_NEW_TOKENS=1536 \
     vision-ocr-cli/.build/release/vision-ocr-cli eobs/tests/DD_3.pdf \
     --dpi 300 --mode document --lang en-US \
     --vlm-runner scripts/dots-ocrd.py --vlm-export runs/vlm --force-vlm
   ```
   Confirm new request hashes appear.

2. Assemble with spatial merge + apply-repair:
   ```bash
   python scripts/assemble_eob.py --pages runs/accuracy_forced_v1 \
     --mappings config/payers --out out/eob_forced_combo \
     --apply-repair --use-vlm-tables --spatial-merge
   ```

3. Inspect outputs:
   - `.spatial_merge.json`: `accepted:true` after micro-OCR (rows + billable cells > 0). 
   - `.reocr.reason.json`: profile hash, cells attempted, reasons on failure.
   - Services contain real procedure codes (no `D0000` fallback) and conservation > baseline.

4. Byte stability: re-run assembly, verify identical hashes. 

## CP12 Progress — 2025-10-27
- Geometry-first spatial merge now runs end-to-end via `scripts/assemble_eob.py … --spatial-merge`; sidecars land in `out/eob_accuracy_spatial_cp12/*.spatial_merge.json`.
- DD_3: rowization surfaced 1/2/1 row segments across pages 0-2 (`segments_retained` mirrors the count); scale factors ≈0.55 are logged alongside row span bounds.
- DD_3_Ohio: only page 2 produced a usable projection (1 segment); earlier pages remain low-signal with sparse tokens.
- AetnaW_Check: page 2 yielded three row bands; other pages still lack usable Vision tokens, so projections short-circuit with `reason:"no_tokens"`.
- All spatial tables remain rejected (`reason:"low_signal"`) pending CP13 micro-OCR to backfill cell text; provenance now records `request_sha256`, `roi_image_sha256`, and row span geometry for audit.
- Validation command:
  ```bash
  python3 scripts/assemble_eob.py --pages runs/accuracy_forced_v1 \
    --mappings config/payers --out out/eob_accuracy_spatial_cp12 \
    --apply-repair --use-vlm-tables --vlm-dir runs/vlm_accuracy_v2 --spatial-merge
  ```
  Output is byte-stable across reruns.

## CP13 Reset — Atlas-first Micro-OCR (2025-10-27)
- CP numbering frozen: **CP12 = Spatial Projection**, **CP13 = Micro-OCR (ROI + Atlas)**, **CP14 = Typed per-cell VLM**.
- Per-cell ladders are retired. New two-pass design:
  - **Pass 0 (once/page):** decide orientation {0°,180°} and polarity {normal,invert}; cache in manifest (`page_orientation`, `page_polarity`, `worker_epoch`).
  - **Pass 1 (ROI fast-fill):** reuse Vision tokens or run a single high-res ROI OCR (percentile [2,98] → CLAHE 2.0/8×8 → Gaussian σ=0.8). Typed band selectors (money right-justified, date left) accept STRICT regex values. Rows that balance within $0.01 or 0.5% are locked.
  - **Pass 2 (Atlas/Patch):** pack all unresolved cells into a deterministic atlas (fixed tile size, 2 px gutters, row-major); run one OCR → scatter lines back via offsets. Remaining money/date cells may go through one **patch atlas** (MoneyPatch, DatePatch profiles) before falling back to HIM.
- **Budgets:** `ocr_calls_per_page ≤ 4`, `p95_page_time ≤ 10 s`. If exceeded, set `attempt_cap_hit:true` and halt further passes. `make bench-kpi` enforces limits (CI fails when violated).
- **Sidecars:** new AP:false artifacts (`roi_observation_map.v1.json`, `atlas.v1.json`, `patch.v1.json`) record `atlas_sha256`, `tile_pack_order`, tile geometry, per-cell offsets, and `accepted_source ∈ {vision,atlas,patch,him}`. Existing `.reocr.*` sidecars sunset once atlas path ships.
- **Row coherence:** assembler prefers sources Vision → Atlas → Patch → HIM. Rows failing arithmetic revert to HIM values; log `row_coherence:false`, `coherence_delta_abs`, `source_mix`.
- **Telemetry:** expose `ocr_calls_per_page`, `page_wall_time_ms`, `tiles_in_atlas`, `tiles_in_patch`, `worker_epoch`, and per-cell `accepted_rung_id`. Bench reports new KPIs and remains byte-stable on reruns.
