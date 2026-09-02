# NativeOCR — AI Alignment Primer

> Copy/paste this into every new co‑pilot/tma session before you touch the repo.

## 0. Ethos
- **Elegance > expediency**. We remove failure categories; no hacks, no silent fallbacks.
- **Determinism before cleverness**. Every step is offline, greedy, hash‑locked, and logged.
- **AP:false contracts** stay put (`page v1.2`, `run v1.2`). Rich detail lives in sidecars/flags.
- **Seams are reversible**. New behavior ships behind a flag, with its own sidecar evidence.

## 1. Architecture snapshot
1. Vision capture → NDJSON (text, tokens, tables, regions). Vision chooses whether to route to VLM.
2. VLM dual pass (headers/data). All artifacts land in `runs/vlm/*` with request/response JSON, ROI PNGs, and sidecars.
3. Assembler (`scripts/assemble_eob.py`) consumes Vision tables, VLM tables, spatial merge output, HIM, CP7 repair, CP8 conservation, CP8 row-retry.
4. Sidecars (AP:false) capture provenance: preproc, conservation, HIM, repair summaries, VLM tables, spatial merge, errors, micro-OCR plans.

## 2. Current status (2025‑10‑27)
- Runner emits `prompt_sha256` / `request_sha256` / `response_sha256`; filenames include the request hash. Header echo tables (`["proc_cd","desc",…]`) are flagged with `reason:"header_echo"`.
- `.vlm_tables.json` / `.vlm_tables_rejected.json` list every table with request/prompt hashes, header-lock hashes, sources. `.spatial_merge.json` logs projection stats (peaks, token count, accepted).
- Geometry-first spatial merge is live; micro-OCR (CP13) is next to lift low-signal grids before HIM fallback.
- Gap: VLM still parrots headers, so services fall back to `D0000` until CP12/CP13 complete.

## 3. Task protocol (for any AI/human)
1. Read `README.md`, `SESSION/Alignment.md`, `SESSION/10-23/Acceptance.md`, and the relevant scripts before editing.
2. Never mutate existing schemas or manifests. All new data → sidecars (JSON, AP:false).
3. Plan concisely; execute deterministically; log reasons; capture hashes.
4. New functionality ships behind a flag, OFF by default, with explicit sidecar output.
5. Inspect sidecars and hashes before concluding success; re-run commands to confirm byte stability.

## 4. Known cracks today
- Geometry projection often yields rows of empty strings because tokens are sparse; we still fall back to HIM (`D0000`) on many pages.
- Micro-OCR (per-cell) is not yet in place, so we can’t recover money/code/dates when projection finds the geometry.
- VLM is not capable of realistic table extraction on our scans; we should not attempt to coax it with prompt hacks.

## 5. Allowed next moves (deterministic ladders)
1. **CP12 – Raster rowization**  
   Treat ROI PNG directly: grayscale → threshold → horizontal projection → row peaks. Split width into `K = len(canonical_order)` columns, bucket tokens by x, produce rectangular grid. Log metrics in `.spatial_merge.json`; run the existing signal predicate.
2. **CP13 – Micro-OCR per cell**  
   When projection fails signal, re-OCR each (row × col) cell with a fixed Vision accurate profile (deskew, CLAHE, threshold). Accept only values that pass money/date/code regex. Log `preproc_profile_sha256` / `…reocr.reason.json`.
3. **CP14 – Typed per-cell VLM (optional)**  
   Off-by-default. Grammar-locked JSON prompt per cell; one shot; logged with hashes.

If none of the above produce signal, HIM fallback + row-retry is the correct outcome (and must be logged).

## 6. Guardrails
- **Idempotency**: new artifacts must include stable hashes (`*_sha256`). Prompt/model changes produce new filenames.
- **Logging**: every rejection path emits a deterministic `reason` string plus slice/request hashes.
- **Flags**: new features default OFF. Gather metrics before flipping defaults.
- **Preprocessing**: deterministic profiles only; never mutate source data in place.

## 7. Validation commands (single-source-of-truth)
```bash
# Forced VLM capture with runner
ALLOW_FORCE_VLM=1 VLM_MODEL=mlx-community/Qwen2-VL-7B-Instruct-4bit \
  VLM_MAX_NEW_TOKENS=1536 \
  vision-ocr-cli/.build/release/vision-ocr-cli eobs/tests/DD_3.pdf \
  --dpi 300 --mode document --lang en-US \
  --vlm-runner scripts/dots-ocrd.py --vlm-export runs/vlm --force-vlm

# Assemble with fallbacks
python scripts/assemble_eob.py --pages runs/accuracy_forced_v1 \
  --mappings config/payers --out out/eob_forced_combo \
  --apply-repair --use-vlm-tables --spatial-merge
```
Inspect `*.vlm_tables.json`, `*.vlm_tables_rejected.json`, `*.spatial_merge.json`, `*.reocr.reason.json` for hashes/reasons. Re-run commands to ensure byte-identical outputs.

## 8. When to consider custom OCR
Only after CP12/CP13 land and you still record a double-digit failure rate on clearly legible scans **and** you have a plan for labeled data + training infrastructure. Until then, we exhaust deterministic ladders.

---

**TL;DR**: keep everything deterministic, hash-tracked, and logged. Solve failure modes with projection + micro-OCR before dreaming of new models. This primer is the contract every agent must follow.
