**NativeOCR — Micro-Brief (Mac-native Vision/CoreML)**

**Objective**
Fast, private, deterministic OCR for EOBs on M-series Macs. Vision is the baseline (speed, paragraphs, tables); VLMs (e.g., dots.ocr via CoreML/MLX) are optional second-stage for hard pages.

**Scope**

* Input: PDF/TIFF/PNG/JPG.
* Output: NDJSON per page with text lines (+ confidences + boxes). Optional: paragraphs + tables when Document API is available (macOS 15 / Xcode 17+).
* Determinism: fixed rasterization DPI, fixed request revision, stable schema, append-only logs.

**Stack**

* SwiftPM executable (`vision-ocr-cli`) linking Vision/CoreGraphics/ImageIO.
* Optional Python harness (Miniforge) for batching + metrics only; OCR stays in Swift.
* Optional CoreML/MLX ready for later VLM stage.

**Build**

```bash
# Swift CLI (release with deterministic caches)
cd vision-ocr-cli
HOME=$PWD SWIFT_MODULE_CACHE_PATH=.build/swift-module-cache \
CLANG_MODULE_CACHE_PATH=.build/clang-module-cache \
swift build -c release --disable-sandbox
# Binary
./.build/release/vision-ocr-cli
```

**Run**

```bash
# OCR → NDJSON (one JSON object per page) + run manifest
./.build/release/vision-ocr-cli doc.pdf \
  --dpi 300 \
  --mode text \
  --lang en-US \
  --ndjson out.jsonl \
  --log-run runs/$(date +%Y%m%d_%H%M%S)_run.json
```

**Output Contract (v1.2)**
NDJSON, one object per page:

```json
{
  "schema_version": "1.2",
  "engine": "Vision.Document",
  "mode": "document",
  "mode_fallback": false,
  "source_path": "doc.pdf",
  "page_index": 0,
  "width": 2550,
  "height": 3300,
  "layout_text": "…",
  "regions": {
    "header": {
      "left": {
        "box": {
          "normalized": {"x": 0.04, "y": 0.88, "w": 0.32, "h": 0.10},
          "pixel": {"x": 98, "y": 980, "w": 610, "h": 110}
        },
        "line_indices": [0, 1, 2],
        "text": "Payer Name\nAddress Line 1\nAddress Line 2"
      },
      "right": {
        "box": {
          "normalized": {"x": 0.62, "y": 0.88, "w": 0.32, "h": 0.10},
          "pixel": {"x": 1120, "y": 980, "w": 540, "h": 110}
        },
        "line_indices": [3, 4],
        "text": "Provider\nNPI"
      }
    }
  },
  "reading_order": ["para:0", "table:0"],
  "kv": [],
  "lines": [
    {
      "text": "...",
      "confidence": 0.97,
      "box": {"x": 0.12, "y": 0.84, "w": 0.23, "h": 0.02},
      "pixel_box": {"x": 145, "y": 410, "w": 320, "h": 52}
    }
  ],
  "paragraphs": [
    {
      "text": "...",
      "confidence": 0.45,
      "box": {"x": 0.08, "y": 0.70, "w": 0.62, "h": 0.05},
      "pixel_box": {"x": 210, "y": 820, "w": 1580, "h": 165}
    }
  ],
  "tables": [
    {
      "id": "t0",
      "rows": 5,
      "cols": 11,
      "box": {"x": 0.07, "y": 0.32, "w": 0.86, "h": 0.38},
      "cells": [
        {"r":0,"c":0,"text":"…","box":{"x":0.07,"y":0.68,"w":0.08,"h":0.04},"pixel_box":{"x":175,"y":1250,"w":210,"h":160},"confidence":0.51}
      ],
      "stats": {"cell_count": 55, "median_cell_conf": 0.43}
    }
  ],
  "meta": {
    "dpi": 300,
    "revision": "RecognizeDocumentsRequestRevision1",
    "lang": ["en-US"],
    "duration_ms": 118,
    "mean_confidence": 0.65,
    "line_density": 18.2,
    "line_spacing_var": 0.006,
    "column_count": 11,
    "line_count": 42,
    "numeric_density": 0.28,
    "tau_conf": 0.64,
    "script_conflict_rate": 0.12,
    "script_conflict_rate_max": 0.18,
    "paragraph_median_conf": 0.41,
    "min_text_height": 0.015,
    "crc32": "2fdc1b83",
    "timestamp": "2025-01-07T20:33:12.148Z"
  },
  "routing": {
    "decision": "vlm",
    "features": {
      "tau_conf": 0.64,
      "est_cols": 11,
      "numeric_density": 0.28,
      "script_conflict_rate": 0.12,
      "script_conflict_rate_max": 0.18,
      "paragraph_median_conf": 0.41,
      "median_cell_conf": 0.43
    },
    "minimum_text_height": 0.015
  },
  "non_content": false
}
```

**CLI Flags**

* `--ndjson <path>`: write NDJSON (stdout if omitted).
* `--dpi <int>`: rasterization DPI (default 300).
* `--mode text|document`: choose processing mode (`document` by default; auto-falls back to `text` if unavailable).
* `--lang codes`: comma-separated language list (default `en-US`).
* `--pages <start:end>`: zero-based inclusive/exclusive page slice.
* `--timeout <ms>`: fail-fast if total runtime exceeds the given budget.
* `--log-run <path>`: emit structured run manifest (defaults to `runs/YYYYMMDD_HHMMSS_run.json`).
* `--emit-debug-overlays <dir>`: draw first three pages with bounding boxes (PNG).
* `--vlm-runner <path>`: optional dots.ocr (or compatible) runner to invoke on routed pages.
* `--vlm-export <dir>`: directory for cropped ROIs + VLM request/response logs (default `runs/vlm`).
* Exit codes: `0` ok, `2` usage, `3` load error, `4` vision error.

**Determinism & Logging**

* Fix DPI, language set, and Vision revision; record all in `meta`.
* Append-only `runs/` manifest (`argv`, macOS build, Swift/Xcode versions, git SHA, schema, per-page CRC32, NDJSON CRC32/line count, timings).
* SHA-256 of the input and CRC32 of each rasterized page for traceability.
* Heuristics populate the `routing` block (`decision`, numeric features, `minimum_text_height`) and `non_content` flag so downstream consumers know which pages were skipped or routed to VLM. Manifests persist every VLM request/response (with CRCs) so escalations are auditable.

**Bench Harness (optional, Miniforge)**

```bash
conda create -n nativeocr python=3.11 -y
conda activate nativeocr
pip install pandas pypdf pillow tqdm
# scripts/bench.py → runs the CLI across a corpus, records runtime/meta, optional WER/CER.
# Usage:
scripts/bench.py --cli vision-ocr-cli/.build/release/vision-ocr-cli --corpus corpus --dpi 300 \
  --mode document --lang en-US --reference-root references --out bench/metrics.csv
```

**Schema validation**

```
make schema-check RAW_DOC=tmp/smoke.jsonl
```
Validates NDJSON payloads against `schemas/v1.2/page.schema.json` (AJV, draft 2020-12).

Run manifest and VLM schemas are also available:

```
# Validate run manifests under runs/
make schema-check-run

# Validate VLM request/response logs under runs/vlm
make schema-check-vlm

# Validate all contracts (pages, run, VLM, EOB)
make schema-check-all
```

**VLM runner (noop example)**

Use the included no-op runner to exercise the stdio contract without ML:

```
vision-ocr-cli/.build/release/vision-ocr-cli doc.pdf --mode document \
  --vlm-runner scripts/vlm-noop.py --vlm-export runs/vlm
```
Generates ROI crops, request/response logs, and populates `runs/*_run.json` with `vlm_runs` entries.

Optional helpers:
- `--vlm-preprocess` to deskew/contrast ROIs via `scripts/preprocess_roi.py` (logged in `vlm_runs[].preprocess_ops`).
- Use `--vlm-export exports/rois` if your team prefers an alternate ROI directory layout.

Contracts for the VLM seam are versioned and validated:
- Request: `schemas/vlm/request.v1.json` (emitted by the CLI)
- Response: `schemas/vlm/response.v1.json` (emitted by the runner)

Use `make schema-check-vlm` to validate recorded JSON logs.

**Run Manifest Contract (v1.2)**

Schema: `schemas/run.v1.2.json`. Highlights:
- Provenance: `input_sha256`, per-page `crc32`, and NDJSON `ndjson_crc32` + `ndjson_line_count`.
- Environment: `macOS`, `swift_version`, `xcode_version`, `cli_git_sha`.
- Routing: `routing.version`, `tau_conf`, `median_confidence`, and feature switches.
- VLM: `vlm_runs[]` entries link ROI PNGs and request/response JSON with CRCs and status.

**Assemble Dental EOBs**

```
pip install PyYAML python-dateutil
make assemble
make schema-check-eob
```
Transforms page-level NDJSON (`bench/raw_doc/*.jsonl`) into consolidated claims under `out/eob/*.json` using `config/payers/*.yaml`, then validates each object with `schemas/DentalEOBSchema.v1.2.json`.

Template mapping lives in `config/payers/`. Each YAML file defines header regex hooks and table column mapping (see `config/payers/Aetna.yaml` for an example). `_common.yaml` provides the fallback.

**Quality Gates (initial)**

* Latency: sub-second to few seconds per page at 300 DPI on clean EOBs.
* Text: WER/CER baseline snapshot; track regressions only.
* Structure: if tables enabled, require stable row/col counts across runs for the same doc.

**Routing Hook (future)**
Current heuristics set `routing.decision = "vlm"` when table geometry, numeric density, or script conflicts trip. Downstream can use this to invoke dots.ocr (CoreML/MLX) for the flagged pages only.

**Repo Skeleton**

```
NativeOCR/
  vision-ocr-cli/
    Package.swift
    Sources/vision-ocr-cli/main.swift
  scripts/
    bench.py
    make_sample_pdf.swift
  schemas/
    v1.2/page.schema.json
  runs/.gitkeep
  README.md
  setup.md
  GLOSSARY.md
  MBP-Specs.md
  fixtures/
  .github/workflows/ci.yml
```

**Guardrails**

* No network calls.
* Fail fast on malformed PDFs.
* Backpressure: stream NDJSON per page; never buffer entire docs in memory.

**Acceptance**

* Build reproducibly on macOS 15 with Xcode 17+.
* Produce NDJSON for mixed EOB corpus with zero crashes; log manifests with hash coverage.
* Tables/paragraphs enabled when the Vision document API is available; schemas validated via CI.
