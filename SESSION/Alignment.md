# NativeOCR — Alignment Primer (Concise)

## Ethos
- Elegance > expediency; remove failure categories, don’t patch symptoms.
- Determinism before cleverness; schemas + sidecars are the truth.
- Offline, reproducible, auditable; best‑practices are baselines we surpass.

## Non‑Negotiables
- Fixed raster (CGPDFPage.getDrawingTransform), fixed Vision revision, greedy decode.
- Contracts AJV‑gated: page v1.2, run v1.2, vlm.header_pass.v1, vlm.data_pass.v1, preproc.page.v1.
- Provenance everywhere: CRC/sha256, lock hashes, route reasons, feature hashes.

## Architecture
- Capture (Vision) → Route → VLM (persistent, dual‑pass) → Assemble (EOB) → Chunk/Index → Query.
- Seams are explicit, swappable, and versioned.

## Contracts & Sidecars
- Page NDJSON (AP:false); Run manifest summary (AP:false).
- Dual‑pass VLM artifacts under runs/vlm/; rectangularization + model_meta.
- Sidecars: preproc (raster+ops+medians+route), conservation (row statuses), HIM evidence, row‑retry NDJSON.

## Extraction Policy
- Header discovery widened; negative headerness → no header (data_start=0).
- HIM v1: type‑tag + conservation fitting → roles {charge, allowed, paid, patient}.
- CP6 mapper precedes HIM; CP7 repair before conservation; no invention.

## Budget & Retries
- Cell‑based tokens (clamped), +payer density, +25% row‑targeted; column‑focused micro‑retry (1x).

## Validation Gates
- lock_sha256 stable; header_match_rate ≥ 0.80 (CP6); typed_cell_accuracy ≥ 0.80 (CP7);
- conservation_rate ≥ 0.90 on gold set (CP8); p95 ROI latency tracked.

## Change Policy
- Flags before policy: --apply-repair (OFF), --force-vlm (gated), --use-vlm-tables (assembly, default OFF).
- Sidecar‑rich analytics; manifests carry summaries only.

## Recent Changes
- PDF transform fix (no reversal). Dual‑pass VLM (schemas + artifacts). HIM v1 implemented + verified.
- CP6 mapper v1.0; CP7 repair integrated; CP8 v0 conservation + row‑retry sidecars.
- `--use-vlm-tables` flag added to assembly; deterministic `.vlm_tables.json` sidecars and smoke harness checks for provenance/error coverage.

## Open Items
- Runner hardening (stricter prompts, JSON enforcement) to eliminate blank `result.data.v1` cells before turning on `--use-vlm-tables` by default.
- CP11 grammar‑locked JSON, CP12 micro‑decode; optional CP14 anti‑repetition.

## Minimal Commands
- Force VLM (test): `ALLOW_FORCE_VLM=1 vision-ocr-cli … --vlm-runner scripts/dots-ocrd.py --vlm-export runs/vlm --force-vlm`
- Assemble: `python scripts/assemble_eob.py --pages … --mappings config/payers --out out/eob`
- With repair: add `--apply-repair`; Row‑retry: `make row-retry-emit OUT=out/eob [APPLY_REPAIR=1]`
- VLM table ingestion (shadow): `python scripts/assemble_eob.py … --apply-repair --use-vlm-tables --vlm-dir runs/vlm`

## Attach (if space‑limited)
- README.md, SESSION/10-23/Acceptance.md
- vision-ocr-cli/Sources/vision-ocr-cli/main.swift
- scripts/assemble_eob.py, scripts/dots-ocrd.py
- schemas/vlm/{header_pass.v1.json,data_pass.v1.json}, schemas/preproc.page.v1.json
- scripts/header_mapper.py, docs/hungarian_matching.md, scripts/lexical_repair.py, config/confusion_maps.v1.json
