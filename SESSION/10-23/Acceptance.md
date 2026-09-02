Session: 2025-10-23 — Acceptance & Checkpoints Log

Alignment Primer: see ../Alignment.md (kept in-repo for determinism and shared context)

This file tracks implementation checkpoints, acceptance criteria, artifacts, and status for the 10-23 session. Owners update status and notes as work progresses.

Legend
- Status: pending | in_progress | complete | blocked
- Owner: initials or handle

CP0 — Schema Pack Landed
- Owner: CONTRACTS
- Status: complete
- Scope:
  - Add schemas: schemas/preproc.page.v1.json, schemas/vlm/header_pass.v1.json, schemas/vlm/data_pass.v1.json, schemas/budget_policy.v1.json
  - Set additionalProperties:false and required fields per contract
- Acceptance:
  - ajv validates 1–2 example payloads per schema ✅
  - Samples checked into repo under samples/ ✅
- Artifacts:
  - schemas/*.json ✅
  - samples/preproc.page.v1.sample.json ✅
  - samples/vlm.header_pass.v1.{request,response}.sample.json ✅
  - samples/vlm.data_pass.v1.{request,response}.sample.json ✅
  - samples/budget_policy.v1.sample.json ✅
  - Makefile: schema-check-cp0 target ✅
- Notes: All schemas use draft/2020-12 spec; oneOf used for dual request/response validation

CP1 — Preproc + Raster Sidecars
- Owner: IMPL
- Status: complete
- Scope:
  - Implement scripts/preprocess_roi.py (CLAHE 3.0/8×8, Sauvola, deskew ±2–12°, optional dewarp)
  - Emit per-page raster sidecar: {dpi, colorspace, interpolation, cropbox, sha256}
- Acceptance:
  - make runner-smoke produces sidecars without violating page schema 1.2
  - ops JSON deterministic (stable across re-runs)
- Artifacts:
  - runs/preproc_<pdf_sha256>_p<page>.json ✅
- Notes: ✅ Full schema validation; header_conf_median tracked; CLAHE params logged; est_source present; deterministic

CP2 — ROI Export Normalization
- Owner: IMPL
- Status: complete
- Scope:
  - Lock ROI export long_side=1792 and pad=12 (white)
  - Tag ops: ["pad=12","resize=1792long"]
- Acceptance:
  - ROI images in runs/vlm/* match 1792 long-side; ops tags present
- Artifacts:
  - runs/vlm/*_pre.png (1792 long-side, 12px padding) ✅
  - preprocess_ops in preproc sidecar ✅
- Notes: ROI normalization verified; ops logged in preproc.page.v1 sidecar

CP3 — est_cells + BudgetPolicy
- Owner: IMPL
- Status: complete
- Scope:
  - Compute est_rows/est_cols/est_cells (vision_lines → proj_peaks → default)
  - Budget thresholds: small<36, typical≤120, else large; payer “dense” +1 bucket
  - +25% on row-targeted retry; record in request + model_meta
- Acceptance:
  - runs/vlm/*_request.json include budget{class,est_*}
  - *_response.json mirror model_meta.budget_*; stable across runs
- Artifacts:
  - vision-ocr-cli/Sources/vision-ocr-cli/main.swift (budget computation) ✅
  - runs/vlm/*_response.json (budget_class mirrored) ✅
- Notes: ✅ Budget thresholds correct (48→typical); est_source="vision_lines" in response; numeric request schema-valid

CP4 — Dual‑Pass STDIO Support
- Owner: IMPL
- Status: complete
- Scope:
  - Runner handles infer.headers.v1/result.headers.v1 and infer.data.v1/result.data.v1
  - Emit header_lock with lock_sha256 bound to image_sha256
  - Back-compat single-pass maintained
- Acceptance:
  - Persistent session yields header then data responses; ajv validates new responses ✅
  - lock_sha256 reproducible ✅
- Artifacts:
  - scripts/dots-ocrd.py ✅ (message types supported)
  - runs/vlm/*_headers_request.json, *_headers_response.json, *_data_request.json, *_data_response.json ✅
  - SESSION/10-23/CP4_VERIFICATION.md ✅
- Notes: ✅ All 4 dual-pass artifact types validate; lock_sha256 matches across headers response → data request → data response (aee39fd3...); determinism verified (identical across runs); backward compat preserved (legacy files coexist); session_id format sess-YYYYMMDD-<docsha8>; roi_id format roi-<docsha10>; payer_hint schema fixed to allow null

CP5 — ROI Classification Logging
- Owner: IMPL
- Status: complete
- Scope:
  - Deterministic rules (header/money/notes) applied
  - Log roi.classification and features
- Acceptance:
  - Classifications present in manifest/sidecars; stable across runs ✅
- Artifacts:
  - runs/preproc_*.json (roi_classifications array) ✅
  - Classification logic validated against docs/roi_classification.md ✅
- Notes: ✅ All 3 ROIs classified correctly per spec; deterministic route_reason codes (vlm:*, vision:*, skip:*); features.sha256 audit trail; schema-valid; no schema changes

CP6 — Hungarian Mapper + Telemetry
- Owner: IMPL + CONTRACTS (spec fixtures)
- Status: complete
- Scope:
  - scripts/header_mapper.py implements frozen cost model: 0.60·lev_norm + 0.20·(1−jacc) + 0.15·pos_pen − 0.25·syn_bonus
  - Reject threshold ≥0.50; deterministic tie-breaks; emit mapping_source telemetry
- Acceptance:
  - make mapper-verify: header_match_rate ≥ 0.80 ✅
  - No accepted mappings with reject_reason ✅
- Artifacts:
  - scripts/header_mapper.py (164 lines) ✅
  - scripts/mapper_verify.py (52 lines) ✅
  - docs/hungarian_matching.md (frozen spec) ✅
  - samples/mapper/{extraneous_header, mixed_case_punct, noisy_ocr, partial_header, split_headers, synonyms_variance} ✅
  - SESSION/10-23/CP6_CP7_VERIFICATION.md ✅
- Notes: ✅ 15/18 headers matched (0.83 > 0.80); mapping_source telemetry ("exact"|"synonym"|"distance"); cost model frozen; partial_header (1/3) expected gap (abbreviations without synonyms - CP13 scope); noisy_ocr (3/4) demonstrates correct rejection of severe OCR corruption

CP7 — Lexical Repair (Per‑Type Confusion Maps) + Integration
- Owner: IMPL + CONTRACTS (map file + integration)
- Status: complete
- Scope:
  - scripts/lexical_repair.py with per-type confusion maps (money/date/code)
  - TEXT immutability enforced (no repair function provided)
  - Validation: MONEY_RE, date formats, ADA_RE, CPT_RE
  - Integration: --apply-repair flag in scripts/assemble_eob.py (default OFF)
- Acceptance:
  - make repair-verify: typed_cell_accuracy ≥ 0.80 ✅
  - TEXT mutations = 0 ✅
  - Flag integration complete ✅
  - Schema-neutral sidecar (.repair_summary.json) ✅
  - Repairs applied BEFORE conservation ✅
- Artifacts:
  - scripts/lexical_repair.py (100 lines) ✅
  - scripts/repair_verify.py (60 lines) ✅
  - config/confusion_maps.v1.json ✅
  - scripts/assemble_eob.py with --apply-repair flag ✅
  - SESSION/10-23/CP6_CP7_VERIFICATION.md ✅
  - SESSION/10-23/CP7_INTEGRATION_VERIFICATION.md ✅
- Notes: ✅ 7/7 test cases passing (typed_cell_accuracy=1.00); confusion maps: money (8 glyphs), date (4 glyphs), code (3 glyphs), text (empty/immutable); repair notes logged; --apply-repair flag defaults OFF (opt-in for shadow evaluation); repairs applied BEFORE conservation (ready for CP8); schema-neutral sidecar for analytics

CP8 — Money Conservation (v0: Minimal Pass)
- Owner: IMPL
- Status: complete
- Scope:
  - check_conservation() with deterministic cents math (±1¢ tolerance)
  - Conservation rules: allowed≤charged, allowed==(paid+patient)
  - Row classification: conserved | inconsistent | incomplete
  - Schema-neutral sidecar (.conservation.json)
- Acceptance:
  - Deterministic checks implemented ✅
  - Sidecar emission after schema validation ✅
  - No EOB schema changes ✅
- Artifacts:
  - scripts/assemble_eob.py (check_conservation, cents helper) ✅
  - SESSION/10-23/CP8_V0_VERIFICATION.md ✅
- Notes: ✅ Conservation rate calculation (conserved/total); per-row status + reasons; graceful failure (try/except); CP7 integration verified (uses repaired values); row-retry queue emission (CP8 full) deferred

CP8 Full — Row-Retry Queue Emission
- Owner: IMPL
- Status: complete
- Scope:
  - Emit runs/row_retry/<slug>.ndjson for inconsistent AND incomplete rows
  - Fields: doc, row, failing_fields[], reasons[], suggested_budget_multiplier
  - Reason→failing_fields mapping for all conservation rules
  - Make target: row-retry-emit
- Acceptance:
  - NDJSON files created (one per EOB) ✅
  - All required fields present and correctly typed ✅
  - Reason codes align with conservation sidecars ✅
  - Row indices match conservation sidecars ✅
  - Budget multiplier = 1.25 ✅
  - failing_fields correctly derived from reasons ✅
  - Make target functional ✅
- Artifacts:
  - scripts/assemble_eob.py (row-retry emission logic lines 652-678) ✅
  - runs/row_retry/*.ndjson (3 files) ✅
  - Makefile: row-retry-emit target ✅
  - SESSION/10-23/CP8_FULL_VERIFICATION.md ✅
- Notes: ✅ Emits for both inconsistent and incomplete rows (pragmatic refinement); reason→failing_fields mapping includes missing:* codes; header_lock_sha256 binding deferred until dual-pass data available; ready for shadow evaluation once extraction fixed

Update — 2025-10-24
- Added optional `--use-vlm-tables` ingestion path in `scripts/assemble_eob.py` (flag-gated, doc SHA resolved via manifest→source→override). Emits deterministic `.vlm_tables.json` sidecars with response hashes + errors; provenance tagged `vlm`.
- Smoke harness expanded to enforce VLM loader checks (sidecar reasons, provenance sources, byte-stable rerun). Step 8 currently blocked by runner returning blank cells; pursuing prompt tightening + JSON validation + larger model to unblock conservation lift.

CP9 — Scheduler Sidecar
- Owner: IMPL + CONTRACTS (schema)
- Status: pending
- Scope:
  - Emit preproc.page.v1.json with medians, route_decision, budget, needs_attention rows; batch hints {model,budget_class}
- Acceptance:
  - Sidecars validate against schema; used by scheduler deterministically
- Artifacts:
  - schemas/preproc.page.v1.json; runs/*/preproc.page.v1.json
- Notes:

CP10 — CI Gates + Daily Report
- Owner: IMPL + CONTRACTS (gold set)
- Status: pending
- Scope:
  - CI: schema checks, runner warm‑load (2B≤10s), conservation ≥99% on gold set v1
  - scripts/daily_report.py: p95, retry reasons, conservation
- Acceptance:
  - CI green on gold set v1; daily report prints KPIs
- Artifacts:
  - .github/workflows/ci.yml; bench/gold_set_v1/*; scripts/daily_report.py
- Notes:

CP11 — Grammar‑Locked JSON (Data Pass)
- Owner: IMPL
- Status: pending
- Scope:
  - Typed domains in data pass (money, date, code, id, text)
  - AJV validation of extracted cells against schema types
  - Greedy decode with grammar constraints
- Acceptance:
  - Data pass responses validate against typed schema
  - Type violations rejected/repaired deterministically
- Artifacts:
  - schemas/vlm/data_pass.v1.json (extended with type constraints)
  - scripts/dots-ocrd.py (grammar enforcement)
- Notes: Paired with CP4 dual-pass; eliminates type confusion in cells

CP12 — Column‑Wise Micro‑Decoding
- Owner: IMPL
- Status: pending
- Scope:
  - Fallback for sparse/failed table extraction
  - Per-column VLM queries with focused prompts
  - Budget allocation: 64-128 tokens per column
- Acceptance:
  - Sparse tables (fill rate <30%) trigger micro-decode
  - Column-level extraction logged with provenance
- Artifacts:
  - scripts/micro_decode.py
  - runs/vlm/*_micro_col*.json (per-column logs)
- Notes: Last-resort fallback for extreme cases; used when dual-pass fails

CP13 — Payer Profiles + Header Prototypes
- Owner: IMPL + CONTRACTS
- Status: pending
- Scope:
  - Payer-specific header prototypes (canonical → expected text variations)
  - Positional priors (column order hints per payer)
  - payer_profile_sha256 fingerprinting
- Acceptance:
  - Profile matching improves header alignment by 10-15%
  - Profiles versioned and deterministic
- Artifacts:
  - config/payer_profiles/*.json (header prototypes, synonyms, positional priors)
  - Manifest field: payer_profile_sha256
- Notes: Reduces Hungarian matching cost for known payers; optional boost

CP14 — Anti‑Repetition Guard
- Owner: IMPL
- Status: pending
- Scope:
  - N-gram detection for hallucinated repetitions
  - Trigger micro-retry on detected loops (e.g., "DENTAL SERVICES" × 36)
  - Log repetition patterns for analysis
- Acceptance:
  - Repetition rate <5% (down from observed 30-40% in baseline)
  - Micro-retries succeed on 70%+ of flagged cases
- Artifacts:
  - scripts/anti_repetition.py
  - runs/vlm/*_repetition_detected.json (flagged cases)
- Notes: Addresses hallucination tail risk; complements dual-pass header lock

Update — 2025-10-27
- Runner outputs now carry `prompt_sha256` / `request_sha256` / `response_sha256`; persistent artifacts are retagged with the request hash so prompt/model tweaks always produce new files.
- VLM tables that echo canonical headers are downgraded with `reason:"header_echo"`; `.vlm_tables.json` and `.vlm_tables_rejected.json` record the new hashes and rejection causes.
- Geometry-first spatial merge (projection on ROI images) is live; `.spatial_merge.json` logs peaks/token counts. Micro-OCR (CP13) is next to lift low-signal grids before HIM fallback.
- Remaining gap: finance rows still fall back to D0000 until micro-OCR finishes; conservation/HIM remain the explicit safety net.

Update — 2025-10-27 (CP12 Rowization Metrics)
- Assembler now consumes ROI PNGs for projection; row spans, scale factors, and request hashes are logged under `out/eob_accuracy_spatial_cp12/*.spatial_merge.json`.
- DD_3 pages register 1→2→1 row segments; DD_3_Ohio captures a single segment on page 2; AetnaW_Check page 2 surfaces three rows while other pages remain token-starved.
- All projected tables still fail `is_valid_table` (`reason:"low_signal"`) pending CP13 micro-OCR to populate empty cells; provenance now carries `roi_image_sha256` + `row_spans_page` to audit geometry before retry ladders.

Update — 2025-10-27 (CP13 Micro-OCR Attempt)
- `--micro-ocr` flag enables deterministic per-cell re-OCR: crops from ROI PNGs, runs `scripts/preprocess_roi.py` (profile hash logged), and invokes `vision-ocr-cli` in text mode before regex validation.
- Added deterministic padding (~4px + 0.4% ROI) plus 2× upsample for small crops, and normalized common OCR confusions (O→0, l→1, etc.) before money/date/code regex checks.
- Sidecars `*.reocr.v1.json` / `*.reocr.reason.v1.json` carry schema + hash metadata; latest run emitted profiles `{144eac08dc5c…, 2c75cc5fe89…, 6ecebf1019d…}` and recorded 343 deterministic attempts (including intermediate stripes/orientation) with 7 unique accepts (6 money, 1 desc). `regex_variant:true` promoted two money amounts (`227.00`, `101.00`) back to strict form.
- Tables still emit `reason:"low_signal"` for some columns, but AetnaW_Check page 2 now carries micro-sourced amounts in the money columns; next rung focuses on clearing residual `ocr_empty`/`regex_money` failures.

Update — 2025-10-27 (CP13 Orientation Sweep + Metrics Harness)
- Orientation/polarity guard now applies to every expansion/stripe/profile attempt; reason sidecars capture `column_type`, `orientation_id`, `polarity_id`, and slab fallback emits `slab_no_match` when the column stripe OCR fails. Hash blocks include stripe + crop image fingerprints plus the fixed profile set (`2c75cc5f…`, `144eac08…`, `6ecebf10…`, slab profile hash).
- Added `scripts/micro_ocr_metrics.py` with Make targets:
  - `make micro-ocr-scan` (asserts schema + required fields like `expand_profile_id`, `column_type`, `stripe_index` when `--micro-ocr` is used).
  - `make bench-kpi` (per-type lift + residual breakdown). Latest run: `money_found=5`, `text_found=2`, `residual_ocr_empty=13` (dcode 7, desc 6), `residual_slab_no_match=43` (money 35, date 8), `residual_regex_dcode=1`; two money cells promoted via `regex_variant → STRICT`.
- Slab fallback now logs deterministic evidence even on failure (`slab_no_match` with hashed crop + profile fingerprint). No slab acceptances yet; remaining gaps point to contrast loss on right-justified amounts/dates rather than regex.

Update — 2025-10-27 (CP13 High-Contrast Profiles)
- Introduced `P4_money_gain` / `P4_date_gain` profiles that apply morphological top-hat + gain before Sauvola; hashes `54e74abb4c73…`, `962daf2fb247…`, `a2a1a424e95e…` now travel with micro sidecars.
- Bench rerun (longer timeout ~8.4 min) yields identical accepted cells (`money_found=5`, `text_found=2`); residuals remain `ocr_empty=13`, `slab_no_match=43`, `regex_dcode=1`.
- Slab fallback still records only failures (no accepts yet), but each miss now carries the new profile hash for provenance. Further capture work needed (e.g., more aggressive contrast on money/date or refined crop expansion) before escalating to CP14.

- Update — 2025-10-27 (CP13 Atlas Reset — FAST)
- CP numbering frozen: **CP12 = Spatial Projection**, **CP13 = Micro-OCR (ROI + Atlas)**, **CP14 = Typed per-cell VLM**.
- Per-cell ladders are deprecated. New two-pass contract:
  * **Pass 0 (page invariants):** decide orientation {0°,180°} and polarity {normal,invert} once per page; cache `page_orientation` / `page_polarity` and `worker_epoch`.
  * **Pass 1 (ROI fast-fill):** reuse Vision tokens or run exactly one high-res ROI OCR (percentile clip [2,98] → CLAHE 2.0/8×8 → Gaussian σ=0.8). Typed band selectors (money right-justified, date left-anchored) accept STRICT regex hits. Rows passing arithmetic tolerance (≤$0.01 or 0.5%) are locked.
  * **Pass 2 (Atlas + Patch):** pack unresolved cells into a deterministic atlas (fixed tile size, 2 px gutters, row-major order); run a single OCR; map lines back via offsets. Remaining money/date cells may enter a **patch atlas** using two frozen profiles (`MoneyPatch`, `DatePatch`). Variant→CP7→STRICT allowed only inside patch rung. No per-cell sweeps.
- **Hard budgets:** `ocr_calls_per_page ≤ 4`, `p95_page_time ≤ 10 s`. If exceeded, set `attempt_cap_hit:true` and stop additional passes. Metrics surface via `make bench-kpi`; CI fails when limits are crossed.
- **Sidecars:** emit `roi_observation_map.v1.json`, `atlas.v1.json`, and optional `patch.v1.json` (AP:false) with `atlas_sha256`, `tile_pack_order`, `tile_size`, `gutter_px`, per-cell offsets, and `accepted_source ∈ {vision,atlas,patch,him}`. Existing `.reocr.*` sidecars retire once atlas path is live.
- **Row coherence:** assembler prefers sources in order Vision → Atlas → Patch → HIM. Rows failing arithmetic revert to HIM values and log `row_coherence:false`, `coherence_delta_abs`, and `source_mix`.
- **Instrumentation:** record `ocr_calls_per_page`, `page_wall_time_ms`, `tiles_in_atlas`, `tiles_in_patch`, `worker_epoch`, and per-cell `accepted_rung_id`. Bench harness enforces budgets and reports p95 plus fill metrics.
---

Session: 2025-10-30 — RCF: Semantic Resolution with Conservation Fitness

Summary:
- Adopted a semantics‑first extraction spine where conservation is the fitness function. Geometry carries tokens; semantics resolves truth (roles), conservation arbitrates ambiguities.
- Established a phased plan (Phase 0→4) with token‑efficient changes: soft‑accept (Mapper vs HIM), Ring including charged, doc‑level priors, multi‑pass + locks, and per‑payer promotion gates.

Acceptance:
- Determinism & budgets remain frozen (p95 ≤ 3s/page, OCR/page ≤ 4, ≤ 3 calls/table, deterministic_equal=true).
- Sidecars present and AP:false only: sem_map.v1.json, sem_map.choice.v1.json, coherence.v1.json, bootstrap.v1.json, micro_{atlas,patch,stats}.v1.json.
- For ambiguous headers (e.g., EREMIT‑like): Mapper vs HIM is chosen by sampled conservation (Δ ≥ 0.10) with deterministic tie‑breakers. If sample < 3 rows: deterministic fallback (mapper if header_match ≥ 0.30 else HIM), `semantic_uncertain=true`.
- Ring now targets full money set {charged, allowed, paid, patient}; enforces inequalities; monotone improvement; logs ring_applied_count.* and repair reasons.
- Priors computed from locked rows only (n ≥ 5; adaptive IQR gate), clamped and quantized; provenance added; multi‑pass up to 3 iterations with monotone locks.

Targets & Gates:
- Coverage: rows_3of4 ≥ 0.55 (smoke).
- Accuracy: conservation ≥ 0.35 (ship); stretch ≥ 0.90 on per‑payer gold sets before promotion to mapper‑first.
- Locks: row_locked_pct ≥ 0.20; monotone across passes.

Evidence Pointers:
- Brief: SESSION/2025-10-30-rcf-semantic-resolution/brief.md
- EREMIT (rowize acceptance): SESSION/AuditLogs/2025-10-30-eremit-rowize/report.json
- Hold‑out (shadow smoke): SESSION/AuditLogs/holdout/EOB_ZIP_2025Q3/report.json
- Coherence example (row‑level): out/eremit_rowize/EREMIT60pg.coherence.v1.json
- Semantic map (doc‑mode): out/eremit_doc_sem/EREMIT60pg.sem_map.v1.json (if present)

Notes:
- Text‑mode NDJSON contains no tables; when `--semantic-map` is set but no tables exist, the assembler must WARN deterministically and take the HIM‑only path.
- All changes are flag‑gated first; promotion from flags→policy happens only after gates pass on both acceptance and hold‑out gold.
