EREMIT60pg — Fast Path Acceptance Suite

Purpose
- Demonstrate sub-1s/page capture using Vision-only Text mode.
- Produce deterministic, canonical EOB output (stable canonical_sha256 across re-runs).

Artifacts
- Capture NDJSON: bench/eremit_fast/eremit60_text.jsonl
- Run manifest: runs/eremit60_text_run.json
- Canonical EOB + sidecars: out/eremit_fast/EREMIT60pg.json[.sha256|.conservation.json]
- Acceptance report: SESSION/AuditLogs/2025-10-30-eremit-fast/report.json

Run
1) Ensure the Swift CLI is built:
   make ensure-release

2) Execute the acceptance runner (150 DPI, Vision Text mode):
   scripts/acceptance_eremit_fast.py

   Options:
   - Override DPI: --dpi 300
   - Override doc: --doc eobs/tests/EREMIT60pg.pdf
   - Override out dirs: --work bench/eremit_fast --out out/eremit_fast

3) Inspect the summary printed to stdout and the JSON report:
   - KPIs: p95_ms < 1000; doc_wall_s < 60; deterministic == true

Notes
- This suite intentionally avoids VLM and micro-OCR to keep latency low and behavior deterministic.
- Assembly uses spatial-merge fallback. If payer templates are not applicable, you still get canonical structure and conservation sidecars.
- No existing defaults or Makefile targets were modified.

